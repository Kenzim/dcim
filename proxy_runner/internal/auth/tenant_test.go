package auth

import "testing"

func tenantFixture() *TenantStore {
	return NewTenantStore([]TenantEntry{
		{PrivateIP: "100.80.0.1", BindIP: "203.0.113.9", Username: "alice", Password: "pw", ServiceID: 2},
		{PrivateIP: "100.80.0.1", BindIP: "203.0.113.2", Username: "alice", Password: "pw", ServiceID: 1},
		{PrivateIP: "100.80.0.1", BindIP: "203.0.113.5", Username: "alice", Password: "other", ServiceID: 3},
		{PrivateIP: "100.80.0.2", BindIP: "198.51.100.7", Username: "alice", Password: "pw", ServiceID: 4},
	})
}

func TestTenantLookupPicksLowestOwnedIP(t *testing.T) {
	store := tenantFixture()
	ip, sid, ok := store.Lookup("100.80.0.1", "alice", "pw", "")
	if !ok || ip != "203.0.113.2" || sid != 1 {
		t.Fatalf("got %q %d %v", ip, sid, ok)
	}
	ip, sid, ok = store.Lookup("100.80.0.1", "alice", "other", "")
	if !ok || ip != "203.0.113.5" || sid != 3 {
		t.Fatalf("second password: got %q %d %v", ip, sid, ok)
	}
	if ip, _, ok := store.Lookup("100.80.0.2", "alice", "pw", ""); !ok || ip != "198.51.100.7" {
		t.Fatalf("other subnet: got %q %v", ip, ok)
	}
}

func TestTenantLookupRefusesWrongCredentialsAndOtherAddresses(t *testing.T) {
	store := tenantFixture()
	cases := []struct{ private, user, pass string }{
		{"100.80.0.1", "alice", "wrong"},
		{"100.80.0.1", "bob", "pw"},
		{"100.80.0.3", "alice", "pw"},
		{"203.0.113.2", "alice", "pw"},
	}
	for _, c := range cases {
		if _, _, ok := store.Lookup(c.private, c.user, c.pass, ""); ok {
			t.Fatalf("expected refusal for %+v", c)
		}
	}
	var empty *TenantStore
	if _, _, ok := empty.Lookup("100.80.0.1", "alice", "pw", ""); ok {
		t.Fatal("nil store must refuse")
	}
}

func TestTenantLookupXBindIPOnlySelectsOwnedIPs(t *testing.T) {
	store := tenantFixture()
	if ip, sid, ok := store.Lookup("100.80.0.1", "alice", "pw", "203.0.113.9"); !ok || ip != "203.0.113.9" || sid != 2 {
		t.Fatalf("owned header IP: got %q %d %v", ip, sid, ok)
	}
	refused := []string{
		"203.0.113.5",  // owned by the same username with a different password
		"198.51.100.7", // owned, but in another subnet's private IP
		"192.0.2.1",    // not owned at all
	}
	for _, header := range refused {
		if _, _, ok := store.Lookup("100.80.0.1", "alice", "pw", header); ok {
			t.Fatalf("X-Bind-IP %s must be refused", header)
		}
	}
}

func TestTenantStoresAreSeparate(t *testing.T) {
	alice := NewTenantStore([]TenantEntry{{PrivateIP: "100.80.0.1", BindIP: "203.0.113.2", Username: "a", Password: "p", ServiceID: 1}})
	bob := NewTenantStore([]TenantEntry{{PrivateIP: "100.80.0.1", BindIP: "203.0.113.3", Username: "b", Password: "q", ServiceID: 2}})
	if _, _, ok := bob.Lookup("100.80.0.1", "a", "p", ""); ok {
		t.Fatal("one tenant's credentials must not work in another tenant")
	}
	if _, _, ok := alice.Lookup("100.80.0.1", "b", "q", ""); ok {
		t.Fatal("one tenant's credentials must not work in another tenant")
	}
	if alice.Len() != 1 || bob.Len() != 1 {
		t.Fatal("unexpected length")
	}
}
