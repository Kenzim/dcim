package dhcp

import (
	"bytes"
	"context"
	"errors"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
)

const (
	macA = "bc:24:11:00:00:01"
	macB = "bc:24:11:00:00:02"
	macC = "bc:24:11:00:00:03"
	macD = "bc:24:11:00:00:04"
)

var (
	serverIP = netip.MustParseAddr("10.0.0.254")
	zeroIP   = netip.IPv4Unspecified()
)

func ip(s string) netip.Addr { return netip.MustParseAddr(s) }
func strp(s string) *string  { return &s }

type fakeProbe struct {
	mu    sync.Mutex
	inUse map[netip.Addr]bool
	calls []netip.Addr
	err   error
}

func (f *fakeProbe) Probe(_ context.Context, a netip.Addr) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, a)
	return f.inUse[a], f.err
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time { return c.t }

func reservedCfg() *api.DHCP {
	return &api.DHCP{Enabled: true, LeaseSeconds: 3600,
		Reservations: []api.DHCPReservation{{MAC: macA, IP: "10.0.0.1"}}}
}

func table(t *testing.T, c *api.DHCP) *Table {
	t.Helper()
	tb, err := NewTable(c, serverIP, 24)
	if err != nil {
		t.Fatal(err)
	}
	return tb
}

type env struct {
	t     *testing.T
	srv   *Server
	probe *fakeProbe
	clock *fakeClock
}

func newEnv(t *testing.T, c *api.DHCP, path string) *env {
	e := &env{t: t, probe: &fakeProbe{inUse: map[netip.Addr]bool{}}, clock: &fakeClock{time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)}}
	e.srv = NewServer(table(t, c), path, e.probe, e.clock.now)
	return e
}

// msg is a client message.
type msg struct {
	typ       byte
	mac       string
	ciaddr    netip.Addr
	giaddr    netip.Addr
	requested netip.Addr
	sid       netip.Addr
	hostname  string
	// src is the packet's source address; unset means ciaddr, or 0.0.0.0.
	src netip.Addr
}

func (m msg) source() netip.Addr {
	if m.src.IsValid() {
		return m.src
	}
	if m.ciaddr.IsValid() {
		return m.ciaddr
	}
	return zeroIP
}

func (m msg) bytes() []byte {
	hw, _ := net.ParseMAC(m.mac)
	p := &Packet{Op: 1, HType: 1, HLen: 6, XID: 0x1234, CIAddr: zeroIP, YIAddr: zeroIP, SIAddr: zeroIP, GIAddr: zeroIP}
	if m.ciaddr.IsValid() {
		p.CIAddr = m.ciaddr
	}
	if m.giaddr.IsValid() {
		p.GIAddr = m.giaddr
	}
	copy(p.CHAddr[:], hw)
	opts := []Option{{OptMessageType, []byte{m.typ}}}
	if m.requested.IsValid() {
		opts = append(opts, Option{OptRequestedIP, addrs(m.requested)})
	}
	if m.sid.IsValid() {
		opts = append(opts, Option{OptServerID, addrs(m.sid)})
	}
	if m.hostname != "" {
		opts = append(opts, Option{OptHostname, []byte(m.hostname)})
	}
	return Marshal(p, opts)
}

// send handles m and returns the parsed reply, nil when none.
func (e *env) send(m msg) (*Packet, netip.AddrPort) {
	e.t.Helper()
	out, dst, ok := e.srv.Handle(context.Background(), m.bytes(), m.source())
	if !ok {
		return nil, dst
	}
	p, err := Parse(out)
	if err != nil {
		e.t.Fatalf("reply does not parse: %v", err)
	}
	if p.Op != 2 || p.XID != 0x1234 || p.MAC() != m.mac {
		e.t.Fatalf("reply header %+v", p)
	}
	return p, dst
}

func (e *env) expect(m msg, typ byte, yiaddr string) *Packet {
	e.t.Helper()
	p, _ := e.send(m)
	if p == nil {
		e.t.Fatalf("%+v: no reply, want type %d", m, typ)
	}
	if p.MessageType() != typ {
		e.t.Fatalf("%+v: reply type %d, want %d", m, p.MessageType(), typ)
	}
	if yiaddr != "" && p.YIAddr != ip(yiaddr) {
		e.t.Fatalf("%+v: yiaddr %s, want %s", m, p.YIAddr, yiaddr)
	}
	return p
}

func (e *env) silent(m msg) {
	e.t.Helper()
	if p, _ := e.send(m); p != nil {
		e.t.Fatalf("%+v: got reply type %d yiaddr %s, want none", m, p.MessageType(), p.YIAddr)
	}
}

// lease runs DISCOVER and a selecting REQUEST and returns the address.
func (e *env) lease(mac, hostname string) netip.Addr {
	e.t.Helper()
	off := e.expect(msg{typ: Discover, mac: mac}, Offer, "")
	e.expect(msg{typ: Request, mac: mac, sid: serverIP, requested: off.YIAddr, hostname: hostname}, Ack, off.YIAddr.String())
	return off.YIAddr
}

func optionCodes(p *Packet) []int {
	var out []int
	for c := range p.Options {
		out = append(out, int(c))
	}
	sort.Ints(out)
	return out
}

func TestReservedMACGetsExactlyItsAddress(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	off, dst := e.send(msg{typ: Discover, mac: macA})
	if off == nil || off.MessageType() != Offer || off.YIAddr != ip("10.0.0.1") {
		t.Fatalf("offer %+v", off)
	}
	if dst != broadcastDst {
		t.Fatalf("offer sent to %s", dst)
	}
	ack := e.expect(msg{typ: Request, mac: macA, sid: serverIP, requested: ip("10.0.0.1")}, Ack, "10.0.0.1")
	if got := ack.AddrOption(OptServerID); got != serverIP {
		t.Fatalf("server id %s", got)
	}
	if !bytes.Equal(ack.Options[OptSubnetMask], []byte{255, 255, 255, 0}) {
		t.Fatalf("mask %v", ack.Options[OptSubnetMask])
	}
	for code, want := range map[byte][]byte{OptLeaseTime: u32(3600), OptRenewalT1: u32(1800), OptRebindingT2: u32(3150)} {
		if !bytes.Equal(ack.Options[code], want) {
			t.Errorf("option %d = %v, want %v", code, ack.Options[code], want)
		}
	}
	// Init-reboot and renewal of the reserved address are acknowledged;
	// the renewal goes unicast to the client.
	e.expect(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")}, Ack, "10.0.0.1")
	_, dst = e.send(msg{typ: Request, mac: macA, ciaddr: ip("10.0.0.1")})
	if dst != netip.AddrPortFrom(ip("10.0.0.1"), 68) {
		t.Fatalf("renewal ack sent to %s", dst)
	}
	if len(e.srv.Leases()) != 0 {
		t.Fatalf("a reservation was recorded as a dynamic lease: %+v", e.srv.Leases())
	}
}

func TestUnknownMACGetsNoReply(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	e.silent(msg{typ: Discover, mac: macB})
	e.silent(msg{typ: Request, mac: macB, sid: serverIP, requested: ip("10.0.0.1")})
	e.silent(msg{typ: Request, mac: macB, requested: ip("10.0.0.7")})
	e.silent(msg{typ: Request, mac: macB, ciaddr: ip("10.0.0.7")})
	e.silent(msg{typ: Inform, mac: macB, ciaddr: ip("10.0.0.7")})
	if len(e.probe.calls) != 0 {
		t.Fatalf("probed for an unknown MAC without a range: %v", e.probe.calls)
	}
}

func TestRelayedAndDisabledAreIgnored(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	e.silent(msg{typ: Discover, mac: macA, giaddr: ip("10.0.0.9")})
	c := reservedCfg()
	c.Enabled = false
	e.srv.Update(table(t, c))
	e.silent(msg{typ: Discover, mac: macA})
	e.silent(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")})
}

func TestDefaultConfigSendsNoRouterOrDNS(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	for _, p := range []*Packet{
		e.expect(msg{typ: Discover, mac: macA}, Offer, ""),
		e.expect(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")}, Ack, ""),
	} {
		want := []int{1, 51, 53, 54, 58, 59}
		if got := optionCodes(p); !equalInts(got, want) {
			t.Fatalf("options %v, want %v", got, want)
		}
	}
	// The default lease time is a day.
	c := reservedCfg()
	c.LeaseSeconds = 0
	e.srv.Update(table(t, c))
	p := e.expect(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")}, Ack, "")
	if !bytes.Equal(p.Options[OptLeaseTime], u32(86400)) {
		t.Fatalf("lease time %v", p.Options[OptLeaseTime])
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestConfiguredOptionsAreSent(t *testing.T) {
	c := reservedCfg()
	mtu := 9000
	c.Options = api.DHCPOptions{
		Router: strp("10.0.0.1"), DNSServers: []string{"1.1.1.1", "8.8.8.8"}, Domain: strp("example.lan"),
		MTU: &mtu, Routes: []api.DHCPRoute{{Destination: "192.168.50.0/24", Gateway: "10.0.0.2"}, {Destination: "172.16.0.0/12", Gateway: "10.0.0.3"}},
		NTPServers: []string{"10.0.0.3"},
	}
	e := newEnv(t, c, "")
	p := e.expect(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")}, Ack, "10.0.0.1")
	want := map[byte][]byte{
		OptRouter: {10, 0, 0, 1},
		OptDNS:    {1, 1, 1, 1, 8, 8, 8, 8},
		OptDomain: []byte("example.lan"),
		OptMTU:    {0x23, 0x28},
		OptNTP:    {10, 0, 0, 3},
		OptClasslessRts: {
			24, 192, 168, 50, 10, 0, 0, 2,
			12, 172, 16, 10, 0, 0, 3,
			0, 10, 0, 0, 1,
		},
	}
	for code, w := range want {
		if !bytes.Equal(p.Options[code], w) {
			t.Errorf("option %d = %v, want %v", code, p.Options[code], w)
		}
	}
	if got := optionCodes(p); !equalInts(got, []int{1, 3, 6, 15, 26, 42, 51, 53, 54, 58, 59, 121}) {
		t.Fatalf("options %v", got)
	}

	// Routes without a router carry no default route.
	c.Options.Router = nil
	e.srv.Update(table(t, c))
	p = e.expect(msg{typ: Request, mac: macA, requested: ip("10.0.0.1")}, Ack, "")
	if _, ok := p.Options[OptRouter]; ok {
		t.Fatal("router sent after it was removed")
	}
	if !bytes.Equal(p.Options[OptClasslessRts], want[OptClasslessRts][:15]) {
		t.Fatalf("routes %v", p.Options[OptClasslessRts])
	}
}

func TestRequestForWrongAddressGetsNak(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	p, dst := e.send(msg{typ: Request, mac: macA, requested: ip("10.0.0.5")})
	if p == nil || p.MessageType() != Nak || p.YIAddr != zeroIP {
		t.Fatalf("init-reboot for the wrong address: %+v", p)
	}
	if dst != broadcastDst {
		t.Fatalf("nak sent to %s", dst)
	}
	if got := optionCodes(p); !equalInts(got, []int{53, 54}) {
		t.Fatalf("nak options %v", got)
	}
	e.expect(msg{typ: Request, mac: macA, sid: serverIP, requested: ip("10.0.0.5")}, Nak, "")
	e.expect(msg{typ: Request, mac: macA, ciaddr: ip("10.0.0.5")}, Nak, "")
	// Selecting another server's offer is ignored.
	e.silent(msg{typ: Request, mac: macA, sid: ip("10.0.0.253"), requested: ip("10.0.0.5")})
}

func TestRemovedReservationIsNoLongerAnswered(t *testing.T) {
	e := newEnv(t, reservedCfg(), "")
	e.lease(macA, "")
	e.srv.Update(table(t, &api.DHCP{Enabled: true}))
	e.silent(msg{typ: Request, mac: macA, ciaddr: ip("10.0.0.1")})
	e.silent(msg{typ: Discover, mac: macA})

	// With a dynamic range the client is NAKed off the old address and
	// then gets a dynamic one.
	e.srv.Update(table(t, &api.DHCP{Enabled: true, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"}}))
	e.expect(msg{typ: Request, mac: macA, ciaddr: ip("10.0.0.1")}, Nak, "")
	if got := e.lease(macA, ""); got != ip("10.0.0.100") {
		t.Fatalf("dynamic address %s", got)
	}
}

func TestDynamicAllocationSkipsTakenAddresses(t *testing.T) {
	c := &api.DHCP{Enabled: true, LeaseSeconds: 3600,
		Reservations: []api.DHCPReservation{{MAC: macA, IP: "10.0.0.100"}},
		Range:        &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.105"},
		Leases: []api.DHCPLease{{MAC: macB, IP: "10.0.0.101",
			ExpiresAt: api.Timestamp{Time: time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC)}}},
	}
	e := newEnv(t, c, "")
	// macC is offered .102 and declines it (quarantined); .103 answers the probe.
	e.expect(msg{typ: Discover, mac: macC}, Offer, "10.0.0.102")
	e.send(msg{typ: Decline, mac: macC, sid: serverIP, requested: ip("10.0.0.102")})
	e.probe.calls = nil
	e.probe.inUse[ip("10.0.0.103")] = true

	off := e.expect(msg{typ: Discover, mac: macD}, Offer, "10.0.0.104")
	if got := e.probe.calls; len(got) != 2 || got[0] != ip("10.0.0.103") || got[1] != ip("10.0.0.104") {
		t.Fatalf("probes %v", got)
	}
	e.expect(msg{typ: Request, mac: macD, sid: serverIP, requested: off.YIAddr}, Ack, "10.0.0.104")

	// The seeded lease is renewed without a probe; the next new client
	// gets the last free address; then the range is full.
	e.probe.calls = nil
	e.expect(msg{typ: Discover, mac: macB}, Offer, "10.0.0.101")
	if len(e.probe.calls) != 0 {
		t.Fatalf("probed a leased address: %v", e.probe.calls)
	}
	e.expect(msg{typ: Discover, mac: macC}, Offer, "10.0.0.105")
	e.silent(msg{typ: Discover, mac: "bc:24:11:00:00:05"})

	// After ten minutes the quarantine ends and .102 is offered again.
	e.clock.t = e.clock.t.Add(QuarantineTime + time.Second)
	e.probe.inUse = map[netip.Addr]bool{}
	e.expect(msg{typ: Discover, mac: "bc:24:11:00:00:05"}, Offer, "10.0.0.102")
}

func TestProbeErrorOffersNothing(t *testing.T) {
	e := newEnv(t, &api.DHCP{Enabled: true, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.105"}}, "")
	e.probe.err = errors.New("arping: not found")
	e.silent(msg{typ: Discover, mac: macB})
	if e.srv.Err() == "" {
		t.Fatal("probe error not reported")
	}
}

func TestRenewingUnknownClientKeepsAFreeAddress(t *testing.T) {
	c := &api.DHCP{Enabled: true, LeaseSeconds: 600, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"},
		Reservations: []api.DHCPReservation{{MAC: macA, IP: "10.0.0.1"}}}
	e := newEnv(t, c, "")
	// A client given .107 by the previous holder renews here.
	e.expect(msg{typ: Request, mac: macB, ciaddr: ip("10.0.0.107")}, Ack, "10.0.0.107")
	if l := e.srv.Leases(); len(l) != 1 || l[0].MAC != macB || l[0].IP != "10.0.0.107" {
		t.Fatalf("leases %+v", l)
	}
	// Another MAC, a reserved address and an address outside the range
	// are refused.
	e.expect(msg{typ: Request, mac: macC, ciaddr: ip("10.0.0.107")}, Nak, "")
	e.expect(msg{typ: Request, mac: macC, ciaddr: ip("10.0.0.1")}, Nak, "")
	e.expect(msg{typ: Request, mac: macC, ciaddr: ip("10.0.0.50")}, Nak, "")
	// Init-reboot with no record stays silent inside the network and is
	// NAKed outside it.
	e.silent(msg{typ: Request, mac: macD, requested: ip("10.0.0.108")})
	e.expect(msg{typ: Request, mac: macD, requested: ip("192.168.1.5")}, Nak, "")
}

func TestReleaseInformAndExpiry(t *testing.T) {
	e := newEnv(t, &api.DHCP{Enabled: true, LeaseSeconds: 600, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"}}, "")
	a := e.lease(macB, "")
	p, dst := e.send(msg{typ: Inform, mac: macB, ciaddr: a})
	if p == nil || p.MessageType() != Ack || p.YIAddr != zeroIP || dst != netip.AddrPortFrom(a, 68) {
		t.Fatalf("inform reply %+v to %s", p, dst)
	}
	if _, ok := p.Options[OptLeaseTime]; ok {
		t.Fatal("lease time in an answer to INFORM")
	}
	e.send(msg{typ: Release, mac: macB, ciaddr: a, sid: serverIP})
	if len(e.srv.Leases()) != 0 {
		t.Fatal("release did not free the lease")
	}
	e.lease(macC, "")
	e.clock.t = e.clock.t.Add(601 * time.Second)
	if len(e.srv.Leases()) != 0 {
		t.Fatal("expired lease not pruned")
	}
}

func TestSpoofedReleaseAndStrayDeclineAreIgnored(t *testing.T) {
	e := newEnv(t, &api.DHCP{Enabled: true, LeaseSeconds: 600, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.101"}}, "")
	a := e.lease(macB, "")
	// A RELEASE naming macB's lease but sent from another address frees nothing.
	e.send(msg{typ: Release, mac: macB, ciaddr: a, sid: serverIP, src: ip("10.0.0.7")})
	if len(e.srv.Leases()) != 1 {
		t.Fatal("a RELEASE from another source freed the lease")
	}
	// A DECLINE from a client that was neither offered nor holds the address
	// does not quarantine it.
	e.send(msg{typ: Decline, mac: macC, sid: serverIP, requested: ip("10.0.0.101")})
	e.expect(msg{typ: Discover, mac: macD}, Offer, "10.0.0.101")
	// Nor does it touch another client's lease.
	e.send(msg{typ: Decline, mac: macC, sid: serverIP, requested: a})
	if l := e.srv.Leases(); len(l) != 1 || l[0].MAC != macB {
		t.Fatalf("leases after a stray DECLINE %+v", l)
	}
}

func TestCorruptLeaseFileIsMovedAside(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "net-6.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := &api.DHCP{Enabled: true, LeaseSeconds: 3600, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"}}
	e := newEnv(t, c, path)
	if !strings.Contains(e.srv.Err(), "moved to") {
		t.Fatalf("error %q", e.srv.Err())
	}
	aside, _ := filepath.Glob(path + ".corrupt-*")
	if len(aside) != 1 {
		t.Fatalf("corrupt copies %v", aside)
	}
	if b, _ := os.ReadFile(aside[0]); string(b) != "{not json" {
		t.Fatalf("corrupt copy holds %q", b)
	}
	e.lease(macB, "")
	if got, err := readLeaseFile(path); err != nil || len(got) != 1 {
		t.Fatalf("new file %+v, %v", got, err)
	}
}

func TestLeasesPersistAndReload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dhcp", "net-6.json")
	c := &api.DHCP{Enabled: true, LeaseSeconds: 3600, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"}}
	e := newEnv(t, c, path)
	a := e.lease(macB, "nas\x01")
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}

	// A new server (runner restart or takeover on this node) reads the file.
	e2 := newEnv(t, c, path)
	e2.clock.t = e.clock.t
	l := e2.srv.Leases()
	if len(l) != 1 || l[0].MAC != macB || l[0].IP != a.String() || l[0].Hostname == nil || *l[0].Hostname != "nas" {
		t.Fatalf("reloaded leases %+v", l)
	}
	if !l[0].ExpiresAt.Equal(e.clock.t.Add(time.Hour)) {
		t.Fatalf("expiry %s", l[0].ExpiresAt)
	}
	e2.probe.calls = nil
	e2.expect(msg{typ: Discover, mac: macB}, Offer, a.String())
	e2.expect(msg{typ: Discover, mac: macC}, Offer, "10.0.0.101")
	if len(e2.probe.calls) != 1 {
		t.Fatalf("probes %v", e2.probe.calls)
	}

	// Expired leases are dropped from the file too.
	e2.clock.t = e2.clock.t.Add(2 * time.Hour)
	e2.srv.Leases()
	if got, _ := readLeaseFile(path); len(got) != 0 {
		t.Fatalf("file still lists %+v", got)
	}
}

func TestSeedPrefersLaterExpiryAndDropsInvalid(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tenant-3.json")
	later := time.Date(2026, 11, 21, 12, 0, 0, 0, time.UTC)
	earlier := time.Date(2026, 11, 21, 9, 0, 0, 0, time.UTC)
	if err := writeLeaseFile(path, []api.DHCPLease{{MAC: macB, IP: "10.0.0.102", ExpiresAt: api.Timestamp{Time: later}}}); err != nil {
		t.Fatal(err)
	}
	c := &api.DHCP{Enabled: true, Range: &api.DHCPRange{Start: "10.0.0.100", End: "10.0.0.110"},
		Reservations: []api.DHCPReservation{{MAC: macD, IP: "10.0.0.1"}},
		Leases: []api.DHCPLease{
			{MAC: macB, IP: "10.0.0.101", ExpiresAt: api.Timestamp{Time: earlier}},
			{MAC: macC, IP: "10.0.0.50", ExpiresAt: api.Timestamp{Time: later}},  // outside the range
			{MAC: macD, IP: "10.0.0.103", ExpiresAt: api.Timestamp{Time: later}}, // MAC now reserved
			{MAC: macA, IP: "10.0.0.104", ExpiresAt: api.Timestamp{Time: time.Date(2026, 11, 19, 0, 0, 0, 0, time.UTC)}},
		}}
	e := newEnv(t, c, path)
	l := e.srv.Leases()
	if len(l) != 1 || l[0].MAC != macB || l[0].IP != "10.0.0.102" {
		t.Fatalf("leases %+v", l)
	}
}

func TestPacketRoundTripAndLongOptions(t *testing.T) {
	long := bytes.Repeat([]byte{7}, 300)
	p := &Packet{Op: 2, HType: 1, HLen: 6, XID: 9, Flags: 0x8000, CIAddr: zeroIP, YIAddr: ip("10.0.0.5"), SIAddr: zeroIP, GIAddr: zeroIP}
	b := Marshal(p, []Option{{OptMessageType, []byte{Ack}}, {OptDomain, long}})
	if len(b) < minReplyLen {
		t.Fatalf("reply %d bytes", len(b))
	}
	got, err := Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	if got.YIAddr != ip("10.0.0.5") || got.Flags != 0x8000 || !bytes.Equal(got.Options[OptDomain], long) {
		t.Fatalf("%+v", got)
	}
	if _, err := Parse(b[:100]); err == nil {
		t.Fatal("short packet parsed")
	}
	bad := append([]byte(nil), b...)
	bad[236] = 0
	if _, err := Parse(bad); err == nil {
		t.Fatal("packet without the magic cookie parsed")
	}
}

func TestTableSkipsBadEntries(t *testing.T) {
	c := &api.DHCP{Enabled: true,
		Reservations: []api.DHCPReservation{{MAC: "nonsense", IP: "10.0.0.1"}, {MAC: macA, IP: "10.0.1.1"}, {MAC: macB, IP: "10.0.0.254"}, {MAC: macC, IP: "10.0.0.3"}},
		Range:        &api.DHCPRange{Start: "10.0.0.200", End: "10.0.0.100"},
		Options:      api.DHCPOptions{Router: strp("192.168.0.1"), DNSServers: []string{"127.0.0.1", "9.9.9.9"}},
	}
	tb := table(t, c)
	if len(tb.Problems) != 6 {
		t.Fatalf("problems %q", tb.Problems)
	}
	if a, ok := tb.Reserved(macC); !ok || a != ip("10.0.0.3") || tb.HasRange || tb.Router.IsValid() || len(tb.DNS) != 1 {
		t.Fatalf("%+v", tb)
	}
	if _, err := NewTable(c, ip("10.0.0.254"), 31); err == nil {
		t.Fatal("/31 accepted")
	}
}

type fakeCmd struct {
	out []byte
	err error
}

func (f fakeCmd) Run(context.Context, []byte, string, ...string) ([]byte, error) { return f.out, f.err }

func TestARPProbeReadsTheSummary(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		out   string
		err   error
		inUse bool
		fail  bool
	}{
		{"Sent 2 probes (2 broadcast(s))\nReceived 0 response(s)\n", nil, false, false},
		{"Unicast reply from 10.0.0.5 [AA:BB:CC:00:00:01]  0.6ms\nSent 1 probes (1 broadcast(s))\nReceived 1 response(s)\n", errors.New("exit status 1"), true, false},
		{"", errors.New("arping: unknown iface"), false, true},
	} {
		inUse, err := ARPProbe{Cmd: fakeCmd{[]byte(tc.out), tc.err}, Netns: "rf-p6", Iface: "rf-w6"}.Probe(ctx, ip("10.0.0.5"))
		if inUse != tc.inUse || (err != nil) != tc.fail {
			t.Errorf("%q: %v %v", tc.out, inUse, err)
		}
	}
}
