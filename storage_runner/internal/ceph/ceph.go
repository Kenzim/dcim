// Package ceph drives the Ceph CLI: pool checks, the runner's subvolume group,
// one subvolume per share and one cephx user per tenant.
package ceph

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"rackflow/storage_runner/internal/execx"
)

type SubvolumeInfo struct {
	Path          string
	DataPool      string
	PoolNamespace string
	BytesUsed     int64
	BytesQuota    int64 // 0 when the subvolume has no quota
}

// Caps are a cephx user's capabilities per daemon type.
type Caps struct {
	Mon string
	MDS string
	OSD string
}

func (c Caps) String() string {
	return fmt.Sprintf("mon=%q mds=%q osd=%q", c.Mon, c.MDS, c.OSD)
}

func (c Caps) args() []string {
	out := []string{"mon", c.Mon}
	if c.MDS != "" {
		out = append(out, "mds", c.MDS)
	}
	if c.OSD != "" {
		out = append(out, "osd", c.OSD)
	}
	return out
}

type User struct {
	Key  string
	Caps Caps
}

// Access is one subvolume a tenant user may read and write.
type Access struct {
	Path      string
	Pool      string
	Namespace string
}

// TenantCaps restricts a user to the given subvolume paths and their RADOS
// namespaces. With no access the user can only read the monitors.
func TenantCaps(access []Access) Caps {
	c := Caps{Mon: "allow r"}
	if len(access) == 0 {
		return c
	}
	sorted := append([]Access(nil), access...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	var mds, osd []string
	seen := map[string]bool{}
	for _, a := range sorted {
		mds = append(mds, "allow rw path="+a.Path)
		o := "allow rw pool=" + a.Pool
		if a.Namespace != "" {
			o += " namespace=" + a.Namespace
		}
		if !seen[o] {
			seen[o] = true
			osd = append(osd, o)
		}
	}
	c.MDS = strings.Join(mds, ", ")
	c.OSD = strings.Join(osd, ", ")
	return c
}

// WithRecovery adds read-write access to a RADOS namespace (Ganesha's
// rados_ng recovery database) to caps that already grant data access.
func WithRecovery(c Caps, pool, namespace string) Caps {
	if c.OSD == "" {
		return c
	}
	c.OSD += ", allow rw pool=" + pool + " namespace=" + namespace
	return c
}

type Cluster interface {
	// Check verifies the EC pool and file system and returns the EC pool's free
	// bytes, capped by what is left under the pool's quota when it has one.
	Check(ctx context.Context) (freeBytes int64, err error)
	// Group reports whether the subvolume group exists and its data pool.
	Group(ctx context.Context) (exists bool, pool string, err error)
	CreateGroup(ctx context.Context) error
	Subvolumes(ctx context.Context) ([]string, error)
	CreateSubvolume(ctx context.Context, name string, size int64) error
	ResizeSubvolume(ctx context.Context, name string, size int64) error
	SubvolumeInfo(ctx context.Context, name string) (SubvolumeInfo, error)
	RemoveSubvolume(ctx context.Context, name string) error
	// Users returns runner-owned users (client.rf-t*) by entity.
	Users(ctx context.Context) (map[string]User, error)
	CreateUser(ctx context.Context, entity string, caps Caps) (key string, err error)
	SetCaps(ctx context.Context, entity string, caps Caps) error
	RemoveUser(ctx context.Context, entity string) error
	// EvictSessions ends the MDS sessions of entity from clients whose
	// hostname is hostname, on each active rank.
	EvictSessions(ctx context.Context, entity, hostname string) error
	// EvictAddrSessions ends every MDS session from address ip.
	EvictAddrSessions(ctx context.Context, ip netip.Addr) (int, error)
	// DaemonAddrs lists the addresses of every mon, mgr, MDS and OSD.
	DaemonAddrs(ctx context.Context) ([]netip.Addr, error)
	MinimalConf(ctx context.Context) (string, error)
}

// CLI implements Cluster with the ceph command and the admin keyring.
type CLI struct {
	Cmd            execx.Commander
	FS             string
	SubvolumeGroup string
	ECPool         string
	Conf           string
	Keyring        string
}

func (c *CLI) ceph(ctx context.Context, args ...string) ([]byte, error) {
	var base []string
	if c.Conf != "" {
		base = append(base, "--conf", c.Conf)
	}
	if c.Keyring != "" {
		base = append(base, "--keyring", c.Keyring)
	}
	return c.Cmd.Run(ctx, nil, "ceph", append(base, args...)...)
}

func (c *CLI) cephJSON(ctx context.Context, v any, args ...string) error {
	out, err := c.ceph(ctx, append(args, "--format", "json")...)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(out, v); err != nil {
		return fmt.Errorf("ceph %s: parse output: %w", strings.Join(args, " "), err)
	}
	return nil
}

func (c *CLI) Check(ctx context.Context) (int64, error) {
	var ow map[string]any
	if err := c.cephJSON(ctx, &ow, "osd", "pool", "get", c.ECPool, "allow_ec_overwrites"); err != nil {
		return 0, fmt.Errorf("EC pool %q missing or not erasure-coded: %w", c.ECPool, err)
	}
	if v, _ := ow["allow_ec_overwrites"].(bool); !v {
		return 0, fmt.Errorf("EC pool %q does not have allow_ec_overwrites enabled", c.ECPool)
	}
	var fsList []struct {
		Name      string   `json:"name"`
		DataPools []string `json:"data_pools"`
	}
	if err := c.cephJSON(ctx, &fsList, "fs", "ls"); err != nil {
		return 0, err
	}
	found := false
	for _, fs := range fsList {
		if fs.Name != c.FS {
			continue
		}
		found = true
		ok := false
		for _, p := range fs.DataPools {
			if p == c.ECPool {
				ok = true
			}
		}
		if !ok {
			return 0, fmt.Errorf("EC pool %q is not a data pool of file system %q (ceph fs add_data_pool)", c.ECPool, c.FS)
		}
	}
	if !found {
		return 0, fmt.Errorf("file system %q not found", c.FS)
	}
	var df struct {
		Pools []struct {
			Name  string `json:"name"`
			Stats struct {
				MaxAvail   int64 `json:"max_avail"`
				Stored     int64 `json:"stored"`
				QuotaBytes int64 `json:"quota_bytes"`
			} `json:"stats"`
		} `json:"pools"`
	}
	// "detail" adds quota_bytes; max_avail ignores a pool quota.
	if err := c.cephJSON(ctx, &df, "df", "detail"); err != nil {
		return 0, err
	}
	for _, p := range df.Pools {
		if p.Name == c.ECPool {
			free := p.Stats.MaxAvail
			if quota := p.Stats.QuotaBytes; quota > 0 {
				free = min(free, max(quota-p.Stats.Stored, 0))
			}
			return free, nil
		}
	}
	return 0, fmt.Errorf("EC pool %q missing from ceph df", c.ECPool)
}

func (c *CLI) Group(ctx context.Context) (bool, string, error) {
	var groups []struct {
		Name string `json:"name"`
	}
	if err := c.cephJSON(ctx, &groups, "fs", "subvolumegroup", "ls", c.FS); err != nil {
		return false, "", err
	}
	exists := false
	for _, g := range groups {
		if g.Name == c.SubvolumeGroup {
			exists = true
		}
	}
	if !exists {
		return false, "", nil
	}
	var info struct {
		DataPool string `json:"data_pool"`
	}
	if err := c.cephJSON(ctx, &info, "fs", "subvolumegroup", "info", c.FS, c.SubvolumeGroup); err != nil {
		return true, "", err
	}
	return true, info.DataPool, nil
}

func (c *CLI) CreateGroup(ctx context.Context) error {
	_, err := c.ceph(ctx, "fs", "subvolumegroup", "create", c.FS, c.SubvolumeGroup, "--pool_layout", c.ECPool)
	return err
}

func (c *CLI) Subvolumes(ctx context.Context) ([]string, error) {
	var list []struct {
		Name string `json:"name"`
	}
	if err := c.cephJSON(ctx, &list, "fs", "subvolume", "ls", c.FS, "--group_name", c.SubvolumeGroup); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(list))
	for _, s := range list {
		out = append(out, s.Name)
	}
	sort.Strings(out)
	return out, nil
}

func (c *CLI) CreateSubvolume(ctx context.Context, name string, size int64) error {
	// root_squash maps client root to nobody, so the share root must be writable
	// by everyone. No sticky bit: clients with fs.protected_regular refuse root's
	// O_CREAT in a sticky world-writable folder because the file belongs to nobody.
	_, err := c.ceph(ctx, "fs", "subvolume", "create", c.FS, name,
		"--size", strconv.FormatInt(size, 10),
		"--group_name", c.SubvolumeGroup,
		"--pool_layout", c.ECPool,
		"--mode", "777",
		"--namespace-isolated")
	return err
}

func (c *CLI) ResizeSubvolume(ctx context.Context, name string, size int64) error {
	_, err := c.ceph(ctx, "fs", "subvolume", "resize", c.FS, name, strconv.FormatInt(size, 10), "--group_name", c.SubvolumeGroup)
	return err
}

func (c *CLI) SubvolumeInfo(ctx context.Context, name string) (SubvolumeInfo, error) {
	var raw struct {
		Path          string          `json:"path"`
		DataPool      string          `json:"data_pool"`
		PoolNamespace string          `json:"pool_namespace"`
		BytesUsed     int64           `json:"bytes_used"`
		BytesQuota    json.RawMessage `json:"bytes_quota"`
	}
	if err := c.cephJSON(ctx, &raw, "fs", "subvolume", "info", c.FS, name, "--group_name", c.SubvolumeGroup); err != nil {
		return SubvolumeInfo{}, err
	}
	info := SubvolumeInfo{Path: raw.Path, DataPool: raw.DataPool, PoolNamespace: raw.PoolNamespace, BytesUsed: raw.BytesUsed}
	if len(raw.BytesQuota) > 0 && raw.BytesQuota[0] != '"' {
		if err := json.Unmarshal(raw.BytesQuota, &info.BytesQuota); err != nil {
			return SubvolumeInfo{}, fmt.Errorf("subvolume %s bytes_quota: %w", name, err)
		}
	}
	if info.Path == "" {
		return SubvolumeInfo{}, fmt.Errorf("subvolume %s info has no path", name)
	}
	return info, nil
}

func (c *CLI) RemoveSubvolume(ctx context.Context, name string) error {
	_, err := c.ceph(ctx, "fs", "subvolume", "rm", c.FS, name, "--group_name", c.SubvolumeGroup)
	return err
}

func (c *CLI) Users(ctx context.Context) (map[string]User, error) {
	var dump struct {
		AuthDump []struct {
			Entity string            `json:"entity"`
			Key    string            `json:"key"`
			Caps   map[string]string `json:"caps"`
		} `json:"auth_dump"`
	}
	if err := c.cephJSON(ctx, &dump, "auth", "ls"); err != nil {
		return nil, err
	}
	out := map[string]User{}
	for _, e := range dump.AuthDump {
		if !strings.HasPrefix(e.Entity, "client.rf-t") {
			continue
		}
		out[e.Entity] = User{Key: e.Key, Caps: Caps{Mon: e.Caps["mon"], MDS: e.Caps["mds"], OSD: e.Caps["osd"]}}
	}
	return out, nil
}

func (c *CLI) CreateUser(ctx context.Context, entity string, caps Caps) (string, error) {
	var res []struct {
		Key string `json:"key"`
	}
	args := append([]string{"auth", "get-or-create", entity}, caps.args()...)
	if err := c.cephJSON(ctx, &res, args...); err != nil {
		return "", err
	}
	if len(res) == 0 || res[0].Key == "" {
		return "", fmt.Errorf("ceph auth get-or-create %s returned no key", entity)
	}
	return res[0].Key, nil
}

func (c *CLI) SetCaps(ctx context.Context, entity string, caps Caps) error {
	_, err := c.ceph(ctx, append([]string{"auth", "caps", entity}, caps.args()...)...)
	return err
}

func (c *CLI) RemoveUser(ctx context.Context, entity string) error {
	_, err := c.ceph(ctx, "auth", "rm", entity)
	return err
}

func (c *CLI) mdsRanks(ctx context.Context) ([]string, error) {
	var fs struct {
		MDSMap struct {
			In []int `json:"in"`
		} `json:"mdsmap"`
	}
	if err := c.cephJSON(ctx, &fs, "fs", "get", c.FS); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(fs.MDSMap.In))
	for _, rank := range fs.MDSMap.In {
		out = append(out, fmt.Sprintf("mds.%s:%d", c.FS, rank))
	}
	return out, nil
}

// EvictSessions evicts entity's sessions whose client hostname is hostname
// (every session of entity when hostname is empty), on each active rank,
// so a dead Ganesha's caps are released at once; otherwise the MDS makes
// its successor wait for session_autoclose. The
// hostname filter spares the same user's sessions from other hosts (the SFTP
// master's kernel mounts).
func (c *CLI) EvictSessions(ctx context.Context, entity, hostname string) error {
	ranks, err := c.mdsRanks(ctx)
	if err != nil {
		return err
	}
	authName := strings.TrimPrefix(entity, "client.")
	args := []string{"client", "evict", "auth_name=" + authName}
	if hostname != "" {
		args = append(args, "client_metadata.hostname="+hostname)
	}
	for _, target := range ranks {
		if _, err := c.ceph(ctx, append([]string{"tell", target}, args...)...); err != nil {
			return fmt.Errorf("evict %s@%s on %s: %w", entity, hostname, target, err)
		}
	}
	return nil
}

// EvictAddrSessions evicts, on each active rank, every session whose client
// address is ip, and returns how many it evicted.
func (c *CLI) EvictAddrSessions(ctx context.Context, ip netip.Addr) (int, error) {
	ranks, err := c.mdsRanks(ctx)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, target := range ranks {
		var sessions []struct {
			ID   int64  `json:"id"`
			Inst string `json:"inst"`
		}
		if err := c.cephJSON(ctx, &sessions, "tell", target, "client", "ls"); err != nil {
			return n, fmt.Errorf("client ls on %s: %w", target, err)
		}
		for _, s := range sessions {
			if a, ok := firstIPv4(s.Inst); !ok || a != ip {
				continue
			}
			if _, err := c.ceph(ctx, "tell", target, "client", "evict", "id="+strconv.FormatInt(s.ID, 10)); err != nil {
				return n, fmt.Errorf("evict client %d on %s: %w", s.ID, target, err)
			}
			n++
		}
	}
	return n, nil
}

var ipv4Re = regexp.MustCompile(`(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):\d+`)

func firstIPv4(s string) (netip.Addr, bool) {
	m := ipv4Re.FindStringSubmatch(s)
	if m == nil {
		return netip.Addr{}, false
	}
	a, err := netip.ParseAddr(m[1])
	return a, err == nil
}

// collectAddrs adds every IPv4 address found in string values under keys
// that contain "addr".
func collectAddrs(v any, underAddr bool, out map[netip.Addr]bool) {
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			collectAddrs(e, underAddr || strings.Contains(k, "addr"), out)
		}
	case []any:
		for _, e := range x {
			collectAddrs(e, underAddr, out)
		}
	case string:
		if underAddr {
			for _, m := range ipv4Re.FindAllStringSubmatch(x, -1) {
				if a, err := netip.ParseAddr(m[1]); err == nil && !a.IsUnspecified() {
					out[a] = true
				}
			}
		}
	}
}

// DaemonAddrs returns the addresses of every monitor, manager, MDS and OSD.
func (c *CLI) DaemonAddrs(ctx context.Context) ([]netip.Addr, error) {
	found := map[netip.Addr]bool{}
	for _, args := range [][]string{{"mon", "dump"}, {"mgr", "dump"}, {"fs", "dump"}, {"osd", "dump"}} {
		var v any
		if err := c.cephJSON(ctx, &v, args...); err != nil {
			return nil, err
		}
		collectAddrs(v, false, found)
	}
	out := make([]netip.Addr, 0, len(found))
	for a := range found {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Less(out[j]) })
	return out, nil
}

func (c *CLI) MinimalConf(ctx context.Context) (string, error) {
	out, err := c.ceph(ctx, "config", "generate-minimal-conf")
	if err != nil {
		return "", err
	}
	return string(out), nil
}
