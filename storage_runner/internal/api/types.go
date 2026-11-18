// Package api is the storage runner's side of the Rackflow contract:
// GET /api/runner/storage/config and POST /api/runner/storage/status.
package api

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Tenant states sent by Rackflow.
const (
	TenantActive     = "active"
	TenantSuspended  = "suspended"
	TenantTerminated = "terminated"
)

// Share states sent by Rackflow.
const (
	ShareActive   = "active"
	ShareDeleting = "deleting"
)

// States reported by the runner.
const (
	StateReady     = "ready"
	StatePending   = "pending"
	StateError     = "error"
	StateSuspended = "suspended"
)

type Config struct {
	Version  string   `json:"version"`
	Node     Node     `json:"node"`
	Settings Settings `json:"settings"`
	// SFTP is the node's public SFTP endpoint; nil (older Rackflow) means off.
	SFTP    *SFTPSettings `json:"sftp,omitempty"`
	Tenants []Tenant      `json:"tenants"`
}

// SFTPSettings configures the node's SFTP front end.
type SFTPSettings struct {
	Enabled              bool  `json:"enabled"`
	Port                 int   `json:"port"`
	MaxAuthFailures      int   `json:"max_auth_failures"`
	FailureWindowSeconds int64 `json:"failure_window_seconds"`
	BanSeconds           int64 `json:"ban_seconds"`
}

// SFTPUser is one login of a tenant. Rackflow sends only a bcrypt hash of
// the password and the public keys.
type SFTPUser struct {
	ID           int64    `json:"id"`
	Username     string   `json:"username"`
	PublicKeys   []string `json:"public_keys"`
	PasswordHash *string  `json:"password_hash"`
	AllowedIPs   []string `json:"allowed_ips"`
}

// SFTP access modes of a share.
const (
	SFTPOff       = "off"
	SFTPReadOnly  = "ro"
	SFTPReadWrite = "rw"
)

type Node struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
}

type Settings struct {
	DestroyGraceSeconds *int64 `json:"destroy_grace_seconds"`
}

type Tenant struct {
	ID           int64      `json:"id"`
	State        string     `json:"state"`
	TerminatedAt *Timestamp `json:"terminated_at"`
	DestroyAfter *Timestamp `json:"destroy_after"`
	VLAN         int        `json:"vlan"`
	NetworkCIDR  string     `json:"network_cidr"`
	EndpointIP   string     `json:"endpoint_ip"`
	PrefixLen    int        `json:"prefix_len"`
	SizeBytes    int64      `json:"size_bytes"`
	Limits       *Limits    `json:"limits"`
	Shares       []Share    `json:"shares"`
	SFTPUsers    []SFTPUser `json:"sftp_users,omitempty"`
}

// Limits are parsed but not enforced yet.
type Limits struct {
	BandwidthMbps *int64 `json:"bandwidth_mbps"`
	IOPS          *int64 `json:"iops"`
}

type Share struct {
	ID           int64      `json:"id"`
	Name         string     `json:"name"`
	QuotaBytes   int64      `json:"quota_bytes"`
	Clients      []string   `json:"clients"`
	ReadOnly     bool       `json:"read_only"`
	SFTP         string     `json:"sftp,omitempty"`
	State        string     `json:"state"`
	DestroyAfter *Timestamp `json:"destroy_after"`
}

// SFTPMode is the share's SFTP access, "off" when missing or unknown.
func (s Share) SFTPMode() string {
	switch s.SFTP {
	case SFTPReadOnly, SFTPReadWrite:
		return s.SFTP
	}
	return SFTPOff
}

type Status struct {
	VersionApplied   string         `json:"version_applied"`
	RunnerVersion    string         `json:"runner_version"`
	Node             NodeStatus     `json:"node"`
	Tenants          []TenantStatus `json:"tenants"`
	DestroyedTenants []int64        `json:"destroyed_tenants"`
	DestroyedShares  []int64        `json:"destroyed_shares"`
	Orphans          []string       `json:"orphans"`
	SFTP             *SFTPStatus    `json:"sftp,omitempty"`
	SFTPLogins       []SFTPLogin    `json:"sftp_logins"`
}

// SFTPStatus reports the node's SFTP front end.
type SFTPStatus struct {
	Listening          bool    `json:"listening"`
	Port               int     `json:"port"`
	HostKey            string  `json:"host_key"`
	HostKeyFingerprint string  `json:"host_key_fingerprint"`
	Error              *string `json:"error"`
}

// SFTPLogin is the latest successful login of a user.
type SFTPLogin struct {
	UserID int64     `json:"user_id"`
	At     time.Time `json:"at"`
	IP     string    `json:"ip"`
}

// TenantSFTP reports a tenant's SFTP back end.
type TenantSFTP struct {
	State string  `json:"state"`
	Error *string `json:"error"`
}

// Tenant SFTP states.
const (
	SFTPStateReady   = "ready"
	SFTPStatePending = "pending"
	SFTPStateError   = "error"
	SFTPStateOff     = "off"
)

type NodeStatus struct {
	CephOK    bool    `json:"ceph_ok"`
	FreeBytes int64   `json:"free_bytes"`
	Error     *string `json:"error"`
}

type TenantStatus struct {
	ID     int64         `json:"id"`
	State  string        `json:"state"`
	Error  *string       `json:"error"`
	Shares []ShareStatus `json:"shares"`
	SFTP   *TenantSFTP   `json:"sftp,omitempty"`
}

type ShareStatus struct {
	ID        int64   `json:"id"`
	State     string  `json:"state"`
	UsedBytes int64   `json:"used_bytes"`
	Error     *string `json:"error"`
}

// MarshalJSON keeps empty lists as [] rather than null.
func (s Status) MarshalJSON() ([]byte, error) {
	type plain Status
	p := plain(s)
	if p.Tenants == nil {
		p.Tenants = []TenantStatus{}
	}
	for i := range p.Tenants {
		if p.Tenants[i].Shares == nil {
			p.Tenants[i].Shares = []ShareStatus{}
		}
	}
	if p.DestroyedTenants == nil {
		p.DestroyedTenants = []int64{}
	}
	if p.DestroyedShares == nil {
		p.DestroyedShares = []int64{}
	}
	if p.Orphans == nil {
		p.Orphans = []string{}
	}
	if p.SFTPLogins == nil {
		p.SFTPLogins = []SFTPLogin{}
	}
	return json.Marshal(p)
}

// Timestamp accepts ISO 8601 with or without an offset; a missing offset means UTC.
type Timestamp struct {
	time.Time
}

var naiveLayouts = []string{
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999",
}

func (t *Timestamp) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return fmt.Errorf("timestamp: %w", err)
	}
	s = strings.TrimSpace(s)
	if v, err := time.Parse(time.RFC3339Nano, s); err == nil {
		t.Time = v.UTC()
		return nil
	}
	if v, err := time.Parse("2006-01-02 15:04:05.999999999Z07:00", s); err == nil {
		t.Time = v.UTC()
		return nil
	}
	for _, layout := range naiveLayouts {
		if v, err := time.ParseInLocation(layout, s, time.UTC); err == nil {
			t.Time = v
			return nil
		}
	}
	return fmt.Errorf("timestamp %q is not ISO 8601", s)
}

func (t Timestamp) MarshalJSON() ([]byte, error) {
	return json.Marshal(t.Time.UTC().Format(time.RFC3339))
}
