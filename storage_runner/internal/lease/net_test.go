package lease

import (
	"context"
	"sync"
	"testing"
	"time"
)

func netSim(t *testing.T) *sim {
	s := newSim(t, 1, 2)
	s.wants = func(n *tnode) []Want { return []Want{netWant(net6, s.order, n.id)} }
	return s
}

func TestNetPresenceTakeoverNeverFences(t *testing.T) {
	for _, tc := range []struct {
		name  string
		stall map[string]bool
	}{
		{"fresh record", map[string]bool{"lock": true}},
		{"stale record", map[string]bool{"lock": true, "write": true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := netSim(t)
			s.mustServe(net6, 1, 10*time.Second)
			n1 := s.nodes[1]
			n1.client.Before = func(op, oid string) error {
				if tc.stall[op] {
					return ErrTimeout
				}
				return nil
			}
			s.mustServe(net6, 2, 2*time.Minute)
			s.run(time.Minute)
			if sv := s.servers(net6); len(sv) != 1 || sv[0] != 2 {
				t.Fatalf("servers %v", sv)
			}
			if ev := s.fencer.list(); len(ev) != 0 {
				t.Fatalf("network presence fenced: %v", ev)
			}
			if bl := s.cluster.Blocklist(); len(bl) != 0 {
				t.Fatalf("network presence blocklisted a node: %v", bl)
			}
			evs := s.nodes[2].m.Events()
			if len(evs) == 0 {
				t.Fatal("no takeover event")
			}
			e := evs[len(evs)-1]
			if e.Fence != nil || e.TenantID != nil || e.NetworkID == nil || *e.NetworkID != 6 {
				t.Fatalf("event %+v", e)
			}
			s.noOverlap()
		})
	}
}

func TestNetPresenceWaitsForFreshRecord(t *testing.T) {
	s := netSim(t)
	s.mustServe(net6, 1, 10*time.Second)
	s.nodes[1].client.Before = func(op, oid string) error {
		if op == "lock" {
			return ErrTimeout
		}
		return nil
	}
	s.until(time.Minute, func() bool { return !s.nodes[1].isServing(net6) })
	took := s.mustServe(net6, 2, 2*time.Minute)
	// As for a tenant: the lease runs out about 6 s after the stop, then
	// node 2 waits its rank step plus one more lease for a fresh record.
	if took < 6*time.Second+6*time.Second+20*time.Second-2*time.Second {
		t.Fatalf("node 2 took over %s after the stop", took)
	}
	s.noOverlap()
}

func TestTwoNodesRacingForNetPresence(t *testing.T) {
	for round := 0; round < 20; round++ {
		s := netSim(t)
		s.wants = func(n *tnode) []Want {
			w := netWant(net6, s.order, n.id)
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
			if n.m.Holds(net6) {
				held++
			}
		}
		if held != 1 {
			t.Fatalf("round %d: %d holders", round, held)
		}
	}
}

func TestDeferHoldsTenantUntilNetReleased(t *testing.T) {
	s := newSim(t, 1, 2)
	presence := true
	s.wants = func(n *tnode) []Want {
		var ws []Want
		if n.id == 1 && presence {
			ws = append(ws, netWant(net6, []int64{1}, n.id))
		}
		if n.id == 2 {
			w := tenantWant(tenant42, []int64{2}, n.id)
			w.Defer = []Key{net6}
			ws = append(ws, w)
		}
		return ws
	}
	s.mustServe(net6, 1, 10*time.Second)
	s.run(time.Minute)
	if s.nodes[2].isServing(tenant42) || s.nodes[2].m.Holds(tenant42) {
		t.Fatal("tenant acquired while its network presence is held")
	}
	if l := s.cluster.Lockers(tenant42.Object()); len(l.Lockers) != 0 {
		t.Fatalf("tenant lock taken: %+v", l)
	}

	// Storage is linked: Rackflow stops listing the presence, its holder
	// releases it and the tenant follows.
	presence = false
	s.mustServe(tenant42, 2, 30*time.Second)
	if len(s.servers(net6)) != 0 {
		t.Fatalf("presence still served by %v", s.servers(net6))
	}
	if len(s.fencer.list()) != 0 {
		t.Fatalf("fenced: %v", s.fencer.list())
	}
}

func TestDeferDoesNotStopOwnRetake(t *testing.T) {
	s := newSim(t, 1)
	s.mustServe(tenant42, 1, 10*time.Second)
	// The network presence of another node appears (a stale config there)
	// while node 1 holds the tenant: node 1 keeps and re-takes its lease.
	other := s.cluster.NewClient("10.0.0.9")
	if err := other.LockShared(context.Background(), net6.Object(), "cookie-x", Tag(9), time.Minute, 0); err != nil {
		t.Fatal(err)
	}
	s.wants = func(n *tnode) []Want {
		w := tenantWant(tenant42, s.order, n.id)
		w.Defer = []Key{net6}
		return []Want{w}
	}
	n1 := s.nodes[1]
	n1.client.Reset()
	n1.m = s.newManager(n1)
	n1.m.SetDesired(s.desired(n1))
	n1.m.RenewTick(context.Background())
	n1.m.PollTick(context.Background())
	if !n1.m.Holds(tenant42) {
		t.Fatal("restarted runner did not re-take its own lease")
	}
}
