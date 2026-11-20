//go:build sandbox

// Runs the real IPRoute driver against the kernel. Only run it inside a
// throwaway namespace, never on a real host:
//
//	unshare -rnm sh -c 'mount -t tmpfs none /run && go test -tags sandbox ./internal/netns/'
package netns

import (
	"context"
	"net/netip"
	"os/exec"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
)

func sh(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		t.Fatalf("%v: %v\n%s", args, err, out)
	}
	return string(out)
}

func TestSandboxEnsureAndRemove(t *testing.T) {
	sh(t, "ip", "link", "add", "trunk0", "type", "dummy")
	sh(t, "ip", "link", "set", "trunk0", "up")
	m := &IPRoute{Cmd: execx.Exec{}, ParentIface: "trunk0"}
	ctx := context.Background()

	if changed, err := m.Ensure(ctx, spec()); err != nil || !changed {
		t.Fatalf("first ensure changed=%v err=%v", changed, err)
	}
	if changed, err := m.Ensure(ctx, spec()); err != nil || changed {
		t.Fatalf("second ensure changed=%v err=%v", changed, err)
	}
	inner := sh(t, "ip", "-n", "rf-t12", "-d", "addr", "show")
	for _, want := range []string{"rf-v12@", "vlan protocol 802.1Q id 1234", "10.0.0.254/24", "rf-n12@", "169.254.128.2/30"} {
		if !strings.Contains(inner, want) {
			t.Errorf("namespace lacks %q:\n%s", want, inner)
		}
	}
	routes := sh(t, "ip", "-n", "rf-t12", "route", "show")
	if !strings.Contains(routes, "10.10.0.0/24 via 169.254.128.1 dev rf-n12") || strings.Contains(routes, "default") {
		t.Errorf("routes:\n%s", routes)
	}

	sh(t, "ip", "-n", "rf-t12", "route", "add", "default", "via", "169.254.128.1")
	other := spec()
	other.VLAN = 1300
	if changed, err := m.Ensure(ctx, other); err != nil || !changed {
		t.Fatalf("drift ensure changed=%v err=%v", changed, err)
	}
	if routes := sh(t, "ip", "-n", "rf-t12", "route", "show"); strings.Contains(routes, "default") {
		t.Errorf("default route kept:\n%s", routes)
	}
	if inner := sh(t, "ip", "-n", "rf-t12", "-d", "link", "show", "rf-v12"); !strings.Contains(inner, "id 1300") {
		t.Errorf("vlan not changed:\n%s", inner)
	}

	ids, err := m.Tenants(ctx)
	if err != nil || len(ids) != 1 || ids[0] != 12 {
		t.Fatalf("tenants %v err %v", ids, err)
	}
	if err := m.Remove(ctx, 12); err != nil {
		t.Fatal(err)
	}
	if out := sh(t, "ip", "link", "show"); strings.Contains(out, "rf-") {
		t.Errorf("leftover links:\n%s", out)
	}
	if out := sh(t, "ip", "netns", "list"); strings.Contains(out, "rf-t12") {
		t.Errorf("namespace left: %s", out)
	}
}

func TestSandboxPresenceEnsureAndRemove(t *testing.T) {
	sh(t, "ip", "link", "add", "trunk1", "type", "dummy")
	sh(t, "ip", "link", "set", "trunk1", "up")
	m := &IPRoute{Cmd: execx.Exec{}, ParentIface: "trunk1"}
	ctx := context.Background()
	spec := PresenceSpec{NetworkID: 6, VLAN: 3921, Address: netip.MustParsePrefix("10.0.0.254/24"), MAC: PresenceMAC(6)}

	if changed, err := m.EnsurePresence(ctx, spec); err != nil || !changed {
		t.Fatalf("first ensure changed=%v err=%v", changed, err)
	}
	if changed, err := m.EnsurePresence(ctx, spec); err != nil || changed {
		t.Fatalf("second ensure changed=%v err=%v", changed, err)
	}
	inner := sh(t, "ip", "-n", "rf-p6", "-d", "addr", "show")
	for _, want := range []string{"rf-w6@", "vlan protocol 802.1Q id 3921", "10.0.0.254/24", "02:52:4e:00:00:06"} {
		if !strings.Contains(inner, want) {
			t.Errorf("namespace lacks %q:\n%s", want, inner)
		}
	}
	if routes := sh(t, "ip", "-n", "rf-p6", "route", "show"); strings.TrimSpace(routes) != "10.0.0.0/24 dev rf-w6 proto kernel scope link src 10.0.0.254" {
		t.Errorf("routes:\n%s", routes)
	}
	if err := m.PresenceLinkDown(ctx, 6); err != nil {
		t.Fatal(err)
	}
	if changed, err := m.EnsurePresence(ctx, spec); err != nil || !changed {
		t.Fatalf("ensure after link down changed=%v err=%v", changed, err)
	}
	if ids, err := m.Presences(ctx); err != nil || len(ids) != 1 || ids[0] != 6 {
		t.Fatalf("presences %v err %v", ids, err)
	}
	if err := m.RemovePresence(ctx, 6); err != nil {
		t.Fatal(err)
	}
	if out := sh(t, "ip", "netns", "list"); strings.Contains(out, "rf-p6") {
		t.Errorf("namespace left: %s", out)
	}
	if out := sh(t, "ip", "link", "show"); strings.Contains(out, "rf-") {
		t.Errorf("leftover links:\n%s", out)
	}
}
