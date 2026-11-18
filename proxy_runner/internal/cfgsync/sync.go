package cfgsync

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"time"

	"rackflow/proxy_runner/internal/auth"
	"rackflow/proxy_runner/internal/fastpath"
)

// FastPathSink receives the fast_path part of the config.
type FastPathSink interface {
	// Apply is called with the fast-path config whenever the version changes;
	// an absent fast_path key is passed as a config with no tenants.
	Apply(fastpath.Config)
	// Retry is called on every poll whose version did not change.
	Retry()
}

type Config struct {
	BaseURL  string
	APIKey   string
	Interval time.Duration
	Client   *http.Client
	// FastPath, when set, receives the fast_path part of each new config.
	FastPath FastPathSink
}

type configResponse struct {
	Version     string `json:"version"`
	RunnerID    int    `json:"runner_id"`
	Assignments []struct {
		ServiceID int    `json:"service_id"`
		BindIP    string `json:"bind_ip"`
		Username  string `json:"username"`
		Password  string `json:"password"`
	} `json:"assignments"`
	FastPath *fastpath.Config `json:"fast_path"`
}

// Loop polls Rackflow until ctx is cancelled.
func Loop(ctx context.Context, store *auth.Store, cfg Config) {
	if cfg.Client == nil {
		cfg.Client = &http.Client{Timeout: 10 * time.Second}
	}
	if cfg.Interval <= 0 {
		cfg.Interval = 30 * time.Second
	}
	ticker := time.NewTicker(cfg.Interval)
	defer ticker.Stop()

	for {
		if err := Once(ctx, store, cfg); err != nil {
			log.Printf("proxy config sync failed: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// validateBaseURL requires HTTPS for the Rackflow config-sync endpoint,
// since the response carries every customer's plaintext proxy credentials.
// Plain HTTP is allowed only for loopback targets so local development/tests
// keep working; any other host must use HTTPS or a network-level MITM could
// read/poison proxy credentials in transit.
func validateBaseURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("invalid RACKFLOW_BASE_URL: %w", err)
	}
	if u.Scheme == "https" {
		return nil
	}
	switch u.Hostname() {
	case "localhost", "127.0.0.1", "::1":
		return nil
	}
	return fmt.Errorf("RACKFLOW_BASE_URL must use https:// (got %q); proxy credentials are transmitted in this request", raw)
}

// Once fetches config and updates the store when the version changes.
func Once(ctx context.Context, store *auth.Store, cfg Config) error {
	if cfg.BaseURL == "" || cfg.APIKey == "" {
		return fmt.Errorf("RACKFLOW_BASE_URL and RUNNER_API_KEY are required")
	}
	if err := validateBaseURL(cfg.BaseURL); err != nil {
		return err
	}
	reqURL := cfg.BaseURL + "/api/runner/proxy/config"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)

	client := cfg.Client
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("config HTTP %d", resp.StatusCode)
	}
	var data configResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return err
	}
	version := data.Version
	if version == store.Version() {
		if cfg.FastPath != nil {
			cfg.FastPath.Retry()
		}
		return nil
	}
	if cfg.FastPath != nil {
		fast := fastpath.Config{}
		if data.FastPath != nil {
			fast = *data.FastPath
		}
		cfg.FastPath.Apply(fast)
	}
	rows := make([]auth.Entry, 0, len(data.Assignments))
	for _, a := range data.Assignments {
		rows = append(rows, auth.Entry{
			BindIP:    a.BindIP,
			Username:  a.Username,
			Password:  a.Password,
			ServiceID: a.ServiceID,
		})
	}
	store.Replace(version, rows)
	log.Printf("synced proxy config version=%s entries=%d runner_id=%d", version, store.Len(), data.RunnerID)
	return nil
}
