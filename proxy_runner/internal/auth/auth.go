package auth

import (
	"crypto/subtle"
	"net"
	"sync"
)

// Store holds the synced (bindIP, username) → (password, serviceID) map.
type Store struct {
	mu      sync.RWMutex
	version string
	entries map[key]value
}

type key struct {
	bindIP   string
	username string
}

type value struct {
	password  string
	serviceID int
}

func NewStore() *Store {
	return &Store{entries: make(map[key]value)}
}

func (s *Store) Version() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.version
}

func (s *Store) Len() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.entries)
}

// Replace atomically swaps the auth map when the config version changes.
func (s *Store) Replace(version string, rows []Entry) {
	next := make(map[key]value, len(rows))
	for _, row := range rows {
		if row.BindIP == "" || row.Username == "" {
			continue
		}
		ip := normalizeIP(row.BindIP)
		next[key{bindIP: ip, username: row.Username}] = value{password: row.Password, serviceID: row.ServiceID}
	}
	s.mu.Lock()
	s.version = version
	s.entries = next
	s.mu.Unlock()
}

// Entry is one assignment from Rackflow.
type Entry struct {
	BindIP    string
	Username  string
	Password  string
	ServiceID int
}

// Allow reports whether credentials are valid for the given bind IP.
//
// The (bindIP, username) lookup is a plain map hit -- neither is secret, so
// its lookup time leaking bucket/presence information isn't a concern. The
// password itself is compared with subtle.ConstantTimeCompare so a
// network-adjacent attacker measuring response timing can't use
// early-exit byte-by-byte string comparison to brute-force it a
// character at a time.
func (s *Store) Allow(bindIP, username, password string) (serviceID int, ok bool) {
	s.mu.RLock()
	v, found := s.entries[key{bindIP: normalizeIP(bindIP), username: username}]
	s.mu.RUnlock()
	if !found {
		return 0, false
	}
	if subtle.ConstantTimeCompare([]byte(v.password), []byte(password)) != 1 {
		return 0, false
	}
	return v.serviceID, true
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
