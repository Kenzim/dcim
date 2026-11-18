package names

import "testing"

func TestNamesFitInterfaceLimit(t *testing.T) {
	for _, n := range []string{VLANIface(MaxTenantID), HostVeth(MaxTenantID), NSVeth(MaxTenantID)} {
		if len(n) > 15 {
			t.Errorf("%s is longer than 15 characters", n)
		}
	}
}

func TestParsersOnlyMatchRunnerNames(t *testing.T) {
	for s, want := range map[string]int64{"rf-t12": 12, "rf-t1": 1, "rf-t012": 0, "rf-t": 0, "rf-t12x": 0, "cni-1": 0, "rf-t12-nfs": 0} {
		got, ok := ParseNamespace(s)
		if (want != 0) != ok || got != want {
			t.Errorf("ParseNamespace(%q) = %d %v", s, got, ok)
		}
	}
	for s, want := range map[string]int64{"client.rf-t12": 12, "client.admin": 0, "client.rf-t12x": 0, "client.rf-tenant": 0} {
		got, ok := ParseCephEntity(s)
		if (want != 0) != ok || got != want {
			t.Errorf("ParseCephEntity(%q) = %d %v", s, got, ok)
		}
	}
	for s, want := range map[string]int64{"rf-t12-nfs": 12, "rf-t12": 0, "rf-t12-nfs2": 0} {
		got, ok := ParseContainer(s)
		if (want != 0) != ok || got != want {
			t.Errorf("ParseContainer(%q) = %d %v", s, got, ok)
		}
	}
	if tn, sh, ok := ParseSubvolume("t12-s5"); !ok || tn != 12 || sh != 5 {
		t.Errorf("ParseSubvolume(t12-s5) = %d %d %v", tn, sh, ok)
	}
	for _, s := range []string{"legacy", "t12", "t12-s", "t0-s1", "t12-s5-old", "csi-vol-1"} {
		if _, _, ok := ParseSubvolume(s); ok {
			t.Errorf("ParseSubvolume(%q) matched", s)
		}
	}
}
