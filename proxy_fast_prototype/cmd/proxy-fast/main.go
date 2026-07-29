// proxy-fast is the prototype dual HTTP+SOCKS5 bind-IP proxy with upstream
// connection pooling. It seeds a static auth table via Store.Replace — the
// same API a real backend sync loop would call periodically.
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

	"rackflow/proxy_fast_prototype/internal/auth"
	"rackflow/proxy_fast_prototype/internal/proxy"
)

func main() {
	proxyListen := env("PROXY_LISTEN", ":8092")
	healthListen := env("HEALTH_LISTEN", ":8093")
	dialTimeout := envDuration("DIAL_TIMEOUT", 15*time.Second)
	idleTimeout := envDuration("IDLE_TIMEOUT", 5*time.Minute)

	store := auth.NewStore()
	// Test harness entries: one customer per loopback bind IP. A production
	// sync loop would call Replace with backend rows on an interval.
	store.Replace([]auth.Entry{
		{BindIP: "127.0.0.1", Username: "user1", Password: "pass1", ServiceID: 101},
		{BindIP: "127.0.0.2", Username: "user2", Password: "pass2", ServiceID: 102},
	})

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

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
	if secs, err := strconv.Atoi(v); err == nil {
		return time.Duration(secs) * time.Second
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}
