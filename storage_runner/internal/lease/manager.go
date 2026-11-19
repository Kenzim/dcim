package lease

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"rackflow/storage_runner/internal/api"
)

// Fencer is the part of the Ceph CLI a fence needs.
type Fencer interface {
	EvictSessions(ctx context.Context, entity, hostname string) error
	EvictAddrSessions(ctx context.Context, ip netip.Addr) (int, error)
	DaemonAddrs(ctx context.Context) ([]netip.Addr, error)
}

// NodeInfo is this node as written to its node record.
type NodeInfo struct {
	ID         int64
	LocationID int64
	Name       string
	Hostname   string
}

// GuardInfo tells the guard how to stop serving a lease whose deadline has
// passed: a tenant's namespace, VLAN link and container, or the SFTP VIP.
type GuardInfo struct {
	Netns        string `json:"netns,omitempty"`
	Link         string `json:"link,omitempty"`
	Container    string `json:"container,omitempty"`
	VIP          string `json:"vip,omitempty"`
	VIPInterface string `json:"vip_interface,omitempty"`
}

// Want is what the config says about one lease.
type Want struct {
	Key Key
	// Acquire allows taking the lease when it is free and it is this
	// node's turn.
	Acquire bool
	// Rank is this node's position in the takeover order; negative means
	// never (except for a move that targets this node).
	Rank   int
	MoveTo int64
	MoveID string
	// Release makes the holder hand the lease off.
	Release bool
	// FenceEntities and FenceHostname describe a tenant fence: evict the
	// sessions of each entity whose client hostname is FenceHostname, or
	// the previous holder's node hostname when FenceHostname is empty.
	FenceEntities []string
	FenceHostname string
	Guard         GuardInfo
}

// Desired is the lease part of the current config.
type Desired struct {
	Enabled  bool
	Draining bool
	Timings  Timings
	// Nodes are the other nodes of the location, whose records are read on
	// every poll to judge whether they are alive.
	Nodes []int64
	Wants []Want
}

// Manager holds, renews, acquires, fences and releases leases. RenewTick,
// CheckDeadlines and PollTick are the three loops of Run; tests call them
// directly with a fake clock.
type Manager struct {
	Store   Store
	Fencer  Fencer
	Clock   Clock
	Node    NodeInfo
	Version string
	// DeadlineDir is where the guard's deadline files go; empty for none.
	DeadlineDir string
	// CallTimeout bounds each Store call; default 10 s.
	CallTimeout time.Duration
	Logf        func(format string, args ...any)
	// Stop stops serving a lease now (section 5.6 steps 1 and 2, or the
	// SFTP gateway, VIP and back ends) and returns once that is done. g is
	// what the lease's deadline file names.
	Stop func(k Key, g GuardInfo)
	// Changed is called after a lease is gained or lost.
	Changed func()
	// Alive is called after every renewal tick (the systemd watchdog).
	Alive func()

	// pollMu serialises PollTick, which the reconciler also calls.
	pollMu      sync.Mutex
	mu          sync.Mutex
	desired     Desired
	haveDesired bool
	leases      map[Key]*held
	watch       map[Key]*watchState
	nodes       map[int64]*nodeObs
	released    map[Key]string // token of a lease this process released
	handedOff   map[Key]string // move id a lease was handed off for
	leftover    map[Key]bool   // deadline files found at start
	seq         int64
	ident       Identity
	cephOK      bool
	// cephUpSince is the boot time Ceph last became reachable; records read
	// before then could not show a change, so freshness counts from it.
	cephUpSince time.Duration
	fenced      bool
	fencedUntil *time.Time
	events      []api.Event
	gen         int64
	loaded      bool
	running     bool
}

type held struct {
	gen          int64
	token        string
	cookie       string
	wallDeadline time.Time
	bootDeadline time.Duration
	since        time.Time
	servable     bool
	releasing    bool
	guard        GuardInfo
}

type watchState struct {
	freeSince  time.Duration // boot clock; 0 when not seen free
	lockerAddr string
	lockerTag  string
}

type nodeObs struct {
	seen    bool
	seq     int64
	changed time.Duration
	rec     NodeRecord
}

const maxEvents = 200

func (m *Manager) logf(format string, args ...any) {
	if m.Logf != nil {
		m.Logf(format, args...)
	}
}

func (m *Manager) init() {
	if m.leases == nil {
		m.leases = map[Key]*held{}
		m.watch = map[Key]*watchState{}
		m.nodes = map[int64]*nodeObs{}
		m.released = map[Key]string{}
		m.handedOff = map[Key]string{}
		m.leftover = map[Key]bool{}
	}
}

func (m *Manager) timings() Timings {
	if m.haveDesired {
		return m.desired.Timings
	}
	return DefaultTimings()
}

func (m *Manager) callCtx(ctx context.Context) (context.Context, context.CancelFunc) {
	d := m.CallTimeout
	if d <= 0 {
		d = 10 * time.Second
	}
	return context.WithTimeout(ctx, d)
}

// SetDesired installs the lease part of a new config.
func (m *Manager) SetDesired(d Desired) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.init()
	m.desired = d
	m.haveDesired = true
}

func (m *Manager) expired(h *held, now time.Time, boot time.Duration) bool {
	return !now.Before(h.wallDeadline) || boot >= h.bootDeadline
}

// Holds reports whether this node may serve key now: it holds the lease,
// has fenced the previous holder, is not releasing it and its local
// deadline has not passed.
func (m *Manager) Holds(k Key) bool {
	now, boot := m.Clock.Now(), m.Clock.Boot()
	m.mu.Lock()
	defer m.mu.Unlock()
	h := m.leases[k]
	return h != nil && h.servable && !h.releasing && !m.expired(h, now, boot)
}

// Since is when this node started holding key, zero if it does not.
func (m *Manager) Since(k Key) time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	if h := m.leases[k]; h != nil && h.servable {
		return h.since
	}
	return time.Time{}
}

// Held lists the leases this node holds and may serve.
func (m *Manager) Held() []Key {
	now, boot := m.Clock.Now(), m.Clock.Boot()
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []Key
	for k, h := range m.leases {
		if h.servable && !h.releasing && !m.expired(h, now, boot) {
			out = append(out, k)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Object() < out[j].Object() })
	return out
}

// Status is the ha block of the runner status.
func (m *Manager) Status() api.HAStatus {
	sftp := false
	for _, k := range m.Held() {
		if k.Kind == KindSFTP {
			sftp = true
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	st := api.HAStatus{LeaseOK: m.cephOK && !m.fenced, Fenced: m.fenced, SFTPMaster: sftp, HAActive: m.haveDesired && m.desired.Enabled}
	if m.fenced && m.fencedUntil != nil {
		t := *m.fencedUntil
		st.FencedUntil = &t
	}
	return st
}

// Events returns the events not yet acknowledged, oldest first.
func (m *Manager) Events() []api.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]api.Event(nil), m.events...)
}

// AckEvents drops the first n events after a status post carrying them.
func (m *Manager) AckEvents(n int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if n > len(m.events) {
		n = len(m.events)
	}
	m.events = append([]api.Event(nil), m.events[n:]...)
}

func (m *Manager) addEvent(kind string, k *Key, from int64, fence, detail string) {
	ev := api.Event{At: m.Clock.Now().UTC(), Kind: kind, Detail: detail}
	if k != nil && k.Kind == KindTenant {
		id := k.ID
		ev.TenantID = &id
	}
	if from > 0 {
		f := from
		ev.FromNodeID = &f
	}
	if fence != "" {
		f := fence
		ev.Fence = &f
	}
	m.events = append(m.events, ev)
	if len(m.events) > maxEvents {
		m.events = m.events[len(m.events)-maxEvents:]
	}
}

func (m *Manager) changed() {
	if m.Changed != nil {
		m.Changed()
	}
}

// --- renewals ---

func (m *Manager) setDeadline(h *held, sendWall time.Time, sendBoot time.Duration, t Timings) {
	d := t.Lease - t.StopMargin
	h.wallDeadline = sendWall.Add(d)
	h.bootDeadline = sendBoot + d
}

// RenewTick rewrites the node record and renews every lease held.
func (m *Manager) RenewTick(ctx context.Context) {
	alive := func() {
		if m.Alive != nil {
			m.Alive()
		}
	}
	alive()
	defer alive()
	m.mu.Lock()
	m.init()
	t := m.timings()
	fenced, until := m.fenced, m.fencedUntil
	m.mu.Unlock()
	if fenced && (until == nil || !m.Clock.Now().Before(*until)) {
		m.Store.Reset()
	}

	cctx, cancel := m.callCtx(ctx)
	ident, err := m.Store.Identity(cctx)
	cancel()
	if err != nil {
		m.cephFailed(err)
		return
	}
	alive()
	m.mu.Lock()
	m.ident = ident
	m.seq = max(m.seq+1, m.Clock.Now().UnixMilli())
	rec := NodeRecord{NodeID: m.Node.ID, LocationID: m.Node.LocationID, Name: m.Node.Name, Hostname: m.Node.Hostname,
		CephAddr: ident.Addr, RunnerVersion: m.Version, Seq: m.seq}
	m.mu.Unlock()
	data, _ := json.Marshal(rec)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		cctx, cancel := m.callCtx(ctx)
		err := m.Store.WriteFull(cctx, NodeObject(m.Node.ID), data)
		cancel()
		if err != nil {
			m.cephFailed(err)
			return
		}
		m.mu.Lock()
		wasFenced := m.fenced
		if !m.cephOK {
			m.cephUpSince = m.Clock.Boot()
		}
		m.cephOK, m.fenced, m.fencedUntil = true, false, nil
		m.mu.Unlock()
		if wasFenced {
			m.logf("lease: Ceph reachable again, no longer fenced")
		}
	}()

	m.mu.Lock()
	type job struct {
		k Key
		h held
	}
	var jobs []job
	for k, h := range m.leases {
		if !h.releasing {
			jobs = append(jobs, job{k, *h})
		}
	}
	m.mu.Unlock()
	for _, j := range jobs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			m.renew(ctx, j.k, j.h, ident, t)
		}()
	}
	wg.Wait()
}

func (m *Manager) cephFailed(err error) {
	m.mu.Lock()
	m.cephOK = false
	m.mu.Unlock()
	if errors.Is(err, ErrBlocklisted) {
		m.enterFenced(context.Background(), err)
	}
}

func (m *Manager) renew(ctx context.Context, k Key, h held, ident Identity, t Timings) {
	sendWall, sendBoot := m.Clock.Now(), m.Clock.Boot()
	cookie := Cookie(m.Node.ID, ident.Instance)
	cctx, cancel := m.callCtx(ctx)
	defer cancel()
	var err error
	if h.cookie == cookie {
		err = m.Store.LockShared(cctx, k.Object(), cookie, Tag(m.Node.ID), t.Lease, FlagMustRenew)
	} else {
		// The lease client was restarted: take the lock by tag with the new
		// instance's cookie, then drop the old instance's entry.
		if m.expired(&h, sendWall, sendBoot) {
			return
		}
		err = m.Store.LockShared(cctx, k.Object(), cookie, Tag(m.Node.ID), t.Lease, 0)
		if errors.Is(err, ErrExists) {
			err = nil
		}
		if err == nil {
			m.breakOtherInstances(cctx, k, cookie)
		}
	}
	switch {
	case err == nil:
		m.mu.Lock()
		cur := m.leases[k]
		if cur == nil || cur.gen != h.gen || cur.releasing {
			m.mu.Unlock()
			return
		}
		cur.cookie = cookie
		m.setDeadline(cur, sendWall, sendBoot, t)
		// Written under m.mu so a lose or release cannot remove the file
		// between the check above and the write.
		m.writeDeadline(k, cur.guard, sendWall, sendBoot, t)
		m.mu.Unlock()
	case errors.Is(err, ErrBusy), errors.Is(err, ErrNotFound), errors.Is(err, ErrBlocklisted):
		m.lose(k, h.gen, "renewal refused: "+err.Error())
		if errors.Is(err, ErrBlocklisted) {
			m.enterFenced(ctx, err)
		}
	default:
		m.logf("lease: renew %s: %v (deadline unchanged)", k, err)
	}
}

func (m *Manager) breakOtherInstances(ctx context.Context, k Key, keep string) {
	l, err := m.Store.ListLockers(ctx, k.Object())
	if err != nil {
		return
	}
	for _, e := range l.Lockers {
		if e.Cookie != keep && strings.HasPrefix(e.Cookie, Tag(m.Node.ID)+".") {
			if err := m.Store.BreakLock(ctx, k.Object(), e.Client, e.Cookie); err != nil && !errors.Is(err, ErrNotFound) {
				m.logf("lease: break old entry %s %s on %s: %v", e.Client, e.Cookie, k, err)
			}
		}
	}
}

// CheckDeadlines stops serving every lease whose local deadline has passed
// on either clock. Run calls it every 500 ms.
func (m *Manager) CheckDeadlines() {
	now, boot := m.Clock.Now(), m.Clock.Boot()
	m.mu.Lock()
	m.init()
	var lost []struct {
		k   Key
		gen int64
	}
	for k, h := range m.leases {
		if !h.releasing && m.expired(h, now, boot) {
			lost = append(lost, struct {
				k   Key
				gen int64
			}{k, h.gen})
		}
	}
	m.mu.Unlock()
	for _, l := range lost {
		m.lose(l.k, l.gen, "local deadline passed")
	}
}

// lose drops a lease and stops serving it.
func (m *Manager) lose(k Key, gen int64, why string) {
	m.mu.Lock()
	h := m.leases[k]
	if h == nil || h.gen != gen {
		m.mu.Unlock()
		return
	}
	delete(m.leases, k)
	if h.servable {
		m.addEvent(api.EventLost, &k, 0, "", why)
	}
	m.mu.Unlock()
	m.logf("lease: lost %s: %s", k, why)
	m.stop(k, h.guard)
	m.changed()
}

func (m *Manager) stop(k Key, g GuardInfo) {
	if m.Stop != nil {
		m.Stop(k, g)
	}
	m.removeDeadline(k)
}

func (m *Manager) enterFenced(ctx context.Context, cause error) {
	m.mu.Lock()
	m.init()
	already := m.fenced
	m.fenced, m.cephOK = true, false
	var all []struct {
		k   Key
		gen int64
	}
	for k, h := range m.leases {
		all = append(all, struct {
			k   Key
			gen int64
		}{k, h.gen})
	}
	ident := m.ident
	m.mu.Unlock()
	for _, l := range all {
		m.lose(l.k, l.gen, "fenced: "+cause.Error())
	}
	if already {
		return
	}
	m.logf("lease: this node is fenced (%v); stopping everything", cause)
	var until *time.Time
	cctx, cancel := m.callCtx(ctx)
	entries, err := m.Store.BlocklistLs(cctx)
	cancel()
	if err == nil {
		if ip, ok := AddrIP(ident.Addr); ok {
			for _, e := range entries {
				if eip, ok := AddrIP(e.Addr); ok && eip == ip && !e.Until.IsZero() {
					u := e.Until
					if until == nil || u.After(*until) {
						until = &u
					}
				}
			}
		}
	}
	m.mu.Lock()
	m.fencedUntil = until
	m.addEvent(api.EventFenced, nil, 0, "", cause.Error())
	m.mu.Unlock()
}

// --- deadline files ---

type deadlineFile struct {
	Key            string    `json:"key"`
	BootDeadlineNS int64     `json:"boot_deadline_ns"`
	WallDeadline   time.Time `json:"wall_deadline"`
	GuardInfo
}

func (m *Manager) deadlinePath(k Key) string {
	return filepath.Join(m.DeadlineDir, k.Object()+".json")
}

// writeDeadline writes the guard's deadline file. Callers hold m.mu and have
// checked that the lease is still held, so a write never follows the
// removal done after a lease is dropped from m.leases.
func (m *Manager) writeDeadline(k Key, g GuardInfo, sendWall time.Time, sendBoot time.Duration, t Timings) {
	if m.DeadlineDir == "" {
		return
	}
	d := t.Lease - t.GuardMargin
	data, _ := json.Marshal(deadlineFile{Key: k.Object(), BootDeadlineNS: int64(sendBoot + d), WallDeadline: sendWall.Add(d).UTC(), GuardInfo: g})
	if err := os.MkdirAll(m.DeadlineDir, 0o700); err != nil {
		m.logf("lease: deadline dir: %v", err)
		return
	}
	tmp := m.deadlinePath(k) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err == nil {
		err = os.Rename(tmp, m.deadlinePath(k))
		if err != nil {
			m.logf("lease: deadline file %s: %v", k, err)
		}
	}
}

func (m *Manager) removeDeadline(k Key) {
	if m.DeadlineDir == "" {
		return
	}
	if err := os.Remove(m.deadlinePath(k)); err != nil && !errors.Is(err, os.ErrNotExist) {
		m.logf("lease: remove deadline file %s: %v", k, err)
	}
}

func (m *Manager) loadLeftovers() {
	if m.DeadlineDir == "" {
		return
	}
	entries, err := os.ReadDir(m.DeadlineDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if k, ok := ParseKey(strings.TrimSuffix(e.Name(), ".json")); ok && strings.HasSuffix(e.Name(), ".json") {
			m.leftover[k] = true
		}
	}
}

// --- polling and acquisition ---

func parseOwner(s string) (int64, string) {
	node, token, ok := strings.Cut(s, ":")
	if !ok {
		return 0, ""
	}
	id, err := strconv.ParseInt(node, 10, 64)
	if err != nil {
		return 0, ""
	}
	return id, token
}

func (m *Manager) readNode(ctx context.Context, id int64) {
	cctx, cancel := m.callCtx(ctx)
	data, err := m.Store.Read(cctx, NodeObject(id))
	cancel()
	if err != nil {
		return
	}
	var rec NodeRecord
	if json.Unmarshal(data, &rec) != nil {
		return
	}
	boot := m.Clock.Boot()
	m.mu.Lock()
	defer m.mu.Unlock()
	o := m.nodes[id]
	if o == nil {
		o = &nodeObs{}
		m.nodes[id] = o
	}
	if !o.seen || o.seq != rec.Seq {
		o.changed = boot
	}
	o.seen, o.seq, o.rec = true, rec.Seq, rec
}

// fresh reports whether a node's record changed within lease_seconds on this
// node's boot clock. A record seen for the first time, or first seen since
// this node's own Ceph connection came back, counts as changed then.
func (m *Manager) fresh(id int64) bool {
	boot := m.Clock.Boot()
	m.mu.Lock()
	defer m.mu.Unlock()
	o := m.nodes[id]
	return o != nil && o.seen && boot-max(o.changed, m.cephUpSince) < m.timings().Lease
}

func (m *Manager) eligible() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.cephOK && !m.fenced && !m.desired.Draining
}

// PollTick reads node records, hands off leases Rackflow wants elsewhere,
// re-takes leases still carrying this node's tag and acquires free leases
// when it is this node's turn.
func (m *Manager) PollTick(ctx context.Context) {
	m.pollMu.Lock()
	defer m.pollMu.Unlock()
	m.mu.Lock()
	m.init()
	if !m.loaded {
		m.loaded = true
		m.loadLeftovers()
	}
	if !m.haveDesired || m.fenced || !m.cephOK || m.ident.Instance == "" {
		m.mu.Unlock()
		return
	}
	d := m.desired
	wants := map[Key]Want{}
	for _, w := range d.Wants {
		wants[w.Key] = w
	}
	var keys []Key
	seen := map[Key]bool{}
	add := func(k Key) {
		if !seen[k] {
			seen[k] = true
			keys = append(keys, k)
		}
	}
	for _, w := range d.Wants {
		add(w.Key)
	}
	for k := range m.leases {
		add(k)
	}
	for k := range m.leftover {
		add(k)
	}
	m.mu.Unlock()

	for _, id := range d.Nodes {
		if id != m.Node.ID {
			m.readNode(ctx, id)
		}
	}
	for _, k := range keys {
		w, ok := wants[k]
		if !ok {
			w = Want{Key: k, Rank: -1, Release: true}
		}
		m.pollKey(ctx, w, d)
	}
	m.mu.Lock()
	m.leftover = map[Key]bool{}
	m.mu.Unlock()
}

func (m *Manager) pollKey(ctx context.Context, w Want, d Desired) {
	k := w.Key
	me := m.Node.ID
	m.mu.Lock()
	h := m.leases[k]
	var hc held
	if h != nil {
		hc = *h
		h.guard = w.Guard
	}
	ws := m.watch[k]
	if ws == nil {
		ws = &watchState{}
		m.watch[k] = ws
	}
	handed := m.handedOff[k]
	m.mu.Unlock()

	if h != nil {
		if hc.releasing || !hc.servable {
			return
		}
		switch {
		case w.Release || !w.Acquire || d.Draining:
			m.release(ctx, k, hc.gen, "released: not wanted on this node", 0, "")
		case w.MoveTo != 0 && w.MoveTo != me && w.MoveID != handed && m.fresh(w.MoveTo):
			m.release(ctx, k, hc.gen, fmt.Sprintf("handed off to node %d (move %s)", w.MoveTo, w.MoveID), w.MoveTo, w.MoveID)
		}
		return
	}

	cctx, cancel := m.callCtx(ctx)
	defer cancel()
	l, err := m.Store.ListLockers(cctx, k.Object())
	if err != nil && !errors.Is(err, ErrNotFound) {
		if errors.Is(err, ErrBlocklisted) {
			m.enterFenced(ctx, err)
		}
		return
	}
	mine := Tag(me)
	if len(l.Lockers) > 0 {
		m.mu.Lock()
		ws.freeSince = 0
		if l.Tag != mine {
			ws.lockerTag, ws.lockerAddr = l.Tag, l.Lockers[0].Addr
		}
		m.mu.Unlock()
		if l.Tag == mine {
			m.ownEntries(cctx, w, d)
		}
		return
	}
	if !w.Acquire || !m.eligible() {
		m.mu.Lock()
		ws.freeSince = 0
		m.mu.Unlock()
		return
	}
	boot := m.Clock.Boot()
	m.mu.Lock()
	if ws.freeSince == 0 {
		ws.freeSince = boot
	}
	freeFor := boot - ws.freeSince
	releasedByMe := m.released[k]
	t := d.Timings
	m.mu.Unlock()

	x, err := m.Store.GetXattrs(cctx, k.Object())
	if err != nil && !errors.Is(err, ErrNotFound) {
		return
	}
	owner, token := parseOwner(x[XattrOwner])
	cleanlyReleased := token != "" && x[XattrReleased] == token

	var wait time.Duration
	switch {
	case w.MoveTo == me:
		wait = 0
	case owner == me && !cleanlyReleased && token != releasedByMe:
		wait = 0
	case w.Rank < 0:
		return
	default:
		wait = time.Duration(w.Rank) * t.RankStep
		if owner != 0 && owner != me && !cleanlyReleased && m.fresh(owner) {
			wait += t.Lease
		}
	}
	if freeFor < wait {
		return
	}
	m.acquire(ctx, w, t)
}

// ownEntries handles a lock that carries this node's tag but is not held by
// this process: a runner restart, a lease client restart, or a lease whose
// local deadline passed while its entry was still alive (a Ceph outage). If
// the lease is still wanted it is taken with the current cookie and the old
// entries are broken; nobody else can have held it meanwhile, so no fence
// is needed. Otherwise the old entries are released.
func (m *Manager) ownEntries(ctx context.Context, w Want, d Desired) {
	k := w.Key
	me := m.Node.ID
	x, err := m.Store.GetXattrs(ctx, k.Object())
	if err != nil && !errors.Is(err, ErrNotFound) {
		return
	}
	owner, token := parseOwner(x[XattrOwner])
	m.mu.Lock()
	releasedByMe := m.released[k]
	ident := m.ident
	m.mu.Unlock()
	keep := w.Acquire && !w.Release && m.eligible() && !(owner == me && (x[XattrReleased] == token || token == releasedByMe))
	if keep && w.MoveTo != 0 && w.MoveTo != me && m.fresh(w.MoveTo) {
		keep = false
	}
	cookie := Cookie(me, ident.Instance)
	if !keep {
		if owner == me && token != "" {
			if err := m.Store.SetXattr(ctx, k.Object(), XattrReleased, token); err != nil {
				m.logf("lease: mark %s released: %v", k, err)
			}
		}
		m.breakOtherInstances(ctx, k, "")
		m.logf("lease: dropped this node's old entries on %s", k)
		return
	}
	sendWall, sendBoot := m.Clock.Now(), m.Clock.Boot()
	err = m.Store.LockShared(ctx, k.Object(), cookie, Tag(me), d.Timings.Lease, 0)
	if errors.Is(err, ErrExists) {
		err = m.Store.LockShared(ctx, k.Object(), cookie, Tag(me), d.Timings.Lease, FlagMustRenew)
	}
	if err != nil {
		m.logf("lease: re-take %s: %v", k, err)
		return
	}
	m.breakOtherInstances(ctx, k, cookie)
	if owner != me || token == "" {
		token = randomHex(8)
		if err := m.Store.SetXattr(ctx, k.Object(), XattrOwner, fmt.Sprintf("%d:%s", me, token)); err != nil {
			m.logf("lease: re-take %s: write owner: %v", k, err)
			m.unlock(ctx, k, cookie)
			return
		}
	}
	m.mu.Lock()
	m.gen++
	h := &held{gen: m.gen, token: token, cookie: cookie, since: m.Clock.Now().UTC(), servable: true, guard: w.Guard}
	m.setDeadline(h, sendWall, sendBoot, d.Timings)
	m.leases[k] = h
	delete(m.released, k)
	m.writeDeadline(k, w.Guard, sendWall, sendBoot, d.Timings)
	m.mu.Unlock()
	m.logf("lease: re-took %s by tag", k)
	m.changed()
}

func (m *Manager) unlock(ctx context.Context, k Key, cookie string) {
	if err := m.Store.Unlock(ctx, k.Object(), cookie); err != nil && !errors.Is(err, ErrNotFound) {
		m.logf("lease: unlock %s: %v", k, err)
	}
}

func (m *Manager) acquire(ctx context.Context, w Want, t Timings) {
	k := w.Key
	me := m.Node.ID
	m.mu.Lock()
	ident := m.ident
	m.mu.Unlock()
	cookie := Cookie(me, ident.Instance)
	cctx, cancel := m.callCtx(ctx)
	sendWall, sendBoot := m.Clock.Now(), m.Clock.Boot()
	err := m.Store.LockShared(cctx, k.Object(), cookie, Tag(me), t.Lease, 0)
	cancel()
	if err != nil {
		if errors.Is(err, ErrBusy) {
			m.logf("lease: %s taken by another node first", k)
		} else {
			m.logf("lease: lock %s: %v", k, err)
		}
		m.mu.Lock()
		if ws := m.watch[k]; ws != nil {
			ws.freeSince = 0
		}
		m.mu.Unlock()
		return
	}
	token := randomHex(8)
	m.mu.Lock()
	m.gen++
	gen := m.gen
	h := &held{gen: gen, token: token, cookie: cookie, guard: w.Guard}
	m.setDeadline(h, sendWall, sendBoot, t)
	m.leases[k] = h
	m.writeDeadline(k, w.Guard, sendWall, sendBoot, t)
	m.mu.Unlock()

	abort := func(why string) {
		m.logf("lease: not serving %s: %s", k, why)
		m.mu.Lock()
		if cur := m.leases[k]; cur != nil && cur.gen == gen {
			delete(m.leases, k)
		}
		m.mu.Unlock()
		m.removeDeadline(k)
		cctx, cancel := m.callCtx(ctx)
		m.unlock(cctx, k, cookie)
		cancel()
	}

	cctx, cancel = m.callCtx(ctx)
	x, err := m.Store.GetXattrs(cctx, k.Object())
	cancel()
	if err != nil && !errors.Is(err, ErrNotFound) {
		abort("read owner: " + err.Error())
		return
	}
	prev, prevToken := parseOwner(x[XattrOwner])
	fence := ""
	if prev != 0 && prev != me && x[XattrReleased] != prevToken {
		m.readNode(ctx, prev)
		if m.fresh(prev) {
			fence = "tenant"
		} else {
			fence = "node"
		}
		var err error
		if fence == "tenant" {
			err = m.tenantFence(ctx, w, prev)
		} else {
			err = m.nodeFence(ctx, k, prev, prevToken, ident, t)
		}
		if err != nil {
			m.logf("lease: %s fence of node %d for %s failed: %v", fence, prev, k, err)
			abort(fence + " fence failed")
			return
		}
	}
	cctx, cancel = m.callCtx(ctx)
	err = m.Store.SetXattr(cctx, k.Object(), XattrOwner, fmt.Sprintf("%d:%s", me, token))
	cancel()
	if err != nil {
		abort("write owner: " + err.Error())
		return
	}
	m.mu.Lock()
	cur := m.leases[k]
	if cur == nil || cur.gen != gen {
		m.mu.Unlock()
		return
	}
	cur.servable = true
	cur.since = m.Clock.Now().UTC()
	delete(m.released, k)
	from := prev
	if from == me {
		from = 0
	}
	detail := fmt.Sprintf("acquired %s", k)
	if fence != "" {
		detail += fmt.Sprintf(" after a %s fence of node %d", fence, prev)
	}
	kind := api.EventTakeover
	if k.Kind == KindSFTP {
		kind = api.EventSFTPMaster
	}
	m.addEvent(kind, &k, from, fence, detail)
	m.mu.Unlock()
	m.logf("lease: %s", detail)
	m.changed()
}

func (m *Manager) tenantFence(ctx context.Context, w Want, prev int64) error {
	host := w.FenceHostname
	if host == "" {
		m.mu.Lock()
		if o := m.nodes[prev]; o != nil {
			host = o.rec.Hostname
		}
		m.mu.Unlock()
		if host == "" {
			return fmt.Errorf("node %d has no hostname in its record", prev)
		}
	}
	if len(w.FenceEntities) == 0 {
		if k := w.Key; k.Kind != KindSFTP {
			return fmt.Errorf("no cephx user to evict for %s", k)
		}
		m.logf("lease: SFTP fence of node %d: no tenant mounts to evict", prev)
		return nil
	}
	for _, entity := range w.FenceEntities {
		if err := m.Fencer.EvictSessions(ctx, entity, host); err != nil {
			return err
		}
	}
	return nil
}

func (m *Manager) nodeFence(ctx context.Context, k Key, prev int64, prevToken string, ident Identity, t Timings) error {
	m.mu.Lock()
	var recAddr string
	if o := m.nodes[prev]; o != nil {
		recAddr = o.rec.CephAddr
	}
	var lockAddr string
	if ws := m.watch[k]; ws != nil && ws.lockerTag == Tag(prev) {
		lockAddr = ws.lockerAddr
	}
	m.mu.Unlock()
	ip, ok := AddrIP(recAddr)
	lip, lok := AddrIP(lockAddr)
	switch {
	case !ok && !lok:
		return fmt.Errorf("no Ceph address known for node %d", prev)
	case !ok:
		ip = lip
	case lok && lip != ip:
		return fmt.Errorf("node %d record address %s does not match its lock entry %s", prev, ip, lip)
	}
	if own, ok := AddrIP(ident.Addr); ok && own == ip {
		return fmt.Errorf("refusing to fence %s: it is this node's own address", ip)
	}
	daemons, err := m.Fencer.DaemonAddrs(ctx)
	if err != nil {
		return fmt.Errorf("list Ceph daemon addresses: %w", err)
	}
	for _, a := range daemons {
		if a == ip {
			return fmt.Errorf("refusing to fence %s: a Ceph daemon runs there", ip)
		}
	}
	entry := ip.String() + ":0/0"
	cctx, cancel := m.callCtx(ctx)
	defer cancel()
	present := false
	if entries, err := m.Store.BlocklistLs(cctx); err == nil {
		for _, e := range entries {
			if e.Addr == entry && (e.Until.IsZero() || e.Until.After(m.Clock.Now().Add(t.Lease))) {
				present = true
			}
		}
	}
	if !present {
		if err := m.Store.BlocklistAdd(cctx, entry, t.Fence); err != nil {
			return fmt.Errorf("blocklist %s: %w", entry, err)
		}
	}
	if _, err := m.Fencer.EvictAddrSessions(ctx, ip); err != nil {
		return fmt.Errorf("evict sessions from %s: %w", ip, err)
	}
	audit := fmt.Sprintf("%d:%s:%d", prev, prevToken, m.Clock.Now().Unix())
	if err := m.Store.SetXattr(cctx, k.Object(), XattrFenced, audit); err != nil {
		m.logf("lease: write %s on %s: %v", XattrFenced, k, err)
	}
	return nil
}

// release hands a lease off: stop serving, mark it released, unlock.
func (m *Manager) release(ctx context.Context, k Key, gen int64, why string, to int64, moveID string) {
	m.mu.Lock()
	h := m.leases[k]
	if h == nil || h.gen != gen || h.releasing {
		m.mu.Unlock()
		return
	}
	h.releasing = true
	token, cookie, guard := h.token, h.cookie, h.guard
	m.released[k] = token
	if moveID != "" {
		m.handedOff[k] = moveID
	}
	m.mu.Unlock()
	m.logf("lease: releasing %s: %s", k, why)
	m.stop(k, guard)
	cctx, cancel := m.callCtx(ctx)
	if err := m.Store.SetXattr(cctx, k.Object(), XattrReleased, token); err != nil {
		m.logf("lease: mark %s released: %v", k, err)
	}
	m.unlock(cctx, k, cookie)
	cancel()
	m.mu.Lock()
	if cur := m.leases[k]; cur != nil && cur.gen == gen {
		delete(m.leases, k)
	}
	m.addEvent(api.EventHandoff, &k, 0, "", why)
	m.mu.Unlock()
	m.changed()
}

// Release hands key off now if this node holds it.
func (m *Manager) Release(ctx context.Context, k Key, why string) {
	m.mu.Lock()
	h := m.leases[k]
	gen := int64(-1)
	if h != nil {
		gen = h.gen
	}
	m.mu.Unlock()
	if gen >= 0 {
		m.release(ctx, k, gen, why, 0, "")
	}
}

// Remove deletes a destroyed tenant's lease object (its lock goes with it)
// and its recovery namespace, and forgets the lease.
func (m *Manager) Remove(ctx context.Context, k Key, recoveryNS string) error {
	cctx, cancel := m.callCtx(ctx)
	defer cancel()
	if recoveryNS != "" {
		if err := m.Store.PurgeNamespace(cctx, recoveryNS); err != nil {
			return fmt.Errorf("remove recovery namespace %s: %w", recoveryNS, err)
		}
	}
	m.mu.Lock()
	h := m.leases[k]
	if h != nil {
		h.releasing = true
	}
	m.mu.Unlock()
	if err := m.Store.Remove(cctx, k.Object()); err != nil && !errors.Is(err, ErrNotFound) {
		if h != nil {
			m.mu.Lock()
			h.releasing = false
			m.mu.Unlock()
		}
		return fmt.Errorf("remove lease object %s: %w", k, err)
	}
	m.mu.Lock()
	delete(m.leases, k)
	delete(m.watch, k)
	delete(m.released, k)
	delete(m.handedOff, k)
	// Until the next config arrives, polling must not re-create the object.
	wants := m.desired.Wants[:0:0]
	for _, w := range m.desired.Wants {
		if w.Key != k {
			wants = append(wants, w)
		}
	}
	m.desired.Wants = wants
	m.mu.Unlock()
	m.removeDeadline(k)
	return nil
}

// Sync makes sure a client instance is known, then runs one poll, so a
// freshly started runner re-takes its own leases before it decides what to
// serve.
func (m *Manager) Sync(ctx context.Context) {
	m.mu.Lock()
	known := m.ident.Instance != ""
	m.mu.Unlock()
	if !known {
		m.RenewTick(ctx)
	}
	m.PollTick(ctx)
}

// Running reports whether Run has started.
func (m *Manager) Running() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.running
}

// Run drives the three loops until ctx ends.
func (m *Manager) Run(ctx context.Context) {
	m.mu.Lock()
	m.running = true
	m.mu.Unlock()
	var wg sync.WaitGroup
	loop := func(every func() time.Duration, fn func()) {
		defer wg.Done()
		for {
			fn()
			select {
			case <-ctx.Done():
				return
			case <-time.After(every()):
			}
		}
	}
	interval := func(pick func(Timings) time.Duration) func() time.Duration {
		return func() time.Duration {
			m.mu.Lock()
			defer m.mu.Unlock()
			return pick(m.timings())
		}
	}
	wg.Add(3)
	go loop(interval(func(t Timings) time.Duration { return t.Renew }), func() { m.RenewTick(ctx) })
	go loop(func() time.Duration { return 500 * time.Millisecond }, m.CheckDeadlines)
	go loop(interval(func(t Timings) time.Duration { return t.Poll }), func() { m.PollTick(ctx) })
	wg.Wait()
}
