package lease

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

type recActions struct {
	mu    sync.Mutex
	calls []string
}

func (r *recActions) add(s string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, s)
}

func (r *recActions) LinkDown(_ context.Context, ns, link string) error {
	r.add("down " + ns + " " + link)
	return nil
}

func (r *recActions) StopContainer(_ context.Context, name string) error {
	r.add("stop " + name)
	return nil
}

func (r *recActions) RemoveVIP(_ context.Context, iface, prefix string) error {
	r.add("vip del " + prefix + " " + iface)
	return nil
}

func (r *recActions) take() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.calls
	r.calls = nil
	sort.Strings(out)
	return out
}

func TestGuardStopsAfterGuardDeadline(t *testing.T) {
	dir := t.TempDir()
	clk := NewFakeClock(time.Date(2026, 11, 19, 10, 0, 0, 0, time.UTC))
	m := &Manager{Clock: clk, DeadlineDir: dir}
	tm := DefaultTimings()
	m.writeDeadline(TenantKey(12), GuardInfo{Netns: "rf-t12", Link: "rf-v12", Container: "rf-t12-nfs"}, clk.Now(), clk.Boot(), tm)
	m.writeDeadline(SFTPKey(1), GuardInfo{VIP: "203.0.113.10/24", VIPInterface: "eno1"}, clk.Now(), clk.Boot(), tm)
	if fi, err := os.Stat(filepath.Join(dir, "tenant.12.json")); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("deadline file: %v %v", fi, err)
	}

	acts := &recActions{}
	g := &Guard{Dir: dir, Clock: clk, Actions: acts}
	clk.Advance(16 * time.Second)
	g.Check(context.Background())
	if got := acts.take(); len(got) != 0 {
		t.Fatalf("acted before the guard deadline: %v", got)
	}
	clk.Advance(time.Second) // 17 s = lease - guard_margin
	g.Check(context.Background())
	want := "down rf-t12 rf-v12,stop rf-t12-nfs,vip del 203.0.113.10/24 eno1"
	if got := strings.Join(acts.take(), ","); got != want {
		t.Fatalf("actions %q, want %q", got, want)
	}
	g.Check(context.Background())
	if got := acts.take(); len(got) != 0 {
		t.Fatalf("acted twice: %v", got)
	}
}

func TestGuardUsesEitherClock(t *testing.T) {
	for name, move := range map[string]func(*FakeClock){
		"wall": func(c *FakeClock) { c.StepWall(time.Hour) },
		"boot": func(c *FakeClock) { c.AdvanceBoot(time.Hour) },
	} {
		dir := t.TempDir()
		clk := NewFakeClock(time.Date(2026, 11, 19, 10, 0, 0, 0, time.UTC))
		m := &Manager{Clock: clk, DeadlineDir: dir}
		m.writeDeadline(TenantKey(7), GuardInfo{Container: "rf-t7-nfs"}, clk.Now(), clk.Boot(), DefaultTimings())
		acts := &recActions{}
		g := &Guard{Dir: dir, Clock: clk, Actions: acts}
		move(clk)
		g.Check(context.Background())
		if got := acts.take(); len(got) != 1 {
			t.Errorf("%s clock past the deadline: actions %v", name, got)
		}
	}
}

func TestGuardLeavesRenewedLeasesAlone(t *testing.T) {
	dir := t.TempDir()
	s := newSim(t, 1)
	s.nodes[1].m.DeadlineDir = dir
	s.mustServe(tenant42, 1, 10*time.Second)
	acts := &recActions{}
	g := &Guard{Dir: dir, Clock: s.nodes[1].clock, Actions: acts}
	for i := 0; i < 120; i++ {
		s.step()
		g.Check(context.Background())
	}
	if got := acts.take(); len(got) != 0 {
		t.Fatalf("guard acted on a renewed lease: %v", got)
	}
	// The runner releases: its deadline file goes.
	s.wants = func(*tnode) []Want { return nil }
	s.run(3 * time.Second)
	if _, err := os.Stat(filepath.Join(dir, "tenant.42.json")); !os.IsNotExist(err) {
		t.Fatalf("deadline file after release: %v", err)
	}
}

func TestNotifier(t *testing.T) {
	path := filepath.Join(t.TempDir(), "notify.sock")
	conn, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		t.Skipf("unixgram: %v", err)
	}
	defer conn.Close()
	if err := (Notifier{Socket: path}).Notify("WATCHDOG=1"); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 64)
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	n, err := conn.Read(buf)
	if err != nil || string(buf[:n]) != "WATCHDOG=1" {
		t.Fatalf("got %q %v", buf[:n], err)
	}
	if err := (Notifier{}).Notify("WATCHDOG=1"); err != nil {
		t.Fatalf("no socket: %v", err)
	}
}

func TestRenewTickFeedsWatchdog(t *testing.T) {
	s := newSim(t, 1)
	n := 0
	s.nodes[1].m.Alive = func() { n++ }
	s.run(8 * time.Second)
	if n < 2 {
		t.Fatalf("watchdog fed %d times in two renewals", n)
	}
	// A partitioned node keeps feeding the watchdog: the loop is alive.
	s.nodes[1].client.SetPartition(PartitionHang)
	n = 0
	s.run(8 * time.Second)
	if n < 2 {
		t.Fatalf("watchdog fed %d times while Ceph hangs", n)
	}
}
