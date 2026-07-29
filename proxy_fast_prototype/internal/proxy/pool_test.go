package proxy

import (
	"net"
	"testing"
	"time"
)

func pipeConn(t *testing.T) net.Conn {
	t.Helper()
	a, b := net.Pipe()
	t.Cleanup(func() { a.Close(); b.Close() })
	return a
}

// TestPoolKeyIncludesBindIP proves two different bind IPs never share a
// pooled connection, even to the identical destination.
func TestPoolKeyIncludesBindIP(t *testing.T) {
	p := newConnPool(10, time.Minute)
	defer p.Close()

	pc1 := newPooledConn(pipeConn(t))
	p.put("127.0.0.1", "origin:80", pc1)

	// Same destination, different customer bind IP: must miss.
	if got := p.get("127.0.0.2", "origin:80"); got != nil {
		t.Fatal("pool leaked a connection across bind IPs")
	}
	// Same bind IP, different destination: must miss.
	if got := p.get("127.0.0.1", "other:80"); got != nil {
		t.Fatal("pool leaked a connection across destinations")
	}
	// Exact key: must hit.
	if got := p.get("127.0.0.1", "origin:80"); got != pc1 {
		t.Fatal("expected pooled conn back for exact (bindIP, dest) key")
	}
	// And it was removed by the get.
	if got := p.get("127.0.0.1", "origin:80"); got != nil {
		t.Fatal("conn should have been checked out")
	}
}

func TestPoolLIFOAndMaxPerKey(t *testing.T) {
	p := newConnPool(2, time.Minute)
	defer p.Close()

	pc1 := newPooledConn(pipeConn(t))
	pc2 := newPooledConn(pipeConn(t))
	pc3 := newPooledConn(pipeConn(t))
	p.put("127.0.0.1", "origin:80", pc1)
	p.put("127.0.0.1", "origin:80", pc2)
	p.put("127.0.0.1", "origin:80", pc3) // over cap: dropped+closed

	if got := p.get("127.0.0.1", "origin:80"); got != pc2 {
		t.Fatal("expected most-recently-used conn first (LIFO)")
	}
	if got := p.get("127.0.0.1", "origin:80"); got != pc1 {
		t.Fatal("expected older conn second")
	}
	if got := p.get("127.0.0.1", "origin:80"); got != nil {
		t.Fatal("third conn should have been rejected by per-key cap")
	}
}

func TestPoolExpiresIdleConns(t *testing.T) {
	p := newConnPool(10, 10*time.Millisecond)
	defer p.Close()

	p.put("127.0.0.1", "origin:80", newPooledConn(pipeConn(t)))
	time.Sleep(30 * time.Millisecond)
	if got := p.get("127.0.0.1", "origin:80"); got != nil {
		t.Fatal("expired idle conn must not be returned")
	}
}
