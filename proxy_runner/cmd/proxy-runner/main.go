package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"rackflow/proxy_runner/internal/auth"
	"rackflow/proxy_runner/internal/cfgsync"
	"rackflow/proxy_runner/internal/proxy"
)

func main() {
	baseURL := env("RACKFLOW_BASE_URL", "")
	apiKey := env("RUNNER_API_KEY", "")
	proxyListen := env("PROXY_LISTEN", ":8080")
	healthListen := env("HEALTH_LISTEN", ":8081")
	syncInterval := envDuration("SYNC_INTERVAL_SECONDS", 10*time.Second)
	dialTimeout := envDuration("DIAL_TIMEOUT", 15*time.Second)
	idleTimeout := envDuration("IDLE_TIMEOUT", 5*time.Minute)

	store := auth.NewStore()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	go cfgsync.Loop(ctx, store, cfgsync.Config{
		BaseURL:  baseURL,
		APIKey:   apiKey,
		Interval: syncInterval,
	})

	srv := &proxy.Server{
		Listen:      proxyListen,
		Store:       store,
		DialTimeout: dialTimeout,
		IdleTimeout: idleTimeout,
	}
	if err := srv.Start(); err != nil {
		log.Fatalf("proxy listen: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok":      true,
			"version": store.Version(),
			"entries": store.Len(),
		})
	})
	healthSrv := &http.Server{Addr: healthListen, Handler: mux}
	go func() {
		log.Printf("health listening on %s", healthListen)
		if err := healthSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("health server: %v", err)
		}
	}()

	<-ctx.Done()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	_ = healthSrv.Shutdown(shutdownCtx)
	_ = srv.Close()
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	// Prefer integer seconds for SYNC_INTERVAL_SECONDS compatibility.
	if secs, err := strconv.Atoi(v); err == nil {
		return time.Duration(secs) * time.Second
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}
