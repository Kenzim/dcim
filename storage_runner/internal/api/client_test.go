package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const contractConfig = `{"version": "abc123", "node": {"id": 3, "name": "store-01"},
 "settings": {"destroy_grace_seconds": 604800},
 "tenants": [
  {"id": 12, "state": "terminated", "terminated_at": "2026-11-18T10:00:00Z", "destroy_after": "2026-11-25T10:00:00",
   "vlan": 1234, "network_cidr": "10.0.0.0/24", "endpoint_ip": "10.0.0.254", "prefix_len": 24,
   "size_bytes": 536870912000, "limits": {"bandwidth_mbps": 100, "iops": null},
   "shares": [{"id": 5, "name": "data", "quota_bytes": 107374182400, "clients": ["10.0.0.5"],
               "read_only": false, "state": "active", "destroy_after": "2026-11-25T10:00:00.123456+00:00"}]}]}`

func TestFetchParsesContract(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/runner/storage/config" || r.Header.Get("Authorization") != "Bearer k" {
			t.Errorf("unexpected request %s %q", r.URL.Path, r.Header.Get("Authorization"))
		}
		_, _ = io.WriteString(w, contractConfig)
	}))
	defer srv.Close()

	cfg, err := (&Client{BaseURL: srv.URL, Key: "k"}).Fetch(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Version != "abc123" || cfg.Node.Name != "store-01" || *cfg.Settings.DestroyGraceSeconds != 604800 || len(cfg.Tenants) != 1 {
		t.Fatalf("cfg %+v", cfg)
	}
	tn := cfg.Tenants[0]
	if !tn.TerminatedAt.Equal(time.Date(2026, 11, 18, 10, 0, 0, 0, time.UTC)) ||
		!tn.DestroyAfter.Equal(time.Date(2026, 11, 25, 10, 0, 0, 0, time.UTC)) {
		t.Fatalf("timestamps %v %v", tn.TerminatedAt, tn.DestroyAfter)
	}
	if tn.Limits == nil || *tn.Limits.BandwidthMbps != 100 || tn.Limits.IOPS != nil {
		t.Fatalf("limits %+v", tn.Limits)
	}
	sh := tn.Shares[0]
	if sh.QuotaBytes != 107374182400 || sh.DestroyAfter.Nanosecond() != 123456000 || sh.Clients[0] != "10.0.0.5" {
		t.Fatalf("share %+v", sh)
	}
}

func TestFetchRejectsMalformed(t *testing.T) {
	for name, body := range map[string]string{
		"no tenants key": `{"version":"v1"}`,
		"no version":     `{"tenants":[]}`,
		"bad json":       `{"version":`,
		"bad timestamp":  `{"version":"v","tenants":[{"id":1,"terminated_at":"yesterday"}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, body)
			}))
			defer srv.Close()
			if _, err := (&Client{BaseURL: srv.URL, Key: "k"}).Fetch(context.Background()); err == nil {
				t.Fatal("expected error")
			}
		})
	}
}

func TestFetchHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	if _, err := (&Client{BaseURL: srv.URL, Key: "bad"}).Fetch(context.Background()); err == nil {
		t.Fatal("expected error")
	}
}

func TestRedirectsAreNotFollowed(t *testing.T) {
	followed := false
	mux := http.NewServeMux()
	mux.HandleFunc("/api/runner/storage/config", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/elsewhere", http.StatusFound)
	})
	mux.HandleFunc("/elsewhere", func(w http.ResponseWriter, r *http.Request) {
		followed = true
		_, _ = io.WriteString(w, `{"version":"v","tenants":[]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	_, err := (&Client{BaseURL: srv.URL, Key: "k", HTTP: srv.Client()}).Fetch(context.Background())
	if err == nil || !strings.Contains(err.Error(), "302") || followed {
		t.Fatalf("err=%v followed=%v", err, followed)
	}
}

func TestRequiresHTTPS(t *testing.T) {
	_, err := (&Client{BaseURL: "http://rackflow.example.com", Key: "k"}).Fetch(context.Background())
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("err %v", err)
	}
}

func TestPostStatus(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/runner/storage/status" || r.Header.Get("Authorization") != "Bearer k" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	msg := "boom"
	st := Status{
		VersionApplied: "v1",
		RunnerVersion:  "0.1.0",
		Node:           NodeStatus{CephOK: true, FreeBytes: 123},
		Tenants:        []TenantStatus{{ID: 12, State: StateError, Error: &msg}},
	}
	if err := (&Client{BaseURL: srv.URL, Key: "k"}).PostStatus(context.Background(), st); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"destroyed_tenants", "destroyed_shares", "orphans"} {
		if v, ok := body[key].([]any); !ok || len(v) != 0 {
			t.Errorf("%s = %#v, want []", key, body[key])
		}
	}
	node := body["node"].(map[string]any)
	if node["error"] != nil || node["ceph_ok"] != true {
		t.Errorf("node %v", node)
	}
	tenant := body["tenants"].([]any)[0].(map[string]any)
	if tenant["error"] != "boom" || len(tenant["shares"].([]any)) != 0 {
		t.Errorf("tenant %v", tenant)
	}
}

func TestPostStatusNon2xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
	}))
	defer srv.Close()
	if err := (&Client{BaseURL: srv.URL, Key: "k"}).PostStatus(context.Background(), Status{}); err == nil {
		t.Fatal("expected error")
	}
}
