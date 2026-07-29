// benchclient drives latency/concurrency benchmarks against an HTTP proxy,
// used to compare proxy-runner against a baseline (e.g. Squid). It fetches a
// fixed local target through the given proxy N times at concurrency C and
// reports throughput + latency percentiles.
package main

import (
	"crypto/tls"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

func main() {
	proxyURL := flag.String("proxy", "", "proxy URL, e.g. http://user:pass@ip:port")
	target := flag.String("target", "http://127.0.0.1:9999/", "target URL to fetch through the proxy")
	n := flag.Int("n", 500, "total requests")
	c := flag.Int("c", 1, "concurrency")
	label := flag.String("label", "proxy", "label for output")
	insecure := flag.Bool("insecure", false, "skip TLS verification (for self-signed bench targets)")
	flag.Parse()

	pu, err := url.Parse(*proxyURL)
	if err != nil {
		fmt.Fprintln(os.Stderr, "bad proxy url:", err)
		os.Exit(1)
	}

	tr := &http.Transport{
		Proxy:               http.ProxyURL(pu),
		MaxIdleConns:        *c * 2,
		MaxIdleConnsPerHost: *c * 2,
		IdleConnTimeout:     30 * time.Second,
		TLSClientConfig:     &tls.Config{InsecureSkipVerify: *insecure},
	}
	client := &http.Client{Transport: tr, Timeout: 10 * time.Second}

	var wg sync.WaitGroup
	var okCount, errCount int64
	latencies := make([]time.Duration, *n)
	for i := range latencies {
		latencies[i] = -1
	}
	idxCh := make(chan int, *n)
	for i := 0; i < *n; i++ {
		idxCh <- i
	}
	close(idxCh)

	start := time.Now()
	for w := 0; w < *c; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for idx := range idxCh {
				t0 := time.Now()
				resp, err := client.Get(*target)
				if err != nil {
					atomic.AddInt64(&errCount, 1)
					continue
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
				latencies[idx] = time.Since(t0)
				atomic.AddInt64(&okCount, 1)
			}
		}()
	}
	wg.Wait()
	total := time.Since(start)

	good := make([]time.Duration, 0, len(latencies))
	for _, l := range latencies {
		if l >= 0 {
			good = append(good, l)
		}
	}
	sort.Slice(good, func(i, j int) bool { return good[i] < good[j] })

	pct := func(p float64) time.Duration {
		if len(good) == 0 {
			return 0
		}
		idx := int(p * float64(len(good)-1))
		return good[idx]
	}
	var sum time.Duration
	for _, l := range good {
		sum += l
	}
	var mean time.Duration
	if len(good) > 0 {
		mean = sum / time.Duration(len(good))
	}

	fmt.Printf("=== %s (n=%d c=%d) ===\n", *label, *n, *c)
	fmt.Printf("ok=%d errors=%d\n", okCount, errCount)
	fmt.Printf("total=%s throughput=%.1f req/s\n", total, float64(okCount)/total.Seconds())
	if len(good) > 0 {
		fmt.Printf("latency min=%s mean=%s p50=%s p90=%s p95=%s p99=%s max=%s\n",
			good[0], mean, pct(0.50), pct(0.90), pct(0.95), pct(0.99), good[len(good)-1])
	}
}
