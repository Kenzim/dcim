package reconcile

import (
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/dhcp"
	"rackflow/storage_runner/internal/lease"
)

// blockConn is a DHCP socket that never receives anything.
type blockConn struct {
	closed chan struct{}
	once   sync.Once
}

func (c *blockConn) ReadFrom([]byte) (int, net.Addr, error) {
	<-c.closed
	return 0, nil, net.ErrClosed
}
func (c *blockConn) WriteTo(b []byte, _ net.Addr) (int, error) { return len(b), nil }
func (c *blockConn) Close() error                              { c.once.Do(func() { close(c.closed) }); return nil }
func (c *blockConn) LocalAddr() net.Addr                       { return &net.UDPAddr{Port: 67} }
func (c *blockConn) SetDeadline(time.Time) error               { return nil }
func (c *blockConn) SetReadDeadline(time.Time) error           { return nil }
func (c *blockConn) SetWriteDeadline(time.Time) error          { return nil }

// newDHCPPool is a DHCP pool whose sockets are fakes; each open is recorded.
func (e *env) newDHCPPool() *dhcp.Pool {
	p := &dhcp.Pool{
		StateDir: e.dir,
		Open: func(ns, iface string) (net.PacketConn, error) {
			e.rec.Add("dhcp open %s %s", ns, iface)
			return &blockConn{closed: make(chan struct{})}, nil
		},
		Now:  func() time.Time { return e.now },
		Logf: e.t.Logf,
	}
	e.t.Cleanup(p.StopAll)
	return p
}

func dhcpBlock() *api.DHCP {
	return &api.DHCP{Enabled: true, LeaseSeconds: 3600,
		Reservations: []api.DHCPReservation{{MAC: "bc:24:11:00:00:01", IP: "10.0.0.1"}}}
}

func network(id int64, vlan int) api.Network {
	return api.Network{ID: id, VLAN: vlan, NetworkCIDR: "10.0.0.0/24", EndpointIP: "10.0.0.254", PrefixLen: 24,
		DHCP: dhcpBlock()}
}

func networkStatus(t *testing.T, st api.Status, id int64) api.NetworkStatus {
	t.Helper()
	for _, n := range st.Networks {
		if n.ID == id {
			return n
		}
	}
	t.Fatalf("network %d missing from status %+v", id, st.Networks)
	return api.NetworkStatus{}
}

const presence6 = "net presence ensure rf-p6 vlan=3921 addr=10.0.0.254/24 mac=02:52:4e:00:00:06"

func TestNetworkPresenceWithoutLeases(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v0"))
	cfg := cfgOf("v1")
	cfg.Networks = []api.Network{network(6, 3921)}
	expect(t, e.apply(cfg), []string{presence6, "dhcp open rf-p6 rf-w6"})
	if !e.r.DHCP.Running("net-6") {
		t.Fatal("dhcp not running for the presence")
	}
	ns := networkStatus(t, e.status(), 6)
	if !ns.Serving || ns.Since == nil || ns.Error != nil || ns.Leases == nil {
		t.Fatalf("network status %+v", ns)
	}

	// Nothing changes on the next pass; a DHCP change swaps the table only.
	expect(t, e.apply(cfg), nil)
	cfg.Networks[0].DHCP.Reservations[0].IP = "10.0.0.2"
	expect(t, e.apply(cfg), nil)
	if a, _ := e.r.DHCP.Server("net-6").Table().Reserved("bc:24:11:00:00:01"); a.String() != "10.0.0.2" {
		t.Fatalf("table not swapped: %s", a)
	}

	// DHCP off: the presence stays, the server stops.
	cfg.Networks[0].DHCP.Enabled = false
	expect(t, e.apply(cfg), nil)
	if e.r.DHCP.Running("net-6") || !e.net.HasPresence(6) {
		t.Fatal("dhcp off did not stop only the server")
	}

	// The network leaves the config: removed, reported once as not serving.
	e.apply(cfgOf("v2"))
	if e.net.HasPresence(6) {
		t.Fatal("presence left behind")
	}
	if ns := networkStatus(t, e.status(), 6); ns.Serving || ns.Since != nil || ns.Leases == nil {
		t.Fatalf("stop status %+v", ns)
	}
	e.apply(cfgOf("v2"))
	if n := e.status().Networks; len(n) != 0 {
		t.Fatalf("serving false reported twice: %+v", n)
	}
}

func TestOrphanPresenceIsRemoved(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v0"))
	e.net.SeedPresence(9)
	expect(t, e.apply(cfgOf("v1")), []string{"net presence remove rf-p9"})
}

func TestPresenceAdoptedAfterRestart(t *testing.T) {
	e := newEnv(t)
	cfg := cfgOf("v1")
	cfg.Networks = []api.Network{network(6, 3921)}
	e.apply(cfg)
	e.restart()
	got := e.apply(cfg)
	if contains(got, "net presence remove") || !contains(got, "dhcp open rf-p6 rf-w6") {
		t.Fatalf("restart actions:\n  %s", strings.Join(got, "\n  "))
	}
}

func TestPresenceNeverTakesATenantVLAN(t *testing.T) {
	e := newEnv(t)
	cfg := cfgOf("v1", basic())
	cfg.Networks = []api.Network{network(6, 1234)}
	got := e.apply(cfg)
	if contains(got, "net presence ensure") || contains(got, "dhcp open rf-p6") {
		t.Fatalf("presence created on a tenant VLAN:\n  %s", strings.Join(got, "\n  "))
	}
	ns := networkStatus(t, e.status(), 6)
	if ns.Serving || ns.Error == nil || !strings.Contains(*ns.Error, "VLAN 1234 is used by tenant 12") {
		t.Fatalf("network status %+v", ns)
	}

	// A terminated tenant whose namespace is gone frees the VLAN; while
	// its namespace is still here it does not.
	e2 := newEnv(t)
	e2.net.Seed(12)
	term := basic()
	term.State = api.TenantTerminated
	cfg = cfgOf("v1", term)
	cfg.Networks = []api.Network{network(6, 1234)}
	if got := e2.apply(cfg); contains(got, "net presence ensure") {
		t.Fatalf("presence created while the tenant namespace is here:\n  %s", strings.Join(got, "\n  "))
	}

	// A presence already on the VLAN is removed before the tenant is set
	// up on it.
	e3 := newEnv(t)
	cfg = cfgOf("v1")
	cfg.Networks = []api.Network{network(6, 1234)}
	e3.apply(cfg)
	got = e3.apply(cfgOf("v2", basic()))
	if len(got) < 2 || got[0] != "net presence remove rf-p6" || !strings.HasPrefix(got[1], "ceph") && !strings.HasPrefix(got[1], "net ensure") {
		t.Fatalf("handover actions:\n  %s", strings.Join(got, "\n  "))
	}
}

func TestInvalidNetworkIsReported(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v0"))
	cfg := cfgOf("v1")
	bad := network(6, 3921)
	bad.EndpointIP = "10.0.1.254"
	cfg.Networks = []api.Network{bad}
	if got := e.apply(cfg); len(got) != 0 {
		t.Fatalf("actions for an invalid network:\n  %s", strings.Join(got, "\n  "))
	}
	ns := networkStatus(t, e.status(), 6)
	if ns.Serving || ns.Error == nil || !strings.Contains(*ns.Error, "endpoint_ip") {
		t.Fatalf("status %+v", ns)
	}
}

func dhcpTenant() api.Tenant {
	t := basic()
	id := int64(6)
	t.NetworkID = &id
	t.DHCP = dhcpBlock()
	return t
}

func TestTenantServesDHCPWithoutTouchingGanesha(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	conf := e.ganeshaConf("t12")

	// The tenant becomes its network's presence: only DHCP starts.
	expect(t, e.apply(cfgOf("v2", dhcpTenant())), []string{"dhcp open rf-t12 rf-v12"})
	srv := e.r.DHCP.Server("tenant-12")
	if srv == nil || srv.Table().ServerID.String() != "10.0.0.254" {
		t.Fatalf("tenant dhcp server %+v", srv)
	}
	ts := tenantStatus(t, e.status(), 12)
	if ts.DHCP == nil || ts.DHCP.Error != nil || ts.DHCP.Leases == nil {
		t.Fatalf("tenant dhcp status %+v", ts.DHCP)
	}

	// Changing the DHCP block swaps the table and changes nothing else.
	tn := dhcpTenant()
	tn.DHCP.Reservations = nil
	expect(t, e.apply(cfgOf("v3", tn)), nil)
	if _, ok := e.r.DHCP.Server("tenant-12").Table().Reserved("bc:24:11:00:00:01"); ok {
		t.Fatal("reservation still in the table")
	}
	if e.ganeshaConf("t12") != conf {
		t.Fatal("dhcp changed ganesha.conf")
	}

	// Back to a plain tenant: the server stops, nfs is untouched.
	expect(t, e.apply(cfgOf("v4", basic())), nil)
	if e.r.DHCP.Running("tenant-12") || tenantStatus(t, e.status(), 12).DHCP != nil {
		t.Fatal("dhcp still running for a tenant without network_id")
	}
}

func TestOldConfigWithoutNetworksBehavesAsBefore(t *testing.T) {
	e := newEnv(t)
	raw := `{"version":"v1","node":{"id":3,"name":"store-01"},"settings":{"destroy_grace_seconds":604800},` +
		`"tenants":[{"id":12,"state":"active","vlan":1234,"network_cidr":"10.0.0.0/24","endpoint_ip":"10.0.0.254",` +
		`"prefix_len":24,"size_bytes":536870912000,"shares":[{"id":5,"name":"data","quota_bytes":107374182400,` +
		`"clients":["10.0.0.5"],"state":"active"}]}]}`
	e.src.mu.Lock()
	e.src.cfg = []byte(raw)
	e.src.mu.Unlock()
	if err := e.r.Once(t.Context()); err != nil {
		t.Fatal(err)
	}
	expect(t, e.rec.Take(), firstApply())
	b, err := json.Marshal(e.status())
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{`"networks"`, `"dhcp"`, `"network_id"`} {
		if strings.Contains(string(b), key) {
			t.Fatalf("old-format status carries %s: %s", key, b)
		}
	}
	if len(e.r.DHCP.Keys()) != 0 {
		t.Fatal("dhcp started for an old config")
	}
}

// HA: the presence is served under net.<id> by one node at a time.

func haNetCfg(n *haNode, version string, order []int64, nets []api.Network, tenants ...api.Tenant) *api.Config {
	c := haCfgFor(n, version, []int64{1, 2}, tenants...)
	for i := range nets {
		nets[i].TakeoverOrder = order
		nets[i].PreferredNodeID = order[0]
	}
	c.Networks = nets
	return c
}

func (n *haNode) presenceUp(id int64) bool {
	return n.net.HasPresence(id) && !n.net.PresenceDown(id) && n.r.DHCP.Running(fmt.Sprintf("net-%d", id))
}

func TestHAPresenceFailoverWithoutFencing(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	order := []int64{1, 2}
	for _, n := range c.nodes {
		n.src.set(haNetCfg(n, "v1", order, []api.Network{network(6, 3921)}))
		n.once()
	}
	c.run(10 * time.Second)
	if !n1.presenceUp(6) || n2.net.HasPresence(6) {
		t.Fatalf("after start: n1 up=%v n2 has=%v", n1.presenceUp(6), n2.net.HasPresence(6))
	}
	st := n1.status()
	if ns := networkStatus(t, st, 6); !ns.Serving || ns.Since == nil {
		t.Fatalf("n1 network status %+v", ns)
	}
	if len(n2.status().Networks) != 0 {
		t.Fatalf("n2 reports a network it does not serve: %+v", n2.status().Networks)
	}

	n1.client.SetPartition(lease.PartitionFail)
	both := false
	ok := c.until(2*time.Minute, func() bool {
		if n1.presenceUp(6) && n2.presenceUp(6) {
			both = true
		}
		return n2.presenceUp(6)
	})
	if !ok {
		t.Fatal("node 2 did not take the presence over")
	}
	if both {
		t.Fatal("both nodes served the presence at once")
	}
	if n1.presenceUp(6) || n1.r.DHCP.Running("net-6") {
		t.Fatal("node 1 still serves after losing the lease")
	}
	if bl := c.pool.Blocklist(); len(bl) != 0 {
		t.Fatalf("a presence takeover blocklisted %+v", bl)
	}
	if ca := c.cephRec.Take(); contains(ca, "evict") || contains(ca, "blocklist") {
		t.Fatalf("a presence takeover fenced:\n  %s", strings.Join(ca, "\n  "))
	}
	var evs []api.Event
	for _, st := range n2.src.posted {
		evs = append(evs, st.Events...)
	}
	if len(evs) != 1 || evs[0].Kind != api.EventTakeover || evs[0].NetworkID == nil || *evs[0].NetworkID != 6 || evs[0].TenantID != nil || evs[0].Fence != nil {
		t.Fatalf("node 2 events %+v", evs)
	}

	// Node 1 reported serving false once after it stopped.
	var stops int
	for _, st := range n1.src.posted {
		for _, ns := range st.Networks {
			if ns.ID == 6 && !ns.Serving {
				stops++
			}
		}
	}
	if stops != 1 {
		t.Fatalf("node 1 sent serving false %d times", stops)
	}
}

func TestHATenantWaitsForPresenceRelease(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	// Node 1 serves network 6's presence; node 2 is first for tenant 12,
	// which becomes the network's storage.
	n1.src.set(haNetCfg(n1, "v1", []int64{1}, []api.Network{network(6, 3921)}))
	n2.src.set(haCfgFor(n2, "v1", []int64{2}))
	n1.once()
	n2.once()
	c.run(10 * time.Second)
	if !n1.presenceUp(6) {
		t.Fatal("node 1 not serving the presence")
	}

	tn := haTenant(n2)
	tn.VLAN = 3921
	id := int64(6)
	tn.NetworkID, tn.DHCP = &id, dhcpBlock()
	cfg2 := haCfgFor(n2, "v2", []int64{2}, tn)
	n2.src.set(cfg2)
	c.run(time.Minute)
	if n2.serving(12) || n2.r.Lease.Holds(lease.TenantKey(12)) {
		t.Fatal("tenant acquired while the network presence is held")
	}

	// Rackflow drops the presence from node 1; node 1 releases it and the
	// tenant follows on node 2 with its DHCP server.
	n1.src.set(haCfgFor(n1, "v2", []int64{2}))
	if !c.until(time.Minute, func() bool { return n2.serving(12) }) {
		t.Fatal("tenant not served after the presence was released")
	}
	if n1.net.HasPresence(6) || n1.r.DHCP.Running("net-6") {
		t.Fatal("node 1 kept the presence")
	}
	if !n2.r.DHCP.Running("tenant-12") {
		t.Fatal("tenant dhcp not running")
	}
	if ts := tenantStatus(t, n2.status(), 12); ts.DHCP == nil {
		t.Fatalf("tenant status lacks dhcp: %+v", ts)
	}
	if bl := c.pool.Blocklist(); len(bl) != 0 {
		t.Fatalf("blocklisted %+v", bl)
	}
}

func TestHADrainingReleasesPresence(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	order := []int64{1, 2}
	for _, n := range c.nodes {
		n.src.set(haNetCfg(n, "v1", order, []api.Network{network(6, 3921)}))
		n.once()
	}
	c.run(10 * time.Second)
	if !n1.presenceUp(6) {
		t.Fatal("node 1 not serving")
	}
	drain := haNetCfg(n1, "v2", order, []api.Network{network(6, 3921)})
	drain.HA.Draining = true
	n1.src.set(drain)
	if !c.until(time.Minute, func() bool { return n2.presenceUp(6) }) {
		t.Fatal("presence not handed off on drain")
	}
	if n1.presenceUp(6) {
		t.Fatal("draining node still serves the presence")
	}
}
