package proxy

import (
	"context"
	"errors"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func countingLookup(calls *int32, ips []net.IP, err error, delay time.Duration) func(context.Context, string, string) ([]net.IP, error) {
	return func(context.Context, string, string) ([]net.IP, error) {
		atomic.AddInt32(calls, 1)
		if delay > 0 {
			time.Sleep(delay)
		}
		return ips, err
	}
}

func TestDNSCacheHitSkipsResolver(t *testing.T) {
	var calls int32
	c := newDNSCache(countingLookup(&calls, []net.IP{net.ParseIP("93.184.216.34")}, nil, 0), time.Minute, time.Second, 100)
	for i := 0; i < 5; i++ {
		ips, err := c.lookup(context.Background(), "ip4", "Example.com")
		if err != nil || len(ips) != 1 {
			t.Fatalf("lookup %d: %v %v", i, ips, err)
		}
	}
	if _, err := c.lookup(context.Background(), "ip4", "example.com"); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("resolver called %d times, want 1", calls)
	}
	if _, err := c.lookup(context.Background(), "ip6", "example.com"); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("family should be part of the key; resolver called %d times, want 2", calls)
	}
}

func TestDNSCacheSharesInFlightLookup(t *testing.T) {
	var calls int32
	c := newDNSCache(countingLookup(&calls, []net.IP{net.ParseIP("1.2.3.4")}, nil, 50*time.Millisecond), time.Minute, time.Second, 100)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := c.lookup(context.Background(), "ip4", "a.test"); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if calls != 1 {
		t.Fatalf("resolver called %d times, want 1", calls)
	}
}

func TestDNSCacheExpiresAndCachesFailuresBriefly(t *testing.T) {
	var calls int32
	fail := errors.New("nxdomain")
	c := newDNSCache(countingLookup(&calls, nil, fail, 0), time.Minute, 30*time.Millisecond, 100)
	for i := 0; i < 3; i++ {
		if _, err := c.lookup(context.Background(), "ip4", "bad.test"); !errors.Is(err, fail) {
			t.Fatalf("want cached failure, got %v", err)
		}
	}
	if calls != 1 {
		t.Fatalf("failure not cached: %d calls", calls)
	}
	time.Sleep(40 * time.Millisecond)
	_, _ = c.lookup(context.Background(), "ip4", "bad.test")
	if calls != 2 {
		t.Fatalf("negative entry should expire: %d calls", calls)
	}
}

func TestDNSCacheDisabledWithZeroTTL(t *testing.T) {
	var calls int32
	c := newDNSCache(countingLookup(&calls, []net.IP{net.ParseIP("1.2.3.4")}, nil, 0), 0, 0, 100)
	_, _ = c.lookup(context.Background(), "ip", "a.test")
	_, _ = c.lookup(context.Background(), "ip", "a.test")
	if calls != 2 {
		t.Fatalf("ttl 0 should bypass the cache: %d calls", calls)
	}
}

func TestDNSCacheStaysBounded(t *testing.T) {
	var calls int32
	c := newDNSCache(countingLookup(&calls, []net.IP{net.ParseIP("1.2.3.4")}, nil, 0), time.Minute, time.Second, 10)
	for i := 0; i < 50; i++ {
		if _, err := c.lookup(context.Background(), "ip4", net.IPv4(10, 0, 0, byte(i)).String()+".test"); err != nil {
			t.Fatal(err)
		}
	}
	c.mu.Lock()
	n := len(c.entries)
	c.mu.Unlock()
	if n > 10 {
		t.Fatalf("cache grew to %d entries, max 10", n)
	}
}

func TestIPFamilyFollowsBindIP(t *testing.T) {
	cases := map[string][2]string{
		"":            {"ip", "tcp"},
		"154.7.136.5": {"ip4", "tcp4"},
		"2001:db8::1": {"ip6", "tcp6"},
	}
	for bind, want := range cases {
		l, d := ipFamily(bind)
		if l != want[0] || d != want[1] {
			t.Errorf("ipFamily(%q) = %s,%s want %s,%s", bind, l, d, want[0], want[1])
		}
	}
}

func TestDialTCPResolvesHostnameAndStillBlocksPrivate(t *testing.T) {
	os.Unsetenv("PROXY_ALLOW_PRIVATE_DESTINATIONS")
	defer os.Setenv("PROXY_ALLOW_PRIVATE_DESTINATIONS", "true")
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	_, err = dialTCP(net.JoinHostPort("localhost", port), "127.0.0.1", 2*time.Second)
	if !errors.Is(err, ErrDestinationBlocked) {
		t.Fatalf("hostname resolving to loopback must be blocked, got %v", err)
	}
}
