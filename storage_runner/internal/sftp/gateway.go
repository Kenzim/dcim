// Package sftp is the node's public SFTP front end. It authenticates each
// login against the hashes and keys Rackflow sends, finds the login's tenant
// from the username, and pipes the sftp subsystem to that tenant's own
// back end. It never opens a tenant file itself.
package sftp

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
	"golang.org/x/crypto/ssh"
)

const (
	MaxAuthTries     = 6
	DefaultMaxFails  = 10
	DefaultWindow    = 600 * time.Second
	DefaultBan       = 900 * time.Second
	handshakeTimeout = 30 * time.Second
)

// dummyHash is compared for unknown users, so a missing user costs the same
// bcrypt work as a wrong password.
var dummyHash = sync.OnceValue(func() []byte {
	h, err := bcrypt.GenerateFromPassword([]byte("rackflow-unknown-user"), 12)
	if err != nil {
		panic(err)
	}
	return h
})

// User is one login the gateway accepts.
type User struct {
	ID           int64
	TenantID     int64
	Username     string
	Keys         [][]byte // ssh wire format
	PasswordHash string
	Allowed      []netip.Prefix
}

// Settings mirror the brute-force settings from Rackflow.
type Settings struct {
	MaxFailures int
	Window      time.Duration
	Ban         time.Duration
}

// Backend starts a tenant's sftp-server and connects it to stdio.
type Backend interface {
	Serve(ctx context.Context, tenantID int64, stdin io.Reader, stdout, stderr io.Writer) error
}

// Login is the latest successful login of a user.
type Login struct {
	UserID int64
	At     time.Time
	IP     string
}

// Gateway is the SSH server. It is safe for concurrent use.
type Gateway struct {
	Backend Backend
	Logf    func(format string, args ...any)

	mu       sync.Mutex
	clock    func() time.Time
	hostKey  ssh.Signer
	users    map[string]User
	settings Settings
	bans     *banTracker
	conns    map[*ssh.ServerConn]connInfo
	logins   map[int64]Login
	listener net.Listener
	addr     string
}

type connInfo struct {
	userID   int64
	tenantID int64
	cred     string
}

// HostKey is the gateway's public host key.
func (g *Gateway) HostKey() ssh.PublicKey {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.hostKey == nil {
		return nil
	}
	return g.hostKey.PublicKey()
}

// SetHostKey replaces the host key for new connections.
func (g *Gateway) SetHostKey(key ssh.Signer) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.hostKey = key
}

// SetClock replaces the clock, for tests.
func (g *Gateway) SetClock(now func() time.Time) {
	g.mu.Lock()
	g.clock = now
	g.mu.Unlock()
}

func (g *Gateway) now() time.Time {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.clock()
}

// credential identifies a user's login details, so open sessions are closed
// when keys, password, allow list or tenant change.
func credential(u User) string {
	h := sha256.New()
	fmt.Fprintf(h, "%d|%d|%s|%s|", u.ID, u.TenantID, u.Username, u.PasswordHash)
	for _, k := range u.Keys {
		fmt.Fprintf(h, "k%x|", k)
	}
	for _, p := range u.Allowed {
		fmt.Fprintf(h, "a%s|", p)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// NewGateway returns a gateway with the given host key.
func NewGateway(hostKey ssh.Signer, backend Backend, logf func(string, ...any)) *Gateway {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &Gateway{
		Backend: backend,
		Logf:    logf,
		clock:   time.Now,
		hostKey: hostKey,
		users:   map[string]User{},
		bans:    newBanTracker(),
		conns:   map[*ssh.ServerConn]connInfo{},
		logins:  map[int64]Login{},
		settings: Settings{
			MaxFailures: DefaultMaxFails, Window: DefaultWindow, Ban: DefaultBan,
		},
	}
}

// SetUsers swaps the routing table and closes connections of users that are
// gone or whose credentials or tenant changed.
func (g *Gateway) SetUsers(users []User, settings Settings) {
	if settings.MaxFailures <= 0 {
		settings.MaxFailures = DefaultMaxFails
	}
	if settings.Window <= 0 {
		settings.Window = DefaultWindow
	}
	if settings.Ban <= 0 {
		settings.Ban = DefaultBan
	}
	table := make(map[string]User, len(users))
	dup := map[string]bool{}
	for _, u := range users {
		if _, ok := table[u.Username]; ok {
			dup[u.Username] = true
		}
		table[u.Username] = u
	}
	// A username claimed twice could route to the wrong tenant: refuse it.
	for name := range dup {
		delete(table, name)
		g.Logf("sftp: login %s is listed more than once; refusing it", name)
	}
	users = users[:0:0]
	for _, u := range table {
		users = append(users, u)
	}
	creds := map[int64]string{}
	for _, u := range users {
		creds[u.ID] = credential(u)
	}
	g.mu.Lock()
	g.users = table
	g.settings = settings
	var stale []*ssh.ServerConn
	for c, info := range g.conns {
		if cred, ok := creds[info.userID]; !ok || cred != info.cred {
			stale = append(stale, c)
		}
	}
	g.mu.Unlock()
	for _, c := range stale {
		g.Logf("sftp: closing session of %s (login removed, changed or tenant not active)", c.User())
		c.Close()
	}
}

// CloseTenant closes every connection of a tenant.
func (g *Gateway) CloseTenant(tenantID int64) {
	g.mu.Lock()
	var conns []*ssh.ServerConn
	for c, info := range g.conns {
		if info.tenantID == tenantID {
			conns = append(conns, c)
		}
	}
	g.mu.Unlock()
	for _, c := range conns {
		c.Close()
	}
}

// Sessions is the number of open connections per tenant.
func (g *Gateway) Sessions() map[int64]int {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := map[int64]int{}
	for _, info := range g.conns {
		out[info.tenantID]++
	}
	return out
}

// Logins returns the latest successful login per user since the last
// AckLogins.
func (g *Gateway) Logins() []Login {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]Login, 0, len(g.logins))
	for _, l := range g.logins {
		out = append(out, l)
	}
	return out
}

// AckLogins forgets reported logins that have not been superseded since.
func (g *Gateway) AckLogins(reported []Login) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, l := range reported {
		if cur, ok := g.logins[l.UserID]; ok && !cur.At.After(l.At) {
			delete(g.logins, l.UserID)
		}
	}
}

// Listen starts serving on addr, or keeps the current listener when addr is
// unchanged. An empty addr stops listening.
func (g *Gateway) Listen(addr string) error {
	g.mu.Lock()
	if g.addr == addr && (g.listener != nil || addr == "") {
		g.mu.Unlock()
		return nil
	}
	old := g.listener
	g.listener, g.addr = nil, ""
	g.mu.Unlock()
	if old != nil {
		old.Close()
		g.Logf("sftp: stopped listening")
	}
	if addr == "" {
		return nil
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	g.mu.Lock()
	g.listener, g.addr = ln, addr
	g.mu.Unlock()
	g.Logf("sftp: listening on %s", ln.Addr())
	go g.serve(ln)
	return nil
}

// Addr is the address being listened on, or nil.
func (g *Gateway) Addr() net.Addr {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.listener == nil {
		return nil
	}
	return g.listener.Addr()
}

// Close stops listening and closes every connection.
func (g *Gateway) Close() {
	g.Listen("")
	g.mu.Lock()
	conns := make([]*ssh.ServerConn, 0, len(g.conns))
	for c := range g.conns {
		conns = append(conns, c)
	}
	g.mu.Unlock()
	for _, c := range conns {
		c.Close()
	}
}

func (g *Gateway) serve(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			g.Logf("sftp: accept: %v", err)
			time.Sleep(100 * time.Millisecond)
			continue
		}
		go g.handle(conn)
	}
}

func remoteAddr(a net.Addr) netip.Addr {
	if ap, err := netip.ParseAddrPort(a.String()); err == nil {
		return ap.Addr().Unmap()
	}
	return netip.Addr{}
}

func (g *Gateway) current() (map[string]User, Settings) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.users, g.settings
}

func (g *Gateway) handle(conn net.Conn) {
	ip := remoteAddr(conn.RemoteAddr())
	if g.bans.banned(ip, g.now()) {
		conn.Close()
		return
	}
	conn.SetDeadline(time.Now().Add(handshakeTimeout))
	failed := false
	cfg := g.serverConfig(ip, &failed)
	sconn, chans, reqs, err := ssh.NewServerConn(conn, cfg)
	if err != nil {
		conn.Close()
		// A connection that ends without logging in after a failed attempt
		// counts once; each connection allows at most MaxAuthTries attempts.
		if failed {
			_, settings := g.current()
			if g.bans.fail(ip, g.now(), settings) {
				g.Logf("sftp: banning %s for %s after %d failed connections", ip, settings.Ban, settings.MaxFailures)
			}
		}
		return
	}
	conn.SetDeadline(time.Time{})
	userID, _ := strconv.ParseInt(sconn.Permissions.Extensions["user_id"], 10, 64)
	tenantID, _ := strconv.ParseInt(sconn.Permissions.Extensions["tenant_id"], 10, 64)
	cred := sconn.Permissions.Extensions["cred"]
	ctx, cancel := context.WithCancel(context.Background())
	g.mu.Lock()
	g.conns[sconn] = connInfo{userID: userID, tenantID: tenantID, cred: cred}
	// The table may have changed between authentication and registration.
	u, ok := g.users[sconn.User()]
	valid := ok && credential(u) == cred
	if valid {
		g.logins[userID] = Login{UserID: userID, At: g.clock().UTC(), IP: ip.String()}
	}
	g.mu.Unlock()
	defer func() {
		cancel()
		g.mu.Lock()
		delete(g.conns, sconn)
		g.mu.Unlock()
		sconn.Close()
		if valid {
			g.Logf("sftp: session end user=%s tenant=%d ip=%s", sconn.User(), tenantID, ip)
		}
	}()
	if !valid {
		return
	}
	g.Logf("sftp: session start user=%s tenant=%d ip=%s", sconn.User(), tenantID, ip)
	go func() {
		sconn.Wait()
		cancel()
	}()
	go rejectGlobal(reqs)
	for nc := range chans {
		if nc.ChannelType() != "session" {
			nc.Reject(ssh.Prohibited, "only sftp sessions are allowed")
			continue
		}
		if !g.stillValid(sconn.User(), cred) {
			nc.Reject(ssh.Prohibited, "login no longer valid")
			sconn.Close()
			continue
		}
		ch, chReqs, err := nc.Accept()
		if err != nil {
			continue
		}
		go g.session(ctx, sconn, tenantID, ch, chReqs)
	}
}

func rejectGlobal(reqs <-chan *ssh.Request) {
	for r := range reqs {
		if r.WantReply {
			r.Reply(false, nil)
		}
	}
}

func (g *Gateway) stillValid(username, cred string) bool {
	users, _ := g.current()
	u, ok := users[username]
	return ok && credential(u) == cred
}

func (g *Gateway) session(ctx context.Context, conn *ssh.ServerConn, tenantID int64, ch ssh.Channel, reqs <-chan *ssh.Request) {
	defer ch.Close()
	started := false
	for r := range reqs {
		if !started && r.Type == "subsystem" && subsystemName(r.Payload) == "sftp" {
			started = true
			r.Reply(true, nil)
			go func() {
				err := g.Backend.Serve(ctx, tenantID, ch, ch, ch.Stderr())
				status := uint32(0)
				if err != nil {
					status = 1
					g.Logf("sftp: back end for tenant %d (user %s): %v", tenantID, conn.User(), err)
				}
				ch.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{status}))
				ch.Close()
			}()
			continue
		}
		if r.WantReply {
			r.Reply(false, nil)
		}
	}
}

func subsystemName(payload []byte) string {
	var msg struct{ Name string }
	if err := ssh.Unmarshal(payload, &msg); err != nil {
		return ""
	}
	return msg.Name
}

func (g *Gateway) serverConfig(ip netip.Addr, failed *bool) *ssh.ServerConfig {
	cfg := &ssh.ServerConfig{
		MaxAuthTries:  MaxAuthTries,
		ServerVersion: "SSH-2.0-RackflowSFTP",
		PublicKeyCallback: func(meta ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
			return g.checkKey(meta.User(), ip, key)
		},
		PasswordCallback: func(meta ssh.ConnMetadata, password []byte) (*ssh.Permissions, error) {
			return g.checkPassword(meta.User(), ip, password)
		},
		AuthLogCallback: func(meta ssh.ConnMetadata, method string, err error) {
			if method == "none" {
				return
			}
			result := "ok"
			if err != nil {
				result = "failed"
				*failed = true
			}
			g.Logf("sftp: auth user=%s ip=%s method=%s result=%s", meta.User(), ip, method, result)
		},
	}
	g.mu.Lock()
	cfg.AddHostKey(g.hostKey)
	g.mu.Unlock()
	return cfg
}

var errDenied = errors.New("access denied")

func (g *Gateway) lookup(username string, ip netip.Addr) (User, bool) {
	users, _ := g.current()
	u, ok := users[username]
	if !ok {
		return User{}, false
	}
	if len(u.Allowed) == 0 {
		return u, true
	}
	for _, p := range u.Allowed {
		if p.Contains(ip) {
			return u, true
		}
	}
	return User{}, false
}

func permissions(u User, extra map[string]string) *ssh.Permissions {
	ext := map[string]string{
		"user_id":   strconv.FormatInt(u.ID, 10),
		"tenant_id": strconv.FormatInt(u.TenantID, 10),
		"cred":      credential(u),
	}
	for k, v := range extra {
		ext[k] = v
	}
	return &ssh.Permissions{Extensions: ext}
}

func (g *Gateway) checkKey(username string, ip netip.Addr, key ssh.PublicKey) (*ssh.Permissions, error) {
	u, ok := g.lookup(username, ip)
	if !ok {
		return nil, errDenied
	}
	offered := key.Marshal()
	match := 0
	for _, k := range u.Keys {
		match |= subtle.ConstantTimeCompare(k, offered)
	}
	if match != 1 {
		return nil, errDenied
	}
	g.Logf("sftp: key accepted user=%s tenant=%d ip=%s fingerprint=%s", username, u.TenantID, ip, ssh.FingerprintSHA256(key))
	return permissions(u, map[string]string{"pubkey-fp": ssh.FingerprintSHA256(key)}), nil
}

func (g *Gateway) checkPassword(username string, ip netip.Addr, password []byte) (*ssh.Permissions, error) {
	u, ok := g.lookup(username, ip)
	hash := dummyHash()
	if ok && u.PasswordHash != "" {
		hash = []byte(u.PasswordHash)
	}
	err := bcrypt.CompareHashAndPassword(hash, password)
	if !ok || u.PasswordHash == "" || err != nil {
		return nil, errDenied
	}
	return permissions(u, nil), nil
}

// ParseUser converts Rackflow's description of a login. Invalid keys or CIDRs
// are reported and the user is skipped by the caller.
func ParseUser(id, tenantID int64, username string, keys []string, passwordHash string, allowed []string) (User, error) {
	u := User{ID: id, TenantID: tenantID, Username: username, PasswordHash: passwordHash}
	if username == "" {
		return u, fmt.Errorf("user %d has no username", id)
	}
	for _, line := range keys {
		k, _, _, _, err := ssh.ParseAuthorizedKey([]byte(line))
		if err != nil {
			return u, fmt.Errorf("user %s: invalid public key: %v", username, err)
		}
		u.Keys = append(u.Keys, k.Marshal())
	}
	if passwordHash != "" {
		if _, err := bcrypt.Cost([]byte(passwordHash)); err != nil {
			return u, fmt.Errorf("user %s: invalid password hash", username)
		}
	}
	for _, s := range allowed {
		p, err := netip.ParsePrefix(s)
		if err != nil {
			a, aerr := netip.ParseAddr(s)
			if aerr != nil {
				return u, fmt.Errorf("user %s: invalid allowed address %q", username, s)
			}
			p = netip.PrefixFrom(a, a.BitLen())
		}
		u.Allowed = append(u.Allowed, p.Masked())
	}
	if len(u.Keys) == 0 && u.PasswordHash == "" {
		return u, fmt.Errorf("user %s has no key and no password", username)
	}
	return u, nil
}
