package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
	"rackflow/storage_runner/internal/config"
	"rackflow/storage_runner/internal/firewall"
	"rackflow/storage_runner/internal/ganesha"
	"rackflow/storage_runner/internal/netns"
	"rackflow/storage_runner/internal/record"
	"rackflow/storage_runner/internal/state"
)

const (
	gib       = int64(1) << 30
	week      = 7 * 24 * time.Hour
	fwNone    = "fw apply links=[] ceph=[10.10.0.0/24]"
	fw12      = "fw apply links=[rf-h12] ceph=[10.10.0.0/24]"
	net12     = "net ensure rf-t12 vlan=1234 addr=10.0.0.254/24 host=169.254.128.1/30 ns=169.254.128.2/30 routes=10.10.0.0/24"
	evict12   = "ceph evict client.rf-t12"
	run12     = "nfs run rf-t12-nfs netns=/run/netns/rf-t12 image=ganesha:test"
	user12    = `ceph user create client.rf-t12 mon="allow r" mds="allow rw path=/volumes/rackflow/t12-s5/uuid-t12-s5" osd="allow rw pool=ec namespace=fsvolumens_t12-s5"`
	groupMake = "ceph group create rackflow pool=ec"
)

var t0 = time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)

type fakeSource struct {
	mu     sync.Mutex
	cfg    []byte
	err    error
	posted []api.Status
}

func (f *fakeSource) set(cfg *api.Config) {
	b, err := json.Marshal(cfg)
	if err != nil {
		panic(err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cfg = b
}

func (f *fakeSource) Fetch(context.Context) (*api.Config, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	var c api.Config
	if err := json.Unmarshal(f.cfg, &c); err != nil {
		return nil, err
	}
	return &c, nil
}

func (f *fakeSource) PostStatus(_ context.Context, st api.Status) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posted = append(f.posted, st)
	return nil
}

type env struct {
	t    *testing.T
	rec  *record.Recorder
	net  *netns.Fake
	fw   *firewall.Fake
	ceph *ceph.Fake
	nfs  *ganesha.Fake
	src  *fakeSource
	r    *Reconciler
	now  time.Time
	dir  string
}

func newEnv(t *testing.T) *env {
	rec := &record.Recorder{}
	e := &env{
		t:    t,
		rec:  rec,
		net:  netns.NewFake(rec),
		fw:   firewall.NewFake(rec),
		ceph: ceph.NewFake(rec, "rackflow", "ec"),
		nfs:  ganesha.NewFake(rec),
		src:  &fakeSource{},
		now:  t0,
		dir:  t.TempDir(),
	}
	e.restart()
	return e
}

func (e *env) host() config.Host {
	return config.Host{
		RackflowURL:        "https://rackflow.test",
		RunnerKey:          "key",
		PollInterval:       30 * time.Second,
		ParentIface:        "eth1",
		CephFSName:         "cephfs",
		CephECPool:         "ec",
		CephSubvolumeGroup: "rackflow",
		CephPublicNetworks: []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24")},
		GaneshaImage:       "ganesha:test",
		LinkCIDR:           netip.MustParsePrefix("169.254.128.0/17"),
		StateDir:           e.dir,
	}
}

// restart builds a new Reconciler over the same host and state directory.
func (e *env) restart() {
	st, err := state.Open(e.dir, netip.MustParsePrefix("169.254.128.0/17"))
	if err != nil {
		e.t.Fatal(err)
	}
	e.r = &Reconciler{
		Host:          e.host(),
		Source:        e.src,
		Net:           e.net,
		Firewall:      e.fw,
		Ceph:          e.ceph,
		NFS:           e.nfs,
		State:         st,
		Now:           func() time.Time { return e.now },
		Logf:          e.t.Logf,
		RunnerVersion: "0.1.0",
	}
}

func (e *env) apply(cfg *api.Config) []string {
	e.t.Helper()
	e.src.set(cfg)
	if err := e.r.Once(context.Background()); err != nil {
		e.t.Fatalf("Once: %v", err)
	}
	return e.rec.Take()
}

func (e *env) status() api.Status {
	e.t.Helper()
	e.src.mu.Lock()
	defer e.src.mu.Unlock()
	if len(e.src.posted) == 0 {
		e.t.Fatal("no status posted")
	}
	return e.src.posted[len(e.src.posted)-1]
}

func (e *env) ganeshaConf(tenant string) string {
	e.t.Helper()
	b, err := os.ReadFile(filepath.Join(e.dir, "tenants", tenant, "ganesha", "ganesha.conf"))
	if err != nil {
		e.t.Fatal(err)
	}
	return string(b)
}

func expect(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("actions differ\n got:\n  %s\nwant:\n  %s", strings.Join(got, "\n  "), strings.Join(want, "\n  "))
	}
}

func ts(t time.Time) *api.Timestamp { return &api.Timestamp{Time: t} }

func share(id int64, name string, quota int64, clients ...string) api.Share {
	return api.Share{ID: id, Name: name, QuotaBytes: quota, Clients: clients, State: api.ShareActive}
}

func tenant(id int64, vlan int, shares ...api.Share) api.Tenant {
	return api.Tenant{
		ID:          id,
		State:       api.TenantActive,
		VLAN:        vlan,
		NetworkCIDR: "10.0.0.0/24",
		EndpointIP:  "10.0.0.254",
		PrefixLen:   24,
		SizeBytes:   500 * gib,
		Shares:      shares,
	}
}

func cfgOf(version string, tenants ...api.Tenant) *api.Config {
	grace := int64(week / time.Second)
	if tenants == nil {
		tenants = []api.Tenant{}
	}
	return &api.Config{
		Version:  version,
		Node:     api.Node{ID: 3, Name: "store-01"},
		Settings: api.Settings{DestroyGraceSeconds: &grace},
		Tenants:  tenants,
	}
}

func basic() api.Tenant { return tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5")) }

func firstApply() []string {
	return []string{groupMake, net12, "ceph subvolume create t12-s5 size=107374182400", user12, fw12, evict12, run12}
}

func tenantStatus(t *testing.T, st api.Status, id int64) api.TenantStatus {
	t.Helper()
	for _, ts := range st.Tenants {
		if ts.ID == id {
			return ts
		}
	}
	t.Fatalf("tenant %d missing from status %+v", id, st.Tenants)
	return api.TenantStatus{}
}

func TestFirstApply(t *testing.T) {
	e := newEnv(t)
	e.ceph.FreeBytes = 123
	expect(t, e.apply(cfgOf("v1", basic())), firstApply())

	st := e.status()
	if st.VersionApplied != "v1" || st.RunnerVersion != "0.1.0" || !st.Node.CephOK || st.Node.FreeBytes != 123 || st.Node.Error != nil {
		t.Fatalf("unexpected node status: %+v", st)
	}
	ts := tenantStatus(t, st, 12)
	if ts.State != api.StateReady || ts.Error != nil || len(ts.Shares) != 1 || ts.Shares[0].State != api.StateReady {
		t.Fatalf("unexpected tenant status: %+v", ts)
	}

	conf := e.ganeshaConf("t12")
	for _, want := range []string{
		"Bind_addr = 10.0.0.254;",
		"Protocols = 4;",
		`Path = "/volumes/rackflow/t12-s5/uuid-t12-s5";`,
		`Pseudo = "/data";`,
		"Squash = root_squash;",
		`User_Id = "rf-t12";`,
		`Filesystem = "cephfs";`,
		"Clients = 10.0.0.5;",
		"Access_Type = RW;",
	} {
		if !strings.Contains(conf, want) {
			t.Errorf("ganesha.conf lacks %q:\n%s", want, conf)
		}
	}
	keyring := filepath.Join(e.dir, "tenants", "t12", "ceph", "ceph.client.rf-t12.keyring")
	fi, err := os.Stat(keyring)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Errorf("keyring mode %v, want 0600", fi.Mode().Perm())
	}
	if b, _ := os.ReadFile(keyring); !strings.Contains(string(b), "key = KEY1") {
		t.Errorf("keyring content %q", b)
	}
	spec, ok := e.nfs.Spec("rf-t12-nfs")
	if !ok || len(spec.Mounts) != 3 || spec.Mounts[0].Target != "/etc/ganesha" || !spec.Mounts[0].ReadOnly {
		t.Fatalf("unexpected container spec %+v", spec)
	}
}

func TestReapplyIsIdempotent(t *testing.T) {
	e := newEnv(t)
	cfg := cfgOf("v1", basic())
	expect(t, e.apply(cfg), firstApply())
	expect(t, e.apply(cfg), nil)

	e.restart()
	expect(t, e.apply(cfg), nil)
	if st := e.status(); tenantStatus(t, st, 12).State != api.StateReady {
		t.Fatalf("after restart: %+v", st)
	}
}

func TestSuspendAndUnsuspend(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))

	sus := basic()
	sus.State = api.TenantSuspended
	expect(t, e.apply(cfgOf("v2", sus)), []string{"nfs rm rf-t12-nfs", fwNone})
	ts := tenantStatus(t, e.status(), 12)
	if ts.State != api.StateSuspended || ts.Shares[0].State != api.StateSuspended {
		t.Fatalf("suspended status: %+v", ts)
	}
	if !e.ceph.HasSubvolume("t12-s5") || !e.net.Has(12) {
		t.Fatal("suspend must keep data and network")
	}
	expect(t, e.apply(cfgOf("v2", sus)), nil)

	expect(t, e.apply(cfgOf("v3", basic())), []string{fw12, evict12, run12})
	if ts := tenantStatus(t, e.status(), 12); ts.State != api.StateReady {
		t.Fatalf("unsuspended status: %+v", ts)
	}
}

func terminated(terminatedAt time.Time, destroyAfter *api.Timestamp) api.Tenant {
	t := basic()
	t.State = api.TenantTerminated
	t.TerminatedAt = ts(terminatedAt)
	t.DestroyAfter = destroyAfter
	return t
}

func TestTerminateBeforeAndAfterGrace(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	e.ceph.SeedSubvolume("t12-s9", gib) // not listed in the config

	term := terminated(t0, ts(t0.Add(week)))
	e.now = t0.Add(time.Hour)
	expect(t, e.apply(cfgOf("v2", term)), []string{"nfs rm rf-t12-nfs", "net remove rf-t12", fwNone})
	st := e.status()
	if ts := tenantStatus(t, st, 12); ts.State != api.StatePending || ts.Shares[0].State != api.StatePending {
		t.Fatalf("terminated status: %+v", ts)
	}
	if len(st.DestroyedTenants) != 0 {
		t.Fatalf("destroyed too early: %v", st.DestroyedTenants)
	}
	if !e.ceph.HasSubvolume("t12-s5") {
		t.Fatal("data destroyed before grace")
	}
	if _, ok := e.r.State.Link(12); ok {
		t.Fatal("link not released")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "tenants", "t12")); !os.IsNotExist(err) {
		t.Fatalf("tenant files (keyring) not removed: %v", err)
	}
	e.now = t0.Add(week - time.Minute)
	expect(t, e.apply(cfgOf("v2", term)), nil)

	e.now = t0.Add(week + time.Hour)
	expect(t, e.apply(cfgOf("v2", term)), []string{"ceph subvolume rm t12-s5", "ceph user rm client.rf-t12"})
	st = e.status()
	if len(st.DestroyedTenants) != 1 || st.DestroyedTenants[0] != 12 || len(st.Tenants) != 0 {
		t.Fatalf("destroy status: %+v", st)
	}
	if !e.ceph.HasSubvolume("t12-s9") {
		t.Fatal("unlisted subvolume destroyed")
	}
	if len(st.Orphans) != 1 || st.Orphans[0] != "t12-s9" {
		t.Fatalf("orphans: %v", st.Orphans)
	}

	expect(t, e.apply(cfgOf("v2", term)), nil)
	if st := e.status(); len(st.DestroyedTenants) != 1 {
		t.Fatalf("destroyed tenant not reported again: %+v", st)
	}
}

func TestTerminateDestroyNeedsBothChecks(t *testing.T) {
	cases := []struct {
		name         string
		destroyAfter *api.Timestamp
		now          time.Time
	}{
		{"destroy_after past, grace not met", ts(t0), t0.Add(24 * time.Hour)},
		{"grace met, destroy_after missing", nil, t0.Add(30 * 24 * time.Hour)},
		{"grace met, destroy_after in the future", ts(t0.Add(60 * 24 * time.Hour)), t0.Add(30 * 24 * time.Hour)},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEnv(t)
			e.apply(cfgOf("v1", basic()))
			e.now = c.now
			expect(t, e.apply(cfgOf("v2", terminated(t0, c.destroyAfter))), []string{"nfs rm rf-t12-nfs", "net remove rf-t12", fwNone})
			if !e.ceph.HasSubvolume("t12-s5") {
				t.Fatal("data destroyed")
			}
			if _, ok := e.ceph.User("client.rf-t12"); !ok {
				t.Fatal("user destroyed")
			}
			if st := e.status(); len(st.DestroyedTenants) != 0 {
				t.Fatalf("reported destroyed: %+v", st)
			}
		})
	}
}

func TestShareDeleting(t *testing.T) {
	e := newEnv(t)
	two := tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5"), share(6, "logs", 10*gib, "10.0.0.6"))
	e.apply(cfgOf("v1", two))
	if !strings.Contains(e.ganeshaConf("t12"), `Pseudo = "/logs";`) {
		t.Fatal("logs not exported")
	}

	del := tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5"), share(6, "logs", 10*gib, "10.0.0.6"))
	del.Shares[1].State = api.ShareDeleting
	del.Shares[1].DestroyAfter = ts(t0) // already past: the runner's own clock still holds it
	e.now = t0.Add(time.Minute)
	expect(t, e.apply(cfgOf("v2", del)), []string{
		`ceph user caps client.rf-t12 mon="allow r" mds="allow rw path=/volumes/rackflow/t12-s5/uuid-t12-s5" osd="allow rw pool=ec namespace=fsvolumens_t12-s5"`,
		"nfs reload rf-t12-nfs",
	})
	if strings.Contains(e.ganeshaConf("t12"), "/logs") {
		t.Fatal("export not removed at once")
	}
	st := e.status()
	if s := tenantStatus(t, st, 12).Shares[1]; s.ID != 6 || s.State != api.StatePending {
		t.Fatalf("deleting share status: %+v", s)
	}
	if len(st.DestroyedShares) != 0 || !e.ceph.HasSubvolume("t12-s6") {
		t.Fatal("share destroyed before grace")
	}

	e.restart()
	e.now = t0.Add(week)
	expect(t, e.apply(cfgOf("v2", del)), nil)

	e.now = t0.Add(week + 2*time.Minute)
	expect(t, e.apply(cfgOf("v2", del)), []string{"ceph subvolume rm t12-s6"})
	st = e.status()
	if len(st.DestroyedShares) != 1 || st.DestroyedShares[0] != 6 || len(tenantStatus(t, st, 12).Shares) != 1 {
		t.Fatalf("destroyed share status: %+v", st)
	}
	expect(t, e.apply(cfgOf("v2", del)), nil)
}

func TestQuotaResize(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	bigger := tenant(12, 1234, share(5, "data", 200*gib, "10.0.0.5"))
	expect(t, e.apply(cfgOf("v2", bigger)), []string{"ceph subvolume resize t12-s5 size=214748364800"})
	expect(t, e.apply(cfgOf("v2", bigger)), nil)
}

func TestClientChangeReloads(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	ro := tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5", "10.0.0.32/28"))
	ro.Shares[0].ReadOnly = true
	expect(t, e.apply(cfgOf("v2", ro)), []string{"nfs reload rf-t12-nfs"})
	conf := e.ganeshaConf("t12")
	if !strings.Contains(conf, "Clients = 10.0.0.5, 10.0.0.32/28;") || !strings.Contains(conf, "Access_Type = RO;") {
		t.Fatalf("conf:\n%s", conf)
	}
}

func TestEndpointChangeRestarts(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	moved := basic()
	moved.EndpointIP = "10.0.0.253"
	expect(t, e.apply(cfgOf("v2", moved)), []string{
		"net ensure rf-t12 vlan=1234 addr=10.0.0.253/24 host=169.254.128.1/30 ns=169.254.128.2/30 routes=10.10.0.0/24",
		"nfs rm rf-t12-nfs",
		evict12,
		run12,
	})
}

func TestStoppedContainerIsRestarted(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	e.nfs.Exit("rf-t12-nfs")
	expect(t, e.apply(cfgOf("v1", basic())), []string{"nfs rm rf-t12-nfs", evict12, run12})
}

func TestConfigFetchFailureChangesNothing(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	posted := len(e.src.posted)
	e.src.err = errors.New("connection refused")
	if err := e.r.Once(context.Background()); err == nil {
		t.Fatal("expected an error")
	}
	expect(t, e.rec.Take(), nil)
	if len(e.src.posted) != posted {
		t.Fatal("status posted after a failed fetch")
	}
	if !e.nfs.Has("rf-t12-nfs") || !e.net.Has(12) {
		t.Fatal("state torn down")
	}
}

func TestEmptyConfigKeepsState(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	expect(t, e.apply(cfgOf("v2")), nil)
	st := e.status()
	if st.VersionApplied != "v1" {
		t.Fatalf("version_applied %q, want the previous version", st.VersionApplied)
	}
	if st.Node.Error == nil || !strings.Contains(*st.Node.Error, "no tenants") {
		t.Fatalf("node error: %v", st.Node.Error)
	}
	if strings.Join(st.Orphans, ",") != "rf-t12,t12-s5" {
		t.Fatalf("orphans: %v", st.Orphans)
	}
	if !e.nfs.Has("rf-t12-nfs") || !e.net.Has(12) || !e.ceph.HasSubvolume("t12-s5") {
		t.Fatal("state torn down")
	}
}

func TestEmptyConfigOnFreshNode(t *testing.T) {
	e := newEnv(t)
	expect(t, e.apply(cfgOf("v0")), []string{groupMake, fwNone})
	if st := e.status(); st.VersionApplied != "v0" || st.Node.Error != nil {
		t.Fatalf("status: %+v", st)
	}
}

func TestOrphansReportedNotTouched(t *testing.T) {
	e := newEnv(t)
	e.ceph.SetGroup("ec")
	e.ceph.SeedSubvolume("t12-s9", gib)
	e.ceph.SeedSubvolume("t40-s1", gib)
	e.ceph.SeedSubvolume("legacy", gib)
	e.ceph.SeedUser("client.rf-t40", ceph.Caps{Mon: "allow r"})
	e.net.Seed(40)
	e.nfs.Seed(ganesha.Container{Name: "rf-t40-nfs", TenantID: 40, Running: true})

	expect(t, e.apply(cfgOf("v1", basic())), firstApply()[1:])
	st := e.status()
	if got := strings.Join(st.Orphans, ","); got != "legacy,rf-t40,t12-s9,t40-s1" {
		t.Fatalf("orphans: %s", got)
	}
	if !e.ceph.HasSubvolume("t40-s1") || !e.ceph.HasSubvolume("legacy") || !e.net.Has(40) || !e.nfs.Has("rf-t40-nfs") {
		t.Fatal("orphan touched")
	}
	if strings.Contains(e.ganeshaConf("t12"), "t12-s9") {
		t.Fatal("unlisted subvolume exported")
	}
}

func TestOverlappingCIDRsAcrossTenants(t *testing.T) {
	e := newEnv(t)
	cfg := cfgOf("v1", basic(), tenant(13, 1235, share(7, "data", 50*gib, "10.0.0.5")))
	expect(t, e.apply(cfg), []string{
		groupMake,
		net12,
		"ceph subvolume create t12-s5 size=107374182400",
		user12,
		"net ensure rf-t13 vlan=1235 addr=10.0.0.254/24 host=169.254.128.5/30 ns=169.254.128.6/30 routes=10.10.0.0/24",
		"ceph subvolume create t13-s7 size=53687091200",
		`ceph user create client.rf-t13 mon="allow r" mds="allow rw path=/volumes/rackflow/t13-s7/uuid-t13-s7" osd="allow rw pool=ec namespace=fsvolumens_t13-s7"`,
		"fw apply links=[rf-h12,rf-h13] ceph=[10.10.0.0/24]",
		evict12,
		run12,
		"ceph evict client.rf-t13",
		"nfs run rf-t13-nfs netns=/run/netns/rf-t13 image=ganesha:test",
	})
	st := e.status()
	for _, id := range []int64{12, 13} {
		if s := tenantStatus(t, st, id); s.State != api.StateReady {
			t.Fatalf("tenant %d: %+v", id, s)
		}
	}
}

func TestSameVLANTwiceIsRejected(t *testing.T) {
	e := newEnv(t)
	e.ceph.SetGroup("ec")
	expect(t, e.apply(cfgOf("v1", basic(), tenant(13, 1234))), []string{fwNone})
	for _, id := range []int64{12, 13} {
		if s := tenantStatus(t, e.status(), id); s.State != api.StateError || !strings.Contains(*s.Error, "VLAN 1234") {
			t.Fatalf("tenant %d: %+v", id, s)
		}
	}
}

func TestLinkAllocationStable(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic(), tenant(13, 1235)))
	e.restart()

	term := terminated(t0, nil)
	expect(t, e.apply(cfgOf("v2", term, tenant(13, 1235))), []string{"nfs rm rf-t12-nfs", "net remove rf-t12", "fw apply links=[rf-h13] ceph=[10.10.0.0/24]"})

	expect(t, e.apply(cfgOf("v3", term, tenant(13, 1235), tenant(14, 1236))), []string{
		"net ensure rf-t14 vlan=1236 addr=10.0.0.254/24 host=169.254.128.1/30 ns=169.254.128.2/30 routes=10.10.0.0/24",
		"fw apply links=[rf-h13,rf-h14] ceph=[10.10.0.0/24]",
	})
	if l, _ := e.r.State.Link(13); l.Host.String() != "169.254.128.5/30" {
		t.Fatalf("tenant 13 link moved to %s", l.Host)
	}
}

func TestCephUnavailable(t *testing.T) {
	e := newEnv(t)
	e.ceph.CheckErr = errors.New(`EC pool "ec" does not have allow_ec_overwrites enabled`)
	expect(t, e.apply(cfgOf("v1", basic())), []string{net12, fw12})
	st := e.status()
	if st.Node.CephOK || st.Node.Error == nil || !strings.Contains(*st.Node.Error, "allow_ec_overwrites") {
		t.Fatalf("node: %+v", st.Node)
	}
	if s := tenantStatus(t, st, 12); s.State != api.StateError {
		t.Fatalf("tenant: %+v", s)
	}

	e.ceph.CheckErr = nil
	expect(t, e.apply(cfgOf("v1", basic())), []string{groupMake, "ceph subvolume create t12-s5 size=107374182400", user12, evict12, run12})

	e.ceph.CheckErr = errors.New("timed out")
	expect(t, e.apply(cfgOf("v1", basic())), nil)
	if !e.nfs.Has("rf-t12-nfs") {
		t.Fatal("ganesha stopped because ceph check failed")
	}
}

func TestGroupOnWrongPoolIsReported(t *testing.T) {
	e := newEnv(t)
	e.ceph.SetGroup("cephfs_data")
	expect(t, e.apply(cfgOf("v1", basic())), []string{net12, fw12})
	if st := e.status(); st.Node.CephOK || !strings.Contains(*st.Node.Error, `uses data pool "cephfs_data"`) {
		t.Fatalf("node: %+v", st.Node)
	}
}

func TestInvalidConfigChangesNothing(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	bad := basic()
	bad.VLAN = 0
	expect(t, e.apply(cfgOf("v2", bad)), nil)
	s := tenantStatus(t, e.status(), 12)
	if s.State != api.StateError || !strings.Contains(*s.Error, "vlan 0") || s.Shares[0].State != api.StateError {
		t.Fatalf("status: %+v", s)
	}
	if !e.nfs.Has("rf-t12-nfs") {
		t.Fatal("invalid config tore the tenant down")
	}
}

func TestShareValidation(t *testing.T) {
	cases := map[string]api.Share{
		"client outside network": share(5, "data", gib, "192.168.1.5"),
		"bad name":               share(5, "../etc", gib),
		"share id not positive":  share(-1, "data", gib),
		"zero quota":             share(5, "data", 0),
	}
	for name, sh := range cases {
		t.Run(name, func(t *testing.T) {
			e := newEnv(t)
			e.ceph.SetGroup("ec")
			expect(t, e.apply(cfgOf("v1", tenant(12, 1234, sh))), []string{net12, fw12})
			s := tenantStatus(t, e.status(), 12)
			if s.State != api.StateReady || s.Shares[0].State != api.StateError || s.Shares[0].Error == nil {
				t.Fatalf("status: %+v", s)
			}
		})
	}
}

func TestNetworkOverlappingCephIsRejected(t *testing.T) {
	e := newEnv(t)
	e.ceph.SetGroup("ec")
	bad := basic()
	bad.NetworkCIDR, bad.EndpointIP = "10.10.0.0/24", "10.10.0.254"
	expect(t, e.apply(cfgOf("v1", bad)), []string{fwNone})
	if s := tenantStatus(t, e.status(), 12); !strings.Contains(*s.Error, "overlaps Ceph public network") {
		t.Fatalf("status: %+v", s)
	}
}

func TestUsedBytesReported(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	e.ceph.SetUsed("t12-s5", 1234)
	e.apply(cfgOf("v1", basic()))
	if got := tenantStatus(t, e.status(), 12).Shares[0].UsedBytes; got != 1234 {
		t.Fatalf("used_bytes %d", got)
	}
}

func TestExportIDsStableAndIndependentOfShareIDs(t *testing.T) {
	e := newEnv(t)
	big := tenant(12, 1234, share(70001, "data", gib, "10.0.0.5"), share(70002, "logs", gib, "10.0.0.5"))
	e.apply(cfgOf("v1", big))
	conf := e.ganeshaConf("t12")
	if !strings.Contains(conf, "Export_Id = 1;\n\tPath = \"/volumes/rackflow/t12-s70001") ||
		!strings.Contains(conf, "Export_Id = 2;\n\tPath = \"/volumes/rackflow/t12-s70002") {
		t.Fatalf("export ids:\n%s", conf)
	}

	// Deleting the first share keeps the second share's id; a new share gets
	// a fresh id, never the deleted one's; a restart changes nothing.
	next := tenant(12, 1234, share(70002, "logs", gib, "10.0.0.5"), share(70003, "new", gib, "10.0.0.5"))
	next.Shares[0], next.Shares[1] = next.Shares[1], next.Shares[0]
	del := next
	del.Shares = append([]api.Share{{ID: 70001, State: api.ShareDeleting}}, next.Shares...)
	e.apply(cfgOf("v2", del))
	e.restart()
	expect(t, e.apply(cfgOf("v2", del)), nil)
	conf = e.ganeshaConf("t12")
	if !strings.Contains(conf, "Export_Id = 3;\n\tPath = \"/volumes/rackflow/t12-s70003") ||
		!strings.Contains(conf, "Export_Id = 2;\n\tPath = \"/volumes/rackflow/t12-s70002") ||
		strings.Contains(conf, "Export_Id = 1;") {
		t.Fatalf("export ids after change:\n%s", conf)
	}

	// Once the deleted share is gone from the config its id stays retired.
	third := tenant(12, 1234, share(70002, "logs", gib, "10.0.0.5"), share(70003, "new", gib, "10.0.0.5"), share(70004, "more", gib, "10.0.0.5"))
	e.apply(cfgOf("v3", third))
	if conf := e.ganeshaConf("t12"); !strings.Contains(conf, "Export_Id = 4;\n\tPath = \"/volumes/rackflow/t12-s70004") {
		t.Fatalf("export ids after prune:\n%s", conf)
	}
}

func twoShares() api.Tenant {
	return tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5"), share(6, "logs", 10*gib, "10.0.0.6"))
}

func TestInvalidExistingShareKeepsAccess(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", twoShares()))
	user, _ := e.ceph.User("client.rf-t12")

	bad := twoShares()
	bad.Shares[1].Clients = []string{"192.168.1.5"}
	expect(t, e.apply(cfgOf("v2", bad)), nil)
	if got, _ := e.ceph.User("client.rf-t12"); got.Caps != user.Caps {
		t.Fatalf("caps narrowed to %s", got.Caps)
	}
	if !strings.Contains(e.ganeshaConf("t12"), `Pseudo = "/logs";`) {
		t.Fatal("export of the existing share dropped")
	}
	s := tenantStatus(t, e.status(), 12)
	if s.State != api.StateError || !strings.Contains(*s.Error, "left unchanged") || s.Shares[1].State != api.StateError {
		t.Fatalf("status: %+v", s)
	}

	expect(t, e.apply(cfgOf("v3", twoShares())), nil)
	if s := tenantStatus(t, e.status(), 12); s.State != api.StateReady {
		t.Fatalf("after fix: %+v", s)
	}
}

func TestSubvolumeInfoFailureKeepsAccess(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", twoShares()))
	user, _ := e.ceph.User("client.rf-t12")

	e.ceph.InfoFail["t12-s6"] = errors.New("timed out")
	expect(t, e.apply(cfgOf("v1", twoShares())), nil)
	if got, _ := e.ceph.User("client.rf-t12"); got.Caps != user.Caps {
		t.Fatalf("caps narrowed to %s", got.Caps)
	}
	if !strings.Contains(e.ganeshaConf("t12"), `Pseudo = "/logs";`) {
		t.Fatal("export dropped")
	}

	delete(e.ceph.InfoFail, "t12-s6")
	expect(t, e.apply(cfgOf("v1", twoShares())), nil)
}

func TestFailedReloadIsRetriedAndBlocksDestroy(t *testing.T) {
	e := newEnv(t)
	grace := int64(0)
	withGrace := func(c *api.Config) *api.Config { c.Settings.DestroyGraceSeconds = &grace; return c }
	e.apply(withGrace(cfgOf("v1", twoShares())))

	del := twoShares()
	del.Shares[1].State = api.ShareDeleting
	del.Shares[1].DestroyAfter = ts(t0.Add(-time.Hour))
	e.nfs.ReloadFail["rf-t12-nfs"] = errors.New("container paused")
	expect(t, e.apply(withGrace(cfgOf("v2", del))), []string{
		`ceph user caps client.rf-t12 mon="allow r" mds="allow rw path=/volumes/rackflow/t12-s5/uuid-t12-s5" osd="allow rw pool=ec namespace=fsvolumens_t12-s5"`,
	})
	if s := tenantStatus(t, e.status(), 12); s.State != api.StateError || !strings.Contains(*s.Error, "container paused") {
		t.Fatalf("status: %+v", s)
	}
	if !e.ceph.HasSubvolume("t12-s6") {
		t.Fatal("subvolume destroyed while Ganesha may still export it")
	}

	delete(e.nfs.ReloadFail, "rf-t12-nfs")
	e.now = t0.Add(time.Minute)
	expect(t, e.apply(withGrace(cfgOf("v2", del))), []string{"nfs reload rf-t12-nfs", "ceph subvolume rm t12-s6"})
	expect(t, e.apply(withGrace(cfgOf("v2", del))), nil)
}

func TestExportIDKeptWhileDeleting(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", twoShares()))
	del := twoShares()
	del.Shares[1].State = api.ShareDeleting
	e.apply(cfgOf("v2", del))
	e.apply(cfgOf("v3", twoShares()))
	if conf := e.ganeshaConf("t12"); !strings.Contains(conf, "Export_Id = 2;\n\tPath = \"/volumes/rackflow/t12-s6") {
		t.Fatalf("export id changed after deleting and back:\n%s", conf)
	}
}

func TestHugeGraceNeverDestroys(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	cfg := cfgOf("v2", terminated(t0, ts(t0)))
	huge := int64(9_300_000_000) // overflows time.Duration when multiplied by time.Second
	cfg.Settings.DestroyGraceSeconds = &huge
	e.now = t0.Add(365 * 24 * time.Hour)
	expect(t, e.apply(cfg), []string{"nfs rm rf-t12-nfs", "net remove rf-t12", fwNone})
	st := e.status()
	if !e.ceph.HasSubvolume("t12-s5") || st.Node.Error == nil || !strings.Contains(*st.Node.Error, "too large") {
		t.Fatalf("huge grace: node %+v", st.Node)
	}
}

func TestNegativeGraceNeverDestroys(t *testing.T) {
	e := newEnv(t)
	e.apply(cfgOf("v1", basic()))
	cfg := cfgOf("v2", terminated(t0, ts(t0)))
	neg := int64(-1)
	cfg.Settings.DestroyGraceSeconds = &neg
	e.now = t0.Add(365 * 24 * time.Hour)
	expect(t, e.apply(cfg), []string{"nfs rm rf-t12-nfs", "net remove rf-t12", fwNone})
	if !e.ceph.HasSubvolume("t12-s5") {
		t.Fatal("destroyed with a negative grace")
	}
}
