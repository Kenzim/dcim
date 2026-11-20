package reconcile

import (
	"context"
	"fmt"
	"net/netip"
	"sort"
	"strings"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/dhcp"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
	"rackflow/storage_runner/internal/netns"
)

// maxNetworkID keeps the presence MAC (24 bits of the id) unique.
const maxNetworkID = 1<<24 - 1

// netWork is one network presence of the config in this pass
// (docs/private-network-dhcp.md 2).
type netWork struct {
	n        api.Network
	valid    bool
	errs     []string
	endpoint netip.Prefix
	// conflict is set when the network's VLAN is used by a tenant in the
	// config or by a tenant namespace on this node: the presence is never
	// created then.
	conflict bool
	// held is set when this node may serve the presence: it holds
	// net.<id>, or the config has no ha block.
	held bool
	// served is set when the presence was ensured in this pass.
	served bool
}

func (w *netWork) fail(format string, args ...any) {
	w.errs = append(w.errs, fmt.Sprintf(format, args...))
}

func presenceGuard(id int64) lease.GuardInfo {
	return lease.GuardInfo{Netns: names.Presence(id), Link: names.PresenceIface(id)}
}

// validateNetworks checks the config's networks and marks those whose VLAN
// is taken by a tenant.
func (p *pass) validateNetworks(cfg *api.Config, works []*tenantWork) []*netWork {
	sorted := append([]api.Network(nil), cfg.Networks...)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })
	idCount := map[int64]int{}
	vlanCount := map[int]int{}
	for _, n := range sorted {
		idCount[n.ID]++
		vlanCount[n.VLAN]++
	}
	// VLANs of tenants that are, or may still be, on the node's parent.
	tenantVLAN := map[int]int64{}
	for _, w := range works {
		if w.t.VLAN <= 0 {
			continue
		}
		if w.t.State != api.TenantTerminated || p.namespaces[w.t.ID] {
			tenantVLAN[w.t.VLAN] = w.t.ID
		}
	}
	var nets []*netWork
	for _, n := range sorted {
		nw := &netWork{n: n, valid: true}
		switch {
		case idCount[n.ID] > 1:
			nw.valid = false
			nw.fail("invalid config: network id %d appears more than once", n.ID)
		case vlanCount[n.VLAN] > 1:
			nw.valid = false
			nw.fail("invalid config: VLAN %d is used by more than one network", n.VLAN)
		default:
			if err := validateNetwork(nw); err != nil {
				nw.valid = false
				nw.fail("invalid config: %v", err)
			}
		}
		if t, ok := tenantVLAN[n.VLAN]; ok && nw.valid {
			nw.conflict = true
			nw.fail("VLAN %d is used by tenant %d on this node or in the config; the presence is not created", n.VLAN, t)
		}
		nets = append(nets, nw)
	}
	return nets
}

func validateNetwork(nw *netWork) error {
	n := nw.n
	if n.ID <= 0 || n.ID > maxNetworkID {
		return fmt.Errorf("network id %d out of range", n.ID)
	}
	if n.VLAN < 1 || n.VLAN > 4094 {
		return fmt.Errorf("vlan %d out of range", n.VLAN)
	}
	network, err := netip.ParsePrefix(n.NetworkCIDR)
	if err != nil || !network.Addr().Is4() || network.Masked() != network || network.Bits() > 30 {
		return fmt.Errorf("network_cidr %q is not a usable IPv4 network", n.NetworkCIDR)
	}
	if n.PrefixLen != network.Bits() {
		return fmt.Errorf("prefix_len %d does not match network_cidr %s", n.PrefixLen, network)
	}
	ep, err := netip.ParseAddr(n.EndpointIP)
	if err != nil || !ep.Is4() || !network.Contains(ep) || ep == network.Addr() || ep == lastAddr(network) {
		return fmt.Errorf("endpoint_ip %q is not a host address in %s", n.EndpointIP, network)
	}
	nw.endpoint = netip.PrefixFrom(ep, network.Bits())
	return nil
}

// netWants are the lease wants of the network presences.
func netWants(cfg *api.Config, nets []*netWork) []lease.Want {
	me := cfg.Node.ID
	var out []lease.Want
	for _, nw := range nets {
		want := lease.Want{Key: lease.NetKey(nw.n.ID), Rank: -1, Guard: presenceGuard(nw.n.ID)}
		switch {
		case !nw.valid || nw.conflict:
			// Nothing to serve here; another node may.
			want.Release = true
		case cfg.HA.Enabled:
			want.Rank = indexOf(nw.n.TakeoverOrder, me)
			want.Acquire = want.Rank >= 0
		default:
			want.Acquire, want.Rank = true, 0
		}
		out = append(out, want)
	}
	return out
}

// tenantDefer holds a tenant's lease back while its network's presence has
// a locker (docs/private-network-dhcp.md 2.3).
func tenantDefer(t api.Tenant) []lease.Key {
	if t.NetworkID == nil || *t.NetworkID <= 0 {
		return nil
	}
	return []lease.Key{lease.NetKey(*t.NetworkID)}
}

// dropPresences removes presence namespaces this node must not have: not in
// the config, not held, invalid or in conflict with a tenant. It runs before
// tenants are applied, so a tenant taking over a network's VLAN finds it
// free.
func (p *pass) dropPresences(nets []*netWork) {
	r := p.r
	keep := map[int64]bool{}
	for _, nw := range nets {
		if nw.valid && nw.held && !nw.conflict {
			keep[nw.n.ID] = true
		}
	}
	for _, id := range sortedIDs(p.presences) {
		if keep[id] {
			continue
		}
		p.stopDHCP(names.NetworkDHCPKey(id))
		if err := r.Net.RemovePresence(p.ctx, id); err != nil {
			p.nodeErr("remove presence of network %d: %v", id, err)
			r.logf("network %d: remove presence: %v", id, err)
			continue
		}
		delete(p.presences, id)
		r.logf("network %d: presence removed", id)
	}
	for id := range r.presenceSince {
		if !keep[id] {
			delete(r.presenceSince, id)
		}
	}
}

func sortedIDs(m map[int64]bool) []int64 {
	out := make([]int64, 0, len(m))
	for id := range m {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// applyNetworks ensures the presences this node serves and their DHCP
// servers, after tenants are applied.
func (p *pass) applyNetworks(nets []*netWork) {
	r := p.r
	for _, nw := range nets {
		if !nw.valid || !nw.held || nw.conflict {
			continue
		}
		id := nw.n.ID
		// A tenant namespace still here on this VLAN (one this pass could
		// not remove) blocks the presence.
		if t := p.tenantOnVLAN(nw.n.VLAN); t != 0 {
			nw.conflict = true
			nw.fail("VLAN %d is used by tenant %d on this node; the presence is not created", nw.n.VLAN, t)
			continue
		}
		spec := netns.PresenceSpec{NetworkID: id, VLAN: nw.n.VLAN, Address: nw.endpoint, MAC: netns.PresenceMAC(id)}
		var changed bool
		var dhcpErr error
		err := p.whileHolding(lease.NetKey(id), func(ctx context.Context) error {
			var err error
			changed, err = r.Net.EnsurePresence(ctx, spec)
			if err != nil {
				return err
			}
			p.presences[id] = true
			p.announcePresence(ctx, nw)
			dhcpErr = p.ensureDHCP(dhcp.Spec{
				Key: names.NetworkDHCPKey(id), Netns: names.Presence(id), Iface: names.PresenceIface(id),
				ServerID: nw.endpoint.Addr(), PrefixLen: nw.endpoint.Bits(), Config: nw.n.DHCP,
			})
			return nil
		})
		if changed {
			r.logf("network %d: presence updated (%s)", id, spec)
		}
		if err != nil {
			nw.fail("presence: %v", err)
			continue
		}
		if dhcpErr != nil {
			nw.fail("dhcp: %v", dhcpErr)
		}
		nw.served = true
		if r.presenceSince == nil {
			r.presenceSince = map[int64]time.Time{}
		}
		if _, ok := r.presenceSince[id]; !ok {
			r.presenceSince[id] = p.now
		}
	}
}

func (p *pass) tenantOnVLAN(vlan int) int64 {
	if p.cfg == nil {
		return 0
	}
	for _, t := range p.cfg.Tenants {
		if t.VLAN == vlan && p.namespaces[t.ID] {
			return t.ID
		}
	}
	return 0
}

// announcePresence sends gratuitous ARP for the presence once per lease
// acquisition, like a tenant's.
func (p *pass) announcePresence(ctx context.Context, nw *netWork) {
	if p.ha == nil {
		return
	}
	r := p.r
	id := nw.n.ID
	since := r.Lease.Since(lease.NetKey(id))
	if r.announcedNets == nil {
		r.announcedNets = map[int64]time.Time{}
	}
	if cur, ok := r.announcedNets[id]; ok && cur.Equal(since) {
		return
	}
	if err := r.Net.AnnouncePresence(ctx, id, nw.endpoint.Addr()); err != nil {
		r.logf("network %d: gratuitous arp: %v", id, err)
		return
	}
	r.announcedNets[id] = since
}

// ensureDHCP starts or updates a DHCP server and records it as wanted in
// this pass; servers not wanted by the end of the pass are stopped.
func (p *pass) ensureDHCP(spec dhcp.Spec) error {
	if p.r.DHCP == nil {
		return nil
	}
	if p.dhcpWanted == nil {
		p.dhcpWanted = map[string]bool{}
	}
	p.dhcpWanted[spec.Key] = true
	return p.r.DHCP.Ensure(spec)
}

func (p *pass) stopDHCP(key string) {
	if p.r.DHCP != nil {
		p.r.DHCP.Stop(key)
	}
}

// keepDHCP leaves a running server as it is in this pass.
func (p *pass) keepDHCP(key string) {
	if p.dhcpWanted == nil {
		p.dhcpWanted = map[string]bool{}
	}
	p.dhcpWanted[key] = true
}

// ensureTenantDHCP runs the DHCP server of a tenant that is its network's
// presence, from the tenant's namespace with the tenant's address as server
// id. It never touches Ganesha.
func (p *pass) ensureTenantDHCP(ctx context.Context, w *tenantWork) {
	if w.t.NetworkID == nil {
		return
	}
	id := w.t.ID
	if err := p.ensureDHCP(dhcp.Spec{
		Key: names.TenantDHCPKey(id), Netns: names.Namespace(id), Iface: names.VLANIface(id),
		ServerID: w.endpoint.Addr(), PrefixLen: w.endpoint.Bits(), Config: w.t.DHCP,
	}); err != nil {
		p.r.logf("tenant %d: dhcp: %v", id, err)
	}
}

// stopUnwantedDHCP stops servers no presence or tenant asked for in this
// pass.
func (p *pass) stopUnwantedDHCP() {
	if p.r.DHCP == nil {
		return
	}
	for _, k := range p.r.DHCP.Keys() {
		if !p.dhcpWanted[k] {
			p.r.DHCP.Stop(k)
		}
	}
}

func dhcpStatus(pool *dhcp.Pool, key string) (*string, []api.DHCPLease) {
	if pool == nil {
		return nil, []api.DHCPLease{}
	}
	return pool.Status(key)
}

// fillNetworkStatus reports the presences served in this pass, and once
// with serving false those served in the previous pass and no longer.
func (p *pass) fillNetworkStatus(st *api.Status, nets []*netWork) {
	r := p.r
	served := map[int64]bool{}
	reported := map[int64]bool{}
	byID := map[int64]*netWork{}
	for _, nw := range nets {
		byID[nw.n.ID] = nw
		if !nw.served {
			continue
		}
		id := nw.n.ID
		served[id] = true
		ns := api.NetworkStatus{ID: id, Serving: true}
		since := r.presenceSince[id]
		if p.ha != nil {
			since = r.Lease.Since(lease.NetKey(id))
		}
		if !since.IsZero() {
			s := since.UTC()
			ns.Since = &s
		}
		msgs := append([]string(nil), nw.errs...)
		derr, leases := dhcpStatus(r.DHCP, names.NetworkDHCPKey(id))
		if derr != nil {
			msgs = append(msgs, "dhcp: "+*derr)
		}
		if len(msgs) > 0 {
			ns.Error = strPtr(strings.Join(msgs, "; "))
		}
		ns.Leases = leases
		st.Networks = append(st.Networks, ns)
	}
	for _, id := range sortedIDs(r.servedNets) {
		if served[id] {
			continue
		}
		ns := api.NetworkStatus{ID: id, Serving: false, Leases: []api.DHCPLease{}}
		if nw := byID[id]; nw != nil && len(nw.errs) > 0 {
			ns.Error = strPtr(strings.Join(nw.errs, "; "))
		}
		st.Networks = append(st.Networks, ns)
		reported[id] = true
	}
	// A network this node should serve but cannot (invalid, or its VLAN is
	// a tenant's) is reported not serving, with the reason.
	for _, nw := range nets {
		id := nw.n.ID
		if served[id] || reported[id] || len(nw.errs) == 0 || !p.mayServeNetwork(nw) {
			continue
		}
		st.Networks = append(st.Networks, api.NetworkStatus{ID: id, Serving: false,
			Error: strPtr(strings.Join(nw.errs, "; ")), Leases: []api.DHCPLease{}})
		reported[id] = true
	}
	p.servedNets = served
}

// mayServeNetwork says whether this node is one that could serve nw: any
// node without leases, else one in its takeover order.
func (p *pass) mayServeNetwork(nw *netWork) bool {
	if p.ha == nil || !p.ha.cfg.Enabled {
		return true
	}
	return indexOf(nw.n.TakeoverOrder, p.ha.node.ID) >= 0
}

// fillTenantDHCP adds the dhcp block to served tenants with a network_id.
func (p *pass) fillTenantDHCP(st *api.Status, works []*tenantWork) {
	byID := map[int64]*tenantWork{}
	for _, w := range works {
		byID[w.t.ID] = w
	}
	for i := range st.Tenants {
		ts := &st.Tenants[i]
		w := byID[ts.ID]
		if w == nil || w.t.NetworkID == nil || !w.held || !w.valid || w.t.State == api.TenantTerminated {
			continue
		}
		derr, leases := dhcpStatus(p.r.DHCP, names.TenantDHCPKey(w.t.ID))
		ts.DHCP = &api.TenantDHCP{Error: derr, Leases: leases}
	}
}
