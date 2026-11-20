// Package lease decides which node serves a tenant (and which node is the
// location's SFTP master) with RADOS cls_lock leases, as described in
// docs/nfs-storage-failover.md. Store is the RADOS side; Manager holds,
// renews, acquires, fences and releases leases on top of it.
package lease

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"strconv"
	"time"
)

// Namespace is the RADOS namespace of lease objects and node records.
const Namespace = "rackflow-ha"

// LockName is the cls_lock name of every lease.
const LockName = "owner"

// FlagMustRenew is librados' LIBRADOS_LOCK_FLAG_MUST_RENEW: the lock call
// fails with ENOENT instead of re-acquiring when the caller's entry is gone.
const FlagMustRenew = 2

// Lease xattrs.
const (
	XattrOwner    = "rf.owner"
	XattrReleased = "rf.released"
	XattrFenced   = "rf.fenced"
)

// Errors a Store maps RADOS failures to; test them with errors.Is.
var (
	ErrBusy        = errors.New("EBUSY")
	ErrNotFound    = errors.New("ENOENT")
	ErrExists      = errors.New("EEXIST")
	ErrBlocklisted = errors.New("EBLOCKLISTED")
	ErrTimeout     = errors.New("timed out")
)

// Locker is one entry of a lock: the RADOS client instance (client.<gid>),
// the cookie it locked with and its address (v1:10.0.0.5:0/123456).
type Locker struct {
	Client string `json:"client"`
	Cookie string `json:"cookie"`
	Addr   string `json:"addr"`
}

// Lockers is the state of one lock; Tag is empty when nobody holds it.
type Lockers struct {
	Tag       string   `json:"tag"`
	Exclusive bool     `json:"exclusive"`
	Lockers   []Locker `json:"lockers"`
}

// Identity is the lease client instance a Store currently talks through.
// Instance is random per client process and is part of every cookie.
type Identity struct {
	Instance string
	Client   string
	Addr     string
}

// BlockEntry is one OSD blocklist entry.
type BlockEntry struct {
	Addr  string
	Until time.Time
}

// Store is RADOS as the lease manager sees it. Object calls work in
// Namespace of the HA pool.
type Store interface {
	Identity(ctx context.Context) (Identity, error)
	LockShared(ctx context.Context, oid, cookie, tag string, duration time.Duration, flags int) error
	Unlock(ctx context.Context, oid, cookie string) error
	BreakLock(ctx context.Context, oid, client, cookie string) error
	ListLockers(ctx context.Context, oid string) (Lockers, error)
	GetXattrs(ctx context.Context, oid string) (map[string]string, error)
	SetXattr(ctx context.Context, oid, name, value string) error
	WriteFull(ctx context.Context, oid string, data []byte) error
	Read(ctx context.Context, oid string) ([]byte, error)
	Remove(ctx context.Context, oid string) error
	// PurgeNamespace removes every object in another namespace of the pool.
	PurgeNamespace(ctx context.Context, ns string) error
	BlocklistAdd(ctx context.Context, addr string, expire time.Duration) error
	BlocklistLs(ctx context.Context) ([]BlockEntry, error)
	// Reset drops the current client instance; the next call starts a new one.
	Reset()
}

var ipv4Re = regexp.MustCompile(`(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):\d+`)

// AddrIP returns the IPv4 address of a RADOS address string such as
// "v1:10.0.0.5:0/123", "10.0.0.5:6789/0" or "[v2:10.0.0.5:3300/0,v1:...]".
func AddrIP(s string) (netip.Addr, bool) {
	m := ipv4Re.FindStringSubmatch(s)
	if m == nil {
		return netip.Addr{}, false
	}
	a, err := netip.ParseAddr(m[1])
	if err != nil {
		return netip.Addr{}, false
	}
	return a, true
}

// blockedIP returns the address a blocklist entry covers whole: an address
// entry ("10.0.0.5:0/0" or with a port and nonce) or a single-address range
// ("10.0.0.5/32", "2001:db8::5/128").
func blockedIP(s string) (netip.Addr, bool) {
	if p, err := netip.ParsePrefix(s); err == nil {
		if p.Bits() != p.Addr().BitLen() {
			return netip.Addr{}, false
		}
		return p.Addr(), true
	}
	return AddrIP(s)
}

// Kind is what a lease is for.
type Kind string

const (
	KindTenant Kind = "tenant"
	KindSFTP   Kind = "sftp"
)

// Key names one lease: a tenant (ID is the tenant id) or a location's SFTP
// master (ID is the location id).
type Key struct {
	Kind Kind
	ID   int64
}

func TenantKey(id int64) Key     { return Key{KindTenant, id} }
func SFTPKey(location int64) Key { return Key{KindSFTP, location} }

// Object is the RADOS object of the lease.
func (k Key) Object() string { return string(k.Kind) + "." + strconv.FormatInt(k.ID, 10) }

func (k Key) String() string { return k.Object() }

// ParseKey reads an object name back into a Key.
func ParseKey(s string) (Key, bool) {
	for _, kind := range []Kind{KindTenant, KindSFTP} {
		prefix := string(kind) + "."
		if len(s) > len(prefix) && s[:len(prefix)] == prefix {
			id, err := strconv.ParseInt(s[len(prefix):], 10, 64)
			if err != nil || id <= 0 {
				return Key{}, false
			}
			return Key{kind, id}, true
		}
	}
	return Key{}, false
}

// NodeObject is the RADOS object of a node record.
func NodeObject(node int64) string { return fmt.Sprintf("node.%d", node) }

// Tag is the lock tag of a node.
func Tag(node int64) string { return fmt.Sprintf("node.%d", node) }

// Cookie is the lock cookie of a node's lease client instance.
func Cookie(node int64, instance string) string { return fmt.Sprintf("node.%d.%s", node, instance) }

// NodeRecord is the JSON content of node.<id>.
type NodeRecord struct {
	NodeID        int64  `json:"node_id"`
	LocationID    int64  `json:"location_id"`
	Name          string `json:"name"`
	Hostname      string `json:"hostname"`
	CephAddr      string `json:"ceph_addr"`
	RunnerVersion string `json:"runner_version"`
	Seq           int64  `json:"seq"`
}
