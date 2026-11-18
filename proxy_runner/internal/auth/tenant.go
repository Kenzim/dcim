package auth

import (
	"bytes"
	"crypto/subtle"
	"net"
	"sort"
)

// TenantEntry is one fast-path assignment: credentials valid on a private IP
// that egress from a public bind IP.
type TenantEntry struct {
	PrivateIP string
	BindIP    string
	Username  string
	Password  string
	ServiceID int
}

type tenantKey struct {
	privateIP string
	username  string
}

type tenantValue struct {
	bindIP    string
	password  string
	serviceID int
}

// TenantStore holds one client's fast-path credentials. It is immutable once
// built; callers swap whole stores when the config changes. It never falls
// back to the public Store, and the public Store never sees these entries.
type TenantStore struct {
	entries map[tenantKey][]tenantValue
}

// NewTenantStore indexes rows by (private IP, username). Entries for one key
// are sorted by bind IP so a lookup without X-Bind-IP is deterministic.
func NewTenantStore(rows []TenantEntry) *TenantStore {
	entries := make(map[tenantKey][]tenantValue, len(rows))
	for _, row := range rows {
		if row.PrivateIP == "" || row.BindIP == "" || row.Username == "" {
			continue
		}
		k := tenantKey{privateIP: normalizeIP(row.PrivateIP), username: row.Username}
		entries[k] = append(entries[k], tenantValue{
			bindIP:    normalizeIP(row.BindIP),
			password:  row.Password,
			serviceID: row.ServiceID,
		})
	}
	for k := range entries {
		list := entries[k]
		sort.Slice(list, func(i, j int) bool { return ipLess(list[i].bindIP, list[j].bindIP) })
	}
	return &TenantStore{entries: entries}
}

// Len is the number of entries in the store.
func (t *TenantStore) Len() int {
	if t == nil {
		return 0
	}
	n := 0
	for _, list := range t.entries {
		n += len(list)
	}
	return n
}

// Lookup checks credentials presented on privateIP and returns the public IP
// to egress from. Every candidate's password is compared in constant time.
// With xBindIP set, only a matching entry that owns that public IP is
// accepted; without it the lowest owned public IP is used.
func (t *TenantStore) Lookup(privateIP, username, password, xBindIP string) (bindIP string, serviceID int, ok bool) {
	if t == nil {
		return "", 0, false
	}
	list := t.entries[tenantKey{privateIP: normalizeIP(privateIP), username: username}]
	want := ""
	if xBindIP != "" {
		want = normalizeIP(xBindIP)
	}
	for _, v := range list {
		if subtle.ConstantTimeCompare([]byte(v.password), []byte(password)) != 1 {
			continue
		}
		if want != "" && v.bindIP != want {
			continue
		}
		if !ok {
			bindIP, serviceID, ok = v.bindIP, v.serviceID, true
		}
	}
	return bindIP, serviceID, ok
}

func ipLess(a, b string) bool {
	pa, pb := net.ParseIP(a), net.ParseIP(b)
	if pa == nil || pb == nil {
		return a < b
	}
	return bytes.Compare(pa.To16(), pb.To16()) < 0
}
