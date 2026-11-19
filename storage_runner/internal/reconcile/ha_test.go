package reconcile

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/record"
	"rackflow/storage_runner/internal/vip"
)

// haCluster is a location of runners sharing one Ceph cluster: one lease
// pool, one CephFS (with its own recorder) and one OSD clock.
type haCluster struct {
	t       *testing.T
	osd     *lease.FakeClock
	pool    *lease.FakeCluster
	ceph    *ceph.Fake
	cephRec *record.Recorder
	nodes   []*haNode
	elapsed time.Duration
}

type haNode struct {
	*sftpEnv
	id     int64
	clock  *lease.FakeClock
	client *lease.FakeClient
	vip    *vip.Fake
	paused bool
}

func newHACluster(t *testing.T) *haCluster {
	c := &haCluster{t: t, osd: lease.NewFakeClock(t0), cephRec: &record.Recorder{}}
	c.pool = lease.NewFakeCluster(c.osd.Now)
	c.ceph = ceph.NewFake(c.cephRec, "rackflow", "ec")
	return c
}

func (c *haCluster) add(id int64) *haNode {
	n := &haNode{sftpEnv: newSFTPEnv(c.t), id: id, clock: lease.NewFakeClock(c.osd.Now())}
	n.ceph = c.ceph
	n.client = c.pool.NewClient(fmt.Sprintf("10.20.0.%d", id))
	n.vip = vip.NewFake(n.rec)
	n.attach()
	c.nodes = append(c.nodes, n)
	return n
}

// attach gives the node's (new) Reconciler its HA parts.
func (n *haNode) attach() {
	n.r.Ceph = n.ceph
	n.r.Host.CephHAPool = "rackflow_ha"
	n.r.Hostname = fmt.Sprintf("stor-%02d", n.id)
	n.r.VIP = n.vip
	n.r.Lease = &lease.Manager{
		Store: n.client, Fencer: n.ceph, Clock: n.clock, Version: "0.3.0",
		DeadlineDir: filepath.Join(n.dir, "leases"), CallTimeout: 50 * time.Millisecond,
		Logf: n.t.Logf,
	}
}

// restart is a new runner process: a new Reconciler, lease manager and
// lease client instance over the same host.
func (n *haNode) restart() {
	n.env.restart()
	n.r.Host.SFTPImage = "sftp:test"
	n.r.Host.SFTPListenAddr = "127.0.0.1"
	n.r.SFTP = n.gw
	n.r.SFTPMounts = n.mounts
	n.r.SFTPRuntime = n.rt
	n.r.SFTPMountRoot = "/run/rf-test"
	n.client.Reset()
	n.attach()
}

func (n *haNode) once() []string {
	n.t.Helper()
	if err := n.r.Once(context.Background()); err != nil {
		n.t.Fatalf("node %d Once: %v", n.id, err)
	}
	return n.rec.Take()
}

func (n *haNode) applyCfg(cfg *api.Config) []string {
	n.src.set(cfg)
	return n.once()
}

// step advances every clock by 500 ms and runs each running node's lease
// loops on their schedule, then a reconcile pass every 2 s.
func (c *haCluster) step() {
	c.elapsed += 500 * time.Millisecond
	c.osd.Advance(500 * time.Millisecond)
	ms := c.elapsed.Milliseconds()
	for _, n := range c.nodes {
		n.clock.Advance(500 * time.Millisecond)
		if n.paused {
			continue
		}
		m := n.r.Lease
		m.CheckDeadlines()
		if ms%4000 == 0 {
			m.RenewTick(context.Background())
		}
		if ms%2000 == 0 {
			m.PollTick(context.Background())
			n.once()
		}
	}
}

func (c *haCluster) run(d time.Duration) {
	for end := c.elapsed + d; c.elapsed < end; {
		c.step()
	}
}

func (c *haCluster) until(limit time.Duration, cond func() bool) bool {
	for start := c.elapsed; c.elapsed-start < limit; c.step() {
		if cond() {
			return true
		}
	}
	return cond()
}

func (n *haNode) serving(id int64) bool {
	return n.nfs.Running(fmt.Sprintf("rf-t%d-nfs", id)) && !n.net.Down(id)
}

func haCfgFor(n *haNode, version string, order []int64, tenants ...api.Tenant) *api.Config {
	c := n.cfg(version, tenants...)
	c.Node = api.Node{ID: n.id, Name: fmt.Sprintf("stor-%02d", n.id), LocationID: 1}
	c.HA = &api.HA{Enabled: true, Nodes: []api.HANode{{ID: 1, Name: "stor-01"}, {ID: 2, Name: "stor-02"}}}
	for i := range c.Tenants {
		c.Tenants[i].TakeoverOrder = order
		c.Tenants[i].PreferredNodeID = order[0]
	}
	return c
}

func haTenant(n *haNode) api.Tenant { return sftpTenant(n.sftpEnv, 12, 1234, "rw") }

func contains(lines []string, sub string) bool {
	for _, l := range lines {
		if strings.Contains(l, sub) {
			return true
		}
	}
	return false
}

func TestHAFirstNodeTakesLeaseAndServes(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	order := []int64{1, 2}

	got := n1.applyCfg(haCfgFor(n1, "v1", order, haTenant(n1)))
	for _, want := range []string{
		"net ensure rf-t12 vlan=1234 addr=10.0.0.254/24 host=169.254.128.1/30 ns=169.254.128.2/30 routes=10.10.0.0/24 mac=02:52:46:00:00:0c",
		"net arping rf-t12 10.0.0.254",
		"nfs run rf-t12-nfs netns=/run/netns/rf-t12 image=ganesha:test hostname=rf-t12-nfs",
	} {
		if !contains(got, want) {
			t.Errorf("node 1 actions lack %q:\n  %s", want, strings.Join(got, "\n  "))
		}
	}
	cephActions := c.cephRec.Take()
	if !contains(cephActions, "ceph evict client.rf-t12 hostname=rf-t12-nfs") {
		t.Errorf("pre-start eviction not filtered by hostname:\n  %s", strings.Join(cephActions, "\n  "))
	}
	if !contains(cephActions, `osd="allow rw pool=ec namespace=fsvolumens_t12-s125, allow rw pool=rackflow_ha namespace=rf-t12"`) {
		t.Errorf("tenant user lacks recovery caps:\n  %s", strings.Join(cephActions, "\n  "))
	}
	conf := n1.ganeshaConf("t12")
	for _, want := range []string{
		"RecoveryBackend = rados_ng;", "Lease_Lifetime = 30;", "Grace_Period = 45;",
		`userid = "rf-t12";`, `pool = "rackflow_ha";`, `namespace = "rf-t12";`, `nodeid = "rf-t12";`,
	} {
		if !strings.Contains(conf, want) {
			t.Errorf("ganesha.conf lacks %q:\n%s", want, conf)
		}
	}
	st := n1.status()
	if st.HA == nil || !st.HA.LeaseOK || !st.HA.HAActive || st.HA.Fenced {
		t.Fatalf("ha status %+v", st.HA)
	}
	ts := tenantStatus(t, st, 12)
	if ts.Serving == nil || !*ts.Serving || ts.Since == nil {
		t.Fatalf("tenant status %+v", ts)
	}
	if len(st.Events) != 1 || st.Events[0].Kind != api.EventTakeover || *st.Events[0].TenantID != 12 || st.Events[0].Fence != nil {
		t.Fatalf("events %+v", st.Events)
	}
	if _, err := os.Stat(filepath.Join(n1.dir, "leases", "tenant.12.json")); err != nil {
		t.Fatalf("deadline file: %v", err)
	}

	// The events were delivered with that post; the next one has none.
	n1.once()
	if evs := n1.status().Events; len(evs) != 0 {
		t.Fatalf("events sent twice: %+v", evs)
	}

	// Node 2 is second in the order: it serves nothing, changes no Ceph
	// state and does not report the tenant.
	got = n2.applyCfg(haCfgFor(n2, "v1", order, haTenant(n2)))
	if contains(got, "net ensure") || contains(got, "nfs run") {
		t.Fatalf("node 2 serves without the lease:\n  %s", strings.Join(got, "\n  "))
	}
	if ca := c.cephRec.Take(); contains(ca, "create") || contains(ca, "caps") || contains(ca, "resize") {
		t.Fatalf("node 2 changed Ceph without the lease:\n  %s", strings.Join(ca, "\n  "))
	}
	st2 := n2.status()
	if len(st2.Tenants) != 0 || st2.HA == nil || !st2.HA.LeaseOK {
		t.Fatalf("node 2 status %+v", st2)
	}
	c.run(time.Minute)
	if !n1.serving(12) || n2.serving(12) {
		t.Fatal("serving moved without a reason")
	}
}

func TestHAFailoverStopsHolderAndPeerTakesOver(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	order := []int64{1, 2}
	n1.src.set(haCfgFor(n1, "v1", order, haTenant(n1)))
	n2.src.set(haCfgFor(n2, "v1", order, haTenant(n2)))
	n1.once()
	n2.once()
	c.run(10 * time.Second)
	if !n1.serving(12) {
		t.Fatal("node 1 not serving")
	}

	n1.client.SetPartition(lease.PartitionFail)
	both := false
	ok := c.until(2*time.Minute, func() bool {
		if n1.serving(12) && n2.serving(12) {
			both = true
		}
		return n2.serving(12)
	})
	if !ok {
		t.Fatal("node 2 did not take over")
	}
	if both {
		t.Fatal("both nodes served at once")
	}
	if n1.serving(12) {
		t.Fatal("node 1 did not stop its interface and container")
	}
	if _, err := os.Stat(filepath.Join(n1.dir, "leases", "tenant.12.json")); !os.IsNotExist(err) {
		t.Fatalf("deadline file left after the stop: %v", err)
	}
	bl := c.pool.Blocklist()
	if len(bl) != 1 || bl[0].Addr != "10.20.0.1:0/0" {
		t.Fatalf("blocklist %+v", bl)
	}
	var evs []api.Event
	for _, st := range n2.src.posted {
		evs = append(evs, st.Events...)
	}
	if len(evs) != 1 || evs[0].Kind != api.EventTakeover || *evs[0].Fence != "node" || *evs[0].FromNodeID != 1 {
		t.Fatalf("node 2 events %+v", evs)
	}

	// Node 1 comes back fenced: it removes its leftovers and reports.
	n1.client.SetPartition(lease.Connected)
	c.run(10 * time.Second)
	st := n1.status()
	if st.HA == nil || !st.HA.Fenced || st.Node.Error == nil || !strings.Contains(*st.Node.Error, "fenced") {
		t.Fatalf("node 1 status %+v", st)
	}
	if n1.nfs.Running("rf-t12-nfs") || len(st.Tenants) != 0 {
		t.Fatalf("fenced node still has the tenant: %+v", st.Tenants)
	}
	var kinds []string
	for _, p := range n1.src.posted {
		for _, e := range p.Events {
			kinds = append(kinds, e.Kind)
		}
	}
	if strings.Join(kinds, ",") != "takeover,lost,fenced" {
		t.Fatalf("node 1 events %v", kinds)
	}
}

func TestHARunnerRestartKeepsServing(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	n2.src.set(haCfgFor(n2, "v1", []int64{1, 2}, haTenant(n2)))
	n1.applyCfg(haCfgFor(n1, "v1", []int64{1, 2}, haTenant(n1)))
	c.run(10 * time.Second)
	n1.rec.Take()

	n1.restart()
	got := n1.once()
	if contains(got, "nfs rm") || contains(got, "net remove") || contains(got, "nfs run") || contains(got, "net down") {
		t.Fatalf("restart interrupted serving:\n  %s", strings.Join(got, "\n  "))
	}
	if !n1.serving(12) || !n1.r.Lease.Holds(lease.TenantKey(12)) {
		t.Fatal("restarted runner does not hold its tenant")
	}
	if l := c.pool.Lockers("tenant.12"); len(l.Lockers) != 1 {
		t.Fatalf("old instance entry left: %+v", l)
	}
	c.run(time.Minute)
	if !n1.serving(12) || len(c.pool.Blocklist()) != 0 {
		t.Fatal("restart led to a failover")
	}
}

func TestHARackflowOutageKeepsServing(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	n1.src.set(haCfgFor(n1, "v1", []int64{1, 2}, haTenant(n1)))
	n2.src.set(haCfgFor(n2, "v1", []int64{1, 2}, haTenant(n2)))
	c.run(10 * time.Second)
	fi, err := os.Stat(filepath.Join(n1.dir, "config.json"))
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("saved config: %v %v", fi, err)
	}
	for _, n := range c.nodes {
		n.src.mu.Lock()
		n.src.err = errors.New("rackflow down")
		n.src.mu.Unlock()
	}
	// A restart during the outage takes the saved config.
	n1.restart()
	c.run(2 * time.Minute)
	if !n1.serving(12) || n2.serving(12) || len(c.pool.Blocklist()) != 0 {
		t.Fatal("a Rackflow outage moved or stopped the tenant")
	}
	// A failover still happens without Rackflow.
	n1.client.SetPartition(lease.PartitionFail)
	if !c.until(2*time.Minute, func() bool { return n2.serving(12) }) {
		t.Fatal("no failover while Rackflow is down")
	}
}

func TestHAEnabledFalseServesOwnTenantsOnly(t *testing.T) {
	c := newHACluster(t)
	n1 := c.add(1)
	cfg := haCfgFor(n1, "v1", []int64{2}, haTenant(n1)) // order does not list this node
	cfg.HA.Enabled = false
	n1.applyCfg(cfg)
	if !n1.serving(12) || !n1.r.Lease.Holds(lease.TenantKey(12)) {
		t.Fatal("tenant in this node's config not served with ha disabled")
	}
	if st := n1.status(); st.HA == nil || st.HA.HAActive {
		t.Fatalf("ha status %+v", st.HA)
	}
	// The tenant leaves the config: its lease is released and the local
	// copy removed. Another tenant stays, as a config with no tenants at
	// all is refused.
	other := tenant(13, 1235, share(135, "data", gib, "10.0.0.5"))
	cfg = haCfgFor(n1, "v2", []int64{1}, other)
	cfg.HA.Enabled = false
	n1.src.set(cfg)
	c.run(5 * time.Second)
	if n1.nfs.Running("rf-t12-nfs") {
		t.Fatal("tenant that left the config still served")
	}
	if l := c.pool.Lockers("tenant.12"); len(l.Lockers) != 0 {
		t.Fatalf("lease not released: %+v", l)
	}
	if c.pool.Xattr("tenant.12", lease.XattrReleased) == "" {
		t.Fatal("rf.released not written")
	}
}

func TestHATerminatedTenantDestroyedByPreferredNode(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	n1.applyCfg(haCfgFor(n1, "v1", []int64{1, 2}, haTenant(n1)))
	c.pool.Put("rf-t12", "rec-1", []byte("x"))

	term := haTenant(n1)
	term.State = api.TenantTerminated
	term.TerminatedAt = ts(t0)
	term.DestroyAfter = ts(t0)
	for _, n := range c.nodes {
		n.now = t0.Add(week + time.Hour)
		n.src.set(haCfgFor(n, "v2", []int64{2, 1}, term)) // preferred node is 2
	}
	c.cephRec.Take()
	c.run(10 * time.Second)
	if n1.nfs.Running("rf-t12-nfs") {
		t.Fatal("terminated tenant still served")
	}
	if c.ceph.HasSubvolume("t12-s125") {
		t.Fatal("subvolume not destroyed")
	}
	if c.pool.Exists(lease.Namespace, "tenant.12") || c.pool.Exists("rf-t12", "rec-1") {
		t.Fatal("lease object or recovery namespace left")
	}
	var destroyedBy []int64
	for _, n := range c.nodes {
		for _, st := range n.src.posted {
			if len(st.DestroyedTenants) > 0 {
				destroyedBy = append(destroyedBy, n.id)
				break
			}
		}
	}
	if len(destroyedBy) != 1 || destroyedBy[0] != 2 {
		t.Fatalf("destroyed by %v, want the preferred node 2", destroyedBy)
	}
	c.run(time.Minute)
	if c.pool.Exists(lease.Namespace, "tenant.12") {
		t.Fatal("lease object re-created after destruction")
	}
	_ = n2
}

func TestHAPlannedMoveHandsOff(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	for _, n := range c.nodes {
		n.src.set(haCfgFor(n, "v1", []int64{1, 2}, haTenant(n)))
	}
	c.run(10 * time.Second)
	for _, n := range c.nodes {
		tn := haTenant(n)
		tn.Move = &api.Move{ID: "m-7f3a", ToNodeID: 2}
		n.src.set(haCfgFor(n, "v2", []int64{2, 1}, tn))
	}
	both := false
	if !c.until(30*time.Second, func() bool {
		both = both || (n1.serving(12) && n2.serving(12))
		return n2.serving(12) && !n1.serving(12)
	}) {
		t.Fatal("move did not complete")
	}
	if both || len(c.pool.Blocklist()) != 0 {
		t.Fatal("planned move overlapped or fenced")
	}
	ts := tenantStatus(t, n2.status(), 12)
	if ts.MoveID != "m-7f3a" || ts.Serving == nil || !*ts.Serving {
		t.Fatalf("target status %+v", ts)
	}
}

func TestLegacyConfigWithoutHAIsUnchanged(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	st := e.status()
	if st.HA != nil || st.Events != nil || tenantStatus(t, st, 12).Serving != nil {
		t.Fatalf("legacy status carries ha fields: %+v", st)
	}
	if conf := e.ganeshaConf("t12"); !strings.Contains(conf, "RecoveryBackend = fs;") || strings.Contains(conf, "RADOS_KV") {
		t.Fatalf("legacy ganesha.conf:\n%s", conf)
	}
}

func sftpHACfg(n *haNode, version string, sftpOrder []int64, tenants ...api.Tenant) *api.Config {
	cfg := haCfgFor(n, version, []int64{1, 2}, tenants...)
	vip := "127.0.0.3/8"
	cfg.SFTP.LocationID = 1
	cfg.SFTP.VirtualIP = &vip
	cfg.SFTP.MasterEligible = true
	cfg.SFTP.MasterOrder = sftpOrder
	return cfg
}

func loginAt(e *sftpEnv, host string, tenantID int64) (ssh.PublicKey, error) {
	var hostKey ssh.PublicKey
	cl, err := ssh.Dial("tcp", net.JoinHostPort(host, itoa(int64(e.port))), &ssh.ClientConfig{
		User: "web." + itoa(tenantID),
		Auth: []ssh.AuthMethod{ssh.PublicKeys(e.key)},
		HostKeyCallback: func(_ string, _ net.Addr, k ssh.PublicKey) error {
			hostKey = k
			return nil
		},
		Timeout: 5 * time.Second,
	})
	if err != nil {
		return nil, err
	}
	return hostKey, cl.Close()
}

func TestHASFTPMasterElectionAndVIP(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	n2.port = n1.port
	vipPfx := netip.MustParsePrefix("127.0.0.3/8")

	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	block, err := ssh.MarshalPrivateKey(priv, "")
	if err != nil {
		t.Fatal(err)
	}
	locKey := string(pem.EncodeToMemory(block))
	locSigner, _ := ssh.ParsePrivateKey([]byte(locKey))

	// SFTP master order is 2 then 1; NFS order is 1 then 2, so the master
	// serves a tenant whose NFS runs on the other node.
	for _, n := range c.nodes {
		cfg := sftpHACfg(n, "v1", []int64{2, 1}, haTenant(n))
		cfg.SFTP.HostKey = locKey
		n.src.set(cfg)
	}
	c.run(10 * time.Second)
	if !n1.serving(12) || n2.serving(12) {
		t.Fatal("NFS not on node 1")
	}
	if !n2.vip.Present("eno1", vipPfx) || n1.vip.Present("eno1", vipPfx) {
		t.Fatal("virtual ip not on the master only")
	}
	st := n2.status()
	if st.HA == nil || !st.HA.SFTPMaster || n1.status().HA.SFTPMaster {
		t.Fatalf("sftp_master flags wrong: %+v / %+v", st.HA, n1.status().HA)
	}
	if ts := tenantStatus(t, st, 12); ts.Serving == nil || *ts.Serving || ts.SFTP == nil {
		t.Fatalf("sftp master's entry for a tenant served elsewhere: %+v", ts)
	}
	key, err := loginAt(n2.sftpEnv, "127.0.0.3", 12)
	if err != nil {
		t.Fatalf("login on the virtual ip: %v", err)
	}
	if string(key.Marshal()) != string(locSigner.PublicKey().Marshal()) {
		t.Fatal("master does not present the location host key")
	}
	fi, err := os.Stat(filepath.Join(n2.dir, HostKeyFile))
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("host key file: %v %v", fi, err)
	}
	if backEnds(n2) != 1 || backEnds(n1) != 0 {
		t.Fatalf("back ends: master %d, standby %d", backEnds(n2), backEnds(n1))
	}
	var kinds []string
	for _, p := range n2.src.posted {
		for _, e := range p.Events {
			kinds = append(kinds, e.Kind)
		}
	}
	if strings.Join(kinds, ",") != "sftp_master" {
		t.Fatalf("master events %v", kinds)
	}

	// The master loses Ceph: it stops the gateway, removes the virtual IP
	// and its back ends; node 1 becomes master.
	n2.client.SetPartition(lease.PartitionFail)
	if !c.until(2*time.Minute, func() bool { return n1.vip.Present("eno1", vipPfx) }) {
		t.Fatal("node 1 did not become sftp master")
	}
	if n2.vip.Present("eno1", vipPfx) || backEnds(n2) != 0 {
		t.Fatal("old master kept its virtual ip or back ends")
	}
	if _, err := loginAt(n1.sftpEnv, "127.0.0.3", 12); err != nil {
		t.Fatalf("login on the new master: %v", err)
	}
}

func TestHASFTPWithoutVIPIsPerNode(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	for _, n := range c.nodes {
		n.src.set(haCfgFor(n, "v1", []int64{1, 2}, haTenant(n)))
	}
	c.run(10 * time.Second)
	if err := n1.login(12); err != nil {
		t.Fatalf("holder sftp: %v", err)
	}
	if backEnds(n2) != 0 || len(c.pool.Lockers("sftp.1").Lockers) != 0 {
		t.Fatal("per-node sftp used the master lease or ran on the standby")
	}
}

func backEnds(n *haNode) int {
	ctrs, _ := n.rt.List(context.Background())
	return len(ctrs)
}

func TestHAOneNodeLocation(t *testing.T) {
	c := newHACluster(t)
	n1 := c.add(1)
	cfg := haCfgFor(n1, "v1", []int64{1}, haTenant(n1))
	cfg.HA.Nodes = []api.HANode{{ID: 1, Name: "stor-01"}}
	n1.src.set(cfg)
	c.run(time.Minute)
	if !n1.serving(12) {
		t.Fatal("one-node location does not serve")
	}
	n1.restart()
	n1.once()
	c.run(time.Minute)
	if !n1.serving(12) || len(c.pool.Blocklist()) != 0 {
		t.Fatal("one-node location lost its tenant over a restart")
	}
}

func TestHADrainingNodeHandsOffSFTPMaster(t *testing.T) {
	c := newHACluster(t)
	n1, n2 := c.add(1), c.add(2)
	n2.port = n1.port
	vipPfx := netip.MustParsePrefix("127.0.0.3/8")
	for _, n := range c.nodes {
		n.src.set(sftpHACfg(n, "v1", []int64{1, 2}, haTenant(n)))
	}
	c.run(10 * time.Second)
	if !n1.vip.Present("eno1", vipPfx) {
		t.Fatal("node 1 is not sftp master")
	}
	cfg := sftpHACfg(n1, "v2", []int64{1, 2}, haTenant(n1))
	cfg.HA.Draining = true
	n1.src.set(cfg)
	if !c.until(time.Minute, func() bool { return n2.vip.Present("eno1", vipPfx) && n2.serving(12) }) {
		t.Fatal("draining node did not hand off the sftp master role and its tenant")
	}
	if n1.vip.Present("eno1", vipPfx) || n1.serving(12) || len(c.pool.Blocklist()) != 0 {
		t.Fatal("drain left the old node serving or fenced it")
	}
	c.run(time.Minute)
	if n1.vip.Present("eno1", vipPfx) || n1.serving(12) {
		t.Fatal("draining node took something back")
	}
}
