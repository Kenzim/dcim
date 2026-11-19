// Package config reads the node-side settings of the storage runner from the
// environment. None of these ever come from Rackflow.
package config

import (
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"rackflow/storage_runner/internal/names"
)

type Host struct {
	RackflowURL        string
	RunnerKey          string
	PollInterval       time.Duration
	ParentIface        string
	CephFSName         string
	CephECPool         string
	CephSubvolumeGroup string
	CephPublicNetworks []netip.Prefix
	CephConf           string
	CephKeyring        string
	GaneshaImage       string
	LinkCIDR           netip.Prefix
	StateDir           string
	PodmanRemoteURL    string
	// SFTP: the gateway's listen address (port from Rackflow), the back end
	// image and the sftp-server path inside it.
	SFTPListenAddr string
	SFTPImage      string
	SFTPServerPath string
	// CephHAPool holds the failover leases, node records and Ganesha
	// recovery databases.
	CephHAPool string
	// SFTPVIPInterface carries the location's SFTP virtual IP; empty means
	// the interface of the default route.
	SFTPVIPInterface string
	// CephUserPrefix starts each tenant's cephx user name; a second Rackflow
	// on the same Ceph cluster needs its own prefix and its own HA pool.
	CephUserPrefix string
}

const (
	DefaultPollInterval = 30 * time.Second
	DefaultGroup        = "rackflow"
	DefaultLinkCIDR     = "169.254.128.0/17"
	DefaultStateDir     = "/var/lib/rackflow-storage-runner"
	DefaultHAPool       = "rackflow_ha"
)

var (
	cephNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)
	// The prefix ends in a letter or '-' so the tenant id after it is unambiguous.
	userPrefixRe = regexp.MustCompile(`^[a-z]([a-z0-9-]{0,14}[a-z-])?$`)
	ifaceRe      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@-]{0,14}$`)
)

// FromEnv builds the host config; getenv is os.Getenv in production.
func FromEnv(getenv func(string) string) (Host, error) {
	get := func(key, def string) string {
		if v := strings.TrimSpace(getenv(key)); v != "" {
			return v
		}
		return def
	}
	var errs []error
	h := Host{
		RackflowURL:        strings.TrimRight(get("RACKFLOW_URL", ""), "/"),
		RunnerKey:          get("RUNNER_KEY", ""),
		ParentIface:        get("PARENT_IFACE", ""),
		CephFSName:         get("CEPH_FS_NAME", ""),
		CephECPool:         get("CEPH_EC_POOL", ""),
		CephSubvolumeGroup: get("CEPH_SUBVOLUME_GROUP", DefaultGroup),
		CephConf:           get("CEPH_CONF", ""),
		CephKeyring:        get("CEPH_KEYRING", ""),
		GaneshaImage:       get("GANESHA_IMAGE", ""),
		StateDir:           get("STATE_DIR", DefaultStateDir),
		PodmanRemoteURL:    get("PODMAN_REMOTE_URL", ""),
		SFTPListenAddr:     get("SFTP_LISTEN_ADDR", "0.0.0.0"),
		SFTPImage:          get("SFTP_IMAGE", ""),
		SFTPServerPath:     get("SFTP_SERVER_PATH", "/usr/lib/ssh/sftp-server"),
		CephHAPool:         get("CEPH_HA_POOL", DefaultHAPool),
		SFTPVIPInterface:   get("SFTP_VIP_INTERFACE", ""),
		CephUserPrefix:     get("CEPH_USER_PREFIX", names.DefaultCephUserPrefix),
	}
	for _, kv := range [][2]string{
		{"RACKFLOW_URL", h.RackflowURL},
		{"RUNNER_KEY", h.RunnerKey},
		{"PARENT_IFACE", h.ParentIface},
		{"CEPH_FS_NAME", h.CephFSName},
		{"CEPH_EC_POOL", h.CephECPool},
		{"GANESHA_IMAGE", h.GaneshaImage},
	} {
		if kv[1] == "" {
			errs = append(errs, fmt.Errorf("%s is required", kv[0]))
		}
	}

	interval, err := parseInterval(get("POLL_INTERVAL", ""))
	if err != nil {
		errs = append(errs, err)
	}
	h.PollInterval = interval

	if h.ParentIface != "" && !ifaceRe.MatchString(h.ParentIface) {
		errs = append(errs, fmt.Errorf("PARENT_IFACE %q is not a valid interface name", h.ParentIface))
	}
	if h.SFTPVIPInterface != "" && !ifaceRe.MatchString(h.SFTPVIPInterface) {
		errs = append(errs, fmt.Errorf("SFTP_VIP_INTERFACE %q is not a valid interface name", h.SFTPVIPInterface))
	}
	for _, kv := range [][2]string{
		{"CEPH_FS_NAME", h.CephFSName},
		{"CEPH_EC_POOL", h.CephECPool},
		{"CEPH_SUBVOLUME_GROUP", h.CephSubvolumeGroup},
		{"CEPH_HA_POOL", h.CephHAPool},
	} {
		if kv[1] != "" && !cephNameRe.MatchString(kv[1]) {
			errs = append(errs, fmt.Errorf("%s %q may only contain letters, digits, '.', '_' and '-'", kv[0], kv[1]))
		}
	}

	if !userPrefixRe.MatchString(h.CephUserPrefix) {
		errs = append(errs, fmt.Errorf("CEPH_USER_PREFIX %q must be 1-16 lower-case letters, digits or '-', starting with a letter and ending in a letter or '-'", h.CephUserPrefix))
	} else if h.CephUserPrefix != names.DefaultCephUserPrefix && h.CephHAPool == DefaultHAPool {
		errs = append(errs, fmt.Errorf("CEPH_USER_PREFIX %q is for a second Rackflow on a shared Ceph cluster, which also needs its own CEPH_HA_POOL (not %q)", h.CephUserPrefix, DefaultHAPool))
	}

	nets, err := parseNetworks(get("CEPH_PUBLIC_NETWORKS", ""))
	if err != nil {
		errs = append(errs, err)
	}
	h.CephPublicNetworks = nets

	link, err := netip.ParsePrefix(get("LINK_CIDR", DefaultLinkCIDR))
	switch {
	case err != nil:
		errs = append(errs, fmt.Errorf("LINK_CIDR: %w", err))
	case !link.Addr().Is4() || link.Bits() > 30:
		errs = append(errs, fmt.Errorf("LINK_CIDR %s must be an IPv4 prefix of /30 or larger", link))
	default:
		h.LinkCIDR = link.Masked()
		for _, n := range h.CephPublicNetworks {
			if n.Overlaps(h.LinkCIDR) {
				errs = append(errs, fmt.Errorf("LINK_CIDR %s overlaps Ceph public network %s", h.LinkCIDR, n))
			}
		}
	}
	return h, errors.Join(errs...)
}

func parseInterval(v string) (time.Duration, error) {
	if v == "" {
		return DefaultPollInterval, nil
	}
	if secs, err := strconv.Atoi(v); err == nil {
		if secs <= 0 {
			return 0, fmt.Errorf("POLL_INTERVAL must be positive")
		}
		return time.Duration(secs) * time.Second, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		return 0, fmt.Errorf("POLL_INTERVAL %q must be seconds or a positive Go duration", v)
	}
	return d, nil
}

func parseNetworks(v string) ([]netip.Prefix, error) {
	if v == "" {
		return nil, fmt.Errorf("CEPH_PUBLIC_NETWORKS is required")
	}
	var out []netip.Prefix
	for _, part := range strings.Split(v, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		p, err := netip.ParsePrefix(part)
		if err != nil {
			return nil, fmt.Errorf("CEPH_PUBLIC_NETWORKS: %w", err)
		}
		if !p.Addr().Is4() {
			return nil, fmt.Errorf("CEPH_PUBLIC_NETWORKS: %s is not IPv4", p)
		}
		out = append(out, p.Masked())
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("CEPH_PUBLIC_NETWORKS is required")
	}
	sort.Slice(out, func(i, j int) bool { return out[i].String() < out[j].String() })
	return out, nil
}
