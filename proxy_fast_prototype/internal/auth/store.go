// Package auth holds the in-memory bind-IP → credentials table.
//
// Production periodically syncs assignments from the Rackflow backend; this
// prototype only exposes the in-memory API (Replace) so a real sync loop can
// be wired in later.
package auth

import (
	"net"
	"sync/atomic"
)

// Credentials is the single credential set assigned to one bind IP. A
// customer is only valid on their assigned IP.
type Credentials struct {
	Username  string
	Password  string
	ServiceID int
}

// Entry is one assignment row as it would arrive from the backend sync.
type Entry struct {
	BindIP    string
	Username  string
	Password  string
	ServiceID int
}

// Store maps bind IP → credentials. Lookups are lock-free reads of an
// atomically swapped map, so the hot path (every proxied request) never
// contends with the periodic Replace.
type Store struct {
	m atomic.Pointer[map[string]Credentials]
}

func NewStore() *Store {
	s := &Store{}
	empty := map[string]Credentials{}
	s.m.Store(&empty)
	return s
}

// Replace atomically swaps the whole assignment table. In-flight Allow calls
// see either the old or the new table, never a partial mix.
func (s *Store) Replace(entries []Entry) {
	next := make(map[string]Credentials, len(entries))
	for _, e := range entries {
		if e.BindIP == "" || e.Username == "" {
			continue
		}
		next[normalizeIP(e.BindIP)] = Credentials{
			Username:  e.Username,
			Password:  e.Password,
			ServiceID: e.ServiceID,
		}
	}
	s.m.Store(&next)
}

func (s *Store) Len() int {
	return len(*s.m.Load())
}

// Allow reports whether the credentials are valid for the given bind IP.
func (s *Store) Allow(bindIP, username, password string) (serviceID int, ok bool) {
	m := *s.m.Load()
	c, found := m[normalizeIP(bindIP)]
	if !found || c.Username != username || c.Password != password {
		return 0, false
	}
	return c.ServiceID, true
}

func normalizeIP(ip string) string {
	parsed := net.ParseIP(ip)
	if parsed == nil {
		return ip
	}
	if v4 := parsed.To4(); v4 != nil {
		return v4.String()
	}
	return parsed.String()
}
