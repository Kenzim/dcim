package auth

import (
	"fmt"
	"sync"
	"testing"
)

func TestAllowScopedToBindIP(t *testing.T) {
	s := NewStore()
	s.Replace([]Entry{
		{BindIP: "127.0.0.1", Username: "user1", Password: "pass1", ServiceID: 101},
		{BindIP: "127.0.0.2", Username: "user2", Password: "pass2", ServiceID: 102},
	})

	if id, ok := s.Allow("127.0.0.1", "user1", "pass1"); !ok || id != 101 {
		t.Fatalf("expected user1 valid on 127.0.0.1, got ok=%v id=%d", ok, id)
	}
	if id, ok := s.Allow("127.0.0.2", "user2", "pass2"); !ok || id != 102 {
		t.Fatalf("expected user2 valid on 127.0.0.2, got ok=%v id=%d", ok, id)
	}
	// Credentials must not work on another customer's bind IP.
	if _, ok := s.Allow("127.0.0.2", "user1", "pass1"); ok {
		t.Fatal("user1 must not be valid on 127.0.0.2")
	}
	if _, ok := s.Allow("127.0.0.1", "user1", "wrong"); ok {
		t.Fatal("wrong password must be rejected")
	}
	if _, ok := s.Allow("127.0.0.1", "nobody", "pass1"); ok {
		t.Fatal("unknown user must be rejected")
	}
	if _, ok := s.Allow("10.0.0.9", "user1", "pass1"); ok {
		t.Fatal("unknown bind IP must be rejected")
	}
}

func TestReplaceSwapsAtomically(t *testing.T) {
	s := NewStore()
	s.Replace([]Entry{{BindIP: "127.0.0.1", Username: "old", Password: "old", ServiceID: 1}})

	if _, ok := s.Allow("127.0.0.1", "old", "old"); !ok {
		t.Fatal("initial entry missing")
	}
	s.Replace([]Entry{{BindIP: "127.0.0.1", Username: "new", Password: "new", ServiceID: 2}})
	if _, ok := s.Allow("127.0.0.1", "old", "old"); ok {
		t.Fatal("old entry should be gone after Replace")
	}
	if id, ok := s.Allow("127.0.0.1", "new", "new"); !ok || id != 2 {
		t.Fatalf("new entry missing after Replace, ok=%v id=%d", ok, id)
	}
	if s.Len() != 1 {
		t.Fatalf("expected 1 entry, got %d", s.Len())
	}
}

func TestReplaceSkipsInvalidRowsAndNormalizesIP(t *testing.T) {
	s := NewStore()
	s.Replace([]Entry{
		{BindIP: "", Username: "u", Password: "p", ServiceID: 1},
		{BindIP: "127.0.0.1", Username: "", Password: "p", ServiceID: 2},
		{BindIP: "::ffff:127.0.0.1", Username: "u", Password: "p", ServiceID: 3},
	})
	if s.Len() != 1 {
		t.Fatalf("expected 1 valid entry, got %d", s.Len())
	}
	// IPv4-mapped form must normalize to dotted quad.
	if id, ok := s.Allow("127.0.0.1", "u", "p"); !ok || id != 3 {
		t.Fatalf("normalized lookup failed, ok=%v id=%d", ok, id)
	}
}

func TestConcurrentReplaceAndAllow(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					s.Allow("127.0.0.1", "u", "p")
				}
			}
		}()
	}
	for i := 0; i < 1000; i++ {
		s.Replace([]Entry{{BindIP: "127.0.0.1", Username: "u", Password: fmt.Sprintf("p%d", i), ServiceID: i}})
	}
	close(stop)
	wg.Wait()
}
