package netns

import (
	"context"
	"fmt"
	"net/netip"
	"sort"
	"strconv"
	"strings"

	"rackflow/storage_runner/internal/names"
)

// PresenceSpec is the desired namespace of a network presence: only the VLAN
// interface with the network's storage address (docs/private-network-dhcp.md
// 2.1). No veth, routes, firewall entry or container.
type PresenceSpec struct {
	NetworkID int64
	VLAN      int
	Address   netip.Prefix
	MAC       string
}

func (s PresenceSpec) String() string {
	return fmt.Sprintf("%s vlan=%d addr=%s mac=%s", names.Presence(s.NetworkID), s.VLAN, s.Address, s.MAC)
}

// PresenceMAC is the fixed MAC of a network presence: 02:52:4e followed by
// the network id as 24 bits.
func PresenceMAC(network int64) string {
	return fmt.Sprintf("02:52:4e:%02x:%02x:%02x", byte(network>>16), byte(network>>8), byte(network))
}

// PresenceManager manages network presence namespaces.
type PresenceManager interface {
	// Presences lists networks that have a presence namespace.
	Presences(ctx context.Context) ([]int64, error)
	EnsurePresence(ctx context.Context, spec PresenceSpec) (changed bool, err error)
	RemovePresence(ctx context.Context, network int64) error
	PresenceLinkDown(ctx context.Context, network int64) error
	AnnouncePresence(ctx context.Context, network int64, addr netip.Addr) error
}

func (m *IPRoute) Presences(ctx context.Context) ([]int64, error) {
	all, err := m.namespaces(ctx)
	if err != nil {
		return nil, err
	}
	var out []int64
	for n := range all {
		if id, ok := names.ParsePresence(n); ok {
			out = append(out, id)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (m *IPRoute) EnsurePresence(ctx context.Context, s PresenceSpec) (bool, error) {
	ns := names.Presence(s.NetworkID)
	vlan := names.PresenceIface(s.NetworkID)
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
		args := []string{"link", "add", "link", m.ParentIface, "name", vlan, "address", s.MAC, "type", "vlan", "id", strconv.Itoa(s.VLAN)}
		if err := run(args...); err != nil {
			return changed, err
		}
		if err := run("link", "set", vlan, "netns", ns); err != nil {
			return changed, err
		}
		inner[vlan] = ipLink{IfName: vlan, Address: s.MAC}
	}
	if !strings.EqualFold(inner[vlan].Address, s.MAC) {
		if err := run("-n", ns, "link", "set", vlan, "address", s.MAC); err != nil {
			return changed, err
		}
	}
	if err := m.syncLink(ns, vlan, inner[vlan], s.Address, run); err != nil {
		return changed, err
	}
	return changed, nil
}

func (m *IPRoute) RemovePresence(ctx context.Context, network int64) error {
	ns := names.Presence(network)
	all, err := m.namespaces(ctx)
	if err != nil {
		return err
	}
	if all[ns] {
		if _, err := m.ip(ctx, "netns", "del", ns); err != nil {
			return err
		}
	}
	host, err := m.links(ctx, "")
	if err != nil {
		return err
	}
	if _, ok := host[names.PresenceIface(network)]; ok {
		if _, err := m.ip(ctx, "link", "del", names.PresenceIface(network)); err != nil {
			return err
		}
	}
	return nil
}

func (m *IPRoute) PresenceLinkDown(ctx context.Context, network int64) error {
	_, err := m.ip(ctx, "-n", names.Presence(network), "link", "set", names.PresenceIface(network), "down")
	return err
}

func (m *IPRoute) AnnouncePresence(ctx context.Context, network int64, addr netip.Addr) error {
	_, err := m.Cmd.Run(ctx, nil, "ip", "netns", "exec", names.Presence(network),
		"arping", "-U", "-c", "3", "-I", names.PresenceIface(network), addr.String())
	return err
}
