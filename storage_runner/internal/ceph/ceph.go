// Package ceph drives the Ceph CLI: pool checks, the runner's subvolume group,
// one subvolume per share and one cephx user per tenant.
package ceph

import (
	"context"
	"encoding/json"
	"fmt"
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

type Cluster interface {
	// Check verifies the EC pool and file system and returns the EC pool's free bytes.
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
				MaxAvail int64 `json:"max_avail"`
			} `json:"stats"`
		} `json:"pools"`
	}
	if err := c.cephJSON(ctx, &df, "df"); err != nil {
		return 0, err
	}
	for _, p := range df.Pools {
		if p.Name == c.ECPool {
			return p.Stats.MaxAvail, nil
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
	_, err := c.ceph(ctx, "fs", "subvolume", "create", c.FS, name,
		"--size", strconv.FormatInt(size, 10),
		"--group_name", c.SubvolumeGroup,
		"--pool_layout", c.ECPool,
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

func (c *CLI) MinimalConf(ctx context.Context) (string, error) {
	out, err := c.ceph(ctx, "config", "generate-minimal-conf")
	if err != nil {
		return "", err
	}
	return string(out), nil
}
