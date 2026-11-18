// Package netns manages one network namespace per tenant: the customer VLAN
// interface with the endpoint IP, and a veth /30 to the host carrying routes
// to the Ceph public networks only.
package netns

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/names"
)

// Spec is the desired network of one tenant.
type Spec struct {
	TenantID int64
	VLAN     int
	Address  netip.Prefix // endpoint IP with the customer prefix length
	HostAddr netip.Prefix // host end of the veth /30
	NSAddr   netip.Prefix // namespace end of the veth /30
	Routes   []netip.Prefix
}

func (s Spec) String() string {
	routes := make([]string, len(s.Routes))
	for i, r := range s.Routes {
		routes[i] = r.String()
	}
	return fmt.Sprintf("%s vlan=%d addr=%s host=%s ns=%s routes=%s",
		names.Namespace(s.TenantID), s.VLAN, s.Address, s.HostAddr, s.NSAddr, strings.Join(routes, ","))
}

type Manager interface {
	// Check reports host problems that stop tenant networking from working.
	Check(ctx context.Context) error
	// Tenants lists tenants that have a runner namespace.
	Tenants(ctx context.Context) ([]int64, error)
	// Ensure makes the tenant's namespace match spec; changed is true if anything was modified.
	Ensure(ctx context.Context, spec Spec) (changed bool, err error)
	// Remove deletes the tenant's namespace, VLAN interface and veth.
	Remove(ctx context.Context, tenant int64) error
}

// IPRoute implements Manager with the iproute2 `ip` command.
type IPRoute struct {
	Cmd         execx.Commander
	ParentIface string
	ProcRoot    string // defaults to /proc
}

type ipLink struct {
	IfName   string   `json:"ifname"`
	Flags    []string `json:"flags"`
	LinkInfo *struct {
		InfoKind string `json:"info_kind"`
		InfoData *struct {
			ID int `json:"id"`
		} `json:"info_data"`
	} `json:"linkinfo"`
	AddrInfo []struct {
		Family    string `json:"family"`
		Local     string `json:"local"`
		PrefixLen int    `json:"prefixlen"`
	} `json:"addr_info"`
}

func (l ipLink) up() bool {
	for _, f := range l.Flags {
		if f == "UP" {
			return true
		}
	}
	return false
}

func (l ipLink) kind() string {
	if l.LinkInfo == nil {
		return ""
	}
	return l.LinkInfo.InfoKind
}

func (l ipLink) vlanID() int {
	if l.LinkInfo == nil || l.LinkInfo.InfoData == nil {
		return 0
	}
	return l.LinkInfo.InfoData.ID
}

func (l ipLink) ipv4() []netip.Prefix {
	var out []netip.Prefix
	for _, a := range l.AddrInfo {
		if a.Family != "inet" {
			continue
		}
		addr, err := netip.ParseAddr(a.Local)
		if err != nil {
			continue
		}
		out = append(out, netip.PrefixFrom(addr, a.PrefixLen))
	}
	return out
}

type ipRoute struct {
	Dst      string `json:"dst"`
	Gateway  string `json:"gateway"`
	Dev      string `json:"dev"`
	Protocol string `json:"protocol"`
}

func nsArgs(ns string, args ...string) []string {
	if ns == "" {
		return args
	}
	return append([]string{"-n", ns}, args...)
}

func (m *IPRoute) ip(ctx context.Context, args ...string) ([]byte, error) {
	return m.Cmd.Run(ctx, nil, "ip", args...)
}

func (m *IPRoute) namespaces(ctx context.Context) (map[string]bool, error) {
	out, err := m.ip(ctx, "netns", "list")
	if err != nil {
		return nil, err
	}
	res := map[string]bool{}
	for _, line := range strings.Split(string(out), "\n") {
		if f := strings.Fields(line); len(f) > 0 {
			res[f[0]] = true
		}
	}
	return res, nil
}

func (m *IPRoute) links(ctx context.Context, ns string) (map[string]ipLink, error) {
	out, err := m.ip(ctx, nsArgs(ns, "-d", "-j", "addr", "show")...)
	if err != nil {
		return nil, err
	}
	var list []ipLink
	if len(strings.TrimSpace(string(out))) > 0 {
		if err := json.Unmarshal(out, &list); err != nil {
			return nil, fmt.Errorf("parse ip addr output: %w", err)
		}
	}
	res := make(map[string]ipLink, len(list))
	for _, l := range list {
		res[l.IfName] = l
	}
	return res, nil
}

func (m *IPRoute) Check(ctx context.Context) error {
	root := m.ProcRoot
	if root == "" {
		root = "/proc"
	}
	b, err := os.ReadFile(filepath.Join(root, "sys/net/ipv4/ip_forward"))
	if err != nil {
		return fmt.Errorf("read ip_forward: %w", err)
	}
	if strings.TrimSpace(string(b)) != "1" {
		return fmt.Errorf("net.ipv4.ip_forward is off; tenants cannot reach remote Ceph nodes")
	}
	host, err := m.links(ctx, "")
	if err != nil {
		return err
	}
	if _, ok := host[m.ParentIface]; !ok {
		return fmt.Errorf("parent interface %q not found", m.ParentIface)
	}
	return nil
}

func (m *IPRoute) Tenants(ctx context.Context) ([]int64, error) {
	all, err := m.namespaces(ctx)
	if err != nil {
		return nil, err
	}
	var out []int64
	for n := range all {
		if id, ok := names.ParseNamespace(n); ok {
			out = append(out, id)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (m *IPRoute) Ensure(ctx context.Context, s Spec) (bool, error) {
	ns := names.Namespace(s.TenantID)
	vlan := names.VLANIface(s.TenantID)
	hv := names.HostVeth(s.TenantID)
	nv := names.NSVeth(s.TenantID)
	changed := false
	run := func(args ...string) error {
		changed = true
		_, err := m.ip(ctx, args...)
		return err
	}

	all, err := m.namespaces(ctx)
	if err != nil {
		return changed, err
	}
	if !all[ns] {
		if err := run("netns", "add", ns); err != nil {
			return changed, err
		}
	}
	host, err := m.links(ctx, "")
	if err != nil {
		return changed, err
	}
	inner, err := m.links(ctx, ns)
	if err != nil {
		return changed, err
	}

	if lo, ok := inner["lo"]; ok && !lo.up() {
		if err := run("-n", ns, "link", "set", "lo", "up"); err != nil {
			return changed, err
		}
	}

	if l, ok := inner[vlan]; ok && (l.kind() != "vlan" || l.vlanID() != s.VLAN) {
		if err := run("-n", ns, "link", "del", vlan); err != nil {
			return changed, err
		}
		delete(inner, vlan)
	}
	if _, ok := inner[vlan]; !ok {
		if _, leftover := host[vlan]; leftover {
			if err := run("link", "del", vlan); err != nil {
				return changed, err
			}
		}
		if err := run("link", "add", "link", m.ParentIface, "name", vlan, "type", "vlan", "id", strconv.Itoa(s.VLAN)); err != nil {
			return changed, err
		}
		if err := run("link", "set", vlan, "netns", ns); err != nil {
			return changed, err
		}
		inner[vlan] = ipLink{IfName: vlan}
	}
	if err := m.syncLink(ns, vlan, inner[vlan], s.Address, run); err != nil {
		return changed, err
	}

	hl, hostOK := host[hv]
	nl, nsOK := inner[nv]
	if !hostOK || !nsOK || hl.kind() != "veth" || nl.kind() != "veth" {
		if hostOK {
			if err := run("link", "del", hv); err != nil {
				return changed, err
			}
		} else if nsOK {
			if err := run("-n", ns, "link", "del", nv); err != nil {
				return changed, err
			}
		}
		if err := run("link", "add", hv, "type", "veth", "peer", "name", nv, "netns", ns); err != nil {
			return changed, err
		}
		hl, nl = ipLink{IfName: hv}, ipLink{IfName: nv}
	}
	if err := m.syncLink("", hv, hl, s.HostAddr, run); err != nil {
		return changed, err
	}
	if err := m.syncLink(ns, nv, nl, s.NSAddr, run); err != nil {
		return changed, err
	}

	if err := m.syncRoutes(ctx, ns, nv, s, run); err != nil {
		return changed, err
	}
	return changed, nil
}

// syncLink leaves exactly one IPv4 address on dev and brings it up.
func (m *IPRoute) syncLink(ns, dev string, l ipLink, want netip.Prefix, run func(...string) error) error {
	have := false
	for _, a := range l.ipv4() {
		if a == want {
			have = true
			continue
		}
		if err := run(nsArgs(ns, "addr", "del", a.String(), "dev", dev)...); err != nil {
			return err
		}
	}
	if !have {
		if err := run(nsArgs(ns, "addr", "add", want.String(), "dev", dev)...); err != nil {
			return err
		}
	}
	if !l.up() {
		if err := run(nsArgs(ns, "link", "set", dev, "up")...); err != nil {
			return err
		}
	}
	return nil
}

func parseDst(dst string) (netip.Prefix, error) {
	if dst == "default" {
		return netip.MustParsePrefix("0.0.0.0/0"), nil
	}
	if !strings.Contains(dst, "/") {
		a, err := netip.ParseAddr(dst)
		if err != nil {
			return netip.Prefix{}, err
		}
		return netip.PrefixFrom(a, a.BitLen()), nil
	}
	return netip.ParsePrefix(dst)
}

// syncRoutes keeps kernel (connected) routes and exactly one route per Ceph
// network via the host end of the veth. Anything else, a default route
// included, is removed.
func (m *IPRoute) syncRoutes(ctx context.Context, ns, nv string, s Spec, run func(...string) error) error {
	out, err := m.ip(ctx, "-n", ns, "-j", "route", "show")
	if err != nil {
		return err
	}
	var routes []ipRoute
	if len(strings.TrimSpace(string(out))) > 0 {
		if err := json.Unmarshal(out, &routes); err != nil {
			return fmt.Errorf("parse ip route output: %w", err)
		}
	}
	want := map[netip.Prefix]bool{}
	for _, r := range s.Routes {
		want[r] = true
	}
	gw := s.HostAddr.Addr().String()
	have := map[netip.Prefix]bool{}
	for _, r := range routes {
		if r.Protocol == "kernel" {
			continue
		}
		dst, err := parseDst(r.Dst)
		if err != nil {
			return fmt.Errorf("route %q: %w", r.Dst, err)
		}
		if want[dst] && r.Gateway == gw && r.Dev == nv && !have[dst] {
			have[dst] = true
			continue
		}
		args := []string{"-n", ns, "route", "del", dst.String()}
		if r.Dev != "" {
			args = append(args, "dev", r.Dev)
		}
		if err := run(args...); err != nil {
			return err
		}
	}
	for _, r := range s.Routes {
		if have[r] {
			continue
		}
		if err := run("-n", ns, "route", "replace", r.String(), "via", gw, "dev", nv); err != nil {
			return err
		}
	}
	return nil
}

func (m *IPRoute) Remove(ctx context.Context, tenant int64) error {
	ns := names.Namespace(tenant)
	all, err := m.namespaces(ctx)
	if err != nil {
		return err
	}
	host, err := m.links(ctx, "")
	if err != nil {
		return err
	}
	if _, ok := host[names.HostVeth(tenant)]; ok {
		if _, err := m.ip(ctx, "link", "del", names.HostVeth(tenant)); err != nil {
			return err
		}
	}
	if all[ns] {
		if _, err := m.ip(ctx, "netns", "del", ns); err != nil {
			return err
		}
	}
	if _, ok := host[names.VLANIface(tenant)]; ok {
		if _, err := m.ip(ctx, "link", "del", names.VLANIface(tenant)); err != nil {
			return err
		}
	}
	return nil
}
