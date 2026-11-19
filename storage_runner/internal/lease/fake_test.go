package lease

import (
	"context"
	"errors"
	"testing"
	"time"

	"rackflow/storage_runner/internal/api"
)

func haBlock(lease, renew, stop int64) *api.HA {
	return &api.HA{Enabled: true, LeaseSeconds: lease, RenewSeconds: renew, StopMarginSeconds: stop}
}

func TestFakeLockSemantics(t *testing.T) {
	ctx := context.Background()
	clk := NewFakeClock(time.Date(2026, 11, 19, 0, 0, 0, 0, time.UTC))
	c := NewFakeCluster(clk.Now)
	a, b := c.NewClient("10.0.0.1"), c.NewClient("10.0.0.2")
	const oid = "tenant.1"

	if err := a.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, FlagMustRenew); !errors.Is(err, ErrNotFound) {
		t.Fatalf("MUST_RENEW on a missing entry: %v", err)
	}
	if err := a.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, 0); err != nil {
		t.Fatal(err)
	}
	if err := a.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, 0); !errors.Is(err, ErrExists) {
		t.Fatalf("relock without renew flags: %v", err)
	}
	if err := b.LockShared(ctx, oid, "cb", "node.2", 20*time.Second, 0); !errors.Is(err, ErrBusy) {
		t.Fatalf("tag mismatch: %v", err)
	}
	// Same tag, other instance: shared lock admits it.
	a2 := c.NewClient("10.0.0.1")
	if err := a2.LockShared(ctx, oid, "ca2", "node.1", 20*time.Second, 0); err != nil {
		t.Fatal(err)
	}
	// Locker identity is client instance plus cookie: another instance with
	// the same cookie is a different entry, so MUST_RENEW fails for it.
	if err := b.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, FlagMustRenew); !errors.Is(err, ErrNotFound) {
		t.Fatalf("renew from another instance: %v", err)
	}
	l, _ := a.ListLockers(ctx, oid)
	if len(l.Lockers) != 2 || l.Tag != "node.1" {
		t.Fatalf("lockers %+v", l)
	}
	ia, _ := a2.Identity(ctx)
	if err := a.BreakLock(ctx, oid, ia.Client, "ca2"); err != nil {
		t.Fatal(err)
	}
	clk.Advance(15 * time.Second)
	if err := a.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, FlagMustRenew); err != nil {
		t.Fatalf("renew: %v", err)
	}
	clk.Advance(21 * time.Second)
	if l, _ := b.ListLockers(ctx, oid); len(l.Lockers) != 0 || l.Tag != "" {
		t.Fatalf("expired lockers listed: %+v", l)
	}
	if err := a.LockShared(ctx, oid, "ca", "node.1", 20*time.Second, FlagMustRenew); !errors.Is(err, ErrNotFound) {
		t.Fatalf("renew after expiry: %v", err)
	}
	if err := b.LockShared(ctx, oid, "cb", "node.2", 20*time.Second, 0); err != nil {
		t.Fatalf("lock after expiry: %v", err)
	}
	if err := b.Unlock(ctx, oid, "cb"); err != nil {
		t.Fatal(err)
	}
	if err := b.Unlock(ctx, oid, "cb"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("double unlock: %v", err)
	}
}

func TestFakeBlocklist(t *testing.T) {
	ctx := context.Background()
	clk := NewFakeClock(time.Date(2026, 11, 19, 0, 0, 0, 0, time.UTC))
	c := NewFakeCluster(clk.Now)
	a, a2, b := c.NewClient("10.0.0.1"), c.NewClient("10.0.0.1"), c.NewClient("10.0.0.2")
	ia, _ := a.Identity(ctx)

	// A per-instance entry cuts off only that instance.
	if err := b.BlocklistAdd(ctx, ia.Addr[len("v1:"):], time.Hour); err != nil {
		t.Fatal(err)
	}
	if err := a.WriteFull(ctx, "x", []byte("1")); !errors.Is(err, ErrBlocklisted) {
		t.Fatalf("instance entry: %v", err)
	}
	if err := a2.WriteFull(ctx, "x", []byte("1")); err != nil {
		t.Fatalf("other instance on the address: %v", err)
	}
	// An address-wide entry cuts off every instance, including new ones.
	if err := b.BlocklistAdd(ctx, "10.0.0.1:0/0", 600*time.Second); err != nil {
		t.Fatal(err)
	}
	a2.Reset()
	if err := a2.WriteFull(ctx, "x", []byte("1")); !errors.Is(err, ErrBlocklisted) {
		t.Fatalf("address entry: %v", err)
	}
	if err := b.WriteFull(ctx, "x", []byte("2")); err != nil {
		t.Fatal(err)
	}
	if n := len(c.Blocklist()); n != 2 {
		t.Fatalf("%d entries", n)
	}
	clk.Advance(601 * time.Second)
	if err := a2.WriteFull(ctx, "x", []byte("1")); err != nil {
		t.Fatalf("after expiry: %v", err)
	}
}

func TestFakePartitions(t *testing.T) {
	clk := NewFakeClock(time.Now())
	c := NewFakeCluster(clk.Now)
	a := c.NewClient("10.0.0.1")
	a.SetPartition(PartitionFail)
	if _, err := a.Read(context.Background(), "x"); !errors.Is(err, ErrTimeout) {
		t.Fatalf("fail partition: %v", err)
	}
	a.SetPartition(PartitionHang)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	if _, err := a.Read(ctx, "x"); !errors.Is(err, ErrTimeout) || time.Since(start) < 15*time.Millisecond {
		t.Fatalf("hang partition: %v after %s", err, time.Since(start))
	}
	done := make(chan error)
	go func() {
		_, err := a.Read(context.Background(), "x")
		done <- err
	}()
	time.Sleep(5 * time.Millisecond)
	a.SetPartition(Connected)
	if err := <-done; !errors.Is(err, ErrTimeout) {
		t.Fatalf("healed hang: %v", err)
	}
	if _, err := a.Read(context.Background(), "x"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("connected: %v", err)
	}
}

func TestHelperErrnoMapping(t *testing.T) {
	for errno, want := range map[int]error{16: ErrBusy, 2: ErrNotFound, 17: ErrExists, 108: ErrBlocklisted, 110: ErrTimeout} {
		if err := mapErrno("op", errno, "x"); !errors.Is(err, want) {
			t.Errorf("errno %d: %v", errno, err)
		}
	}
	if err := mapErrno("op", 1, "EPERM"); errors.Is(err, ErrBusy) || errors.Is(err, ErrTimeout) {
		t.Errorf("errno 1: %v", err)
	}
}
