package auth

import "testing"

func TestAllowNormalizeIP(t *testing.T) {
	s := NewStore()
	s.Replace("v1", []Entry{{BindIP: "127.0.0.1", Username: "u", Password: "p", ServiceID: 9}})
	if id, ok := s.Allow("127.0.0.1", "u", "p"); !ok || id != 9 {
		t.Fatalf("expected allow, got ok=%v id=%d", ok, id)
	}
	if _, ok := s.Allow("127.0.0.1", "u", "wrong"); ok {
		t.Fatal("expected deny")
	}
	if _, ok := s.Allow("10.0.0.1", "u", "p"); ok {
		t.Fatal("expected deny for other bind")
	}
}
