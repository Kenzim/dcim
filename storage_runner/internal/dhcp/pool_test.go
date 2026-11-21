package dhcp

import (
	"net"
	"net/netip"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
)

// fakeConn is a PacketConn fed through in and recording what is written.
type fakeConn struct {
	in     chan []byte
	mu     sync.Mutex
	out    []sent
	closed chan struct{}
	once   sync.Once
}

type sent struct {
	b   []byte
	dst string
}

func newFakeConn() *fakeConn { return &fakeConn{in: make(chan []byte, 4), closed: make(chan struct{})} }

func (c *fakeConn) ReadFrom(b []byte) (int, net.Addr, error) {
	select {
	case p := <-c.in:
		return copy(b, p), &net.UDPAddr{Port: 68}, nil
	case <-c.closed:
		return 0, nil, net.ErrClosed
	}
}

func (c *fakeConn) WriteTo(b []byte, a net.Addr) (int, error) {
	select {
	case <-c.closed:
		return 0, net.ErrClosed
	default:
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.out = append(c.out, sent{append([]byte(nil), b...), a.String()})
	return len(b), nil
}

func (c *fakeConn) sent() []sent {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]sent(nil), c.out...)
}

func (c *fakeConn) Close() error                     { c.once.Do(func() { close(c.closed) }); return nil }
func (c *fakeConn) LocalAddr() net.Addr              { return &net.UDPAddr{Port: 67} }
func (c *fakeConn) SetDeadline(time.Time) error      { return nil }
func (c *fakeConn) SetReadDeadline(time.Time) error  { return nil }
func (c *fakeConn) SetWriteDeadline(time.Time) error { return nil }

func (c *fakeConn) isClosed() bool {
	select {
	case <-c.closed:
		return true
	default:
		return false
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	for end := time.Now().Add(5 * time.Second); time.Now().Before(end); time.Sleep(5 * time.Millisecond) {
		if cond() {
			return
		}
	}
	t.Fatal("condition not reached")
}

func TestPoolSwapsTableWithoutReopening(t *testing.T) {
	var mu sync.Mutex
	var conns []*fakeConn
	p := &Pool{StateDir: t.TempDir(), Open: func(netns, iface string) (net.PacketConn, error) {
		mu.Lock()
		defer mu.Unlock()
		if netns != "rf-p6" || iface != "rf-w6" {
			t.Errorf("opened %s/%s", netns, iface)
		}
		c := newFakeConn()
		conns = append(conns, c)
		return c, nil
	}}
	spec := Spec{Key: "net-6", Netns: "rf-p6", Iface: "rf-w6", ServerID: serverIP, PrefixLen: 24, Config: reservedCfg()}
	if err := p.Ensure(spec); err != nil {
		t.Fatal(err)
	}
	c := conns[0]
	c.in <- msg{typ: Discover, mac: macA}.bytes()
	waitFor(t, func() bool { return len(c.sent()) == 1 })
	if s := c.sent()[0]; s.dst != "255.255.255.255:68" {
		t.Fatalf("sent to %s", s.dst)
	}

	// The reservation moves to another address: same socket, new table.
	cfg := reservedCfg()
	cfg.Reservations[0].IP = "10.0.0.9"
	spec.Config = cfg
	if err := p.Ensure(spec); err != nil {
		t.Fatal(err)
	}
	if len(conns) != 1 || c.isClosed() {
		t.Fatal("a table change reopened the socket")
	}
	c.in <- msg{typ: Discover, mac: macA}.bytes()
	waitFor(t, func() bool { return len(c.sent()) == 2 })
	if r, _ := Parse(c.sent()[1].b); r.YIAddr != ip("10.0.0.9") {
		t.Fatalf("offered %s after the swap", r.YIAddr)
	}

	if e, l := p.Status("net-6"); e != nil || l == nil {
		t.Fatalf("status %v %v", e, l)
	}
	p.Stop("net-6")
	if !c.isClosed() || p.Running("net-6") {
		t.Fatal("stop left the socket open")
	}

	// DHCP switched off: nothing is opened.
	spec.Config = &api.DHCP{Enabled: false}
	if err := p.Ensure(spec); err != nil || p.Running("net-6") || len(conns) != 1 {
		t.Fatalf("disabled dhcp: %v running=%v opens=%d", err, p.Running("net-6"), len(conns))
	}
	if p.Path("net-6") != filepath.Join(p.StateDir, "dhcp", "net-6.json") {
		t.Fatal(p.Path("net-6"))
	}
}

func TestPoolReportsOpenErrorsAndBadConfig(t *testing.T) {
	p := &Pool{Open: func(string, string) (net.PacketConn, error) { return nil, net.UnknownNetworkError("no such device") }}
	spec := Spec{Key: "tenant-3", Netns: "rf-t3", Iface: "rf-v3", ServerID: serverIP, PrefixLen: 24, Config: reservedCfg()}
	if err := p.Ensure(spec); err == nil {
		t.Fatal("open error not returned")
	}
	if e, l := p.Status("tenant-3"); e == nil || len(l) != 0 {
		t.Fatalf("status %v %v", e, l)
	}
	spec.ServerID = netip.Addr{}
	if err := p.Ensure(spec); err == nil {
		t.Fatal("bad server address accepted")
	}
}
