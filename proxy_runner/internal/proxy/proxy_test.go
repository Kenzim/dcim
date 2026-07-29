package proxy

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"testing"
	"time"

	"rackflow/proxy_runner/internal/auth"
)

// The functional tests below deliberately proxy to 127.0.0.1 test targets,
// which the destination-allowlist introduced to prevent SSRF-via-proxy
// would otherwise reject. Opt in for the whole test binary; a dedicated
// test below temporarily clears this to prove the default-deny behavior.
func init() {
	os.Setenv("PROXY_ALLOW_PRIVATE_DESTINATIONS", "true")
}

func startTarget(t *testing.T) (addr string, closeFn func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(conn net.Conn) {
				defer conn.Close()
				br := bufio.NewReader(conn)
				req, err := http.ReadRequest(br)
				if err != nil {
					return
				}
				body := "ok-target"
				fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(body), body)
				_ = req.Body.Close()
			}(c)
		}
	}()
	return ln.Addr().String(), func() { _ = ln.Close() }
}

func startProxy(t *testing.T, store *auth.Store) (addr string, closeFn func()) {
	t.Helper()
	srv := &Server{
		Listen:      "127.0.0.1:0",
		Store:       store,
		DialTimeout: 3 * time.Second,
		IdleTimeout: 5 * time.Second,
	}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	return srv.ln.Addr().String(), func() { _ = srv.Close() }
}

func TestHTTPConnectAndAuth(t *testing.T) {
	targetAddr, closeTarget := startTarget(t)
	defer closeTarget()

	store := auth.NewStore()
	proxyAddr, closeProxy := startProxy(t, store)
	defer closeProxy()

	// Seed after listen so bind IP matches LocalAddr (127.0.0.1).
	store.Replace("v1", []auth.Entry{{
		BindIP: "127.0.0.1", Username: "user1", Password: "pass1", ServiceID: 1,
	}})

	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	authHeader := "Basic " + base64.StdEncoding.EncodeToString([]byte("user1:pass1"))
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", targetAddr, targetAddr, authHeader)
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("CONNECT status %d", resp.StatusCode)
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
	resp2, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp2.Body)
	if string(body) != "ok-target" {
		t.Fatalf("body=%q", body)
	}
}

func TestHTTPForbidden(t *testing.T) {
	store := auth.NewStore()
	store.Replace("v1", []auth.Entry{{BindIP: "127.0.0.1", Username: "u", Password: "p", ServiceID: 1}})
	proxyAddr, closeProxy := startProxy(t, store)
	defer closeProxy()

	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	bad := "Basic " + base64.StdEncoding.EncodeToString([]byte("u:wrong"))
	fmt.Fprintf(conn, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: %s\r\n\r\n", bad)
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status %d", resp.StatusCode)
	}
}

// TestOutboundDialUsesBindIP proves egress traffic actually leaves via the
// customer's assigned IP (bindIP), not the host's default route. It listens
// the proxy on 127.0.0.2 (a distinct loopback alias from the target's
// 127.0.0.1) and asserts the target sees the *proxy's* connection sourced
// from 127.0.0.2 — which only happens if dialTCP honors bindIP.
func TestOutboundDialUsesBindIP(t *testing.T) {
	remoteIPs := make(chan string, 1)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		host, _, _ := net.SplitHostPort(c.RemoteAddr().String())
		remoteIPs <- host
		br := bufio.NewReader(c)
		if _, err := http.ReadRequest(br); err != nil {
			return
		}
		body := "ok-target"
		fmt.Fprintf(c, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(body), body)
	}()
	targetAddr := ln.Addr().String()

	store := auth.NewStore()
	srv := &Server{
		Listen:      "127.0.0.2:0",
		Store:       store,
		DialTimeout: 3 * time.Second,
		IdleTimeout: 5 * time.Second,
	}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	store.Replace("v1", []auth.Entry{{BindIP: "127.0.0.2", Username: "bu", Password: "bp", ServiceID: 5}})

	conn, err := net.DialTimeout("tcp", srv.ln.Addr().String(), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	authHeader := "Basic " + base64.StdEncoding.EncodeToString([]byte("bu:bp"))
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", targetAddr, targetAddr, authHeader)
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("CONNECT status %d", resp.StatusCode)
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")

	select {
	case ip := <-remoteIPs:
		if ip != "127.0.0.2" {
			t.Fatalf("expected egress from bind IP 127.0.0.2, target saw %q", ip)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for target to observe inbound connection")
	}
}

// TestHTTPKeepAliveReusesConnection proves plain (non-CONNECT) HTTP proxy
// requests can be pipelined over one TCP connection instead of forcing a
// fresh connection (and fresh upstream dial) per request — the fix for a
// real perf bottleneck found while benchmarking against Squid.
func TestHTTPKeepAliveReusesConnection(t *testing.T) {
	// Use a target that itself keeps connections alive (unlike startTarget,
	// which closes after one request) so this test isolates the proxy's own
	// client-facing keep-alive behavior.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(conn net.Conn) {
				defer conn.Close()
				br := bufio.NewReader(conn)
				for {
					req, err := http.ReadRequest(br)
					if err != nil {
						return
					}
					body := "ok-target"
					fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s", len(body), body)
					_ = req.Body.Close()
				}
			}(c)
		}
	}()
	targetAddr := ln.Addr().String()

	store := auth.NewStore()
	store.Replace("v1", []auth.Entry{{BindIP: "127.0.0.1", Username: "u", Password: "p", ServiceID: 1}})
	proxyAddr, closeProxy := startProxy(t, store)
	defer closeProxy()

	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	br := bufio.NewReader(conn)

	authHeader := "Basic " + base64.StdEncoding.EncodeToString([]byte("u:p"))
	for i := 0; i < 3; i++ {
		fmt.Fprintf(conn, "GET http://%s/ HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", targetAddr, targetAddr, authHeader)
		resp, err := http.ReadResponse(br, nil)
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 || string(body) != "ok-target" {
			t.Fatalf("request %d: status=%d body=%q", i, resp.StatusCode, body)
		}
	}
}

// TestDialTCPBlocksPrivateDestinationsByDefault proves that, without the
// explicit opt-out, dialTCP refuses to connect to loopback/private
// addresses -- the fix for the proxy being usable as an open relay/SSRF
// primitive into internal infrastructure reachable from the runner host.
func TestDialTCPBlocksPrivateDestinationsByDefault(t *testing.T) {
	os.Unsetenv("PROXY_ALLOW_PRIVATE_DESTINATIONS")
	defer os.Setenv("PROXY_ALLOW_PRIVATE_DESTINATIONS", "true")

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()

	if _, err := dialTCP(ln.Addr().String(), "", 2*time.Second); err == nil {
		t.Fatal("expected dial to loopback target to be blocked by default")
	}
}

func TestIsBlockedDestination(t *testing.T) {
	blocked := []string{
		"127.0.0.1", "169.254.169.254", "10.0.0.1", "192.168.1.1",
		"172.16.5.5", "::1", "fe80::1", "fc00::1", "0.0.0.0",
	}
	for _, ip := range blocked {
		if !isBlockedDestination(net.ParseIP(ip)) {
			t.Errorf("expected %s to be blocked", ip)
		}
	}
	allowed := []string{"8.8.8.8", "1.1.1.1", "93.184.216.34"}
	for _, ip := range allowed {
		if isBlockedDestination(net.ParseIP(ip)) {
			t.Errorf("expected %s to be allowed", ip)
		}
	}
}

func TestSOCKS5Connect(t *testing.T) {
	targetAddr, closeTarget := startTarget(t)
	defer closeTarget()
	host, portStr, err := net.SplitHostPort(targetAddr)
	if err != nil {
		t.Fatal(err)
	}
	var port int
	fmt.Sscanf(portStr, "%d", &port)

	store := auth.NewStore()
	proxyAddr, closeProxy := startProxy(t, store)
	defer closeProxy()
	store.Replace("v1", []auth.Entry{{BindIP: "127.0.0.1", Username: "su", Password: "sp", ServiceID: 2}})

	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	// greeting: ver=5, 1 method, user/pass
	_, _ = conn.Write([]byte{0x05, 0x01, 0x02})
	buf := make([]byte, 2)
	if _, err := io.ReadFull(conn, buf); err != nil || buf[0] != 0x05 || buf[1] != 0x02 {
		t.Fatalf("greeting reply %v %v", buf, err)
	}
	// user/pass
	ub, pb := []byte("su"), []byte("sp")
	msg := []byte{0x01, byte(len(ub))}
	msg = append(msg, ub...)
	msg = append(msg, byte(len(pb)))
	msg = append(msg, pb...)
	_, _ = conn.Write(msg)
	if _, err := io.ReadFull(conn, buf); err != nil || buf[1] != 0x00 {
		t.Fatalf("auth reply %v %v", buf, err)
	}
	// CONNECT to IPv4 target
	ip := net.ParseIP(host).To4()
	req := []byte{0x05, 0x01, 0x00, 0x01}
	req = append(req, ip...)
	req = append(req, byte(port>>8), byte(port))
	_, _ = conn.Write(req)
	reply := make([]byte, 10)
	if _, err := io.ReadFull(conn, reply); err != nil || reply[1] != 0x00 {
		t.Fatalf("connect reply %v %v", reply, err)
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "ok-target" {
		t.Fatalf("body=%q", body)
	}
}
