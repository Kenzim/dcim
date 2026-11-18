package firewall

import (
	"context"
	"errors"
	"net/netip"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
)

func ruleset() Ruleset {
	return Ruleset{
		Links:    []string{"rf-h13", "rf-h12"},
		CephNets: []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24"), netip.MustParsePrefix("10.20.0.0/24")},
		LinkCIDR: netip.MustParsePrefix("169.254.128.0/17"),
	}
}

func TestRender(t *testing.T) {
	want := `table inet rf-storage
delete table inet rf-storage
table inet rf-storage {
	set tenant_links {
		type ifname
		elements = { "rf-h12", "rf-h13" }
	}
	set ceph_nets {
		type ipv4_addr
		flags interval
		elements = { 10.10.0.0/24, 10.20.0.0/24 }
	}
	chain input {
		type filter hook input priority filter - 10; policy accept;
		iifname @tenant_links ip daddr @ceph_nets tcp dport { 3300, 6789, 6800-7568 } accept
		iifname "rf-h*" drop
	}
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname @tenant_links ip daddr @ceph_nets tcp dport { 3300, 6789, 6800-7568 } accept
		oifname @tenant_links ct state established,related accept
		iifname "rf-h*" drop
		oifname "rf-h*" drop
	}
	chain postrouting {
		type nat hook postrouting priority srcnat; policy accept;
		ip saddr 169.254.128.0/17 ip daddr @ceph_nets masquerade
	}
}
`
	if got := Render(ruleset()); got != want {
		t.Fatalf("render mismatch\n got:\n%s\nwant:\n%s", got, want)
	}
}

func TestRenderEmptySetsHaveNoElements(t *testing.T) {
	got := Render(Ruleset{CephNets: ruleset().CephNets, LinkCIDR: ruleset().LinkCIDR})
	if strings.Contains(got, `elements = { }`) || strings.Count(got, "elements") != 1 {
		t.Fatalf("empty tenant set rendered wrongly:\n%s", got)
	}
}

// packet is what the rules can see of an IPv4 packet at one hook.
type packet struct {
	iif, oif    string
	src, dst    string
	tcpPort     int // 0 for non-TCP
	established bool
}

func portAllowed(p int) bool {
	return p == 3300 || p == 6789 || (p >= 6800 && p <= 7568)
}

func (m Match) matches(rs Ruleset, p packet) bool {
	inSet := func(name string) bool {
		for _, l := range rs.Links {
			if l == name {
				return true
			}
		}
		return false
	}
	isTenant := func(name string) bool { return strings.HasPrefix(name, "rf-h") }
	dst := netip.MustParseAddr(p.dst)
	src := netip.MustParseAddr(p.src)
	inCeph := false
	for _, n := range rs.CephNets {
		if n.Contains(dst) {
			inCeph = true
		}
	}
	switch {
	case m.IifAllowed && !inSet(p.iif),
		m.OifAllowed && !inSet(p.oif),
		m.IifTenant && !isTenant(p.iif),
		m.OifTenant && !isTenant(p.oif),
		m.SrcLinkCIDR && !rs.LinkCIDR.Contains(src),
		m.DstCeph && !inCeph,
		m.CephPort && !portAllowed(p.tcpPort),
		m.Established && !p.established:
		return false
	}
	return true
}

// verdict evaluates the runner's chain for hook; the policy is accept.
func verdict(rs Ruleset, hook string, p packet) string {
	for _, c := range Chains() {
		if c.Hook != hook {
			continue
		}
		for _, r := range c.Rules {
			if r.matches(rs, p) {
				return r.Verdict
			}
		}
	}
	return "accept"
}

func TestLocalCephOnSameHost(t *testing.T) {
	rs := ruleset()
	// 10.10.0.5 is one of the host's own addresses: delivered through input,
	// never forwarded, so no masquerade applies.
	cases := []struct {
		name string
		p    packet
		want string
	}{
		{"mon v2", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 3300}, "accept"},
		{"mon v1", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 6789}, "accept"},
		{"osd", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 6801}, "accept"},
		{"osd range end", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 7568}, "accept"},
		{"ssh", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 22}, "drop"},
		{"above osd range", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5", tcpPort: 7569}, "drop"},
		{"udp", packet{iif: "rf-h12", src: "169.254.128.2", dst: "10.10.0.5"}, "drop"},
		{"veth host address", packet{iif: "rf-h12", src: "169.254.128.2", dst: "169.254.128.1", tcpPort: 3300}, "drop"},
		{"host address outside ceph", packet{iif: "rf-h12", src: "169.254.128.2", dst: "192.168.11.74", tcpPort: 22}, "drop"},
		{"tenant not allowed", packet{iif: "rf-h99", src: "169.254.128.10", dst: "10.10.0.5", tcpPort: 3300}, "drop"},
		{"other interfaces untouched", packet{iif: "eth0", src: "192.168.11.10", dst: "192.168.11.74", tcpPort: 22}, "accept"},
	}
	for _, c := range cases {
		if got := verdict(rs, "input", c.p); got != c.want {
			t.Errorf("%s: input verdict %s, want %s", c.name, got, c.want)
		}
	}
}

func TestRemoteCephForwarded(t *testing.T) {
	rs := ruleset()
	out := packet{iif: "rf-h12", oif: "eth1", src: "169.254.128.2", dst: "10.20.0.7", tcpPort: 6800}
	if got := verdict(rs, "forward", out); got != "accept" {
		t.Fatalf("forward to remote OSD: %s", got)
	}
	if got := verdict(rs, "postrouting", out); got != "masquerade" {
		t.Fatalf("postrouting to remote OSD: %s", got)
	}
	reply := packet{iif: "eth1", oif: "rf-h12", src: "10.20.0.7", dst: "169.254.128.2", tcpPort: 0, established: true}
	if got := verdict(rs, "forward", reply); got != "accept" {
		t.Fatalf("reply: %s", got)
	}
	cases := []struct {
		name string
		p    packet
	}{
		{"remote ssh", packet{iif: "rf-h12", oif: "eth1", src: "169.254.128.2", dst: "10.20.0.7", tcpPort: 22}},
		{"internet", packet{iif: "rf-h12", oif: "eth0", src: "169.254.128.2", dst: "1.1.1.1", tcpPort: 443}},
		{"tenant to tenant", packet{iif: "rf-h12", oif: "rf-h13", src: "169.254.128.2", dst: "169.254.128.6", tcpPort: 3300}},
		{"new inbound to tenant", packet{iif: "eth1", oif: "rf-h12", src: "10.20.0.7", dst: "169.254.128.2", tcpPort: 2049}},
		{"tenant not allowed", packet{iif: "rf-h99", oif: "eth1", src: "169.254.128.10", dst: "10.20.0.7", tcpPort: 6789}},
	}
	for _, c := range cases {
		if got := verdict(rs, "forward", c.p); got != "drop" {
			t.Errorf("%s: forward verdict %s, want drop", c.name, got)
		}
	}
	other := packet{iif: "eth0", oif: "eth1", src: "192.168.1.5", dst: "10.20.0.7", tcpPort: 22}
	if got := verdict(rs, "forward", other); got != "accept" {
		t.Errorf("non-tenant forwarding changed: %s", got)
	}
	if got := verdict(rs, "postrouting", other); got != "accept" {
		t.Errorf("non-tenant traffic masqueraded: %s", got)
	}
}

func TestNFTApply(t *testing.T) {
	s := execx.NewScript()
	n := &NFT{Cmd: s}
	ctx := context.Background()
	changed, err := n.Apply(ctx, ruleset())
	if err != nil || !changed {
		t.Fatalf("first apply changed=%v err=%v", changed, err)
	}
	calls := s.Calls()
	if len(calls) != 1 || calls[0].Line != "nft -f -" || calls[0].Stdin != Render(ruleset()) {
		t.Fatalf("calls: %+v", calls)
	}

	s.Reset()
	if changed, err := n.Apply(ctx, ruleset()); err != nil || changed {
		t.Fatalf("second apply changed=%v err=%v", changed, err)
	}
	if got := s.Lines(); len(got) != 1 || got[0] != "nft list table inet rf-storage" {
		t.Fatalf("second apply ran %v", got)
	}

	s.Reset()
	s.Fail("nft list table inet rf-storage", errors.New("No such file or directory"))
	if changed, err := n.Apply(ctx, ruleset()); err != nil || !changed {
		t.Fatalf("table flushed externally: changed=%v err=%v", changed, err)
	}
}
