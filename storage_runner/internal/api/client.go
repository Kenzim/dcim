package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// Client talks to Rackflow with the runner's bearer key.
type Client struct {
	BaseURL string
	Key     string
	HTTP    *http.Client
}

func (c *Client) http() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return &http.Client{Timeout: 20 * time.Second}
}

// validateBaseURL requires HTTPS unless the target is loopback, because the
// config response steers what the runner does to customer data.
func validateBaseURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("invalid RACKFLOW_URL: %w", err)
	}
	if u.Scheme == "https" {
		return nil
	}
	if u.Scheme == "http" {
		switch u.Hostname() {
		case "localhost", "127.0.0.1", "::1":
			return nil
		}
	}
	return fmt.Errorf("RACKFLOW_URL must use https:// (got %q)", raw)
}

func (c *Client) request(ctx context.Context, method, path string, body []byte) (*http.Response, error) {
	if c.BaseURL == "" || c.Key == "" {
		return nil, fmt.Errorf("RACKFLOW_URL and RUNNER_KEY are required")
	}
	if err := validateBaseURL(c.BaseURL); err != nil {
		return nil, err
	}
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Key)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return c.http().Do(req)
}

// Fetch returns the desired state. A response without a version or without a
// tenants list is rejected, so a malformed reply is never applied.
func (c *Client) Fetch(ctx context.Context) (*Config, error) {
	resp, err := c.request(ctx, http.MethodGet, "/api/runner/storage/config", nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("config HTTP %d", resp.StatusCode)
	}
	var raw struct {
		Config
		Tenants *[]Tenant `json:"tenants"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 32<<20)).Decode(&raw); err != nil {
		return nil, fmt.Errorf("config decode: %w", err)
	}
	if raw.Version == "" {
		return nil, fmt.Errorf("config has no version")
	}
	if raw.Tenants == nil {
		return nil, fmt.Errorf("config has no tenants list")
	}
	cfg := raw.Config
	cfg.Tenants = *raw.Tenants
	return &cfg, nil
}

// PostStatus reports the applied state; Rackflow answers 204.
func (c *Client) PostStatus(ctx context.Context, st Status) error {
	body, err := json.Marshal(st)
	if err != nil {
		return err
	}
	resp, err := c.request(ctx, http.MethodPost, "/api/runner/storage/status", body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("status HTTP %d", resp.StatusCode)
	}
	return nil
}
