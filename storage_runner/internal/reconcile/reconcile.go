// Package reconcile makes a storage node match the desired state from
// Rackflow. One pass fetches the config, applies it in phases (tenant network
// and Ceph, firewall, Ganesha, destruction) and posts a status report.
//
// Safety rules enforced here:
//   - nothing is destroyed unless the config lists it with a destroy state and
//     both destroy checks pass on the runner's own clock;
//   - tenants and subvolumes missing from the config are reported as orphans
//     and left untouched;
//   - a failed fetch, or a config with no tenants while the node has some,
//     changes nothing;
//   - only objects named by package names are ever touched.
package reconcile

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"math"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
	"rackflow/storage_runner/internal/config"
	"rackflow/storage_runner/internal/dhcp"
	"rackflow/storage_runner/internal/firewall"
	"rackflow/storage_runner/internal/ganesha"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
	"rackflow/storage_runner/internal/netns"
	"rackflow/storage_runner/internal/sftp"
	"rackflow/storage_runner/internal/state"
	"rackflow/storage_runner/internal/vip"
)

// DefaultDestroyGrace applies when the config does not carry destroy_grace_seconds.
const DefaultDestroyGrace = 7 * 24 * time.Hour

// maxGraceSeconds is the largest grace that fits a time.Duration.
const maxGraceSeconds = int64(math.MaxInt64 / int64(time.Second))

var shareNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`)

// Source is Rackflow as seen by the reconciler.
type Source interface {
	Fetch(ctx context.Context) (*api.Config, error)
	PostStatus(ctx context.Context, st api.Status) error
}

type Reconciler struct {
	Host          config.Host
	Source        Source
	Net           netns.Manager
	Firewall      firewall.Firewall
	Ceph          ceph.Cluster
	NFS           ganesha.Runtime
	State         *state.Store
	Now           func() time.Time
	Logf          func(format string, args ...any)
	RunnerVersion string

	// SFTP is optional; without it SFTP stays off.
	SFTP          *sftp.Gateway
	SFTPMounts    sftp.Mounter
	SFTPRuntime   sftp.Runtime
	SFTPMountRoot string

	// Lease decides what this node may serve when the config has an ha
	// block. Its Stop hook should be StopLease and its Changed hook Kick.
	Lease *lease.Manager
	// RunLease starts the lease loops on the first pass with an ha block;
	// tests drive them by hand instead.
	RunLease bool
	// Hostname is this node's host name, as its kernel CephFS mounts report
	// it to the MDS.
	Hostname string
	// VIP adds and removes the location's SFTP virtual IP.
	VIP vip.Manager
	// DHCP runs the DHCP servers of network presences and tenants; nil
	// serves no DHCP.
	DHCP *dhcp.Pool

	lastApplied string
	lastOrphans string

	lastConfig      *api.Config
	haTimings       *lease.Timings
	leaseStarted    bool
	destroyedLeases map[int64]bool
	announced       map[int64]time.Time
	announcedNets   map[int64]time.Time
	// presenceSince is when each presence was first served without leases.
	presenceSince map[int64]time.Time
	// servedNets are the presences the last posted status reported serving.
	servedNets map[int64]bool
	vipIface   string
	// serveMu orders starting to serve against StopLease, so nothing is
	// started after the lease that allows it has ended.
	serveMu  sync.Mutex
	kick     chan struct{}
	kickOnce sync.Once
}

func (r *Reconciler) logf(format string, args ...any) {
	if r.Logf != nil {
		r.Logf(format, args...)
		return
	}
	log.Printf(format, args...)
}

func (r *Reconciler) now() time.Time {
	if r.Now != nil {
		return r.Now().UTC()
	}
	return time.Now().UTC()
}

// Loop runs a pass every interval, and at once after Kick, until ctx is
// cancelled.
func (r *Reconciler) Loop(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	kick := r.kickCh()
	for {
		_ = r.Once(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-kick:
		}
	}
}

type pass struct {
	r       *Reconciler
	ctx     context.Context
	now     time.Time
	grace   time.Duration
	graceOK bool

	cephOK   bool
	cephErr  error
	free     int64
	nodeErrs []string

	namespaces map[int64]bool
	// presences are the networks with a presence namespace on this node.
	presences  map[int64]bool
	containers map[int64]ganesha.Container
	subvols    map[string]bool
	users      map[string]ceph.User
	minimal    *string
	sftp       *sftpPass
	ha         *haPass
	cfg        *api.Config
	// dhcpWanted are the DHCP servers wanted in this pass.
	dhcpWanted map[string]bool
	// servedNets are the presences reported serving in this pass.
	servedNets map[int64]bool
	// sftpMaster is set when this node serves the location's SFTP on the
	// virtual IP in this pass.
	sftpMaster bool
}

type tenantWork struct {
	t      api.Tenant
	valid  bool
	errs   []string
	shares []*shareWork

	network  netip.Prefix
	endpoint netip.Prefix

	netOK          bool
	userOK         bool
	exportsCurrent bool
	destroyed      bool
	// held is set when this node may serve the tenant and change its Ceph
	// state: it holds the tenant's lease, or the config has no ha block.
	held bool
	// hadLocal is set when a tenant this node does not hold had a container
	// or network here that this pass removed.
	hadLocal bool
	// hold is set when an existing subvolume could not be described this
	// pass (invalid share config or failed info). Caps are then never
	// narrowed and Ganesha is left as it is, so access is not lost.
	hold bool
}

func (w *tenantWork) fail(format string, args ...any) {
	w.errs = append(w.errs, fmt.Sprintf(format, args...))
}

func (w *tenantWork) invalid(format string, args ...any) {
	w.valid = false
	w.fail("invalid config: "+format, args...)
}

type shareWork struct {
	s         api.Share
	name      string
	valid     bool
	err       string
	clients   []string
	info      *ceph.SubvolumeInfo
	destroyed bool
}

func (s *shareWork) invalid(format string, args ...any) {
	s.valid = false
	s.err = "invalid config: " + fmt.Sprintf(format, args...)
}

func (p *pass) nodeErr(format string, args ...any) {
	p.nodeErrs = append(p.nodeErrs, fmt.Sprintf(format, args...))
}

// Once runs a single reconcile pass.
func (r *Reconciler) Once(ctx context.Context) error {
	cfg, err := r.Source.Fetch(ctx)
	if err != nil {
		saved := r.savedConfig()
		if saved == nil || saved.HA == nil {
			r.logf("config fetch failed, keeping current state: %v", err)
			return fmt.Errorf("fetch config: %w", err)
		}
		// Leases keep running on the last config, so a Rackflow outage
		// neither stops serving nor holds up a takeover.
		r.logf("config fetch failed, applying the last config %s: %v", saved.Version, err)
		cfg = saved
	} else {
		r.saveConfig(cfg)
	}
	p := &pass{
		r:          r,
		ctx:        ctx,
		now:        r.now(),
		namespaces: map[int64]bool{},
		presences:  map[int64]bool{},
		containers: map[int64]ganesha.Container{},
		subvols:    map[string]bool{},
		users:      map[string]ceph.User{},
		cfg:        cfg,
	}
	p.checkCeph()
	if err := p.inventory(cfg); err != nil {
		p.nodeErr("inventory: %v", err)
		r.logf("cannot list host state, changing nothing: %v", err)
		return r.post(ctx, p.baseStatus(r.lastApplied))
	}
	if len(cfg.Tenants) == 0 {
		if host := p.hostTenants(); len(host) > 0 {
			r.logf("config %s lists no tenants but this node has %d; keeping current state", cfg.Version, len(host))
			p.nodeErr("config lists no tenants while this node has %d; nothing was changed", len(host))
			st := p.baseStatus(r.lastApplied)
			st.Orphans = p.orphans(nil)
			return r.post(ctx, st)
		}
	}
	if err := r.Net.Check(ctx); err != nil {
		p.nodeErr("network: %v", err)
	}
	p.setGrace(cfg.Settings)

	works := p.validate(cfg.Tenants)
	nets := p.validateNetworks(cfg, works)
	p.setupHA(cfg, works, nets)
	p.dropPresences(nets)
	for _, w := range works {
		p.applyTenant(w)
	}
	p.dropUnlisted(works)
	p.applyNetworks(nets)
	p.applyFirewall(works)
	for _, w := range works {
		p.applyNFS(w)
	}
	p.applySFTP(cfg, works)
	for _, w := range works {
		p.destroy(w)
	}
	p.stopUnwantedDHCP()
	p.pruneState(works)

	st := p.baseStatus(cfg.Version)
	p.fillStatus(&st, works)
	p.fillTenantDHCP(&st, works)
	st.Orphans = p.orphans(works)
	p.fillSFTPStatus(&st)
	p.fillHAStatus(&st, works)
	p.fillNetworkStatus(&st, nets)
	r.lastApplied = cfg.Version
	if err := r.post(ctx, st); err != nil {
		return err
	}
	r.servedNets = p.servedNets
	p.ackSFTP()
	if p.ha != nil {
		r.Lease.AckEvents(len(st.Events))
	}
	return nil
}

func (r *Reconciler) post(ctx context.Context, st api.Status) error {
	if err := r.Source.PostStatus(ctx, st); err != nil {
		r.logf("status post failed: %v", err)
		return fmt.Errorf("post status: %w", err)
	}
	return nil
}

func (p *pass) baseStatus(version string) api.Status {
	st := api.Status{
		VersionApplied: version,
		RunnerVersion:  p.r.RunnerVersion,
		Node:           api.NodeStatus{CephOK: p.cephOK, FreeBytes: p.free},
	}
	if len(p.nodeErrs) > 0 {
		msg := strings.Join(p.nodeErrs, "; ")
		st.Node.Error = &msg
	}
	return st
}

func (p *pass) cephDown(err error) {
	p.cephOK = false
	p.cephErr = err
	p.nodeErr("ceph: %v", err)
	p.r.logf("ceph: %v", err)
}

func (p *pass) checkCeph() {
	r := p.r
	free, err := r.Ceph.Check(p.ctx)
	if err == nil {
		var exists bool
		var pool string
		exists, pool, err = r.Ceph.Group(p.ctx)
		switch {
		case err != nil:
		case !exists:
			r.logf("creating subvolume group %s on pool %s", r.Host.CephSubvolumeGroup, r.Host.CephECPool)
			err = r.Ceph.CreateGroup(p.ctx)
		case pool != r.Host.CephECPool:
			err = fmt.Errorf("subvolume group %q uses data pool %q, expected %q", r.Host.CephSubvolumeGroup, pool, r.Host.CephECPool)
		}
	}
	if err != nil {
		p.cephDown(err)
		return
	}
	p.cephOK = true
	p.free = free
}

func (p *pass) inventory(cfg *api.Config) error {
	r := p.r
	ids, err := r.Net.Tenants(p.ctx)
	if err != nil {
		return err
	}
	for _, id := range ids {
		p.namespaces[id] = true
	}
	nets, err := r.Net.Presences(p.ctx)
	if err != nil {
		return err
	}
	for _, id := range nets {
		p.presences[id] = true
	}
	ctrs, err := r.NFS.List(p.ctx)
	if err != nil {
		return err
	}
	for _, c := range ctrs {
		p.containers[c.TenantID] = c
	}
	if !p.cephOK {
		return nil
	}
	subs, err := r.Ceph.Subvolumes(p.ctx)
	if err != nil {
		p.cephDown(err)
		return nil
	}
	for _, s := range subs {
		p.subvols[s] = true
	}
	users, err := r.Ceph.Users(p.ctx, p.userCandidates(cfg))
	if err != nil {
		p.cephDown(err)
		return nil
	}
	p.users = users
	return nil
}

// userCandidates names the cephx users a scoped key looks up, since it
// cannot list them: every tenant in the config or with something on this
// node. A user of a tenant that has neither is not seen.
func (p *pass) userCandidates(cfg *api.Config) []string {
	if !p.r.Host.CephScopedKey {
		return nil
	}
	set := map[int64]bool{}
	for _, t := range cfg.Tenants {
		if t.ID > 0 {
			set[t.ID] = true
		}
	}
	for _, id := range p.hostTenants() {
		set[id] = true
	}
	out := make([]string, 0, len(set))
	for id := range set {
		out = append(out, names.CephEntity(id))
	}
	sort.Strings(out)
	return out
}

// hostTenants lists every tenant with something on this node.
func (p *pass) hostTenants() []int64 {
	set := map[int64]bool{}
	for id := range p.namespaces {
		set[id] = true
	}
	for id := range p.containers {
		set[id] = true
	}
	for name := range p.subvols {
		if t, _, ok := names.ParseSubvolume(name); ok {
			set[t] = true
		}
	}
	for e := range p.users {
		if id, ok := names.ParseCephEntity(e); ok {
			set[id] = true
		}
	}
	for _, id := range p.r.State.LinkTenants() {
		set[id] = true
	}
	out := make([]int64, 0, len(set))
	for id := range set {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// orphans lists subvolumes, and tenants with a namespace, container or cephx
// user, that the config does not mention. They are reported, never removed.
func (p *pass) orphans(works []*tenantWork) []string {
	knownT := map[int64]bool{}
	knownS := map[string]bool{}
	for _, w := range works {
		knownT[w.t.ID] = true
		for _, s := range w.shares {
			knownS[s.name] = true
		}
	}
	set := map[string]bool{}
	for name := range p.subvols {
		if !knownS[name] {
			set[name] = true
		}
	}
	tenantIDs := []int64{}
	for id := range p.namespaces {
		tenantIDs = append(tenantIDs, id)
	}
	for id := range p.containers {
		tenantIDs = append(tenantIDs, id)
	}
	for e := range p.users {
		if id, ok := names.ParseCephEntity(e); ok {
			tenantIDs = append(tenantIDs, id)
		}
	}
	for _, id := range tenantIDs {
		if !knownT[id] {
			set[names.Namespace(id)] = true
		}
	}
	out := make([]string, 0, len(set))
	for s := range set {
		out = append(out, s)
	}
	sort.Strings(out)
	if joined := strings.Join(out, ", "); joined != p.r.lastOrphans {
		p.r.lastOrphans = joined
		if joined != "" {
			p.r.logf("orphans (left untouched): %s", joined)
		}
	}
	return out
}

func (p *pass) setGrace(s api.Settings) {
	switch {
	case s.DestroyGraceSeconds == nil:
		p.grace, p.graceOK = DefaultDestroyGrace, true
	case *s.DestroyGraceSeconds < 0:
		p.nodeErr("destroy_grace_seconds is negative; nothing will be destroyed")
	case *s.DestroyGraceSeconds > maxGraceSeconds:
		p.nodeErr("destroy_grace_seconds %d is too large; nothing will be destroyed", *s.DestroyGraceSeconds)
	default:
		p.grace, p.graceOK = time.Duration(*s.DestroyGraceSeconds)*time.Second, true
	}
}

func (p *pass) validate(tenants []api.Tenant) []*tenantWork {
	sorted := append([]api.Tenant(nil), tenants...)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })
	idCount := map[int64]int{}
	shareCount := map[int64]int{}
	for _, t := range sorted {
		idCount[t.ID]++
		for _, s := range t.Shares {
			shareCount[s.ID]++
		}
	}
	var works []*tenantWork
	for _, t := range sorted {
		w := &tenantWork{t: t, valid: true}
		for _, s := range t.Shares {
			w.shares = append(w.shares, &shareWork{s: s, name: names.Subvolume(t.ID, s.ID), valid: true})
		}
		if idCount[t.ID] > 1 {
			w.invalid("tenant id %d appears more than once", t.ID)
		} else if err := p.validateTenant(w); err != nil {
			w.invalid("%v", err)
		}
		works = append(works, w)
	}

	vlans := map[int][]*tenantWork{}
	for _, w := range works {
		if w.valid && w.t.State != api.TenantTerminated {
			vlans[w.t.VLAN] = append(vlans[w.t.VLAN], w)
		}
	}
	for vlan, list := range vlans {
		if len(list) > 1 {
			for _, w := range list {
				w.invalid("VLAN %d is used by more than one tenant", vlan)
			}
		}
	}

	for _, w := range works {
		seenNames := map[string]bool{}
		for _, s := range w.shares {
			if shareCount[s.s.ID] > 1 {
				s.invalid("share id %d appears more than once", s.s.ID)
				continue
			}
			if err := p.validateShare(w, s); err != nil {
				s.invalid("%v", err)
				continue
			}
			if s.s.State == api.ShareActive {
				if seenNames[s.s.Name] {
					s.invalid("share name %q is used twice", s.s.Name)
					continue
				}
				seenNames[s.s.Name] = true
			}
		}
	}
	return works
}

func lastAddr(p netip.Prefix) netip.Addr {
	a := p.Masked().Addr().As4()
	host := 32 - p.Bits()
	v := uint32(a[0])<<24 | uint32(a[1])<<16 | uint32(a[2])<<8 | uint32(a[3])
	v |= (1<<host - 1)
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}

func (p *pass) validateTenant(w *tenantWork) error {
	t := w.t
	if t.ID <= 0 || t.ID > names.MaxTenantID {
		return fmt.Errorf("tenant id %d out of range", t.ID)
	}
	switch t.State {
	case api.TenantTerminated:
		return nil
	case api.TenantActive, api.TenantSuspended:
	default:
		return fmt.Errorf("unknown tenant state %q", t.State)
	}
	if t.VLAN < 1 || t.VLAN > 4094 {
		return fmt.Errorf("vlan %d out of range", t.VLAN)
	}
	network, err := netip.ParsePrefix(t.NetworkCIDR)
	if err != nil || !network.Addr().Is4() {
		return fmt.Errorf("network_cidr %q is not an IPv4 prefix", t.NetworkCIDR)
	}
	if network.Masked() != network {
		return fmt.Errorf("network_cidr %s is not a network address", network)
	}
	if network.Bits() > 30 {
		return fmt.Errorf("network_cidr %s is too small", network)
	}
	if t.PrefixLen != network.Bits() {
		return fmt.Errorf("prefix_len %d does not match network_cidr %s", t.PrefixLen, network)
	}
	ep, err := netip.ParseAddr(t.EndpointIP)
	if err != nil || !ep.Is4() || !network.Contains(ep) {
		return fmt.Errorf("endpoint_ip %q is not an address in %s", t.EndpointIP, network)
	}
	if ep == network.Addr() || ep == lastAddr(network) {
		return fmt.Errorf("endpoint_ip %s is the network or broadcast address", ep)
	}
	for _, n := range p.r.Host.CephPublicNetworks {
		if n.Overlaps(network) {
			return fmt.Errorf("network_cidr %s overlaps Ceph public network %s", network, n)
		}
	}
	if p.r.Host.LinkCIDR.Overlaps(network) {
		return fmt.Errorf("network_cidr %s overlaps LINK_CIDR %s", network, p.r.Host.LinkCIDR)
	}
	w.network = network
	w.endpoint = netip.PrefixFrom(ep, network.Bits())
	return nil
}

func (p *pass) validateShare(w *tenantWork, s *shareWork) error {
	if s.s.ID <= 0 {
		return fmt.Errorf("share id %d must be positive", s.s.ID)
	}
	switch s.s.State {
	case api.ShareActive, api.ShareDeleting:
	default:
		return fmt.Errorf("unknown share state %q", s.s.State)
	}
	if !w.valid || w.t.State == api.TenantTerminated || s.s.State != api.ShareActive {
		return nil
	}
	if !shareNameRe.MatchString(s.s.Name) {
		return fmt.Errorf("share name %q may only contain letters, digits, '.', '_' and '-'", s.s.Name)
	}
	if s.s.QuotaBytes <= 0 {
		return fmt.Errorf("quota_bytes must be positive")
	}
	for _, c := range s.s.Clients {
		var pfx netip.Prefix
		if strings.Contains(c, "/") {
			v, err := netip.ParsePrefix(c)
			if err != nil {
				return fmt.Errorf("client %q is not an IPv4 address or prefix", c)
			}
			pfx = v.Masked()
		} else {
			a, err := netip.ParseAddr(c)
			if err != nil {
				return fmt.Errorf("client %q is not an IPv4 address or prefix", c)
			}
			pfx = netip.PrefixFrom(a, 32)
		}
		if !pfx.Addr().Is4() || !w.network.Contains(pfx.Addr()) || pfx.Bits() < w.network.Bits() {
			return fmt.Errorf("client %q is not inside %s", c, w.network)
		}
		if pfx.Bits() == 32 {
			s.clients = append(s.clients, pfx.Addr().String())
		} else {
			s.clients = append(s.clients, pfx.String())
		}
	}
	return nil
}

func (p *pass) tenantDir(id int64) string {
	return filepath.Join(p.r.Host.StateDir, "tenants", names.TenantDir(id))
}

// applyTenant is phase A: network and Ceph for live tenants, immediate
// teardown of Ganesha and networking for terminated ones.
func (p *pass) applyTenant(w *tenantWork) {
	if !w.held {
		p.dropLocal(w.t.ID, w)
		return
	}
	if !w.valid {
		// A config that cannot be applied leaves what runs as it is.
		p.keepDHCP(names.TenantDHCPKey(w.t.ID))
		return
	}
	if w.t.State == api.TenantTerminated {
		p.teardown(w)
		return
	}
	p.ensureTenant(w)
}

func (p *pass) ensureTenant(w *tenantWork) {
	r := p.r
	id := w.t.ID
	link, err := r.State.AllocateLink(id)
	if err != nil {
		w.fail("link allocation: %v", err)
	} else {
		spec := netns.Spec{
			TenantID: id,
			VLAN:     w.t.VLAN,
			Address:  w.endpoint,
			HostAddr: link.Host,
			NSAddr:   link.NS,
			Routes:   r.Host.CephPublicNetworks,
		}
		if p.ha != nil {
			spec.MAC = netns.TenantMAC(id)
		}
		var changed bool
		err := p.whileHolding(lease.TenantKey(id), func(ctx context.Context) error {
			var err error
			changed, err = r.Net.Ensure(ctx, spec)
			if err == nil {
				p.announce(ctx, w)
				p.ensureTenantDHCP(ctx, w)
			}
			return err
		})
		if changed {
			p.namespaces[id] = true
			r.logf("tenant %d: network updated (%s)", id, spec)
		}
		if err != nil {
			w.fail("network: %v", err)
			if p.namespaces[id] && w.t.NetworkID != nil {
				// Keep a running DHCP server through a failed network
				// update; the namespace is still there.
				p.keepDHCP(names.TenantDHCPKey(id))
			}
		} else {
			w.netOK = true
			p.namespaces[id] = true
		}
	}
	if w.t.State == api.TenantSuspended {
		p.removeContainer(w)
	}
	p.ensureStorage(w)
	p.applyLimits(w)
}

// applyLimits is the hook for per-tenant bandwidth and IOPS limits. Limits
// are parsed from the config but not enforced yet.
func (p *pass) applyLimits(w *tenantWork) {
	_ = w.t.Limits
}

func (p *pass) ensureStorage(w *tenantWork) {
	r := p.r
	id := w.t.ID
	if !p.cephOK {
		w.fail("ceph unavailable: %v", p.cephErr)
		return
	}
	if !p.mayChangeCeph(id) {
		w.fail("lease lost during the pass")
		return
	}
	var access []ceph.Access
	for _, s := range w.shares {
		if !s.valid {
			if p.subvols[s.name] {
				w.hold = true
			}
			continue
		}
		switch s.s.State {
		case api.ShareActive:
			if !p.subvols[s.name] {
				if err := p.changeCeph(id, func(ctx context.Context) error {
					return r.Ceph.CreateSubvolume(ctx, s.name, s.s.QuotaBytes)
				}); err != nil {
					s.err = fmt.Sprintf("create subvolume: %v", err)
					continue
				}
				p.subvols[s.name] = true
				r.logf("tenant %d: created subvolume %s (%d bytes)", id, s.name, s.s.QuotaBytes)
			}
			info, err := r.Ceph.SubvolumeInfo(p.ctx, s.name)
			if err != nil {
				s.err = fmt.Sprintf("subvolume info: %v", err)
				w.hold = true
				continue
			}
			if info.BytesQuota != s.s.QuotaBytes {
				if err := p.changeCeph(id, func(ctx context.Context) error {
					return r.Ceph.ResizeSubvolume(ctx, s.name, s.s.QuotaBytes)
				}); err != nil {
					s.err = fmt.Sprintf("resize subvolume: %v", err)
				} else {
					r.logf("tenant %d: resized subvolume %s from %d to %d bytes", id, s.name, info.BytesQuota, s.s.QuotaBytes)
					info.BytesQuota = s.s.QuotaBytes
				}
			}
			s.info = &info
			access = append(access, ceph.Access{Path: info.Path, Pool: info.DataPool, Namespace: info.PoolNamespace})
		case api.ShareDeleting:
			if _, err := r.State.MarkDeleting(s.s.ID, p.now); err != nil {
				s.err = fmt.Sprintf("record deleting time: %v", err)
			}
			p.loadInfo(s)
		}
	}
	if w.hold {
		w.fail("cephx caps and nfs exports left unchanged until every existing share can be applied")
	}
	p.ensureUser(w, access)
}

func (p *pass) loadInfo(s *shareWork) {
	if !p.cephOK || !p.subvols[s.name] {
		return
	}
	info, err := p.r.Ceph.SubvolumeInfo(p.ctx, s.name)
	if err != nil {
		s.err = fmt.Sprintf("subvolume info: %v", err)
		return
	}
	s.info = &info
}

func (p *pass) ensureUser(w *tenantWork, access []ceph.Access) {
	r := p.r
	entity := names.CephEntity(w.t.ID)
	existing, has := p.users[entity]
	if !has && len(access) == 0 {
		w.userOK = true
		return
	}
	if r.Host.CephScopedKey {
		p.ensureUserScoped(w, access)
		return
	}
	caps := ceph.TenantCaps(access)
	if p.ha != nil {
		caps = ceph.WithRecovery(caps, r.Host.CephHAPool, names.CephUserID(w.t.ID))
	}
	switch {
	case !has:
		var key string
		err := p.changeCeph(w.t.ID, func(ctx context.Context) error {
			var err error
			key, err = r.Ceph.CreateUser(ctx, entity, caps)
			return err
		})
		if err != nil {
			w.fail("create cephx user: %v", err)
			return
		}
		p.users[entity] = ceph.User{Key: key, Caps: caps}
		r.logf("tenant %d: created cephx user %s", w.t.ID, entity)
	case existing.Caps != caps && !w.hold:
		if err := p.changeCeph(w.t.ID, func(ctx context.Context) error {
			return r.Ceph.SetCaps(ctx, entity, caps)
		}); err != nil {
			w.fail("update cephx caps: %v", err)
			return
		}
		existing.Caps = caps
		p.users[entity] = existing
		r.logf("tenant %d: updated caps of %s", w.t.ID, entity)
	}
	w.userOK = true
}

// ensureUserScoped grants and revokes subvolumes through the mgr, which
// writes the caps itself, and then reads the user back for its key. A path
// the mgr did not grant (a user created before the key was scoped) is
// authorized first so the mgr knows it and will revoke it.
func (p *pass) ensureUserScoped(w *tenantWork, access []ceph.Access) {
	r := p.r
	entity := names.CephEntity(w.t.ID)
	existing := p.users[entity]
	want := map[string]bool{}
	for _, a := range access {
		want[a.Path] = true
	}
	have := map[string]bool{}
	for _, path := range ceph.AuthorizedPaths(existing.Caps.MDS) {
		have[path] = true
	}
	var grant, revoke []string
	for _, s := range w.shares {
		if s.info != nil && want[s.info.Path] && !have[s.info.Path] {
			grant = append(grant, s.name)
		}
	}
	if !w.hold {
		for path := range have {
			if want[path] {
				continue
			}
			name, ok := names.SubvolumeOfPath(r.Host.CephSubvolumeGroup, path)
			if !ok || !p.subvols[name] {
				r.logf("tenant %d: %s keeps access to %s, which is not a subvolume of group %s", w.t.ID, entity, path, r.Host.CephSubvolumeGroup)
				continue
			}
			revoke = append(revoke, name)
		}
	}
	sort.Strings(grant)
	sort.Strings(revoke)
	if len(grant) == 0 && len(revoke) == 0 {
		w.userOK = true
		return
	}
	err := p.changeCeph(w.t.ID, func(ctx context.Context) error {
		for _, name := range grant {
			if err := r.Ceph.Authorize(ctx, entity, name); err != nil {
				return fmt.Errorf("authorize %s: %w", name, err)
			}
		}
		for _, name := range revoke {
			if err := r.Ceph.Authorize(ctx, entity, name); err != nil {
				return fmt.Errorf("authorize %s before revoking it: %w", name, err)
			}
			if err := r.Ceph.Deauthorize(ctx, entity, name); err != nil {
				return fmt.Errorf("deauthorize %s: %w", name, err)
			}
		}
		return nil
	})
	users, rerr := r.Ceph.Users(p.ctx, []string{entity})
	if rerr == nil {
		if u, ok := users[entity]; ok {
			p.users[entity] = u
		} else {
			delete(p.users, entity)
		}
	}
	switch {
	case err != nil:
		w.fail("update cephx access: %v", err)
		return
	case rerr != nil:
		w.fail("read cephx user: %v", rerr)
		return
	}
	if len(grant) > 0 {
		r.logf("tenant %d: authorized %s for %s", w.t.ID, entity, strings.Join(grant, ", "))
	}
	if len(revoke) > 0 {
		r.logf("tenant %d: deauthorized %s for %s", w.t.ID, entity, strings.Join(revoke, ", "))
	}
	w.userOK = true
}

func (p *pass) removeContainer(w *tenantWork) bool {
	c, ok := p.containers[w.t.ID]
	if !ok {
		return true
	}
	if err := p.r.NFS.Remove(p.ctx, c.Name); err != nil {
		w.fail("stop nfs: %v", err)
		return false
	}
	delete(p.containers, w.t.ID)
	p.r.logf("tenant %d: nfs stopped", w.t.ID)
	return true
}

func (p *pass) teardown(w *tenantWork) {
	r := p.r
	id := w.t.ID
	defer func() {
		for _, s := range w.shares {
			p.loadInfo(s)
		}
	}()
	if !p.removeContainer(w) {
		return
	}
	_, linked := r.State.Link(id)
	if p.namespaces[id] || linked {
		// The DHCP socket keeps the namespace alive; close it first.
		p.stopDHCP(names.TenantDHCPKey(id))
		if err := r.Net.Remove(p.ctx, id); err != nil {
			w.fail("remove network: %v", err)
			return
		}
		delete(p.namespaces, id)
		if err := r.State.ReleaseLink(id); err != nil {
			w.fail("release link: %v", err)
		}
		r.logf("tenant %d: network removed", id)
	}
	dir := p.tenantDir(id)
	if _, err := os.Stat(dir); err == nil {
		if err := os.RemoveAll(dir); err != nil {
			w.fail("remove %s: %v", dir, err)
		} else {
			r.logf("tenant %d: removed %s", id, dir)
		}
	}
}

// applyFirewall is phase B. Active tenants may reach Ceph; so may tenants
// the config does not validly describe, so their current state is kept.
func (p *pass) applyFirewall(works []*tenantWork) {
	r := p.r
	described := map[int64]bool{}
	allowed := map[int64]bool{}
	for _, w := range works {
		if !w.valid {
			continue
		}
		described[w.t.ID] = true
		if w.t.State == api.TenantActive {
			allowed[w.t.ID] = true
		}
	}
	for id := range p.namespaces {
		if !described[id] {
			allowed[id] = true
		}
	}
	var links []string
	for id := range allowed {
		if _, ok := r.State.Link(id); ok {
			links = append(links, names.HostVeth(id))
		}
	}
	sort.Strings(links)
	rs := firewall.Ruleset{Links: links, CephNets: r.Host.CephPublicNetworks, LinkCIDR: r.Host.LinkCIDR}
	changed, err := r.Firewall.Apply(p.ctx, rs)
	if err != nil {
		p.nodeErr("firewall: %v", err)
		r.logf("firewall: %v", err)
		return
	}
	if changed {
		r.logf("firewall applied: %s", rs)
	}
}

// applyNFS is phase C: run, reload or stop each tenant's Ganesha.
func (p *pass) applyNFS(w *tenantWork) {
	if !w.valid || !w.held {
		return
	}
	_, running := p.containers[w.t.ID]
	if w.t.State != api.TenantActive {
		w.exportsCurrent = !running
		return
	}
	if !p.cephOK || w.hold {
		return
	}
	var exports []ganesha.Export
	for _, s := range w.shares {
		if !s.valid || s.s.State != api.ShareActive {
			continue
		}
		if s.info == nil {
			if p.subvols[s.name] {
				// An existing share whose state is unknown this pass: keep
				// Ganesha as it is rather than drop the export.
				return
			}
			continue
		}
		exportID, err := p.r.State.ExportID(w.t.ID, s.s.ID)
		if err != nil {
			s.err = fmt.Sprintf("export id: %v", err)
			s.info = nil
			continue
		}
		exports = append(exports, ganesha.Export{
			ID:       int64(exportID),
			Path:     s.info.Path,
			Pseudo:   "/" + s.s.Name,
			Clients:  s.clients,
			ReadOnly: s.s.ReadOnly,
		})
	}
	if len(exports) == 0 {
		w.exportsCurrent = p.removeContainer(w)
		return
	}
	if !w.netOK || !w.userOK {
		return
	}
	if err := p.ensureNFS(w, exports); err != nil {
		w.fail("nfs: %v", err)
		return
	}
	w.exportsCurrent = true
}

func (p *pass) minimalConf() (string, error) {
	if p.minimal != nil {
		return *p.minimal, nil
	}
	conf, err := p.r.Ceph.MinimalConf(p.ctx)
	if err != nil {
		return "", fmt.Errorf("ceph minimal conf: %w", err)
	}
	if !strings.HasSuffix(conf, "\n") {
		conf += "\n"
	}
	p.minimal = &conf
	return conf, nil
}

func writeIfChanged(path string, data []byte, mode os.FileMode) (bool, error) {
	if cur, err := os.ReadFile(path); err == nil && bytes.Equal(cur, data) {
		return false, os.Chmod(path, mode)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, mode); err != nil {
		return false, err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return false, err
	}
	return true, os.Rename(tmp, path)
}

func (p *pass) ensureNFS(w *tenantWork, exports []ganesha.Export) error {
	r := p.r
	id := w.t.ID
	entity := names.CephEntity(id)
	user, ok := p.users[entity]
	if !ok {
		return errors.New("cephx user missing")
	}
	cephConf, err := p.minimalConf()
	if err != nil {
		return err
	}
	keyring := fmt.Sprintf("[%s]\n\tkey = %s\n", entity, user.Key)
	recoveryKeyring := ""
	gcfg := ganesha.Config{
		TenantID:   id,
		BindAddr:   w.endpoint.Addr(),
		UserID:     names.CephUserID(id),
		Filesystem: r.Host.CephFSName,
		Exports:    exports,
	}
	// hostname names this tenant's Ganesha to the MDS, so evictions can
	// target it alone; without leases 0.2.x behaviour is kept.
	hostname := ""
	if p.ha != nil {
		hostname = names.Container(id)
		gcfg.RADOS = &ganesha.RADOSRecovery{
			Pool:          r.Host.CephHAPool,
			Namespace:     names.CephUserID(id),
			NodeID:        names.CephUserID(id),
			LeaseLifetime: p.ha.timings.NFSLeaseLifetime,
			GracePeriod:   p.ha.timings.NFSGracePeriod,
		}
		// The mgr grants tenant users no access to the HA pool, so with a
		// scoped key the recovery database uses the shared recovery login.
		if r.Host.CephScopedKey {
			recKey, err := ceph.ReadKeyring(r.Host.CephRecoveryKeyring, r.Host.CephRecoveryName)
			if err != nil {
				return fmt.Errorf("recovery keyring: %w", err)
			}
			gcfg.RADOS.UserID = strings.TrimPrefix(r.Host.CephRecoveryName, "client.")
			recoveryKeyring = fmt.Sprintf("[%s]\n\tkey = %s\n", r.Host.CephRecoveryName, recKey)
		}
	}
	core, exp := ganesha.Render(gcfg)

	dir := p.tenantDir(id)
	confDir := filepath.Join(dir, "ganesha")
	cephDir := filepath.Join(dir, "ceph")
	recDir := filepath.Join(dir, "recovery")
	for _, d := range []string{confDir, cephDir, recDir} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return err
		}
	}
	if _, err := writeIfChanged(filepath.Join(cephDir, "ceph.conf"), []byte(cephConf), 0o644); err != nil {
		return err
	}
	if _, err := writeIfChanged(filepath.Join(cephDir, "ceph."+entity+".keyring"), []byte(keyring), 0o600); err != nil {
		return err
	}
	if recoveryKeyring != "" {
		if _, err := writeIfChanged(filepath.Join(cephDir, "ceph."+r.Host.CephRecoveryName+".keyring"), []byte(recoveryKeyring), 0o600); err != nil {
			return err
		}
	}
	mounts := []ganesha.Mount{
		{Source: confDir, Target: ganesha.ConfDir, ReadOnly: true},
		{Source: cephDir, Target: ganesha.CephDir, ReadOnly: true},
		{Source: recDir, Target: ganesha.RecoveryDir},
	}
	h := sha256.New()
	parts := []string{r.Host.GaneshaImage, names.NetnsPath(id), core, cephConf, keyring}
	if recoveryKeyring != "" {
		parts = append(parts, recoveryKeyring)
	}
	for _, part := range parts {
		h.Write([]byte(part))
		h.Write([]byte{0})
	}
	for _, m := range mounts {
		fmt.Fprintf(h, "%s:%s:%v\x00", m.Source, m.Target, m.ReadOnly)
	}
	if hostname != "" {
		fmt.Fprintf(h, "hostname:%s\x00", hostname)
	}
	spec := ganesha.Spec{
		Name:     names.Container(id),
		TenantID: id,
		Image:    r.Host.GaneshaImage,
		Netns:    names.NetnsPath(id),
		CoreHash: hex.EncodeToString(h.Sum(nil))[:32],
		Mounts:   mounts,
		Hostname: hostname,
	}

	confPath := filepath.Join(confDir, "ganesha.conf")
	conf := []byte(core + exp)
	// appliedPath holds the hash of the config Ganesha last loaded; it is
	// written only after a successful start or reload, so a failed reload is
	// retried on the next pass.
	appliedPath := filepath.Join(dir, "ganesha.applied")
	sum := sha256.Sum256(conf)
	confHash := []byte(hex.EncodeToString(sum[:]) + "\n")
	cur, has := p.containers[id]
	if !has || !cur.Running || cur.CoreHash != spec.CoreHash {
		if _, err := writeIfChanged(confPath, conf, 0o644); err != nil {
			return err
		}
		if has {
			if err := r.NFS.Remove(p.ctx, cur.Name); err != nil {
				return err
			}
			delete(p.containers, id)
		}
		err := p.whileHolding(lease.TenantKey(id), func(ctx context.Context) error {
			// A previous Ganesha for this tenant may still hold CephFS caps;
			// the new one would stall until the MDS timed that session out.
			// With leases only the tenant's Ganesha sessions are evicted, so
			// the SFTP master's mounts of the same user survive.
			if err := r.Ceph.EvictSessions(ctx, entity, hostname); err != nil {
				r.logf("tenant %d: evict old cephfs sessions: %v", id, err)
			}
			return r.NFS.Run(ctx, spec)
		})
		if err != nil {
			return err
		}
		p.containers[id] = ganesha.Container{Name: spec.Name, TenantID: id, Running: true, CoreHash: spec.CoreHash}
		r.logf("tenant %d: nfs started with %d exports", id, len(exports))
		_, err = writeIfChanged(appliedPath, confHash, 0o600)
		return err
	}
	if _, err := writeIfChanged(confPath, conf, 0o644); err != nil {
		return err
	}
	if applied, err := os.ReadFile(appliedPath); err == nil && bytes.Equal(applied, confHash) {
		return nil
	}
	if err := p.whileHolding(lease.TenantKey(id), func(ctx context.Context) error { return r.NFS.Reload(ctx, cur.Name) }); err != nil {
		return err
	}
	r.logf("tenant %d: nfs exports reloaded (%d exports)", id, len(exports))
	_, err = writeIfChanged(appliedPath, confHash, 0o600)
	return err
}

func (p *pass) tenantDue(t api.Tenant) bool {
	if !p.graceOK || t.DestroyAfter == nil || t.TerminatedAt == nil {
		return false
	}
	return p.now.After(t.DestroyAfter.Time) && p.now.Sub(t.TerminatedAt.Time) >= p.grace
}

func (p *pass) shareDue(s api.Share, since time.Time) bool {
	if !p.graceOK || s.DestroyAfter == nil {
		return false
	}
	return p.now.After(s.DestroyAfter.Time) && p.now.Sub(since) >= p.grace
}

// destroy is phase D: data of terminated tenants and deleting shares, only
// after both destroy checks pass and nothing serves the data any more.
func (p *pass) destroy(w *tenantWork) {
	r := p.r
	if !w.valid || !w.held {
		return
	}
	id := w.t.ID
	if w.t.State == api.TenantTerminated {
		if !p.tenantDue(w.t) {
			return
		}
		if !p.cephOK {
			w.fail("destroy due but ceph unavailable: %v", p.cephErr)
			return
		}
		if !p.mayChangeCeph(id) {
			w.fail("destroy due but the lease was lost during the pass")
			return
		}
		if _, ok := p.containers[id]; ok || p.namespaces[id] {
			w.fail("destroy due but nfs or network still present")
			return
		}
		ok := true
		for _, s := range w.shares {
			if s.s.ID <= 0 || !p.subvols[s.name] {
				continue
			}
			if err := p.changeCeph(id, func(ctx context.Context) error {
				return r.Ceph.RemoveSubvolume(ctx, s.name)
			}); err != nil {
				s.err = fmt.Sprintf("destroy subvolume: %v", err)
				ok = false
				continue
			}
			delete(p.subvols, s.name)
			s.info = nil
			r.logf("tenant %d: destroyed subvolume %s", id, s.name)
		}
		entity := names.CephEntity(id)
		if _, has := p.users[entity]; has {
			if err := p.changeCeph(id, func(ctx context.Context) error {
				return r.Ceph.RemoveUser(ctx, entity)
			}); err != nil {
				w.fail("remove cephx user: %v", err)
				ok = false
			} else {
				delete(p.users, entity)
				r.logf("tenant %d: removed cephx user %s", id, entity)
			}
		}
		if ok && p.destroyLease(w) {
			w.destroyed = true
			r.logf("tenant %d: destroyed", id)
		}
		return
	}
	if !p.cephOK || !w.exportsCurrent || !p.mayChangeCeph(id) {
		return
	}
	for _, s := range w.shares {
		if !s.valid || s.s.State != api.ShareDeleting || s.err != "" {
			continue
		}
		since, err := r.State.MarkDeleting(s.s.ID, p.now)
		if err != nil || !p.shareDue(s.s, since) {
			continue
		}
		if p.subvols[s.name] {
			if err := p.changeCeph(id, func(ctx context.Context) error {
				return r.Ceph.RemoveSubvolume(ctx, s.name)
			}); err != nil {
				s.err = fmt.Sprintf("destroy subvolume: %v", err)
				continue
			}
			delete(p.subvols, s.name)
			r.logf("tenant %d: destroyed subvolume %s of deleted share %d", id, s.name, s.s.ID)
		}
		s.destroyed = true
		s.info = nil
	}
}

// pruneState forgets deleting marks of shares that are no longer deleting and
// export ids of shares that a validly described tenant no longer lists or
// that were destroyed.
func (p *pass) pruneState(works []*tenantWork) {
	keep := map[int64]bool{}
	listed := map[int64]map[int64]bool{}
	for _, w := range works {
		live := map[int64]bool{}
		for _, s := range w.shares {
			if s.s.State == api.ShareDeleting {
				keep[s.s.ID] = true
			}
			live[s.s.ID] = !w.destroyed && !s.destroyed
		}
		if w.valid {
			listed[w.t.ID] = live
		}
	}
	if err := p.r.State.PruneDeleting(keep); err != nil {
		p.nodeErr("state: %v", err)
	}
	if err := p.r.State.PruneExports(listed); err != nil {
		p.nodeErr("state: %v", err)
	}
}

func strPtr(s string) *string { return &s }

func (p *pass) fillStatus(st *api.Status, works []*tenantWork) {
	for _, w := range works {
		if w.destroyed {
			st.DestroyedTenants = append(st.DestroyedTenants, w.t.ID)
			continue
		}
		if !p.reportTenant(w) {
			continue
		}
		ts := api.TenantStatus{ID: w.t.ID}
		switch {
		case len(w.errs) > 0:
			ts.State = api.StateError
			ts.Error = strPtr(strings.Join(w.errs, "; "))
		case w.t.State == api.TenantActive:
			ts.State = api.StateReady
		case w.t.State == api.TenantSuspended:
			ts.State = api.StateSuspended
		default:
			ts.State = api.StatePending
		}
		for _, s := range w.shares {
			if s.destroyed {
				st.DestroyedShares = append(st.DestroyedShares, s.s.ID)
				continue
			}
			ss := api.ShareStatus{ID: s.s.ID}
			if s.info != nil {
				ss.UsedBytes = s.info.BytesUsed
			}
			switch {
			case s.err != "":
				ss.State = api.StateError
				ss.Error = strPtr(s.err)
			case !w.valid:
				ss.State = api.StateError
				ss.Error = strPtr("tenant config is invalid")
			case w.t.State == api.TenantTerminated || s.s.State == api.ShareDeleting:
				ss.State = api.StatePending
			case w.t.State == api.TenantSuspended:
				ss.State = api.StateSuspended
			case s.info == nil:
				ss.State = api.StateError
				ss.Error = strPtr("not applied, see the tenant error")
			default:
				ss.State = api.StateReady
			}
			ts.Shares = append(ts.Shares, ss)
		}
		st.Tenants = append(st.Tenants, ts)
	}
}
