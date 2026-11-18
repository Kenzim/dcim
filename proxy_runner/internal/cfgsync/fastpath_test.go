package cfgsync

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"rackflow/proxy_runner/internal/auth"
	"rackflow/proxy_runner/internal/fastpath"
)

type recordingSink struct {
	applied []fastpath.Config
	retries int
}

func (r *recordingSink) Apply(cfg fastpath.Config) { r.applied = append(r.applied, cfg) }
func (r *recordingSink) Retry()                    { r.retries++ }

func TestOncePassesFastPathToSinkAndKeepsItOutOfThePublicStore(t *testing.T) {
	bodies := []map[string]any{
		{
			"version":   "v1",
			"runner_id": 3,
			"assignments": []map[string]any{
				{"service_id": 1, "bind_ip": "203.0.113.10", "username": "pub", "password": "pp"},
			},
			"fast_path": map[string]any{
				"cidr": "100.80.0.0/16",
				"port": 11080,
				"tenants": []map[string]any{{
					"network_id": 41, "vlan": 2017, "addresses": []string{"100.80.0.1"},
					"assignments": []map[string]any{{
						"service_id": 9, "private_ip": "100.80.0.1", "bind_ip": "203.0.113.10",
						"username": "fast", "password": "fp",
					}},
				}},
			},
		},
		{"version": "v1", "runner_id": 3, "assignments": []map[string]any{}},
		{"version": "v2", "runner_id": 3, "assignments": []map[string]any{}},
	}
	call := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(bodies[call])
		call++
	}))
	defer srv.Close()

	store := auth.NewStore()
	sink := &recordingSink{}
	cfg := Config{BaseURL: srv.URL, APIKey: "k", FastPath: sink}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatal(err)
	}
	if len(sink.applied) != 1 {
		t.Fatalf("applied %d times", len(sink.applied))
	}
	got := sink.applied[0]
	if got.CIDR != "100.80.0.0/16" || got.Port != 11080 || len(got.Tenants) != 1 {
		t.Fatalf("fast path parsed as %+v", got)
	}
	tenant := got.Tenants[0]
	if tenant.NetworkID != 41 || tenant.VLAN != 2017 || tenant.Addresses[0] != "100.80.0.1" ||
		tenant.Assignments[0].BindIP != "203.0.113.10" || tenant.Assignments[0].Username != "fast" {
		t.Fatalf("tenant parsed as %+v", tenant)
	}
	if _, ok := store.Allow("100.80.0.1", "fast", "fp"); ok {
		t.Fatal("fast-path credentials must not enter the public store")
	}
	if _, ok := store.Allow("203.0.113.10", "fast", "fp"); ok {
		t.Fatal("fast-path credentials must not work on the public path")
	}
	if _, ok := store.Allow("203.0.113.10", "pub", "pp"); !ok {
		t.Fatal("public assignment missing")
	}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatal(err)
	}
	if len(sink.applied) != 1 || sink.retries != 1 {
		t.Fatalf("unchanged version: applied=%d retries=%d", len(sink.applied), sink.retries)
	}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatal(err)
	}
	if len(sink.applied) != 2 || len(sink.applied[1].Tenants) != 0 {
		t.Fatalf("absent fast_path must apply zero tenants, got %+v", sink.applied)
	}
}
