package reconcile

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
)

// scopedEnv is an env whose node key is a scoped one: the fake answers
// Users only for the entities asked for, as auth get does.
func scopedEnv(t *testing.T) *env {
	e := newEnv(t)
	e.ceph.Scoped = true
	e.r.Host.CephScopedKey = true
	return e
}

func TestScopedKeyGrantsThroughMgr(t *testing.T) {
	e := scopedEnv(t)
	got := e.apply(cfgOf("v1", basic()))
	if contains(got, "ceph user create") || contains(got, "ceph user caps") {
		t.Fatalf("scoped key wrote caps itself:\n  %s", strings.Join(got, "\n  "))
	}
	if !contains(got, "ceph authorize client.rf-t12 t12-s5") || !contains(got, run12) {
		t.Fatalf("actions:\n  %s", strings.Join(got, "\n  "))
	}
	if u, ok := e.ceph.User("client.rf-t12"); !ok || u.Caps.MDS != "allow rw path=/volumes/rackflow/t12-s5/uuid-t12-s5" {
		t.Fatalf("user %+v %v", u, ok)
	}
	if ts := tenantStatus(t, e.status(), 12); ts.State != api.StateReady {
		t.Fatalf("tenant status %+v", ts)
	}
	b, err := os.ReadFile(filepath.Join(e.dir, "tenants", "t12", "ceph", "ceph.client.rf-t12.keyring"))
	if err != nil || !strings.Contains(string(b), "key = KEY") {
		t.Fatalf("keyring %q err %v", b, err)
	}
	// A second pass reads the user back and changes nothing.
	e.restart()
	e.r.Host.CephScopedKey = true
	expect(t, e.apply(cfgOf("v1", basic())), nil)
}

func TestScopedKeyAdoptsAndRevokesRunnerMadeUser(t *testing.T) {
	e := scopedEnv(t)
	two := tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5"), share(6, "logs", 10*gib, "10.0.0.6"))
	e.ceph.SetGroup("ec")
	e.ceph.SeedSubvolume("t12-s5", 100*gib)
	e.ceph.SeedSubvolume("t12-s6", 10*gib)
	// Made by an admin-key runner before the switch: the mgr has no record.
	e.ceph.SeedUser("client.rf-t12", ceph.TenantCaps([]ceph.Access{
		{Path: "/volumes/rackflow/t12-s5/uuid-t12-s5", Pool: "ec", Namespace: "fsvolumens_t12-s5"},
		{Path: "/volumes/rackflow/t12-s6/uuid-t12-s6", Pool: "ec", Namespace: "fsvolumens_t12-s6"},
	}))
	got := e.apply(cfgOf("v1", two))
	if contains(got, "ceph authorize") || contains(got, "ceph user") {
		t.Fatalf("user already has every path, yet:\n  %s", strings.Join(got, "\n  "))
	}

	del := tenant(12, 1234, share(5, "data", 100*gib, "10.0.0.5"), share(6, "logs", 10*gib, "10.0.0.6"))
	del.Shares[1].State = api.ShareDeleting
	del.Shares[1].DestroyAfter = ts(t0)
	e.now = t0.Add(time.Minute)
	got = e.apply(cfgOf("v2", del))
	want := []string{"ceph authorize client.rf-t12 t12-s6", "ceph deauthorize client.rf-t12 t12-s6"}
	if !strings.Contains(strings.Join(got, "\n"), strings.Join(want, "\n")) {
		t.Fatalf("revoke without a mgr record:\n  %s", strings.Join(got, "\n  "))
	}
	u, _ := e.ceph.User("client.rf-t12")
	if strings.Contains(u.Caps.MDS, "t12-s6") || strings.Contains(u.Caps.OSD, "t12-s6") || !strings.Contains(u.Caps.MDS, "t12-s5") {
		t.Fatalf("caps after revoke %+v", u.Caps)
	}
	expect(t, e.apply(cfgOf("v2", del)), nil)
}

func TestScopedKeyStillDestroysUser(t *testing.T) {
	e := scopedEnv(t)
	e.apply(cfgOf("v1", basic()))
	term := terminated(t0, ts(t0.Add(week)))
	e.now = t0.Add(week + time.Hour)
	got := e.apply(cfgOf("v2", term))
	if !contains(got, "ceph subvolume rm t12-s5") || !contains(got, "ceph user rm client.rf-t12") {
		t.Fatalf("actions:\n  %s", strings.Join(got, "\n  "))
	}
	if _, ok := e.ceph.User("client.rf-t12"); ok {
		t.Fatal("user left behind")
	}
}

func TestScopedKeyHAUsesRecoveryLogin(t *testing.T) {
	c := newHACluster(t)
	c.ceph.Scoped = true
	n1 := c.add(1)
	recKeyring := filepath.Join(n1.dir, "recovery.keyring")
	if err := os.WriteFile(recKeyring, []byte("[client.rf-recovery]\n\tkey = RECKEY==\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	n1.r.Host.CephScopedKey = true
	n1.r.Host.CephRecoveryName = "client.rf-recovery"
	n1.r.Host.CephRecoveryKeyring = recKeyring
	n1.applyCfg(haCfgFor(n1, "v1", []int64{1, 2}, haTenant(n1)))

	cephActions := c.cephRec.Take()
	if contains(cephActions, "ceph user create") || contains(cephActions, "rackflow_ha") {
		t.Fatalf("tenant user got HA pool caps:\n  %s", strings.Join(cephActions, "\n  "))
	}
	conf := n1.ganeshaConf("t12")
	for _, want := range []string{`userid = "rf-recovery";`, `pool = "rackflow_ha";`, `namespace = "rf-t12";`, `User_Id = "rf-t12";`} {
		if !strings.Contains(conf, want) {
			t.Errorf("ganesha.conf lacks %q:\n%s", want, conf)
		}
	}
	b, err := os.ReadFile(filepath.Join(n1.dir, "tenants", "t12", "ceph", "ceph.client.rf-recovery.keyring"))
	if err != nil || string(b) != "[client.rf-recovery]\n\tkey = RECKEY==\n" {
		t.Fatalf("recovery keyring %q err %v", b, err)
	}
	if !n1.serving(12) {
		t.Fatal("not serving")
	}
}
