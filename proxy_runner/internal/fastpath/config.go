// Package fastpath serves the proxy fast path: one network namespace per
// client access network, holding that client's VLAN interface and the private
// IPs of the proxy subnets this runner hosts, with a proxy listener inside.
package fastpath

import (
	"fmt"
	"net"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Config is the fast_path part of the runner config.
type Config struct {
	CIDR    string   `json:"cidr"`
	Port    int      `json:"port"`
	Tenants []Tenant `json:"tenants"`
}

// Tenant is one client's access network.
type Tenant struct {
	NetworkID   int          `json:"network_id"`
	VLAN        int          `json:"vlan"`
	Addresses   []string     `json:"addresses"`
	Assignments []Assignment `json:"assignments"`
}

// Assignment is one proxy IP of the client, reachable on a private IP.
type Assignment struct {
	ServiceID int    `json:"service_id"`
	PrivateIP string `json:"private_ip"`
	BindIP    string `json:"bind_ip"`
	Username  string `json:"username"`
	Password  string `json:"password"`
}

// TableName is the nftables table the runner owns inside each namespace.
const TableName = "rf-proxy-fast"

var nsPattern = regexp.MustCompile(`^pf-n([1-9][0-9]{0,10})$`)

// NamespaceName is the namespace of a tenant.
func NamespaceName(networkID int) string { return fmt.Sprintf("pf-n%d", networkID) }

// LinkName is the VLAN interface of a tenant, inside its namespace.
func LinkName(networkID int) string { return fmt.Sprintf("pf-v%d", networkID) }

// ParseNamespace returns the network ID of a namespace the runner owns.
func ParseNamespace(name string) (int, bool) {
	m := nsPattern.FindStringSubmatch(name)
	if m == nil {
		return 0, false
	}
	id, err := strconv.Atoi(m[1])
	return id, err == nil
}

// prefixLen is the prefix length the tenant addresses are configured with.
func (c Config) prefixLen() (int, *net.IPNet, error) {
	_, network, err := net.ParseCIDR(c.CIDR)
	if err != nil || network.IP.To4() == nil {
		return 0, nil, fmt.Errorf("fast path cidr %q is not an IPv4 network", c.CIDR)
	}
	ones, _ := network.Mask.Size()
	return ones, network, nil
}

// validate checks one tenant and returns its addresses as sorted CIDR strings.
func (c Config) validate(t Tenant) ([]string, error) {
	if t.NetworkID <= 0 {
		return nil, fmt.Errorf("tenant network_id %d is not valid", t.NetworkID)
	}
	if t.VLAN < 1 || t.VLAN > 4094 {
		return nil, fmt.Errorf("tenant %d: vlan %d is out of range", t.NetworkID, t.VLAN)
	}
	ones, network, err := c.prefixLen()
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(t.Addresses))
	for _, raw := range t.Addresses {
		ip := net.ParseIP(raw).To4()
		if ip == nil || !network.Contains(ip) {
			return nil, fmt.Errorf("tenant %d: address %q is not in %s", t.NetworkID, raw, c.CIDR)
		}
		out = append(out, fmt.Sprintf("%s/%d", ip, ones))
	}
	sort.Strings(out)
	return out, nil
}

// RenderRuleset is the nftables ruleset applied inside a tenant namespace:
// only new TCP connections to the proxy port are accepted, replies to them
// pass, and nothing is forwarded. Declaring then deleting the table first
// makes the replacement atomic and leaves other tables alone.
func RenderRuleset(port int) string {
	var b strings.Builder
	fmt.Fprintf(&b, "table inet %s {}\n", TableName)
	fmt.Fprintf(&b, "delete table inet %s\n", TableName)
	fmt.Fprintf(&b, "table inet %s {\n", TableName)
	b.WriteString("\tchain input {\n")
	b.WriteString("\t\ttype filter hook input priority 0; policy drop;\n")
	b.WriteString("\t\tiif \"lo\" accept\n")
	b.WriteString("\t\tct state established,related accept\n")
	fmt.Fprintf(&b, "\t\ttcp dport %d ct state new accept\n", port)
	b.WriteString("\t}\n")
	b.WriteString("\tchain forward {\n")
	b.WriteString("\t\ttype filter hook forward priority 0; policy drop;\n")
	b.WriteString("\t}\n")
	b.WriteString("}\n")
	return b.String()
}
