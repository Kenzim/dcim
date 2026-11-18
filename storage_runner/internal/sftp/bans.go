package sftp

import (
	"net/netip"
	"sync"
	"time"
)

// maxTracked bounds the number of source addresses remembered.
const maxTracked = 50000

type banEntry struct {
	failures []time.Time
	until    time.Time
}

// banTracker counts failed connections per source address in a sliding
// window and bans an address once it reaches the limit.
type banTracker struct {
	mu      sync.Mutex
	entries map[netip.Addr]*banEntry
}

func newBanTracker() *banTracker {
	return &banTracker{entries: map[netip.Addr]*banEntry{}}
}

func (b *banTracker) banned(ip netip.Addr, now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	e, ok := b.entries[ip]
	return ok && now.Before(e.until)
}

// fail records a failure and reports whether it started a ban.
func (b *banTracker) fail(ip netip.Addr, now time.Time, s Settings) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	e, ok := b.entries[ip]
	if !ok {
		if len(b.entries) >= maxTracked {
			b.prune(now, s.Window)
		}
		if len(b.entries) >= maxTracked {
			// Still full of live entries: forget an arbitrary one that is not banned.
			for k, v := range b.entries {
				if !now.Before(v.until) {
					delete(b.entries, k)
					break
				}
			}
		}
		e = &banEntry{}
		b.entries[ip] = e
	}
	cutoff := now.Add(-s.Window)
	kept := e.failures[:0]
	for _, t := range e.failures {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	e.failures = append(kept, now)
	if len(e.failures) >= s.MaxFailures {
		e.failures = nil
		e.until = now.Add(s.Ban)
		return true
	}
	return false
}

func (b *banTracker) prune(now time.Time, window time.Duration) {
	cutoff := now.Add(-window)
	for k, e := range b.entries {
		if now.Before(e.until) {
			continue
		}
		live := false
		for _, t := range e.failures {
			if t.After(cutoff) {
				live = true
				break
			}
		}
		if !live {
			delete(b.entries, k)
		}
	}
}
