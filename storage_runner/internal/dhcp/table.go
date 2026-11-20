package dhcp

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"
	"strings"
	"time"

	"rackflow/storage_runner/internal/api"
)

// DefaultLeaseTime applies when the config sends no lease time.
const DefaultLeaseTime = 86400 * time.Second

// Route is one classless static route (option 121).
type Route struct {
	Dst netip.Prefix
	Gw  netip.Addr
}

// Table is one immutable snapshot of a DHCP config: who gets which address
// and which options are sent. A config change builds a new Table and the
// server swaps it in.
type Table struct {
	Enabled   bool
	ServerID  netip.Addr
	Prefix    netip.Prefix
	LeaseTime time.Duration

	reserved   map[string]netip.Addr
	reservedIP map[netip.Addr]string

	HasRange   bool
	RangeStart netip.Addr
	RangeEnd   netip.Addr

	Router netip.Addr
	DNS    []netip.Addr
	Domain string
	MTU    int
	Routes []Route
	NTP    []netip.Addr

	// Seed is the config's dynamic leases, read once when a server starts.
	Seed []api.DHCPLease
	// Problems lists config entries that were skipped.
	Problems []string
}

// NewTable builds a Table for a server answering as serverID on a network of
// prefixLen bits. Invalid entries are skipped and listed in Problems; only a
// bad server address is an error.
func NewTable(cfg *api.DHCP, serverID netip.Addr, prefixLen int) (*Table, error) {
	if !serverID.Is4() || prefixLen < 1 || prefixLen > 30 {
		return nil, fmt.Errorf("dhcp: bad server address %s/%d", serverID, prefixLen)
	}
	t := &Table{
		ServerID:   serverID,
		Prefix:     netip.PrefixFrom(serverID, prefixLen).Masked(),
		LeaseTime:  DefaultLeaseTime,
		reserved:   map[string]netip.Addr{},
		reservedIP: map[netip.Addr]string{},
	}
	if cfg == nil {
		return t, nil
	}
	t.Enabled = cfg.Enabled
	if cfg.LeaseSeconds > 0 {
		t.LeaseTime = time.Duration(cfg.LeaseSeconds) * time.Second
	}
	for _, r := range cfg.Reservations {
		mac, err := normMAC(r.MAC)
		if err != nil {
			t.problem("reservation %q: %v", r.MAC, err)
			continue
		}
		ip, err := netip.ParseAddr(r.IP)
		if err != nil || !t.usable(ip) {
			t.problem("reservation %s: address %q is not usable in %s", mac, r.IP, t.Prefix)
			continue
		}
		if other, dup := t.reservedIP[ip]; dup {
			t.problem("reservation %s: address %s is already reserved for %s", mac, ip, other)
			continue
		}
		if _, dup := t.reserved[mac]; dup {
			t.problem("reservation %s: listed twice", mac)
			continue
		}
		t.reserved[mac] = ip
		t.reservedIP[ip] = mac
	}
	if r := cfg.Range; r != nil {
		s, err1 := netip.ParseAddr(r.Start)
		e, err2 := netip.ParseAddr(r.End)
		switch {
		case err1 != nil || err2 != nil || !t.usable(s) || !t.usable(e) || e.Less(s):
			t.problem("range %s-%s is not usable in %s", r.Start, r.End, t.Prefix)
		default:
			t.HasRange, t.RangeStart, t.RangeEnd = true, s, e
		}
	}
	o := cfg.Options
	if o.Router != nil {
		if a, err := netip.ParseAddr(*o.Router); err == nil && t.usable(a) {
			t.Router = a
		} else {
			t.problem("router %q is not usable in %s", *o.Router, t.Prefix)
		}
	}
	t.DNS = t.addrList("dns server", o.DNSServers)
	t.NTP = t.addrList("ntp server", o.NTPServers)
	if o.Domain != nil {
		t.Domain = strings.TrimSpace(*o.Domain)
	}
	if o.MTU != nil {
		if *o.MTU >= 576 && *o.MTU <= 65535 {
			t.MTU = *o.MTU
		} else {
			t.problem("mtu %d out of range", *o.MTU)
		}
	}
	for _, r := range o.Routes {
		dst, err1 := netip.ParsePrefix(r.Destination)
		gw, err2 := netip.ParseAddr(r.Gateway)
		if err1 != nil || err2 != nil || !dst.Addr().Is4() || dst.Bits() == 0 || !t.usable(gw) {
			t.problem("route %s via %s is not usable", r.Destination, r.Gateway)
			continue
		}
		t.Routes = append(t.Routes, Route{Dst: dst.Masked(), Gw: gw})
	}
	t.Seed = cfg.Leases
	return t, nil
}

func (t *Table) problem(f string, a ...any) { t.Problems = append(t.Problems, fmt.Sprintf(f, a...)) }

func (t *Table) addrList(what string, in []string) []netip.Addr {
	var out []netip.Addr
	for _, s := range in {
		a, err := netip.ParseAddr(s)
		if err != nil || !a.Is4() || a.IsUnspecified() || a.IsLoopback() || a.IsMulticast() {
			t.problem("%s %q is not a unicast IPv4 address", what, s)
			continue
		}
		out = append(out, a)
	}
	return out
}

// usable reports whether a is a host address of the network other than the
// server's own.
func (t *Table) usable(a netip.Addr) bool {
	if !a.Is4() || !t.Prefix.Contains(a) || a == t.ServerID {
		return false
	}
	return a != t.Prefix.Addr() && a != broadcast(t.Prefix)
}

// Reserved returns the address reserved for mac.
func (t *Table) Reserved(mac string) (netip.Addr, bool) {
	a, ok := t.reserved[mac]
	return a, ok
}

// ReservedIP reports whether ip is reserved for any MAC.
func (t *Table) ReservedIP(ip netip.Addr) bool {
	_, ok := t.reservedIP[ip]
	return ok
}

// InRange reports whether ip is inside the dynamic range.
func (t *Table) InRange(ip netip.Addr) bool {
	return t.HasRange && ip.Is4() && !ip.Less(t.RangeStart) && !t.RangeEnd.Less(ip) && t.usable(ip)
}

// Mask is the subnet mask (option 1).
func (t *Table) Mask() []byte { return net.CIDRMask(t.Prefix.Bits(), 32) }

// options are the configured options after the message type and server id,
// in a fixed order. withLease adds the lease time and T1/T2.
func (t *Table) options(withLease bool) []Option {
	var o []Option
	if withLease {
		secs := uint32(t.LeaseTime / time.Second)
		o = append(o, Option{OptLeaseTime, u32(secs)}, Option{OptRenewalT1, u32(secs / 2)}, Option{OptRebindingT2, u32(uint32(uint64(secs) * 7 / 8))})
	}
	o = append(o, Option{OptSubnetMask, t.Mask()})
	if t.Router.IsValid() {
		o = append(o, Option{OptRouter, addrs(t.Router)})
	}
	if len(t.DNS) > 0 {
		o = append(o, Option{OptDNS, addrs(t.DNS...)})
	}
	if t.Domain != "" {
		o = append(o, Option{OptDomain, []byte(t.Domain)})
	}
	if t.MTU > 0 {
		o = append(o, Option{OptMTU, []byte{byte(t.MTU >> 8), byte(t.MTU)}})
	}
	if len(t.NTP) > 0 {
		o = append(o, Option{OptNTP, addrs(t.NTP...)})
	}
	if len(t.Routes) > 0 {
		var b []byte
		for _, r := range t.Routes {
			b = appendRoute(b, r.Dst, r.Gw)
		}
		// RFC 3442 clients ignore option 3 when option 121 is present.
		if t.Router.IsValid() {
			b = appendRoute(b, netip.PrefixFrom(netip.IPv4Unspecified(), 0), t.Router)
		}
		o = append(o, Option{OptClasslessRts, b})
	}
	return o
}

// appendRoute encodes one RFC 3442 route: the prefix width, the significant
// octets of the destination, then the gateway.
func appendRoute(b []byte, dst netip.Prefix, gw netip.Addr) []byte {
	b = append(b, byte(dst.Bits()))
	d := dst.Addr().As4()
	b = append(b, d[:(dst.Bits()+7)/8]...)
	g := gw.As4()
	return append(b, g[:]...)
}

func u32(v uint32) []byte { return binary.BigEndian.AppendUint32(nil, v) }

func addrs(a ...netip.Addr) []byte {
	var b []byte
	for _, x := range a {
		v := x.As4()
		b = append(b, v[:]...)
	}
	return b
}

func broadcast(p netip.Prefix) netip.Addr {
	v := binary.BigEndian.Uint32(p.Addr().AsSlice())
	v |= ^uint32(0) >> p.Bits()
	return fromU32(v)
}

func toU32(a netip.Addr) uint32 { return binary.BigEndian.Uint32(a.AsSlice()) }

func fromU32(v uint32) netip.Addr {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], v)
	return netip.AddrFrom4(b)
}

// normMAC returns a unicast six-octet MAC in lower case, colon separated.
func normMAC(s string) (string, error) {
	hw, err := net.ParseMAC(strings.TrimSpace(s))
	if err != nil || len(hw) != 6 {
		return "", fmt.Errorf("not a six-octet MAC")
	}
	return hw.String(), nil
}
