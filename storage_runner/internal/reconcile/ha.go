package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
)

// errNotHolder stops work that needs a lease this node no longer holds.
var errNotHolder = errors.New("lease not held")

// haPass is the lease view of one pass. It is nil when the config has no ha
// block (an older Rackflow): the runner then serves the tenants in its
// config without leases, exactly as 0.2.x did.
type haPass struct {
	cfg     *api.HA
	node    api.Node
	timings lease.Timings
	sftp    *api.SFTPSettings
}

// configPath is where the last config from Rackflow is kept, so a runner
// that starts while Rackflow is unreachable can still take its leases.
func (r *Reconciler) configPath() string {
	return filepath.Join(r.Host.StateDir, "config.json")
}

func (r *Reconciler) saveConfig(cfg *api.Config) {
	r.lastConfig = cfg
	data, err := json.Marshal(cfg)
	if err != nil {
		return
	}
	if err := os.MkdirAll(r.Host.StateDir, 0o700); err != nil {
		r.logf("save config: %v", err)
		return
	}
	if _, err := writeIfChanged(r.configPath(), data, 0o600); err != nil {
		r.logf("save config: %v", err)
	}
}

func (r *Reconciler) savedConfig() *api.Config {
	if r.lastConfig != nil {
		return r.lastConfig
	}
	data, err := os.ReadFile(r.configPath())
	if err != nil {
		return nil
	}
	var cfg api.Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		r.logf("saved config %s is unreadable: %v", r.configPath(), err)
		return nil
	}
	r.lastConfig = &cfg
	return &cfg
}

// Kick asks the loop for a pass now, for example after a lease was gained.
func (r *Reconciler) Kick() {
	select {
	case r.kickCh() <- struct{}{}:
	default:
	}
}

func (r *Reconciler) kickCh() chan struct{} {
	r.kickOnce.Do(func() { r.kick = make(chan struct{}, 1) })
	return r.kick
}

func indexOf(list []int64, v int64) int {
	for i, x := range list {
		if x == v {
			return i
		}
	}
	return -1
}

func tenantGuard(id int64) lease.GuardInfo {
	return lease.GuardInfo{Netns: names.Namespace(id), Link: names.VLANIface(id), Container: names.Container(id)}
}

// setupHA hands the lease part of the config to the lease manager and
// records which tenants this node may serve in this pass. Without an ha
// block every valid tenant in the config is served, as before leases.
func (p *pass) setupHA(cfg *api.Config, works []*tenantWork, nets []*netWork) {
	r := p.r
	if cfg.HA == nil {
		if r.leaseStarted {
			// Rackflow no longer sends an ha block: hand every lease back.
			r.Lease.SetDesired(lease.Desired{Timings: lease.DefaultTimings()})
		}
		for _, w := range works {
			w.held = true
		}
		// Without leases a network is served by the node it is listed for.
		for _, nw := range nets {
			nw.held = true
		}
		return
	}
	if r.Lease == nil {
		p.nodeErr("ha: the config has an ha block but this runner has no lease client; nothing is served")
		return
	}
	if cfg.Node.ID <= 0 {
		p.nodeErr("ha: the config does not name this node; nothing is served")
		return
	}
	if !r.leaseStarted {
		r.Lease.Node = lease.NodeInfo{ID: cfg.Node.ID, LocationID: cfg.Node.LocationID, Name: cfg.Node.Name, Hostname: r.Hostname}
	} else if r.Lease.Node.ID != cfg.Node.ID {
		p.nodeErr("ha: the config names node %d but this runner holds leases as node %d; restart the runner", cfg.Node.ID, r.Lease.Node.ID)
		return
	}
	t, err := lease.TimingsFromConfig(cfg.HA)
	if err != nil {
		p.nodeErr("ha: %v; keeping the previous timings", err)
		t = lease.DefaultTimings()
		if r.haTimings != nil {
			t = *r.haTimings
		}
	}
	r.haTimings = &t
	p.ha = &haPass{cfg: cfg.HA, node: cfg.Node, timings: t, sftp: cfg.SFTP}

	d := lease.Desired{Enabled: cfg.HA.Enabled, Draining: cfg.HA.Draining, Timings: t}
	for _, n := range cfg.HA.Nodes {
		d.Nodes = append(d.Nodes, n.ID)
	}
	me := cfg.Node.ID
	for _, w := range works {
		id := w.t.ID
		if r.destroyedLeases[id] {
			continue
		}
		want := lease.Want{
			Key:           lease.TenantKey(id),
			Rank:          -1,
			FenceEntities: []string{names.CephEntity(id)},
			FenceHostname: names.Container(id),
			Guard:         tenantGuard(id),
		}
		switch {
		case !w.valid:
			// Keep a lease already held, but take nothing on a config
			// that cannot be applied.
			want.Acquire = true
		case w.t.State == api.TenantTerminated:
			destroyer := w.t.PreferredNodeID
			if destroyer == 0 && len(w.t.TakeoverOrder) > 0 {
				destroyer = w.t.TakeoverOrder[0]
			}
			if !cfg.HA.Enabled {
				destroyer = me
			}
			want.Guard = lease.GuardInfo{}
			if destroyer == me {
				want.Acquire, want.Rank = true, 0
			} else {
				want.Release = true
			}
		default:
			if cfg.HA.Enabled {
				want.Rank = indexOf(w.t.TakeoverOrder, me)
				want.Acquire = want.Rank >= 0
			} else {
				want.Acquire, want.Rank = true, 0
			}
			want.Defer = tenantDefer(w.t)
			if mv := w.t.Move; mv != nil && mv.ToNodeID != 0 {
				want.MoveTo, want.MoveID = mv.ToNodeID, mv.ID
				if mv.ToNodeID == me {
					want.Acquire = true
				}
			}
		}
		d.Wants = append(d.Wants, want)
	}
	if sw, ok := p.sftpWant(cfg); ok {
		d.Wants = append(d.Wants, sw)
	}
	d.Wants = append(d.Wants, netWants(cfg, nets)...)
	r.Lease.SetDesired(d)
	if !r.leaseStarted {
		if r.Lease.Stop == nil {
			r.Lease.Stop = r.StopLease
		}
		if r.Lease.Changed == nil {
			r.Lease.Changed = r.Kick
		}
		r.leaseStarted = true
		// Re-take this node's own leases before deciding what to serve, so a
		// runner restart does not tear down what it still holds.
		r.Lease.Sync(p.ctx)
		if r.RunLease {
			go r.Lease.Run(p.ctx)
		}
	}
	// A lease about to be handed off is not served or acted on any more.
	releasing := map[lease.Key]bool{}
	for _, want := range d.Wants {
		if want.Release || (d.Draining && (want.Key.Kind == lease.KindTenant || want.Key.Kind == lease.KindNet)) {
			releasing[want.Key] = true
		}
	}
	for _, w := range works {
		k := lease.TenantKey(w.t.ID)
		w.held = r.Lease.Holds(k) && !releasing[k]
	}
	for _, nw := range nets {
		k := lease.NetKey(nw.n.ID)
		nw.held = r.Lease.Holds(k) && !releasing[k]
	}
}

// whileHolding runs fn, which starts serving what lease k covers or changes
// the tenant's Ceph state, only if this node holds k. StopLease waits for it,
// so serving never outlives the lease; fn's context ends after holdBound so
// that wait stays inside the stop margin.
func (p *pass) whileHolding(k lease.Key, fn func(ctx context.Context) error) error {
	if p.ha == nil {
		return fn(p.ctx)
	}
	p.r.serveMu.Lock()
	defer p.r.serveMu.Unlock()
	if !p.r.Lease.Holds(k) {
		return errNotHolder
	}
	ctx, cancel := context.WithTimeout(p.ctx, holdBound(p.ha.timings))
	defer cancel()
	return fn(ctx)
}

// holdBound is how long whileHolding may keep StopLease waiting: a second
// less than the stop margin, so a lost lease is stopped before the OSD lets
// it go.
func holdBound(t lease.Timings) time.Duration {
	if b := t.StopMargin - time.Second; b >= time.Second {
		return b
	}
	return time.Second
}

// changeCeph runs fn, which changes a tenant's Ceph state (subvolumes,
// quotas, caps, destruction), only while this node holds the tenant's lease.
func (p *pass) changeCeph(id int64, fn func(ctx context.Context) error) error {
	return p.whileHolding(lease.TenantKey(id), fn)
}

// mayChangeCeph says whether this node may change a tenant's Ceph state
// (subvolumes, quotas, caps, destruction): only its lease holder may.
func (p *pass) mayChangeCeph(id int64) bool {
	return p.ha == nil || p.r.Lease.Holds(lease.TenantKey(id))
}

// announce sends gratuitous ARP for the tenant's endpoint once per lease
// acquisition, so switches and clients move to this node at once.
func (p *pass) announce(ctx context.Context, w *tenantWork) {
	if p.ha == nil {
		return
	}
	r := p.r
	id := w.t.ID
	since := r.Lease.Since(lease.TenantKey(id))
	if r.announced == nil {
		r.announced = map[int64]time.Time{}
	}
	if cur, ok := r.announced[id]; ok && cur.Equal(since) {
		return
	}
	if err := r.Net.Announce(ctx, id, w.endpoint.Addr()); err != nil {
		r.logf("tenant %d: gratuitous arp: %v", id, err)
		return
	}
	r.announced[id] = since
}

// dropLocal removes what a tenant this node does not hold has here: its
// Ganesha container and its network. Ceph state is never touched.
func (p *pass) dropLocal(id int64, w *tenantWork) {
	r := p.r
	_, hasCtr := p.containers[id]
	_, linked := r.State.Link(id)
	if !hasCtr && !p.namespaces[id] && !linked {
		return
	}
	fail := func(format string, args ...any) {
		if w != nil {
			w.fail(format, args...)
		}
		r.logf("tenant %d: "+format, append([]any{id}, args...)...)
	}
	if w != nil {
		w.hadLocal = true
	}
	if c, ok := p.containers[id]; ok {
		if err := r.NFS.Remove(p.ctx, c.Name); err != nil {
			fail("stop nfs of a tenant not held here: %v", err)
			return
		}
		delete(p.containers, id)
		r.logf("tenant %d: not held here; nfs removed", id)
	}
	if p.namespaces[id] || linked {
		p.stopDHCP(names.TenantDHCPKey(id))
		if err := r.Net.Remove(p.ctx, id); err != nil {
			fail("remove network of a tenant not held here: %v", err)
			return
		}
		delete(p.namespaces, id)
		if err := r.State.ReleaseLink(id); err != nil {
			fail("release link: %v", err)
		}
		r.logf("tenant %d: not held here; network removed", id)
	}
	if r.announced != nil {
		delete(r.announced, id)
	}
}

// dropUnlisted removes local containers and networks of tenants the config
// does not list, when leases are in use: nothing may be served here without
// a lease.
func (p *pass) dropUnlisted(works []*tenantWork) {
	if p.ha == nil {
		return
	}
	listed := map[int64]bool{}
	for _, w := range works {
		listed[w.t.ID] = true
	}
	var ids []int64
	for _, id := range p.hostTenants() {
		if !listed[id] && !p.r.Lease.Holds(lease.TenantKey(id)) {
			ids = append(ids, id)
		}
	}
	for _, id := range ids {
		if _, ok := p.containers[id]; ok || p.namespaces[id] {
			p.dropLocal(id, nil)
		}
	}
}

// StopLease stops serving what a lease covered, at once: for a tenant its
// VLAN interface goes down and its Ganesha and DHCP server stop; for a
// network presence its interface goes down and its DHCP server stops; for
// the SFTP master lease the gateway stops and the virtual IP is removed. The
// lease manager calls it when a lease is lost or released.
func (r *Reconciler) StopLease(k lease.Key, g lease.GuardInfo) {
	r.serveMu.Lock()
	defer r.serveMu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	switch k.Kind {
	case lease.KindTenant:
		id := k.ID
		if r.DHCP != nil {
			r.DHCP.Stop(names.TenantDHCPKey(id))
		}
		if err := r.Net.LinkDown(ctx, id); err != nil {
			r.logf("tenant %d: lease ended; set interface down: %v", id, err)
		}
		if err := r.NFS.Stop(ctx, names.Container(id)); err != nil {
			r.logf("tenant %d: lease ended; stop nfs: %v", id, err)
		} else {
			r.logf("tenant %d: lease ended; nfs stopped", id)
		}
	case lease.KindSFTP:
		r.stopSFTPMaster(ctx, g)
	case lease.KindNet:
		id := k.ID
		if r.DHCP != nil {
			r.DHCP.Stop(names.NetworkDHCPKey(id))
		}
		if err := r.Net.PresenceLinkDown(ctx, id); err != nil {
			r.logf("network %d: lease ended; set interface down: %v", id, err)
		} else {
			r.logf("network %d: lease ended; presence down", id)
		}
	}
	r.Kick()
}

// destroyLease removes a destroyed tenant's lease object and its Ganesha
// recovery namespace.
func (p *pass) destroyLease(w *tenantWork) bool {
	if p.ha == nil {
		return true
	}
	id := w.t.ID
	if err := p.r.Lease.Remove(p.ctx, lease.TenantKey(id), names.CephUserID(id)); err != nil {
		w.fail("remove lease: %v", err)
		return false
	}
	if p.r.destroyedLeases == nil {
		p.r.destroyedLeases = map[int64]bool{}
	}
	p.r.destroyedLeases[id] = true
	p.r.logf("tenant %d: removed lease and recovery namespace", id)
	return true
}

// fillHAStatus adds the lease state, per-tenant serving and the pending
// events.
func (p *pass) fillHAStatus(st *api.Status, works []*tenantWork) {
	if p.ha == nil {
		return
	}
	r := p.r
	hs := r.Lease.Status()
	st.HA = &hs
	if hs.Fenced {
		msg := "this node is fenced by Ceph"
		if hs.FencedUntil != nil {
			msg += " until " + hs.FencedUntil.Format(time.RFC3339)
		}
		if st.Node.Error != nil {
			msg = *st.Node.Error + "; " + msg
		}
		st.Node.Error = &msg
	}
	st.Events = r.Lease.Events()
	byID := map[int64]*tenantWork{}
	for _, w := range works {
		byID[w.t.ID] = w
	}
	for i := range st.Tenants {
		ts := &st.Tenants[i]
		w := byID[ts.ID]
		if w == nil {
			continue
		}
		serving := w.held && w.t.State != api.TenantTerminated
		ts.Serving = &serving
		if serving {
			if since := r.Lease.Since(lease.TenantKey(w.t.ID)); !since.IsZero() {
				ts.Since = &since
			}
			if mv := w.t.Move; mv != nil && mv.ToNodeID == p.ha.node.ID {
				ts.MoveID = mv.ID
			}
		}
	}
}

// reportTenant says whether a tenant gets an entry in the status: with
// leases only tenants this node serves, is destroying, or has just stopped
// serving, and on the SFTP master every tenant whose SFTP it serves (with
// serving false when its NFS runs elsewhere).
func (p *pass) reportTenant(w *tenantWork) bool {
	if p.ha == nil || w.held || w.hadLocal {
		return true
	}
	if p.sftpMaster && p.sftp != nil {
		_, ok := p.sftp.tenants[w.t.ID]
		return ok
	}
	return false
}
