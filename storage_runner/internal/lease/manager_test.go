package lease

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeFencer struct {
	mu        sync.Mutex
	daemons   []netip.Addr
	err       error
	evictions []string
}

func (f *fakeFencer) EvictSessions(_ context.Context, entity, hostname string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.evictions = append(f.evictions, entity+"@"+hostname)
	return nil
}

func (f *fakeFencer) EvictAddrSessions(_ context.Context, ip netip.Addr) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return 0, f.err
	}
	f.evictions = append(f.evictions, "addr="+ip.String())
	return 1, nil
}

func (f *fakeFencer) DaemonAddrs(context.Context) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]netip.Addr(nil), f.daemons...), nil
}

func (f *fakeFencer) list() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.evictions...)
}

type tnode struct {
	id     int64
	m      *Manager
	client *FakeClient
	clock  *FakeClock
	paused bool

	mu      sync.Mutex
	serving map[Key]bool
	stops   map[Key]int
}

type sim struct {
	t        *testing.T
	osd      *FakeClock
	cluster  *FakeCluster
	fencer   *fakeFencer
	nodes    map[int64]*tnode
	order    []int64
	wants    func(n *tnode) []Want
	elapsed  time.Duration
	draining map[int64]bool
	// overlap counts steps in which two nodes served the same lease.
	overlap int
}

var tenant42 = TenantKey(42)

func tenantWant(k Key, order []int64, me int64) Want {
	rank := -1
	for i, id := range order {
		if id == me {
			rank = i
		}
	}
	return Want{Key: k, Acquire: rank >= 0, Rank: rank,
		FenceEntities: []string{fmt.Sprintf("client.rf-t%d", k.ID)}, FenceHostname: fmt.Sprintf("rf-t%d-nfs", k.ID)}
}

func newSim(t *testing.T, ids ...int64) *sim {
	start := time.Date(2026, 11, 19, 10, 0, 0, 0, time.UTC)
	s := &sim{t: t, osd: NewFakeClock(start), fencer: &fakeFencer{}, nodes: map[int64]*tnode{}, order: ids}
	s.cluster = NewFakeCluster(s.osd.Now)
	s.wants = func(n *tnode) []Want { return []Want{tenantWant(tenant42, s.order, n.id)} }
	for _, id := range ids {
		s.addNode(id, fmt.Sprintf("10.0.0.%d", id))
	}
	return s
}

func (s *sim) addNode(id int64, ip string) *tnode {
	n := &tnode{id: id, clock: NewFakeClock(s.osd.Now()), serving: map[Key]bool{}, stops: map[Key]int{}}
	n.client = s.cluster.NewClient(ip)
	n.m = s.newManager(n)
	s.nodes[id] = n
	return n
}

func (s *sim) newManager(n *tnode) *Manager {
	m := &Manager{
		Store: n.client, Fencer: s.fencer, Clock: n.clock, Version: "test",
		Node:        NodeInfo{ID: n.id, LocationID: 1, Name: fmt.Sprintf("stor-%02d", n.id), Hostname: fmt.Sprintf("stor-%02d", n.id)},
		CallTimeout: 50 * time.Millisecond,
	}
	m.Stop = func(k Key, _ GuardInfo) {
		n.mu.Lock()
		defer n.mu.Unlock()
		if n.serving[k] {
			n.stops[k]++
		}
		n.serving[k] = false
	}
	m.Changed = func() {
		for _, k := range m.Held() {
			n.mu.Lock()
			n.serving[k] = true
			n.mu.Unlock()
		}
	}
	return m
}

func (n *tnode) isServing(k Key) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.serving[k]
}

func (s *sim) desired(n *tnode) Desired {
	var others []int64
	for id := range s.nodes {
		others = append(others, id)
	}
	return Desired{Enabled: true, Draining: s.draining[n.id], Timings: DefaultTimings(), Nodes: others, Wants: s.wants(n)}
}

// step advances every clock by 500 ms and runs each running node's loops
// on their schedule: deadlines every step, polls every 2 s, renewals every 4 s.
func (s *sim) step() {
	s.elapsed += 500 * time.Millisecond
	s.osd.Advance(500 * time.Millisecond)
	for _, id := range s.order {
		n := s.nodes[id]
		n.clock.Advance(500 * time.Millisecond)
	}
	ms := s.elapsed.Milliseconds()
	for _, id := range s.order {
		n := s.nodes[id]
		if n.paused {
			continue
		}
		n.m.SetDesired(s.desired(n))
		n.m.CheckDeadlines()
		if ms%4000 == 0 {
			n.m.RenewTick(context.Background())
		}
		if ms%2000 == 0 {
			n.m.PollTick(context.Background())
		}
	}
	for _, k := range []Key{tenant42, SFTPKey(1)} {
		count := 0
		for _, n := range s.nodes {
			if n.isServing(k) {
				count++
			}
		}
		if count > 1 {
			s.overlap++
		}
	}
}

func (s *sim) run(d time.Duration) {
	for end := s.elapsed + d; s.elapsed < end; {
		s.step()
	}
}

// until steps until cond holds and returns the simulated time taken.
func (s *sim) until(limit time.Duration, cond func() bool) (time.Duration, bool) {
	start := s.elapsed
	for s.elapsed-start < limit {
		if cond() {
			return s.elapsed - start, true
		}
		s.step()
	}
	return s.elapsed - start, cond()
}

func (s *sim) servers(k Key) []int64 {
	var out []int64
	for _, id := range s.order {
		if s.nodes[id].isServing(k) {
			out = append(out, id)
		}
	}
	return out
}

func (s *sim) mustServe(k Key, id int64, limit time.Duration) time.Duration {
	s.t.Helper()
	d, ok := s.until(limit, func() bool {
		sv := s.servers(k)
		return len(sv) == 1 && sv[0] == id
	})
	if !ok {
		s.t.Fatalf("%s not served by node %d within %s (servers %v)", k, id, limit, s.servers(k))
	}
	return d
}

func (s *sim) noOverlap() {
	s.t.Helper()
	if s.overlap > 0 {
		s.t.Fatalf("two nodes served one lease at once in %d steps", s.overlap)
	}
}

func eventKinds(m *Manager) []string {
	var out []string
	for _, e := range m.Events() {
		f := ""
		if e.Fence != nil {
			f = "/" + *e.Fence
		}
		out = append(out, e.Kind+f)
	}
	return out
}

func TestPreferredNodeAcquiresAndHolds(t *testing.T) {
	s := newSim(t, 1, 2, 3)
	if d := s.mustServe(tenant42, 1, 10*time.Second); d > 6*time.Second {
		t.Fatalf("took %s to acquire", d)
	}
	// Peers never talk to each other, so a partition between them changes
	// nothing: the holder keeps renewing and serving.
	s.run(3 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 1 {
		t.Fatalf("servers %v", sv)
	}
	if got := eventKinds(s.nodes[1].m); strings.Join(got, ",") != "takeover" {
		t.Fatalf("events %v", got)
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 1 || l.Tag != "node.1" {
		t.Fatalf("lockers %+v", l)
	}
	if owner := s.cluster.Xattr(tenant42.Object(), XattrOwner); !strings.HasPrefix(owner, "1:") {
		t.Fatalf("rf.owner %q", owner)
	}
	if len(s.cluster.Blocklist()) != 0 || len(s.fencer.list()) != 0 {
		t.Fatalf("unexpected fence: %v %v", s.cluster.Blocklist(), s.fencer.list())
	}
	st := s.nodes[1].m.Status()
	if !st.LeaseOK || st.Fenced || !st.HAActive {
		t.Fatalf("status %+v", st)
	}
	s.noOverlap()
}

func TestHolderPartitionedFromCephFailsOver(t *testing.T) {
	s := newSim(t, 1, 2, 3)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.run(3 * time.Second)
	n1 := s.nodes[1]
	lastSend := n1.m.leases[tenant42].wallDeadline.Add(-14 * time.Second)
	n1.client.SetPartition(PartitionFail)

	s.until(time.Minute, func() bool { return !n1.isServing(tenant42) })
	stoppedAfter := n1.clock.Now().Sub(lastSend)
	if stoppedAfter > 14*time.Second+500*time.Millisecond || stoppedAfter < 13*time.Second {
		t.Fatalf("holder stopped %s after its last renewal send, want about 14 s", stoppedAfter)
	}
	s.mustServe(tenant42, 2, time.Minute)
	s.noOverlap()
	bl := s.cluster.Blocklist()
	if len(bl) != 1 || bl[0].Addr != "10.0.0.1:0/0" || bl[0].Until.Sub(s.osd.Now()) < 590*time.Second {
		t.Fatalf("blocklist %+v", bl)
	}
	if ev := s.fencer.list(); len(ev) != 1 || ev[0] != "addr=10.0.0.1" {
		t.Fatalf("evictions %v", ev)
	}
	if f := s.cluster.Xattr(tenant42.Object(), XattrFenced); !strings.HasPrefix(f, "1:") {
		t.Fatalf("rf.fenced %q", f)
	}
	evs := s.nodes[2].m.Events()
	if len(evs) != 1 || evs[0].Kind != "takeover" || *evs[0].Fence != "node" || *evs[0].FromNodeID != 1 || *evs[0].TenantID != 42 {
		t.Fatalf("events %+v", evs)
	}

	// The old holder comes back: it is blocklisted, so it reports fenced and
	// serves nothing until the entry expires.
	n1.client.SetPartition(Connected)
	s.run(10 * time.Second)
	st := n1.m.Status()
	if !st.Fenced || st.LeaseOK || st.FencedUntil == nil {
		t.Fatalf("old holder status %+v", st)
	}
	if n1.isServing(tenant42) {
		t.Fatal("fenced node serves")
	}
	s.run(11 * time.Minute)
	st = n1.m.Status()
	if st.Fenced || !st.LeaseOK {
		t.Fatalf("after the blocklist expired: %+v", st)
	}
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 2 {
		t.Fatalf("servers after recovery %v; the old node must not take its tenant back on its own", sv)
	}
	s.noOverlap()
}

func TestHangingCephCallsStopAtDeadline(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	n1.client.SetPartition(PartitionHang)
	d, ok := s.until(time.Minute, func() bool { return !n1.isServing(tenant42) })
	if !ok || d > 15*time.Second {
		t.Fatalf("still serving after %s with hanging calls", d)
	}
	s.mustServe(tenant42, 2, time.Minute)
	n1.client.SetPartition(Connected)
	s.noOverlap()
}

func TestTwoNodesRacingForOneLease(t *testing.T) {
	for round := 0; round < 20; round++ {
		s := newSim(t, 1, 2)
		s.wants = func(n *tnode) []Want {
			w := tenantWant(tenant42, s.order, n.id)
			w.Rank = 0
			return []Want{w}
		}
		for _, n := range s.nodes {
			n.m.SetDesired(s.desired(n))
			n.m.RenewTick(context.Background())
		}
		var wg sync.WaitGroup
		for _, n := range s.nodes {
			wg.Add(1)
			go func() {
				defer wg.Done()
				n.m.PollTick(context.Background())
			}()
		}
		wg.Wait()
		held := 0
		for _, n := range s.nodes {
			if n.m.Holds(tenant42) {
				held++
			}
		}
		if held != 1 {
			t.Fatalf("round %d: %d holders", round, held)
		}
		if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 1 {
			t.Fatalf("round %d: lockers %+v", round, l)
		}
	}
}

func TestRenewalStallWithFreshRecordTenantFences(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	n1.client.Before = func(op, oid string) error {
		if op == "lock" {
			return ErrTimeout
		}
		return nil
	}
	s.until(time.Minute, func() bool { return !n1.isServing(tenant42) })
	stopAt := s.elapsed
	took := s.mustServe(tenant42, 2, 2*time.Minute)
	// The lease expires 6 s after the stop; node 2 (rank 1) then waits its
	// rank step plus an extra lease because node 1's record is fresh.
	if total := took; total < 6*time.Second+6*time.Second+20*time.Second-2*time.Second {
		t.Fatalf("node 2 took over %s after the stop, before the extra wait for a fresh record (stop at %s)", total, stopAt)
	}
	if len(s.cluster.Blocklist()) != 0 {
		t.Fatalf("node fence for a fresh record: %v", s.cluster.Blocklist())
	}
	if ev := s.fencer.list(); len(ev) != 1 || ev[0] != "client.rf-t42@rf-t42-nfs" {
		t.Fatalf("evictions %v", ev)
	}
	evs := s.nodes[2].m.Events()
	if len(evs) != 1 || *evs[0].Fence != "tenant" {
		t.Fatalf("events %+v", evs)
	}
	s.noOverlap()
}

func TestRenewalStallWithStaleRecordNodeFences(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	n1.client.Before = func(op, oid string) error {
		if op == "lock" || op == "write" {
			return ErrTimeout
		}
		return nil
	}
	s.mustServe(tenant42, 2, 2*time.Minute)
	if bl := s.cluster.Blocklist(); len(bl) != 1 || bl[0].Addr != "10.0.0.1:0/0" {
		t.Fatalf("blocklist %v", bl)
	}
	s.noOverlap()
}

func TestRunnerRestartRetakesByTag(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.run(5 * time.Second)
	n1 := s.nodes[1]
	owner := s.cluster.Xattr(tenant42.Object(), XattrOwner)
	oldCookie := n1.m.leases[tenant42].cookie

	// A new runner process: a new lease client instance and no memory.
	n1.client.Reset()
	n1.m = s.newManager(n1)
	n1.m.SetDesired(s.desired(n1))
	n1.m.RenewTick(context.Background())
	n1.m.PollTick(context.Background())
	if !n1.m.Holds(tenant42) {
		t.Fatal("restarted runner did not re-take its lease at once")
	}
	l := s.cluster.Lockers(tenant42.Object())
	if len(l.Lockers) != 1 || l.Lockers[0].Cookie == oldCookie || l.Tag != "node.1" {
		t.Fatalf("lockers %+v (old cookie %s)", l, oldCookie)
	}
	if got := s.cluster.Xattr(tenant42.Object(), XattrOwner); got != owner {
		t.Fatalf("rf.owner changed on re-take: %q -> %q", owner, got)
	}
	s.run(2 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 1 {
		t.Fatalf("servers %v", sv)
	}
	if len(s.fencer.list()) != 0 || len(s.cluster.Blocklist()) != 0 {
		t.Fatal("restart caused a fence")
	}
	for _, e := range s.nodes[2].m.Events() {
		t.Fatalf("peer event %+v", e)
	}
}

func TestLeaseClientRestartKeepsLease(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	stops := n1.stops[tenant42]
	n1.client.Reset()
	s.run(time.Minute)
	if !n1.isServing(tenant42) || n1.stops[tenant42] != stops {
		t.Fatal("helper restart interrupted serving")
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 1 {
		t.Fatalf("old instance entry not broken: %+v", l)
	}
}

func TestPausedVMStopsOnResume(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	bootAtPause := n1.clock.Boot()
	n1.paused = true
	if _, ok := s.until(2*time.Minute, func() bool { return s.nodes[2].isServing(tenant42) }); !ok {
		t.Fatal("node 2 did not take over from a paused node")
	}
	s.overlap = 0 // node 1 is frozen, so its serving flag is stale until it runs again

	// The pause did not count on the boot clock, only on the wall clock.
	n1.clock.mu.Lock()
	n1.clock.boot = bootAtPause
	n1.clock.mu.Unlock()
	n1.paused = false
	n1.m.CheckDeadlines()
	if n1.isServing(tenant42) {
		t.Fatal("resumed node still serves after its first deadline check")
	}
	s.run(10 * time.Second)
	if !n1.m.Status().Fenced {
		t.Fatalf("resumed node not fenced: %+v", n1.m.Status())
	}
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 2 {
		t.Fatalf("servers %v", sv)
	}
	s.noOverlap()
}

func TestWallClockStepBackDoesNotExtendServing(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	n1.clock.StepWall(-time.Hour)
	n1.client.SetPartition(PartitionFail)
	d, _ := s.until(time.Minute, func() bool { return !n1.isServing(tenant42) })
	if d > 15*time.Second {
		t.Fatalf("stopped after %s: the boot clock deadline was not used", d)
	}
	s.mustServe(tenant42, 2, time.Minute)
	s.noOverlap()
}

func TestWallClockStepForwardStopsThenRetakes(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	n1 := s.nodes[1]
	n1.clock.StepWall(time.Hour)
	n1.m.CheckDeadlines()
	if n1.isServing(tenant42) {
		t.Fatal("still serving after the wall clock passed the deadline")
	}
	s.mustServe(tenant42, 1, 10*time.Second)
	if len(s.fencer.list()) != 0 || len(s.cluster.Blocklist()) != 0 {
		t.Fatal("clock step caused a fence")
	}
	s.noOverlap()
}

func TestNodeFenceRefusesCephDaemonAddress(t *testing.T) {
	s := newSim(t, 1, 2)
	s.fencer.daemons = []netip.Addr{netip.MustParseAddr("10.0.0.1")}
	s.mustServe(tenant42, 1, 10*time.Second)
	s.nodes[1].client.SetPartition(PartitionFail)
	s.run(2 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 0 {
		t.Fatalf("served without a fence: %v", sv)
	}
	if len(s.cluster.Blocklist()) != 0 {
		t.Fatalf("blocklisted a Ceph daemon address: %v", s.cluster.Blocklist())
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 0 {
		t.Fatalf("refused fence left the lock held: %+v", l)
	}
}

func TestNodeFenceRefusesOwnAddress(t *testing.T) {
	s := newSim(t, 1)
	s.order = []int64{1, 2}
	s.addNode(2, "10.0.0.1") // same address as node 1
	s.mustServe(tenant42, 1, 10*time.Second)
	s.nodes[1].client.SetPartition(PartitionFail)
	s.run(2 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 0 {
		t.Fatalf("served after fencing its own address: %v", sv)
	}
	if len(s.cluster.Blocklist()) != 0 {
		t.Fatalf("blocklisted own address: %v", s.cluster.Blocklist())
	}
}

func TestFenceFailureServesNothing(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.nodes[1].client.SetPartition(PartitionFail)
	s.fencer.mu.Lock()
	s.fencer.err = errors.New("mds unreachable")
	s.fencer.mu.Unlock()
	s.run(2 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 0 {
		t.Fatalf("served after a failed fence: %v", sv)
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 0 {
		t.Fatalf("failed fence left the lock held: %+v", l)
	}
	s.fencer.mu.Lock()
	s.fencer.err = nil
	s.fencer.mu.Unlock()
	s.mustServe(tenant42, 2, time.Minute)
	s.noOverlap()
}

func TestPlannedMoveHandsOffWithoutFence(t *testing.T) {
	s := newSim(t, 1, 2, 3)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.run(5 * time.Second)
	s.wants = func(n *tnode) []Want {
		w := tenantWant(tenant42, []int64{2, 1, 3}, n.id)
		w.MoveTo, w.MoveID = 2, "m-7f3a"
		return []Want{w}
	}
	took := s.mustServe(tenant42, 2, 10*time.Second)
	if took > 5*time.Second {
		t.Fatalf("hand-off took %s", took)
	}
	if len(s.fencer.list()) != 0 || len(s.cluster.Blocklist()) != 0 {
		t.Fatal("planned move fenced")
	}
	if got := eventKinds(s.nodes[1].m); strings.Join(got, ",") != "takeover,handoff" {
		t.Fatalf("holder events %v", got)
	}
	evs := s.nodes[2].m.Events()
	if len(evs) != 1 || evs[0].Kind != "takeover" || evs[0].Fence != nil || *evs[0].FromNodeID != 1 {
		t.Fatalf("target events %+v", evs)
	}
	s.run(time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 2 {
		t.Fatalf("servers %v", sv)
	}
	s.noOverlap()
}

func TestMoveToDeadTargetTimesOut(t *testing.T) {
	s := newSim(t, 1, 2, 3)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.run(5 * time.Second)
	n3 := s.nodes[3]
	// Node 3 dies right after the holder saw its record fresh and handed off.
	s.wants = func(n *tnode) []Want {
		w := tenantWant(tenant42, []int64{3, 1, 2}, n.id)
		w.MoveTo, w.MoveID = 3, "m-1"
		return []Want{w}
	}
	n3.paused = true
	_, ok := s.until(time.Minute, func() bool { return !s.nodes[1].isServing(tenant42) })
	if !ok {
		t.Fatal("holder did not hand off to a target with a fresh record")
	}
	// The target never takes it; the holder, next in the order, takes it
	// back after its rank step and does not hand off again for the same move.
	took := s.mustServe(tenant42, 1, time.Minute)
	if took > 15*time.Second {
		t.Fatalf("took %s to recover from a dead move target", took)
	}
	s.run(time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 1 {
		t.Fatalf("servers %v", sv)
	}
	if len(s.fencer.list()) != 0 {
		t.Fatalf("fenced: %v", s.fencer.list())
	}
}

func TestMoveToStaleTargetIsIgnored(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.wants = func(n *tnode) []Want {
		w := tenantWant(tenant42, s.order, n.id)
		w.MoveTo, w.MoveID = 9, "m-2"
		return []Want{w}
	}
	s.run(time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 1 {
		t.Fatalf("handed off to a node without a fresh record: %v", sv)
	}
}

func TestCephWideOutageRecoversWithoutFences(t *testing.T) {
	s := newSim(t, 1, 2, 3)
	s.wants = func(n *tnode) []Want {
		return []Want{tenantWant(tenant42, s.order, n.id), tenantWant(TenantKey(43), []int64{2, 1, 3}, n.id)}
	}
	s.mustServe(tenant42, 1, 10*time.Second)
	s.mustServe(TenantKey(43), 2, 10*time.Second)
	// Move 43 to node 1 so that its holder is not its rank-0 node.
	s.wants = func(n *tnode) []Want {
		w := tenantWant(TenantKey(43), []int64{1, 2, 3}, n.id)
		w.MoveTo, w.MoveID = 1, "m-3"
		return []Want{tenantWant(tenant42, s.order, n.id), w}
	}
	s.mustServe(TenantKey(43), 1, 10*time.Second)
	s.wants = func(n *tnode) []Want {
		return []Want{tenantWant(tenant42, s.order, n.id), tenantWant(TenantKey(43), []int64{2, 1, 3}, n.id)}
	}
	s.run(10 * time.Second)

	for _, n := range s.nodes {
		n.client.SetPartition(PartitionFail)
	}
	s.run(15 * time.Second)
	for _, n := range s.nodes {
		if n.isServing(tenant42) || n.isServing(TenantKey(43)) {
			t.Fatalf("node %d serves during a Ceph-wide outage", n.id)
		}
	}
	s.run(time.Minute)
	for _, n := range s.nodes {
		n.client.SetPartition(Connected)
	}
	s.mustServe(tenant42, 1, 30*time.Second)
	s.mustServe(TenantKey(43), 1, 30*time.Second)
	s.run(2 * time.Minute)
	if len(s.fencer.list()) != 0 || len(s.cluster.Blocklist()) != 0 {
		t.Fatalf("outage recovery fenced: %v %v", s.fencer.list(), s.cluster.Blocklist())
	}
	if sv := s.servers(TenantKey(43)); len(sv) != 1 || sv[0] != 1 {
		t.Fatalf("tenant 43 servers %v; its previous holder must get first claim", sv)
	}
	s.noOverlap()
}

func TestReleaseWhenNoLongerWanted(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.wants = func(n *tnode) []Want { return nil }
	s.run(5 * time.Second)
	if len(s.servers(tenant42)) != 0 {
		t.Fatal("still served")
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 0 {
		t.Fatalf("lock not released: %+v", l)
	}
	owner := s.cluster.Xattr(tenant42.Object(), XattrOwner)
	if _, tok := parseOwner(owner); s.cluster.Xattr(tenant42.Object(), XattrReleased) != tok {
		t.Fatal("rf.released not written")
	}
}

func TestDrainingNodeHandsOffAndAcquiresNothing(t *testing.T) {
	s := newSim(t, 1, 2)
	s.mustServe(tenant42, 1, 10*time.Second)
	s.draining = map[int64]bool{1: true}
	// Node 1 is still first in the order it has, but draining wins.
	s.mustServe(tenant42, 2, 30*time.Second)
	s.run(2 * time.Minute)
	if sv := s.servers(tenant42); len(sv) != 1 || sv[0] != 2 {
		t.Fatalf("servers %v", sv)
	}
	if len(s.fencer.list()) != 0 {
		t.Fatalf("drain hand-off fenced: %v", s.fencer.list())
	}
	s.noOverlap()
}

func TestTimingsValidation(t *testing.T) {
	tm, err := TimingsFromConfig(nil)
	if err != nil || tm != DefaultTimings() {
		t.Fatalf("%+v %v", tm, err)
	}
	if _, err := TimingsFromConfig(haBlock(20, 4, 20)); err == nil {
		t.Fatal("stop margin equal to the lease accepted")
	}
	if _, err := TimingsFromConfig(haBlock(20, 7, 6)); err == nil {
		t.Fatal("renew*3 above the lease accepted")
	}
	tm, err = TimingsFromConfig(haBlock(30, 5, 8))
	if err != nil || tm.Lease != 30*time.Second || tm.Renew != 5*time.Second || tm.StopMargin != 8*time.Second || tm.RankStep != 6*time.Second {
		t.Fatalf("%+v %v", tm, err)
	}
}

func TestAddrIP(t *testing.T) {
	for in, want := range map[string]string{
		"v1:10.0.0.5:0/123":                       "10.0.0.5",
		"10.0.0.5:6789/0":                         "10.0.0.5",
		"[v2:10.0.0.6:3300/0,v1:10.0.0.6:6789/0]": "10.0.0.6",
		"client.4101 v1:192.168.1.2:0/3456":       "192.168.1.2",
	} {
		if a, ok := AddrIP(in); !ok || a.String() != want {
			t.Errorf("AddrIP(%q) = %v %v", in, a, ok)
		}
	}
	if _, ok := AddrIP("nonsense"); ok {
		t.Error("parsed nonsense")
	}
}

func TestParseKey(t *testing.T) {
	for _, k := range []Key{TenantKey(42), SFTPKey(1)} {
		if got, ok := ParseKey(k.Object()); !ok || got != k {
			t.Errorf("%v -> %v %v", k, got, ok)
		}
	}
	for _, bad := range []string{"node.1", "tenant.", "tenant.x", "tenant.-1"} {
		if _, ok := ParseKey(bad); ok {
			t.Errorf("parsed %q", bad)
		}
	}
}
