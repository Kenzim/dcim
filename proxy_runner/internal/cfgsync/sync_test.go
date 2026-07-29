package cfgsync

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"rackflow/proxy_runner/internal/auth"
)

func TestOnceUpdatesStoreOnNewVersion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer runner-key" {
			t.Errorf("unexpected Authorization header: %q", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"version":     "v1",
			"location_id": 7,
			"assignments": []map[string]any{
				{"service_id": 1, "bind_ip": "10.0.0.5", "username": "u1", "password": "p1"},
			},
		})
	}))
	defer srv.Close()

	store := auth.NewStore()
	cfg := Config{BaseURL: srv.URL, APIKey: "runner-key"}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatalf("Once returned error: %v", err)
	}
	if store.Version() != "v1" {
		t.Fatalf("expected version v1, got %q", store.Version())
	}
	if id, ok := store.Allow("10.0.0.5", "u1", "p1"); !ok || id != 1 {
		t.Fatalf("expected entry allowed, got ok=%v id=%d", ok, id)
	}
}

func TestOnceSkipsReplaceWhenVersionUnchanged(t *testing.T) {
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"version":     "same",
			"location_id": 1,
			"assignments": []map[string]any{
				{"service_id": 2, "bind_ip": "10.0.0.9", "username": "u2", "password": "p2"},
			},
		})
	}))
	defer srv.Close()

	store := auth.NewStore()
	cfg := Config{BaseURL: srv.URL, APIKey: "runner-key"}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatalf("first Once error: %v", err)
	}
	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatalf("second Once error: %v", err)
	}
	if got := atomic.LoadInt32(&hits); got != 2 {
		t.Fatalf("expected server to be hit twice, got %d", got)
	}
	// Same version both times: store must still reflect the one entry
	// (Replace short-circuits, but the data must not have been lost/duplicated).
	if id, ok := store.Allow("10.0.0.9", "u2", "p2"); !ok || id != 2 {
		t.Fatalf("expected entry still allowed, got ok=%v id=%d", ok, id)
	}
}

func TestOnceRemovesEntriesWhenVersionChangesToEmpty(t *testing.T) {
	// Simulates a suspend/terminate: assignments disappear and the version
	// changes, so the runner must drop the old auth entries immediately.
	version := "v1"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if version == "v1" {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"version":     "v1",
				"location_id": 1,
				"assignments": []map[string]any{
					{"service_id": 3, "bind_ip": "10.0.0.11", "username": "u3", "password": "p3"},
				},
			})
		} else {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"version":     "v2-empty",
				"location_id": 1,
				"assignments": []map[string]any{},
			})
		}
	}))
	defer srv.Close()

	store := auth.NewStore()
	cfg := Config{BaseURL: srv.URL, APIKey: "runner-key"}

	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatalf("first Once error: %v", err)
	}
	if _, ok := store.Allow("10.0.0.11", "u3", "p3"); !ok {
		t.Fatal("expected entry allowed after first sync")
	}

	version = "v2"
	if err := Once(context.Background(), store, cfg); err != nil {
		t.Fatalf("second Once error: %v", err)
	}
	if _, ok := store.Allow("10.0.0.11", "u3", "p3"); ok {
		t.Fatal("expected entry removed after suspend/terminate sync")
	}
	if store.Len() != 0 {
		t.Fatalf("expected empty store, got %d entries", store.Len())
	}
}

func TestOnceReturnsErrorOnNonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	store := auth.NewStore()
	cfg := Config{BaseURL: srv.URL, APIKey: "wrong-key"}

	if err := Once(context.Background(), store, cfg); err == nil {
		t.Fatal("expected error for 401 response")
	}
}

func TestOnceRequiresBaseURLAndAPIKey(t *testing.T) {
	store := auth.NewStore()
	if err := Once(context.Background(), store, Config{}); err == nil {
		t.Fatal("expected error when BaseURL/APIKey are missing")
	}
}

func TestLoopStopsOnContextCancel(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"version": "v1", "location_id": 1, "assignments": []map[string]any{}})
	}))
	defer srv.Close()

	store := auth.NewStore()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		Loop(ctx, store, Config{BaseURL: srv.URL, APIKey: "k", Interval: 5 * time.Millisecond})
		close(done)
	}()

	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Loop did not stop after context cancellation")
	}
}
