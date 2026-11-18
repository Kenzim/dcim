package state

import (
	"net/netip"
	"os"
	"path/filepath"
	"testing"
	"time"
)

var cidr = netip.MustParsePrefix("169.254.128.0/17")

func TestLinkAllocationIsStable(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir, cidr)
	if err != nil {
		t.Fatal(err)
	}
	a, _ := s.AllocateLink(12)
	b, _ := s.AllocateLink(13)
	if a.Block.String() != "169.254.128.0/30" || a.Host.String() != "169.254.128.1/30" || a.NS.String() != "169.254.128.2/30" {
		t.Fatalf("first link %+v", a)
	}
	if b.Block.String() != "169.254.128.4/30" {
		t.Fatalf("second link %+v", b)
	}
	if again, _ := s.AllocateLink(12); again != a {
		t.Fatalf("re-allocation moved: %+v", again)
	}

	s, err = Open(dir, cidr)
	if err != nil {
		t.Fatal(err)
	}
	if got, ok := s.Link(13); !ok || got != b {
		t.Fatalf("after reopen: %+v %v", got, ok)
	}
	if err := s.ReleaseLink(12); err != nil {
		t.Fatal(err)
	}
	c, _ := s.AllocateLink(14)
	if c.Block != a.Block {
		t.Fatalf("freed block not reused: %+v", c)
	}
	if got, _ := s.Link(13); got != b {
		t.Fatalf("tenant 13 moved: %+v", got)
	}
	if fi, _ := os.Stat(filepath.Join(dir, "state.json")); fi.Mode().Perm() != 0o600 {
		t.Fatalf("state file mode %v", fi.Mode().Perm())
	}
}

func TestLinkOutsideCIDRIsReallocated(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir, cidr)
	_, _ = s.AllocateLink(12)
	s, _ = Open(dir, netip.MustParsePrefix("100.64.0.0/22"))
	if _, ok := s.Link(12); ok {
		t.Fatal("link outside the new LINK_CIDR still returned")
	}
	l, _ := s.AllocateLink(12)
	if l.Block.String() != "100.64.0.0/30" {
		t.Fatalf("got %+v", l)
	}
}

func TestLinkCIDRExhausted(t *testing.T) {
	s, _ := Open(t.TempDir(), netip.MustParsePrefix("10.0.0.0/29"))
	_, _ = s.AllocateLink(1)
	_, _ = s.AllocateLink(2)
	if _, err := s.AllocateLink(3); err == nil {
		t.Fatal("expected exhaustion error")
	}
}

func TestExportIDsNeverReused(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir, cidr)
	a, _ := s.ExportID(12, 900000)
	b, _ := s.ExportID(12, 900001)
	c, _ := s.ExportID(13, 900002)
	if a != 1 || b != 2 || c != 1 {
		t.Fatalf("ids %d %d %d", a, b, c)
	}
	_ = s.PruneExports(map[int64]map[int64]bool{12: {900001: true}})
	s, _ = Open(dir, cidr)
	if got, _ := s.ExportID(12, 900001); got != 2 {
		t.Fatalf("kept id moved to %d", got)
	}
	if got, _ := s.ExportID(12, 900005); got != 3 {
		t.Fatalf("new id %d, want 3 (1 is retired)", got)
	}
	if got, _ := s.ExportID(13, 900002); got != 1 {
		t.Fatalf("unlisted tenant pruned: %d", got)
	}
}

func TestDeletingMarks(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir, cidr)
	t0 := time.Date(2026, 11, 18, 0, 0, 0, 0, time.UTC)
	got, _ := s.MarkDeleting(6, t0)
	if !got.Equal(t0) {
		t.Fatal(got)
	}
	got, _ = s.MarkDeleting(6, t0.Add(time.Hour))
	if !got.Equal(t0) {
		t.Fatalf("mark moved to %v", got)
	}
	s, _ = Open(dir, cidr)
	if got, _ := s.MarkDeleting(6, t0.Add(2*time.Hour)); !got.Equal(t0) {
		t.Fatalf("mark lost on reopen: %v", got)
	}
	_ = s.PruneDeleting(map[int64]bool{})
	if got, _ := s.MarkDeleting(6, t0.Add(3*time.Hour)); !got.Equal(t0.Add(3 * time.Hour)) {
		t.Fatalf("pruned mark kept: %v", got)
	}
}
