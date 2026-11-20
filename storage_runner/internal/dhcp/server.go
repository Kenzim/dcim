package dhcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"rackflow/storage_runner/internal/api"
)

const (
	// QuarantineTime keeps a declined or probe-answering address out of
	// the dynamic pool.
	QuarantineTime = 10 * time.Minute
	// offerHold keeps an offered address for its client until it requests.
	offerHold = time.Minute
	// maxProbes bounds the duplicate address probes per DISCOVER; each
	// takes about a second.
	maxProbes = 3
	clientPort = 68
)

var broadcastDst = netip.AddrPortFrom(netip.AddrFrom4([4]byte{255, 255, 255, 255}), clientPort)

// Prober checks whether an address answers ARP (duplicate address
// detection) before it is offered.
type Prober interface {
	Probe(ctx context.Context, ip netip.Addr) (inUse bool, err error)
}

// Lease is a dynamic lease.
type Lease struct {
	MAC      string
	IP       netip.Addr
	Expires  time.Time
	Hostname string
}

type offer struct {
	ip    netip.Addr
	until time.Time
}

// Server answers DHCP for one presence or tenant. Handle is called from one
// goroutine (the socket loop); Update and Leases may be called from others.
type Server struct {
	// Path is the lease file; empty keeps leases in memory only.
	Path  string
	Probe Prober
	Now   func() time.Time
	Logf  func(string, ...any)

	mu         sync.Mutex
	table      *Table
	leases     map[string]*Lease
	offers     map[string]offer
	quarantine map[netip.Addr]time.Time
	lastErr    string
}

// NewServer returns a server for t, seeding its leases from the lease file
// and the config's leases (the later expiry wins).
func NewServer(t *Table, path string, probe Prober, now func() time.Time) *Server {
	if now == nil {
		now = time.Now
	}
	s := &Server{Path: path, Probe: probe, Now: now, table: t,
		leases: map[string]*Lease{}, offers: map[string]offer{}, quarantine: map[netip.Addr]time.Time{}}
	seed := append([]api.DHCPLease(nil), t.Seed...)
	if path != "" {
		file, err := readLeaseFile(path)
		if err != nil {
			s.lastErr = err.Error()
		}
		seed = append(seed, file...)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now0 := s.Now()
	for _, l := range seed {
		mac, err := normMAC(l.MAC)
		ip, err2 := netip.ParseAddr(l.IP)
		if err != nil || err2 != nil || !l.ExpiresAt.After(now0) {
			continue
		}
		if cur := s.leases[mac]; cur != nil && !cur.Expires.Before(l.ExpiresAt.Time) {
			continue
		}
		host := ""
		if l.Hostname != nil {
			host = cleanHostname([]byte(*l.Hostname))
		}
		s.leases[mac] = &Lease{MAC: mac, IP: ip, Expires: l.ExpiresAt.Time, Hostname: host}
	}
	// Two MACs on one address: the later expiry keeps it.
	byIP := map[netip.Addr]*Lease{}
	for mac, l := range s.leases {
		if other := byIP[l.IP]; other != nil {
			if other.Expires.Before(l.Expires) {
				delete(s.leases, other.MAC)
			} else {
				delete(s.leases, mac)
				continue
			}
		}
		byIP[l.IP] = l
	}
	s.dropInvalidLocked()
	s.persistLocked()
	return s
}

// Update swaps in a new table. Leases the new table no longer allows (the
// address left the range or became reserved, or the MAC got a reservation)
// are dropped.
func (s *Server) Update(t *Table) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.table = t
	if s.dropInvalidLocked() {
		s.persistLocked()
	}
}

// Table is the current table.
func (s *Server) Table() *Table {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.table
}

func (s *Server) dropInvalidLocked() bool {
	t := s.table
	changed := false
	for mac, l := range s.leases {
		if _, res := t.Reserved(mac); res || !t.InRange(l.IP) || t.ReservedIP(l.IP) {
			delete(s.leases, mac)
			changed = true
		}
	}
	for mac, o := range s.offers {
		if _, res := t.Reserved(mac); res || !t.InRange(o.ip) || t.ReservedIP(o.ip) {
			delete(s.offers, mac)
		}
	}
	return changed
}

// Leases returns the current dynamic leases, sorted by address.
func (s *Server) Leases() []api.DHCPLease {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked()
	return s.leaseListLocked()
}

func (s *Server) leaseListLocked() []api.DHCPLease {
	out := []api.DHCPLease{}
	for _, l := range s.leases {
		e := api.DHCPLease{MAC: l.MAC, IP: l.IP.String(), ExpiresAt: api.Timestamp{Time: l.Expires.UTC().Truncate(time.Second)}}
		if l.Hostname != "" {
			h := l.Hostname
			e.Hostname = &h
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool {
		a, _ := netip.ParseAddr(out[i].IP)
		b, _ := netip.ParseAddr(out[j].IP)
		return a.Less(b)
	})
	return out
}

// Err is the last lease file or probe error, empty when none.
func (s *Server) Err() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastErr
}

func (s *Server) logf(f string, a ...any) {
	if s.Logf != nil {
		s.Logf(f, a...)
	}
}

func (s *Server) pruneLocked() {
	now := s.Now()
	changed := false
	for mac, l := range s.leases {
		if !l.Expires.After(now) {
			delete(s.leases, mac)
			changed = true
		}
	}
	for mac, o := range s.offers {
		if !o.until.After(now) {
			delete(s.offers, mac)
		}
	}
	for ip, until := range s.quarantine {
		if !until.After(now) {
			delete(s.quarantine, ip)
		}
	}
	if changed {
		s.persistLocked()
	}
}

// Handle answers one received message. ok is false when nothing is sent.
func (s *Server) Handle(ctx context.Context, b []byte) (reply []byte, dst netip.AddrPort, ok bool) {
	req, err := Parse(b)
	if err != nil || req.Op != 1 || req.HType != 1 || req.HLen != 6 {
		return nil, dst, false
	}
	// Relayed requests are ignored: these are flat layer 2 networks.
	if req.GIAddr.IsValid() && !req.GIAddr.IsUnspecified() {
		return nil, dst, false
	}
	s.mu.Lock()
	s.pruneLocked()
	t := s.table
	s.mu.Unlock()
	if !t.Enabled {
		return nil, dst, false
	}
	switch req.MessageType() {
	case Discover:
		return s.discover(ctx, t, req)
	case Request:
		return s.request(t, req)
	case Inform:
		return s.inform(t, req)
	case Release:
		s.release(t, req)
	case Decline:
		s.decline(t, req)
	}
	return nil, dst, false
}

func (s *Server) discover(ctx context.Context, t *Table, req *Packet) ([]byte, netip.AddrPort, bool) {
	mac := req.MAC()
	if ip, ok := t.Reserved(mac); ok {
		s.forgetDynamic(mac)
		return s.reply(t, req, Offer, ip)
	}
	if !t.HasRange {
		return nil, netip.AddrPort{}, false
	}
	ip, ok := s.allocate(ctx, t, mac, req.AddrOption(OptRequestedIP))
	if !ok {
		return nil, netip.AddrPort{}, false
	}
	s.mu.Lock()
	if s.table != t || !s.freeLocked(t, ip, mac) {
		s.mu.Unlock()
		return nil, netip.AddrPort{}, false
	}
	s.offers[mac] = offer{ip: ip, until: s.Now().Add(offerHold)}
	s.mu.Unlock()
	return s.reply(t, req, Offer, ip)
}

// allocate picks the dynamic address for mac: its lease or pending offer,
// else the requested address, else the lowest free one. An address not in
// the lease table is probed first and quarantined if it answers.
func (s *Server) allocate(ctx context.Context, t *Table, mac string, requested netip.Addr) (netip.Addr, bool) {
	s.mu.Lock()
	if l := s.leases[mac]; l != nil && s.freeLocked(t, l.IP, mac) {
		s.mu.Unlock()
		return l.IP, true
	}
	if o, ok := s.offers[mac]; ok && s.freeLocked(t, o.ip, mac) {
		s.mu.Unlock()
		return o.ip, true
	}
	s.mu.Unlock()

	tried := map[netip.Addr]bool{}
	for probes := 0; probes < maxProbes; probes++ {
		s.mu.Lock()
		cand := netip.Addr{}
		busy := s.busyLocked(mac)
		free := func(a netip.Addr) bool { return !tried[a] && !busy[a] && t.InRange(a) && !t.ReservedIP(a) }
		if requested.IsValid() && free(requested) {
			cand = requested
		} else {
			for v := toU32(t.RangeStart); v <= toU32(t.RangeEnd); v++ {
				a := fromU32(v)
				if free(a) {
					cand = a
					break
				}
				if v == ^uint32(0) {
					break
				}
			}
		}
		s.mu.Unlock()
		if !cand.IsValid() {
			return cand, false
		}
		tried[cand] = true
		if s.Probe == nil {
			return cand, true
		}
		inUse, err := s.Probe.Probe(ctx, cand)
		s.mu.Lock()
		if err != nil {
			s.lastErr = fmt.Sprintf("probe %s: %v", cand, err)
			s.mu.Unlock()
			s.logf("dhcp: %v", s.lastErr)
			return netip.Addr{}, false
		}
		s.lastErr = ""
		if inUse {
			s.quarantine[cand] = s.Now().Add(QuarantineTime)
			s.mu.Unlock()
			s.logf("dhcp: %s answers ARP, quarantined", cand)
			continue
		}
		s.mu.Unlock()
		return cand, true
	}
	return netip.Addr{}, false
}

// freeLocked reports whether ip may be given to mac dynamically: inside the
// range, not reserved, not quarantined, and not leased or offered to another
// MAC.
func (s *Server) freeLocked(t *Table, ip netip.Addr, mac string) bool {
	return t.InRange(ip) && !t.ReservedIP(ip) && !s.busyLocked(mac)[ip]
}

// busyLocked is the set of addresses quarantined or leased or offered to a
// MAC other than mac.
func (s *Server) busyLocked(mac string) map[netip.Addr]bool {
	now := s.Now()
	busy := map[netip.Addr]bool{}
	for ip, until := range s.quarantine {
		if until.After(now) {
			busy[ip] = true
		}
	}
	for m, l := range s.leases {
		if m != mac && l.Expires.After(now) {
			busy[l.IP] = true
		}
	}
	for m, o := range s.offers {
		if m != mac && o.until.After(now) {
			busy[o.ip] = true
		}
	}
	return busy
}

func (s *Server) forgetDynamic(mac string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.offers, mac)
	if _, ok := s.leases[mac]; ok {
		delete(s.leases, mac)
		s.persistLocked()
	}
}

// request follows RFC 2131 4.3.2. The client state is told apart by the
// server identifier, requested address and ciaddr.
func (s *Server) request(t *Table, req *Packet) ([]byte, netip.AddrPort, bool) {
	none := netip.AddrPort{}
	mac := req.MAC()
	sid := req.AddrOption(OptServerID)
	requested := req.AddrOption(OptRequestedIP)
	selecting := sid.IsValid()
	if selecting && sid != t.ServerID {
		// The client chose another server's offer.
		s.mu.Lock()
		delete(s.offers, mac)
		s.mu.Unlock()
		return nil, none, false
	}
	set := func(a netip.Addr) bool { return a.IsValid() && !a.IsUnspecified() }
	var asked netip.Addr
	renewing := false
	switch {
	case selecting && set(requested):
		asked = requested
	case selecting:
		return nil, none, false
	case set(req.CIAddr):
		// RENEWING or REBINDING.
		asked, renewing = req.CIAddr, true
	case set(requested):
		// INIT-REBOOT.
		asked = requested
	default:
		return nil, none, false
	}

	if ip, ok := t.Reserved(mac); ok {
		s.forgetDynamic(mac)
		if asked == ip {
			return s.reply(t, req, Ack, ip)
		}
		return s.reply(t, req, Nak, netip.Addr{})
	}
	if !t.HasRange {
		return nil, none, false
	}

	s.mu.Lock()
	known := netip.Addr{}
	if l := s.leases[mac]; l != nil {
		known = l.IP
	} else if o, ok := s.offers[mac]; ok {
		known = o.ip
	}
	switch {
	case known.IsValid():
		if asked != known || !s.freeLocked(t, known, mac) {
			s.mu.Unlock()
			return s.reply(t, req, Nak, netip.Addr{})
		}
	case renewing:
		// A client renewing an address this node has not heard of (given
		// by the previous holder): accept it when nobody else has it.
		if !s.freeLocked(t, asked, mac) {
			s.mu.Unlock()
			return s.reply(t, req, Nak, netip.Addr{})
		}
	case selecting:
		// Our offer has lapsed: the client starts again.
		s.mu.Unlock()
		return s.reply(t, req, Nak, netip.Addr{})
	default:
		// INIT-REBOOT with no record: silent unless on the wrong network.
		s.mu.Unlock()
		if !t.Prefix.Contains(asked) {
			return s.reply(t, req, Nak, netip.Addr{})
		}
		return nil, none, false
	}
	delete(s.offers, mac)
	s.leases[mac] = &Lease{MAC: mac, IP: asked, Expires: s.Now().Add(t.LeaseTime), Hostname: cleanHostname(req.Options[OptHostname])}
	s.persistLocked()
	s.mu.Unlock()
	return s.reply(t, req, Ack, asked)
}

func (s *Server) inform(t *Table, req *Packet) ([]byte, netip.AddrPort, bool) {
	ip := req.CIAddr
	if !ip.IsValid() || ip.IsUnspecified() {
		return nil, netip.AddrPort{}, false
	}
	known := t.ReservedIP(ip)
	if !known {
		s.mu.Lock()
		for _, l := range s.leases {
			if l.IP == ip {
				known = true
			}
		}
		s.mu.Unlock()
	}
	if !known {
		return nil, netip.AddrPort{}, false
	}
	return s.reply(t, req, Ack, netip.Addr{})
}

func (s *Server) release(t *Table, req *Packet) {
	if sid := req.AddrOption(OptServerID); sid.IsValid() && sid != t.ServerID {
		return
	}
	mac := req.MAC()
	s.mu.Lock()
	defer s.mu.Unlock()
	if l := s.leases[mac]; l != nil && l.IP == req.CIAddr {
		delete(s.leases, mac)
		s.persistLocked()
	}
}

func (s *Server) decline(t *Table, req *Packet) {
	if sid := req.AddrOption(OptServerID); sid.IsValid() && sid != t.ServerID {
		return
	}
	ip := req.AddrOption(OptRequestedIP)
	if !t.InRange(ip) {
		return
	}
	mac := req.MAC()
	s.mu.Lock()
	defer s.mu.Unlock()
	if l := s.leases[mac]; l != nil && l.IP == ip {
		delete(s.leases, mac)
		s.persistLocked()
	}
	if o, ok := s.offers[mac]; ok && o.ip == ip {
		delete(s.offers, mac)
	}
	s.quarantine[ip] = s.Now().Add(QuarantineTime)
	s.logf("dhcp: %s declined %s, quarantined", mac, ip)
}

// reply builds the answer. A NAK is always broadcast (RFC 2131 4.1);
// otherwise the reply goes to ciaddr when set, else to the broadcast address.
func (s *Server) reply(t *Table, req *Packet, typ byte, yiaddr netip.Addr) ([]byte, netip.AddrPort, bool) {
	zero := netip.IPv4Unspecified()
	p := &Packet{Op: 2, HType: 1, HLen: 6, XID: req.XID, Flags: req.Flags,
		CIAddr: zero, YIAddr: zero, SIAddr: zero, GIAddr: zero, CHAddr: req.CHAddr}
	if yiaddr.IsValid() {
		p.YIAddr = yiaddr
	}
	opts := []Option{{OptMessageType, []byte{typ}}, {OptServerID, addrs(t.ServerID)}}
	switch typ {
	case Nak:
	case Ack:
		if req.MessageType() == Inform {
			// RFC 2131: no lease time in an answer to INFORM.
			opts = append(opts, t.options(false)...)
		} else {
			opts = append(opts, t.options(true)...)
		}
		p.CIAddr = req.CIAddr
	default:
		opts = append(opts, t.options(true)...)
	}
	dst := broadcastDst
	if typ != Nak && req.CIAddr.IsValid() && !req.CIAddr.IsUnspecified() {
		dst = netip.AddrPortFrom(req.CIAddr, clientPort)
	}
	return Marshal(p, opts), dst, true
}

// cleanHostname keeps printable ASCII, at most 64 characters.
func cleanHostname(b []byte) string {
	var sb strings.Builder
	for _, c := range b {
		if c > 0x20 && c < 0x7f && sb.Len() < 64 {
			sb.WriteByte(c)
		}
	}
	return sb.String()
}

type leaseFile struct {
	Leases []api.DHCPLease `json:"leases"`
}

func readLeaseFile(path string) ([]api.DHCPLease, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var f leaseFile
	if err := json.Unmarshal(b, &f); err != nil {
		return nil, fmt.Errorf("lease file %s: %w", path, err)
	}
	return f.Leases, nil
}

func (s *Server) persistLocked() {
	if s.Path == "" {
		return
	}
	if err := writeLeaseFile(s.Path, s.leaseListLocked()); err != nil {
		s.lastErr = err.Error()
		s.logf("dhcp: %v", err)
		return
	}
	s.lastErr = ""
}

func writeLeaseFile(path string, leases []api.DHCPLease) error {
	b, err := json.MarshalIndent(leaseFile{Leases: leases}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Serve answers on conn until ctx ends or conn is closed.
func (s *Server) Serve(ctx context.Context, conn net.PacketConn) {
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	buf := make([]byte, 1500)
	for {
		n, _, err := conn.ReadFrom(buf)
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return
			}
			s.logf("dhcp: read: %v", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(100 * time.Millisecond):
			}
			continue
		}
		out, dst, ok := s.Handle(ctx, buf[:n])
		if !ok || ctx.Err() != nil {
			continue
		}
		if _, err := conn.WriteTo(out, net.UDPAddrFromAddrPort(dst)); err != nil && ctx.Err() == nil {
			s.logf("dhcp: send to %s: %v", dst, err)
		}
	}
}
