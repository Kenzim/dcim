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
	SFTP *SFTPSettings `json:"sftp,omitempty"`
	// HA is the failover block; nil (older Rackflow) means no leases at all.
	HA      *HA      `json:"ha,omitempty"`
	Tenants []Tenant `json:"tenants"`
	// Networks are the private networks whose presence this node may
	// serve (docs/private-network-dhcp.md 4.4); absent before 0.4.0.
	Networks []Network `json:"networks,omitempty"`
}

// Network is one private network that needs a presence: its storage
// address answered by one node, with DHCP.
type Network struct {
	ID              int64   `json:"id"`
	VLAN            int     `json:"vlan"`
	NetworkCIDR     string  `json:"network_cidr"`
	EndpointIP      string  `json:"endpoint_ip"`
	PrefixLen       int     `json:"prefix_len"`
	PreferredNodeID int64   `json:"preferred_node_id"`
	TakeoverOrder   []int64 `json:"takeover_order"`
	DHCP            *DHCP   `json:"dhcp"`
}

// DHCP is the DHCP block of a network or tenant.
type DHCP struct {
	Enabled      bool              `json:"enabled"`
	LeaseSeconds int64             `json:"lease_seconds"`
	Reservations []DHCPReservation `json:"reservations"`
	// Range nil means no dynamic range.
	Range   *DHCPRange  `json:"range"`
	Leases  []DHCPLease `json:"leases"`
	Options DHCPOptions `json:"options"`
}

type DHCPReservation struct {
	MAC string `json:"mac"`
	IP  string `json:"ip"`
}

type DHCPRange struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

// DHCPLease is a dynamic lease, in the config (seed) and in the status.
// Hostname is null when the client sent none.
type DHCPLease struct {
	MAC       string    `json:"mac"`
	IP        string    `json:"ip"`
	ExpiresAt Timestamp `json:"expires_at"`
	Hostname  *string   `json:"hostname"`
}

// DHCPOptions are the owner's options; null or empty means not sent.
type DHCPOptions struct {
	Router     *string     `json:"router"`
	DNSServers []string    `json:"dns_servers"`
	Domain     *string     `json:"domain"`
	MTU        *int        `json:"mtu"`
	Routes     []DHCPRoute `json:"routes"`
	NTPServers []string    `json:"ntp_servers"`
}

type DHCPRoute struct {
	Destination string `json:"destination"`
	Gateway     string `json:"gateway"`
}

// HA configures leases and failover (docs/nfs-storage-failover.md 11.2).
// Timings are seconds; zero means the default.
type HA struct {
	Enabled            bool     `json:"enabled"`
	LeaseSeconds       int64    `json:"lease_seconds"`
	RenewSeconds       int64    `json:"renew_seconds"`
	StopMarginSeconds  int64    `json:"stop_margin_seconds"`
	GuardMarginSeconds int64    `json:"guard_margin_seconds"`
	PollSeconds        int64    `json:"poll_seconds"`
	RankStepSeconds    int64    `json:"rank_step_seconds"`
	FenceSeconds       int64    `json:"fence_seconds"`
	NFSLeaseLifetime   int64    `json:"nfs_lease_lifetime"`
	NFSGracePeriod     int64    `json:"nfs_grace_period"`
	Draining           bool     `json:"draining"`
	Nodes              []HANode `json:"nodes"`
}

type HANode struct {
	ID       int64  `json:"id"`
	Name     string `json:"name"`
	Draining bool   `json:"draining"`
}

// Move is a planned hand-off of a tenant to another node.
type Move struct {
	ID       string `json:"id"`
	ToNodeID int64  `json:"to_node_id"`
}

// SFTPSettings configures the node's SFTP front end.
type SFTPSettings struct {
	Enabled              bool  `json:"enabled"`
	Port                 int   `json:"port"`
	MaxAuthFailures      int   `json:"max_auth_failures"`
	FailureWindowSeconds int64 `json:"failure_window_seconds"`
	BanSeconds           int64 `json:"ban_seconds"`

	LocationID int64 `json:"location_id"`
	// VirtualIP is the location's SFTP address with prefix ("203.0.113.10/24");
	// nil means SFTP is served per node.
	VirtualIP *string `json:"virtual_ip"`
	// HostKey is the location's OpenSSH ed25519 private key; empty means the
	// node keeps its own key.
	HostKey        string  `json:"host_key"`
	MasterEligible bool    `json:"master_eligible"`
	MasterOrder    []int64 `json:"master_order"`
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
	ID         int64  `json:"id"`
	Name       string `json:"name"`
	LocationID int64  `json:"location_id"`
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

	PreferredNodeID int64   `json:"preferred_node_id"`
	TakeoverOrder   []int64 `json:"takeover_order"`
	Move            *Move   `json:"move"`

	// NetworkID names the private network this tenant is the presence of;
	// with DHCP the tenant serves DHCP for it. Both are absent before 0.4.0.
	NetworkID *int64 `json:"network_id,omitempty"`
	DHCP      *DHCP  `json:"dhcp,omitempty"`
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
	// HA and Events are sent only when the config had an ha block.
	HA     *HAStatus `json:"ha,omitempty"`
	Events []Event   `json:"events,omitempty"`
	// Networks lists the presences this node serves or has just stopped
	// serving.
	Networks []NetworkStatus `json:"networks,omitempty"`
}

// NetworkStatus reports one network presence.
type NetworkStatus struct {
	ID      int64       `json:"id"`
	Serving bool        `json:"serving"`
	Since   *time.Time  `json:"since"`
	Error   *string     `json:"error"`
	Leases  []DHCPLease `json:"leases"`
}

// TenantDHCP reports the DHCP server a tenant runs for its network.
type TenantDHCP struct {
	Error  *string     `json:"error"`
	Leases []DHCPLease `json:"leases"`
}

// HAStatus is the node's lease state.
type HAStatus struct {
	LeaseOK     bool       `json:"lease_ok"`
	Fenced      bool       `json:"fenced"`
	FencedUntil *time.Time `json:"fenced_until"`
	SFTPMaster  bool       `json:"sftp_master"`
	HAActive    bool       `json:"ha_active"`
}

// Event kinds.
const (
	EventTakeover   = "takeover"
	EventHandoff    = "handoff"
	EventLost       = "lost"
	EventFenced     = "fenced"
	EventSFTPMaster = "sftp_master"
)

// Event is a lease change, sent until a status post carrying it succeeds.
type Event struct {
	At       time.Time `json:"at"`
	Kind     string    `json:"kind"`
	TenantID *int64    `json:"tenant_id"`
	// NetworkID is set instead of TenantID for a network presence.
	NetworkID  *int64 `json:"network_id,omitempty"`
	FromNodeID *int64 `json:"from_node_id"`
	// Fence is "tenant" or "node", nil when no fence was needed.
	Fence  *string `json:"fence"`
	Detail string  `json:"detail"`
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
	// Serving, Since and MoveID are sent only when the config had an ha block.
	Serving *bool      `json:"serving,omitempty"`
	Since   *time.Time `json:"since,omitempty"`
	MoveID  string     `json:"move_id,omitempty"`
	// DHCP is sent for a tenant with a network_id that this node serves.
	DHCP *TenantDHCP `json:"dhcp,omitempty"`
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
		if d := p.Tenants[i].DHCP; d != nil && d.Leases == nil {
			d.Leases = []DHCPLease{}
		}
	}
	for i := range p.Networks {
		if p.Networks[i].Leases == nil {
			p.Networks[i].Leases = []DHCPLease{}
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
