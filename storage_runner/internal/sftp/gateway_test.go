package sftp

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"io"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/bcrypt"
	"golang.org/x/crypto/ssh"
)

// echoBackend copies stdin to stdout and records which tenant was served.
type echoBackend struct {
	mu      sync.Mutex
	tenants []int64
	started chan struct{}
}

func (b *echoBackend) Serve(ctx context.Context, tenantID int64, stdin io.Reader, stdout, stderr io.Writer) error {
	b.mu.Lock()
	b.tenants = append(b.tenants, tenantID)
	b.mu.Unlock()
	if b.started != nil {
		select {
		case b.started <- struct{}{}:
		default:
		}
	}
	done := make(chan error, 1)
	go func() {
		_, err := io.Copy(stdout, stdin)
		done <- err
	}()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-done:
		return err
	}
}

func (b *echoBackend) served() []int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]int64(nil), b.tenants...)
}

func newSigner(t *testing.T) ssh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	s, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func authorizedLine(s ssh.Signer) string {
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(s.PublicKey()))) + " test"
}

type fixture struct {
	g       *Gateway
	backend *echoBackend
	key     ssh.Signer
	addr    string
	hash    string
}

func newFixture(t *testing.T, allowed []string, settings Settings) *fixture {
	t.Helper()
	f := &fixture{backend: &echoBackend{started: make(chan struct{}, 4)}, key: newSigner(t)}
	f.g = NewGateway(newSigner(t), f.backend, testLogger(t))
	hash, err := bcrypt.GenerateFromPassword([]byte("correct horse battery"), 4)
	if err != nil {
		t.Fatal(err)
	}
	// Rackflow's Python bcrypt writes $2b$ hashes.
	f.hash = "$2b$" + string(hash)[4:]
	u, err := ParseUser(7, 65, "web.65", []string{authorizedLine(f.key)}, f.hash, allowed)
	if err != nil {
		t.Fatal(err)
	}
	f.g.SetUsers([]User{u}, settings)
	if err := f.g.Listen("127.0.0.1:0"); err != nil {
		t.Fatal(err)
	}
	f.addr = f.g.Addr().String()
	t.Cleanup(f.g.Close)
	return f
}

func dial(addr, user string, auth ...ssh.AuthMethod) (*ssh.Client, error) {
	return ssh.Dial("tcp", addr, &ssh.ClientConfig{
		User:            user,
		Auth:            auth,
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         5 * time.Second,
	})
}

func openSFTP(t *testing.T, c *ssh.Client) (*ssh.Session, io.WriteCloser, io.Reader) {
	t.Helper()
	sess, err := c.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	in, _ := sess.StdinPipe()
	out, _ := sess.StdoutPipe()
	if err := sess.RequestSubsystem("sftp"); err != nil {
		t.Fatal(err)
	}
	return sess, in, out
}

func TestKeyAndPasswordLoginReachTheirTenant(t *testing.T) {
	f := newFixture(t, nil, Settings{})
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatalf("key login: %v", err)
	}
	_, in, out := openSFTP(t, c)
	in.Write([]byte("ping"))
	buf := make([]byte, 4)
	if _, err := io.ReadFull(out, buf); err != nil || string(buf) != "ping" {
		t.Fatalf("echo: %q %v", buf, err)
	}
	c.Close()

	c, err = dial(f.addr, "web.65", ssh.Password("correct horse battery"))
	if err != nil {
		t.Fatalf("password login: %v", err)
	}
	c.Close()
	<-f.backend.started
	if got := f.backend.served(); len(got) != 1 || got[0] != 65 {
		t.Fatalf("served tenants %v", got)
	}
	logins := f.g.Logins()
	if len(logins) != 1 || logins[0].UserID != 7 || logins[0].IP != "127.0.0.1" {
		t.Fatalf("logins %+v", logins)
	}
	f.g.AckLogins(logins)
	if len(f.g.Logins()) != 0 {
		t.Fatal("acknowledged logins were kept")
	}
}

func TestBadCredentialsAndUnknownUsersAreRefused(t *testing.T) {
	f := newFixture(t, nil, Settings{MaxFailures: 100})
	for _, tc := range []struct {
		user string
		auth ssh.AuthMethod
	}{
		{"web.65", ssh.PublicKeys(newSigner(t))},
		{"web.65", ssh.Password("wrong password here")},
		{"nobody.1", ssh.Password("correct horse battery")},
		{"nobody.1", ssh.PublicKeys(f.key)},
		{"web.66", ssh.PublicKeys(f.key)},
	} {
		if c, err := dial(f.addr, tc.user, tc.auth); err == nil {
			c.Close()
			t.Fatalf("%s logged in", tc.user)
		}
	}
	if len(f.backend.served()) != 0 {
		t.Fatal("back end started without a login")
	}
}

func TestAllowListIsCheckedBeforeCredentials(t *testing.T) {
	f := newFixture(t, []string{"203.0.113.0/24"}, Settings{MaxFailures: 100})
	if c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key)); err == nil {
		c.Close()
		t.Fatal("login from outside the allow list succeeded")
	}
	u, _ := ParseUser(7, 65, "web.65", []string{authorizedLine(f.key)}, "", []string{"127.0.0.1"})
	f.g.SetUsers([]User{u}, Settings{})
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatalf("allowed address refused: %v", err)
	}
	c.Close()
}

func TestRepeatedFailuresBanTheAddressUntilExpiry(t *testing.T) {
	f := newFixture(t, nil, Settings{MaxFailures: 3, Window: time.Minute, Ban: 10 * time.Minute})
	var mu sync.Mutex
	now := time.Date(2026, 11, 18, 10, 0, 0, 0, time.UTC)
	f.g.SetClock(func() time.Time { mu.Lock(); defer mu.Unlock(); return now })
	for i := 0; i < 3; i++ {
		if c, err := dial(f.addr, "web.65", ssh.Password("wrong password here")); err == nil {
			c.Close()
		}
	}
	waitFor(t, func() bool { return f.g.bans.banned(remoteLoopback(), f.g.now()) })
	if c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key)); err == nil {
		c.Close()
		t.Fatal("banned address logged in")
	}
	mu.Lock()
	now = now.Add(11 * time.Minute)
	mu.Unlock()
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatalf("login after the ban expired: %v", err)
	}
	c.Close()
}

func TestOnlyTheSftpSubsystemIsAllowed(t *testing.T) {
	f := newFixture(t, nil, Settings{})
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	for name, run := range map[string]func(*ssh.Session) error{
		"exec":  func(s *ssh.Session) error { return s.Start("id") },
		"shell": func(s *ssh.Session) error { return s.Shell() },
		"pty":   func(s *ssh.Session) error { return s.RequestPty("xterm", 24, 80, ssh.TerminalModes{}) },
		"other": func(s *ssh.Session) error { return s.RequestSubsystem("netconf") },
	} {
		s, err := c.NewSession()
		if err != nil {
			t.Fatal(err)
		}
		if err := run(s); err == nil {
			t.Fatalf("%s was accepted", name)
		}
		s.Close()
	}
	if _, err := c.Dial("tcp", "127.0.0.1:22"); err == nil {
		t.Fatal("port forwarding was accepted")
	}
	if ok, _, _ := c.SendRequest("tcpip-forward", true, nil); ok {
		t.Fatal("remote forwarding was accepted")
	}
	if len(f.backend.served()) != 0 {
		t.Fatal("back end started for a refused request")
	}
}

func TestRemovedOrChangedLoginsLoseTheirSessions(t *testing.T) {
	f := newFixture(t, nil, Settings{})
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatal(err)
	}
	sess, _, _ := openSFTP(t, c)
	<-f.backend.started
	waitFor(t, func() bool { return f.g.Sessions()[65] == 1 })

	// Changing the password closes the session too.
	u, _ := ParseUser(7, 65, "web.65", []string{authorizedLine(f.key)}, "", nil)
	f.g.SetUsers([]User{u}, Settings{})
	if err := waitErr(sess.Wait); err == nil {
		t.Fatal("session survived a credential change")
	}
	waitFor(t, func() bool { return f.g.Sessions()[65] == 0 })

	c, err = dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatal(err)
	}
	openSFTP(t, c)
	waitFor(t, func() bool { return f.g.Sessions()[65] == 1 })
	f.g.SetUsers(nil, Settings{})
	waitFor(t, func() bool { return f.g.Sessions()[65] == 0 })
	if c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key)); err == nil {
		c.Close()
		t.Fatal("removed login still works")
	}
}

func TestCloseTenantAndListenToggle(t *testing.T) {
	f := newFixture(t, nil, Settings{})
	c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key))
	if err != nil {
		t.Fatal(err)
	}
	openSFTP(t, c)
	waitFor(t, func() bool { return f.g.Sessions()[65] == 1 })
	f.g.CloseTenant(65)
	waitFor(t, func() bool { return f.g.Sessions()[65] == 0 })

	if err := f.g.Listen(""); err != nil || f.g.Addr() != nil {
		t.Fatalf("still listening: %v", err)
	}
	if c, err := dial(f.addr, "web.65", ssh.PublicKeys(f.key)); err == nil {
		c.Close()
		t.Fatal("connected while not listening")
	}
}

func TestParseUserRejectsBadInput(t *testing.T) {
	key := authorizedLine(newSigner(t))
	for name, call := range map[string]func() error{
		"no username": func() error { _, err := ParseUser(1, 1, "", []string{key}, "", nil); return err },
		"bad key":     func() error { _, err := ParseUser(1, 1, "a.1", []string{"ssh-ed25519 xx"}, "", nil); return err },
		"bad hash":    func() error { _, err := ParseUser(1, 1, "a.1", nil, "plaintext", nil); return err },
		"bad cidr":    func() error { _, err := ParseUser(1, 1, "a.1", []string{key}, "", []string{"nope"}); return err },
		"no creds":    func() error { _, err := ParseUser(1, 1, "a.1", nil, "", nil); return err },
	} {
		if call() == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

// testLogger logs through t until the test ends; gateway goroutines may still
// log while connections wind down.
func testLogger(t *testing.T) func(string, ...any) {
	var mu sync.Mutex
	done := false
	t.Cleanup(func() { mu.Lock(); done = true; mu.Unlock() })
	return func(format string, args ...any) {
		mu.Lock()
		defer mu.Unlock()
		if !done {
			t.Logf(format, args...)
		}
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}

func waitErr(wait func() error) error {
	done := make(chan error, 1)
	go func() { done <- wait() }()
	select {
	case err := <-done:
		return err
	case <-time.After(5 * time.Second):
		return nil
	}
}

func remoteLoopback() netip.Addr { return netip.MustParseAddr("127.0.0.1") }
