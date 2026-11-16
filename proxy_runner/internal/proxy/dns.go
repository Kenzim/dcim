package proxy

import (
	"context"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// dnsCache memoises hostname lookups for outbound dials. Without it every
// proxied connection to a hostname waits on an uncached query to the host's
// resolver, which dominated connection setup time.
//
// Concurrent lookups for the same key share one query. Failures are cached
// for a shorter time so a dead name does not hammer the resolver.
type dnsCache struct {
	lookupFn func(ctx context.Context, network, host string) ([]net.IP, error)
	ttl      time.Duration
	negTTL   time.Duration
	maxSize  int

	mu      sync.Mutex
	entries map[string]*dnsEntry
}

type dnsEntry struct {
	ready   chan struct{}
	ips     []net.IP
	err     error
	expires time.Time
}

// dnsLookupTimeout bounds a shared query independently of the caller that
// started it, so one client giving up does not fail every waiter.
const dnsLookupTimeout = 10 * time.Second

func newDNSCache(lookup func(ctx context.Context, network, host string) ([]net.IP, error), ttl, negTTL time.Duration, maxSize int) *dnsCache {
	return &dnsCache{
		lookupFn: lookup,
		ttl:      ttl,
		negTTL:   negTTL,
		maxSize:  maxSize,
		entries:  make(map[string]*dnsEntry),
	}
}

func entryReady(e *dnsEntry) bool {
	select {
	case <-e.ready:
		return true
	default:
		return false
	}
}

// lookup resolves host for network ("ip", "ip4" or "ip6").
func (c *dnsCache) lookup(ctx context.Context, network, host string) ([]net.IP, error) {
	if c.ttl <= 0 {
		return c.lookupFn(ctx, network, host)
	}
	key := network + "|" + strings.ToLower(host)

	c.mu.Lock()
	e := c.entries[key]
	if e == nil || (entryReady(e) && !time.Now().Before(e.expires)) {
		e = &dnsEntry{ready: make(chan struct{})}
		c.evictLocked()
		c.entries[key] = e
		c.mu.Unlock()
		go c.resolve(e, network, host)
	} else {
		c.mu.Unlock()
	}

	select {
	case <-e.ready:
		return e.ips, e.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (c *dnsCache) resolve(e *dnsEntry, network, host string) {
	ctx, cancel := context.WithTimeout(context.Background(), dnsLookupTimeout)
	defer cancel()
	ips, err := c.lookupFn(ctx, network, host)
	if err == nil && len(ips) == 0 {
		err = &net.DNSError{Err: "no addresses", Name: host, IsNotFound: true}
	}
	e.ips, e.err = ips, err
	if err != nil {
		e.expires = time.Now().Add(c.negTTL)
	} else {
		e.expires = time.Now().Add(c.ttl)
	}
	close(e.ready)
}

// evictLocked keeps the cache under maxSize: expired entries go first, then
// arbitrary resolved ones. In-flight entries are never dropped.
func (c *dnsCache) evictLocked() {
	if c.maxSize <= 0 || len(c.entries) < c.maxSize {
		return
	}
	now := time.Now()
	for k, e := range c.entries {
		if entryReady(e) && !now.Before(e.expires) {
			delete(c.entries, k)
		}
	}
	for k, e := range c.entries {
		if len(c.entries) < c.maxSize {
			return
		}
		if entryReady(e) {
			delete(c.entries, k)
		}
	}
}

// newResolver returns the resolver for outbound lookups. PROXY_DNS_SERVER
// ("1.1.1.1" or "1.1.1.1:53") overrides the host's /etc/resolv.conf.
func newResolver() *net.Resolver {
	server := strings.TrimSpace(os.Getenv("PROXY_DNS_SERVER"))
	if server == "" {
		return net.DefaultResolver
	}
	if _, _, err := net.SplitHostPort(server); err != nil {
		server = net.JoinHostPort(server, "53")
	}
	return &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, network, server)
		},
	}
}

func envDuration(name string, def time.Duration) time.Duration {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d >= 0 {
			return d
		}
	}
	return def
}

func envInt(name string, def int) int {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			return n
		}
	}
	return def
}

var (
	sharedDNSOnce sync.Once
	sharedDNS     *dnsCache
)

func outboundDNS() *dnsCache {
	sharedDNSOnce.Do(func() {
		r := newResolver()
		sharedDNS = newDNSCache(
			r.LookupIP,
			envDuration("PROXY_DNS_CACHE_TTL", 60*time.Second),
			envDuration("PROXY_DNS_NEGATIVE_TTL", 5*time.Second),
			envInt("PROXY_DNS_CACHE_SIZE", 10000),
		)
	})
	return sharedDNS
}

// ipFamily picks the lookup and dial families from the bind IP: an IPv4
// bind IP can only reach IPv4 destinations, so AAAA queries are wasted.
func ipFamily(bindIP string) (lookupNet, dialNet string) {
	ip := net.ParseIP(bindIP)
	switch {
	case ip == nil:
		return "ip", "tcp"
	case ip.To4() != nil:
		return "ip4", "tcp4"
	default:
		return "ip6", "tcp6"
	}
}
