package dhcp

import (
	"context"
	"net"
	"net/netip"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"rackflow/storage_runner/internal/api"
)

// Spec is one DHCP server the reconciler wants: a presence's or a tenant's.
type Spec struct {
	// Key names the server and its lease file: net-<id> or tenant-<id>.
	Key       string
	Netns     string
	Iface     string
	ServerID  netip.Addr
	PrefixLen int
	Config    *api.DHCP
}

// Pool runs the node's DHCP servers. Ensure starts a server or swaps its
// table in place; Stop closes its socket at once.
type Pool struct {
	// StateDir holds lease files in dhcp/<key>.json; empty keeps leases in
	// memory only.
	StateDir string
	Open     func(netns, iface string) (net.PacketConn, error)
	Prober   func(netns, iface string) Prober
	Now      func() time.Time
	Logf     func(string, ...any)

	mu       sync.Mutex
	run      map[string]*instance
	stopping map[string]chan struct{}
	errs     map[string]string
}

type instance struct {
	spec   Spec
	srv    *Server
	conn   net.PacketConn
	cancel context.CancelFunc
	done   chan struct{}
}

func (p *Pool) init() {
	if p.run == nil {
		p.run = map[string]*instance{}
		p.stopping = map[string]chan struct{}{}
		p.errs = map[string]string{}
	}
}

func (p *Pool) logf(f string, a ...any) {
	if p.Logf != nil {
		p.Logf(f, a...)
	}
}

// Path is the lease file of key.
func (p *Pool) Path(key string) string {
	if p.StateDir == "" {
		return ""
	}
	return filepath.Join(p.StateDir, "dhcp", key+".json")
}

// Ensure makes the server of spec run with spec's config. DHCP switched off
// (or no config) stops it. A change of namespace, interface or address
// reopens the socket; anything else only swaps the table.
func (p *Pool) Ensure(spec Spec) error {
	t, err := NewTable(spec.Config, spec.ServerID, spec.PrefixLen)
	if err != nil {
		p.Stop(spec.Key)
		p.setErr(spec.Key, err.Error())
		return err
	}
	if !t.Enabled {
		p.Stop(spec.Key)
		p.setErr(spec.Key, "")
		return nil
	}
	p.mu.Lock()
	p.init()
	if in := p.run[spec.Key]; in != nil {
		if in.spec.Netns == spec.Netns && in.spec.Iface == spec.Iface && in.spec.ServerID == spec.ServerID && in.spec.PrefixLen == spec.PrefixLen {
			in.spec = spec
			p.mu.Unlock()
			in.srv.Update(t)
			p.setErr(spec.Key, "")
			return nil
		}
	}
	p.mu.Unlock()
	p.Stop(spec.Key)
	p.waitStopped(spec.Key)

	conn, err := p.Open(spec.Netns, spec.Iface)
	if err != nil {
		p.setErr(spec.Key, err.Error())
		return err
	}
	var probe Prober
	if p.Prober != nil {
		probe = p.Prober(spec.Netns, spec.Iface)
	}
	srv := NewServer(t, p.Path(spec.Key), probe, p.Now)
	srv.Logf = p.Logf
	ctx, cancel := context.WithCancel(context.Background())
	in := &instance{spec: spec, srv: srv, conn: conn, cancel: cancel, done: make(chan struct{})}
	p.mu.Lock()
	p.run[spec.Key] = in
	p.errs[spec.Key] = ""
	p.mu.Unlock()
	go func() {
		defer close(in.done)
		srv.Serve(ctx, conn)
	}()
	p.logf("dhcp: serving %s on %s/%s as %s", spec.Key, spec.Netns, spec.Iface, spec.ServerID)
	return nil
}

func (p *Pool) setErr(key, msg string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.init()
	if msg == "" {
		delete(p.errs, key)
	} else {
		p.errs[key] = msg
	}
}

// Stop closes the server's socket so it sends nothing more; it does not
// wait for a reply in progress to finish (that one fails on the closed
// socket). The lease file is kept.
func (p *Pool) Stop(key string) {
	p.mu.Lock()
	p.init()
	in := p.run[key]
	delete(p.run, key)
	if in != nil {
		p.stopping[key] = in.done
	}
	p.mu.Unlock()
	if in == nil {
		return
	}
	in.cancel()
	in.conn.Close()
	p.logf("dhcp: stopped %s", key)
}

// waitStopped waits a little for a stopped server of key to finish, so a new
// one does not read its lease file while the old one still writes it.
func (p *Pool) waitStopped(key string) {
	p.mu.Lock()
	done := p.stopping[key]
	delete(p.stopping, key)
	p.mu.Unlock()
	if done == nil {
		return
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
	}
}

// StopAll stops every server (runner shutdown).
func (p *Pool) StopAll() {
	for _, k := range p.Keys() {
		p.Stop(k)
	}
}

// Running reports whether key's server is running.
func (p *Pool) Running(key string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.run[key] != nil
}

// Keys lists the running servers.
func (p *Pool) Keys() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for k := range p.run {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Server returns key's running server, nil when not running.
func (p *Pool) Server(key string) *Server {
	p.mu.Lock()
	defer p.mu.Unlock()
	if in := p.run[key]; in != nil {
		return in.srv
	}
	return nil
}

// Status is key's error (nil when none) and dynamic leases (empty when the
// server is not running).
func (p *Pool) Status(key string) (*string, []api.DHCPLease) {
	p.mu.Lock()
	p.init()
	in := p.run[key]
	var msgs []string
	if e := p.errs[key]; e != "" {
		msgs = append(msgs, e)
	}
	p.mu.Unlock()
	leases := []api.DHCPLease{}
	if in != nil {
		leases = in.srv.Leases()
		msgs = append(msgs, in.srv.Table().Problems...)
		if e := in.srv.Err(); e != "" {
			msgs = append(msgs, e)
		}
	}
	if len(msgs) == 0 {
		return nil, leases
	}
	msg := strings.Join(msgs, "; ")
	if len(msg) > 2000 {
		msg = msg[:2000]
	}
	return &msg, leases
}
