package netns

import (
	"context"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
)

const (
	hostFresh = `[{"ifname":"lo","flags":["LOOPBACK","UP"],"addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8}]},
{"ifname":"eth1","flags":["BROADCAST","UP"],"addr_info":[]}]`
	nsFresh   = `[{"ifname":"lo","flags":["LOOPBACK"],"addr_info":[]}]`
	hostReady = `[{"ifname":"eth1","flags":["UP"]},
{"ifname":"rf-h12","flags":["UP"],"linkinfo":{"info_kind":"veth"},"addr_info":[{"family":"inet","local":"169.254.128.1","prefixlen":30},{"family":"inet6","local":"fe80::1","prefixlen":64}]}]`
	nsReady = `[{"ifname":"lo","flags":["LOOPBACK","UP"]},
{"ifname":"rf-v12","flags":["UP"],"linkinfo":{"info_kind":"vlan","info_data":{"protocol":"802.1Q","id":1234}},"addr_info":[{"family":"inet","local":"10.0.0.254","prefixlen":24}]},
{"ifname":"rf-n12","flags":["UP"],"linkinfo":{"info_kind":"veth"},"addr_info":[{"family":"inet","local":"169.254.128.2","prefixlen":30}]}]`
	routesReady = `[{"dst":"10.0.0.0/24","dev":"rf-v12","protocol":"kernel","scope":"link"},
{"dst":"169.254.128.0/30","dev":"rf-n12","protocol":"kernel","scope":"link"},
{"dst":"10.10.0.0/24","gateway":"169.254.128.1","dev":"rf-n12"}]`
)

func spec() Spec {
	return Spec{
		TenantID: 12,
		VLAN:     1234,
		Address:  netip.MustParsePrefix("10.0.0.254/24"),
		HostAddr: netip.MustParsePrefix("169.254.128.1/30"),
		NSAddr:   netip.MustParsePrefix("169.254.128.2/30"),
		Routes:   []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24")},
	}
}

func expectLines(t *testing.T, got, want []string) {
	t.Helper()
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("commands differ\n got:\n  %s\nwant:\n  %s", strings.Join(got, "\n  "), strings.Join(want, "\n  "))
	}
}

func TestEnsureFromScratch(t *testing.T) {
	s := execx.NewScript().
		On("ip -d -j addr show", hostFresh).
		On("ip -n rf-t12 -d -j addr show", nsFresh).
		On("ip -n rf-t12 -j route show", "[]")
	m := &IPRoute{Cmd: s, ParentIface: "eth1"}
	changed, err := m.Ensure(context.Background(), spec())
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	expectLines(t, s.Lines(), []string{
		"ip netns list",
		"ip netns add rf-t12",
		"ip -d -j addr show",
		"ip -n rf-t12 -d -j addr show",
		"ip -n rf-t12 link set lo up",
		"ip link add link eth1 name rf-v12 type vlan id 1234",
		"ip link set rf-v12 netns rf-t12",
		"ip -n rf-t12 addr add 10.0.0.254/24 dev rf-v12",
		"ip -n rf-t12 link set rf-v12 up",
		"ip link add rf-h12 type veth peer name rf-n12 netns rf-t12",
		"ip addr add 169.254.128.1/30 dev rf-h12",
		"ip link set rf-h12 up",
		"ip -n rf-t12 addr add 169.254.128.2/30 dev rf-n12",
		"ip -n rf-t12 link set rf-n12 up",
		"ip -n rf-t12 -j route show",
		"ip -n rf-t12 route replace 10.10.0.0/24 via 169.254.128.1 dev rf-n12",
	})
}

func TestEnsureConvergedOnlyReads(t *testing.T) {
	s := execx.NewScript().
		On("ip netns list", "rf-t12 (id: 0)\nother\n").
		On("ip -d -j addr show", hostReady).
		On("ip -n rf-t12 -d -j addr show", nsReady).
		On("ip -n rf-t12 -j route show", routesReady)
	m := &IPRoute{Cmd: s, ParentIface: "eth1"}
	changed, err := m.Ensure(context.Background(), spec())
	if err != nil || changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	expectLines(t, s.Lines(), []string{
		"ip netns list",
		"ip -d -j addr show",
		"ip -n rf-t12 -d -j addr show",
		"ip -n rf-t12 -j route show",
	})
}

func TestEnsureRemovesForeignRoutesAndFixesDrift(t *testing.T) {
	nsDrift := strings.Replace(nsReady, `"id":1234`, `"id":999`, 1)
	routes := strings.TrimSuffix(routesReady, "]") +
		`,{"dst":"default","gateway":"169.254.128.1","dev":"rf-n12"},{"dst":"192.168.0.1","gateway":"169.254.128.1","dev":"rf-n12"}]`
	s := execx.NewScript().
		On("ip netns list", "rf-t12\n").
		On("ip -d -j addr show", hostReady).
		On("ip -n rf-t12 -d -j addr show", nsDrift).
		On("ip -n rf-t12 -j route show", routes)
	m := &IPRoute{Cmd: s, ParentIface: "bond0"}
	if _, err := m.Ensure(context.Background(), spec()); err != nil {
		t.Fatal(err)
	}
	expectLines(t, s.Lines(), []string{
		"ip netns list",
		"ip -d -j addr show",
		"ip -n rf-t12 -d -j addr show",
		"ip -n rf-t12 link del rf-v12",
		"ip link add link bond0 name rf-v12 type vlan id 1234",
		"ip link set rf-v12 netns rf-t12",
		"ip -n rf-t12 addr add 10.0.0.254/24 dev rf-v12",
		"ip -n rf-t12 link set rf-v12 up",
		"ip -n rf-t12 -j route show",
		"ip -n rf-t12 route del 0.0.0.0/0 dev rf-n12",
		"ip -n rf-t12 route del 192.168.0.1/32 dev rf-n12",
	})
}

func TestEnsureReplacesEndpointAddress(t *testing.T) {
	sp := spec()
	sp.Address = netip.MustParsePrefix("10.0.0.253/24")
	s := execx.NewScript().
		On("ip netns list", "rf-t12\n").
		On("ip -d -j addr show", hostReady).
		On("ip -n rf-t12 -d -j addr show", nsReady).
		On("ip -n rf-t12 -j route show", routesReady)
	m := &IPRoute{Cmd: s, ParentIface: "eth1"}
	if _, err := m.Ensure(context.Background(), sp); err != nil {
		t.Fatal(err)
	}
	lines := s.Lines()
	expectLines(t, lines[3:5], []string{
		"ip -n rf-t12 addr del 10.0.0.254/24 dev rf-v12",
		"ip -n rf-t12 addr add 10.0.0.253/24 dev rf-v12",
	})
}

func TestRemove(t *testing.T) {
	s := execx.NewScript().
		On("ip netns list", "rf-t12\n").
		On("ip -d -j addr show", `[{"ifname":"rf-h12"},{"ifname":"rf-v12"},{"ifname":"rf-h13"}]`)
	m := &IPRoute{Cmd: s, ParentIface: "eth1"}
	if err := m.Remove(context.Background(), 12); err != nil {
		t.Fatal(err)
	}
	expectLines(t, s.Lines(), []string{
		"ip netns list",
		"ip -d -j addr show",
		"ip link del rf-h12",
		"ip netns del rf-t12",
		"ip link del rf-v12",
	})
}

func TestTenantsIgnoresForeignNamespaces(t *testing.T) {
	s := execx.NewScript().On("ip netns list", "rf-t12 (id: 0)\nrf-t7\ncni-123\nrf-tx\nrf-t012\n")
	got, err := (&IPRoute{Cmd: s}).Tenants(context.Background())
	if err != nil || len(got) != 2 || got[0] != 7 || got[1] != 12 {
		t.Fatalf("got %v err %v", got, err)
	}
}

func TestCheck(t *testing.T) {
	proc := t.TempDir()
	if err := os.MkdirAll(filepath.Join(proc, "sys/net/ipv4"), 0o755); err != nil {
		t.Fatal(err)
	}
	fwd := filepath.Join(proc, "sys/net/ipv4/ip_forward")
	s := execx.NewScript().On("ip -d -j addr show", hostFresh)
	m := &IPRoute{Cmd: s, ParentIface: "eth1", ProcRoot: proc}

	_ = os.WriteFile(fwd, []byte("0\n"), 0o644)
	if err := m.Check(context.Background()); err == nil || !strings.Contains(err.Error(), "ip_forward") {
		t.Fatalf("expected ip_forward error, got %v", err)
	}
	_ = os.WriteFile(fwd, []byte("1\n"), 0o644)
	if err := m.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	m.ParentIface = "bond0"
	if err := m.Check(context.Background()); err == nil {
		t.Fatal("expected missing parent interface error")
	}
}
