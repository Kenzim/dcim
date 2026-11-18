// Package firewall owns one nftables table, inet rf-storage. Tenant veths may
// reach the Ceph public networks on the Ceph ports only: locally delivered
// when the address is one of the host's own (input hook), forwarded and
// masqueraded when it is a remote Ceph node (forward and postrouting hooks).
// Every other packet from or to a tenant veth is dropped.
package firewall

import (
	"context"
	"fmt"
	"net/netip"
	"sort"
	"strings"

	"rackflow/storage_runner/internal/execx"
)

// Table is the only nftables table the runner touches.
const Table = "rf-storage"

// VethPattern matches the host end of every tenant veth.
const VethPattern = "rf-h*"

// CephPorts are the TCP ports tenants may reach: mon v2 and v1, then the
// OSD/MDS/MGR range.
var CephPorts = []string{"3300", "6789", "6800-7568"}

type Ruleset struct {
	Links    []string // host veth names of tenants allowed to reach Ceph
	CephNets []netip.Prefix
	LinkCIDR netip.Prefix
}

func (r Ruleset) normalised() Ruleset {
	out := Ruleset{LinkCIDR: r.LinkCIDR}
	out.Links = append([]string(nil), r.Links...)
	sort.Strings(out.Links)
	out.CephNets = append([]netip.Prefix(nil), r.CephNets...)
	sort.Slice(out.CephNets, func(i, j int) bool { return out.CephNets[i].String() < out.CephNets[j].String() })
	return out
}

func (r Ruleset) String() string {
	n := r.normalised()
	nets := make([]string, len(n.CephNets))
	for i, p := range n.CephNets {
		nets[i] = p.String()
	}
	return fmt.Sprintf("links=[%s] ceph=[%s]", strings.Join(n.Links, ","), strings.Join(nets, ","))
}

// Match is the condition of one rule; zero fields match anything.
type Match struct {
	IifAllowed  bool // iifname @tenant_links
	OifAllowed  bool // oifname @tenant_links
	IifTenant   bool // iifname "rf-h*"
	OifTenant   bool // oifname "rf-h*"
	SrcLinkCIDR bool // ip saddr LINK_CIDR
	DstCeph     bool // ip daddr @ceph_nets
	CephPort    bool // tcp dport { Ceph ports }
	Established bool // ct state established,related
}

type Rule struct {
	Match
	Verdict string // accept, drop or masquerade
}

type Chain struct {
	Name  string
	Type  string
	Hook  string
	Prio  string
	Rules []Rule
}

// Chains is the ruleset as data; Render and the tests both read it.
func Chains() []Chain {
	cephAccept := Rule{Match{IifAllowed: true, DstCeph: true, CephPort: true}, "accept"}
	return []Chain{
		{Name: "input", Type: "filter", Hook: "input", Prio: "filter - 10", Rules: []Rule{
			cephAccept,
			{Match{IifTenant: true}, "drop"},
		}},
		{Name: "forward", Type: "filter", Hook: "forward", Prio: "filter - 10", Rules: []Rule{
			cephAccept,
			{Match{OifAllowed: true, Established: true}, "accept"},
			{Match{IifTenant: true}, "drop"},
			{Match{OifTenant: true}, "drop"},
		}},
		{Name: "postrouting", Type: "nat", Hook: "postrouting", Prio: "srcnat", Rules: []Rule{
			{Match{SrcLinkCIDR: true, DstCeph: true}, "masquerade"},
		}},
	}
}

func (m Match) render(rs Ruleset) string {
	var p []string
	if m.IifAllowed {
		p = append(p, "iifname @tenant_links")
	}
	if m.OifAllowed {
		p = append(p, "oifname @tenant_links")
	}
	if m.IifTenant {
		p = append(p, fmt.Sprintf("iifname %q", VethPattern))
	}
	if m.OifTenant {
		p = append(p, fmt.Sprintf("oifname %q", VethPattern))
	}
	if m.SrcLinkCIDR {
		p = append(p, "ip saddr "+rs.LinkCIDR.String())
	}
	if m.DstCeph {
		p = append(p, "ip daddr @ceph_nets")
	}
	if m.CephPort {
		p = append(p, "tcp dport { "+strings.Join(CephPorts, ", ")+" }")
	}
	if m.Established {
		p = append(p, "ct state established,related")
	}
	return strings.Join(p, " ")
}

// Render returns an nft script that replaces the runner's table in one
// transaction (the empty "table" line makes the delete safe on first run).
func Render(rs Ruleset) string {
	rs = rs.normalised()
	var b strings.Builder
	fmt.Fprintf(&b, "table inet %s\ndelete table inet %s\ntable inet %s {\n", Table, Table, Table)
	b.WriteString("\tset tenant_links {\n\t\ttype ifname\n")
	if len(rs.Links) > 0 {
		q := make([]string, len(rs.Links))
		for i, l := range rs.Links {
			q[i] = fmt.Sprintf("%q", l)
		}
		fmt.Fprintf(&b, "\t\telements = { %s }\n", strings.Join(q, ", "))
	}
	b.WriteString("\t}\n")
	b.WriteString("\tset ceph_nets {\n\t\ttype ipv4_addr\n\t\tflags interval\n")
	if len(rs.CephNets) > 0 {
		q := make([]string, len(rs.CephNets))
		for i, n := range rs.CephNets {
			q[i] = n.String()
		}
		fmt.Fprintf(&b, "\t\telements = { %s }\n", strings.Join(q, ", "))
	}
	b.WriteString("\t}\n")
	for _, c := range Chains() {
		fmt.Fprintf(&b, "\tchain %s {\n\t\ttype %s hook %s priority %s; policy accept;\n", c.Name, c.Type, c.Hook, c.Prio)
		for _, r := range c.Rules {
			fmt.Fprintf(&b, "\t\t%s %s\n", r.render(rs), r.Verdict)
		}
		b.WriteString("\t}\n")
	}
	b.WriteString("}\n")
	return b.String()
}

type Firewall interface {
	// Apply replaces the runner's table if it differs; changed reports whether it did.
	Apply(ctx context.Context, rs Ruleset) (changed bool, err error)
}

// NFT applies the ruleset with the nft command.
type NFT struct {
	Cmd  execx.Commander
	last string
}

func (n *NFT) Apply(ctx context.Context, rs Ruleset) (bool, error) {
	text := Render(rs)
	if text == n.last {
		if _, err := n.Cmd.Run(ctx, nil, "nft", "list", "table", "inet", Table); err == nil {
			return false, nil
		}
	}
	if _, err := n.Cmd.Run(ctx, []byte(text), "nft", "-f", "-"); err != nil {
		return false, err
	}
	n.last = text
	return true, nil
}
