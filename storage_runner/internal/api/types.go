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
	Tenants  []Tenant `json:"tenants"`
}

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
	State        string     `json:"state"`
	DestroyAfter *Timestamp `json:"destroy_after"`
}

type Status struct {
	VersionApplied   string         `json:"version_applied"`
	RunnerVersion    string         `json:"runner_version"`
	Node             NodeStatus     `json:"node"`
	Tenants          []TenantStatus `json:"tenants"`
	DestroyedTenants []int64        `json:"destroyed_tenants"`
	DestroyedShares  []int64        `json:"destroyed_shares"`
	Orphans          []string       `json:"orphans"`
}

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
