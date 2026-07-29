package proxy

import (
	"bufio"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"rackflow/proxy_fast_prototype/internal/auth"
)

// startOrigin runs an HTTP origin that responds with the client's remote
// address and counts distinct TCP connections it accepts.
func startOrigin(t *testing.T) (*httptest.Server, *int64) {
	t.Helper()
	var conns int64
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "remote=%s", r.RemoteAddr)
	}))
	srv.Config.ConnState = func(c net.Conn, st http.ConnState) {
		if st == http.StateNew {
			atomic.AddInt64(&conns, 1)
		}
	}
	srv.Start()
	t.Cleanup(srv.Close)
	return srv, &conns
}

// startProxy starts the dual-protocol server on a wildcard port so it is
// reachable via both 127.0.0.1 and 127.0.0.2, and returns the port.
func startProxy(t *testing.T) int {
	t.Helper()
	store := auth.NewStore()
	store.Replace([]auth.Entry{
		{BindIP: "127.0.0.1", Username: "user1", Password: "pass1", ServiceID: 101},
		{BindIP: "127.0.0.2", Username: "user2", Password: "pass2", ServiceID: 102},
	})
	srv := &Server{
		Listen:      ":0",
		Store:       store,
		DialTimeout: 5 * time.Second,
		IdleTimeout: 30 * time.Second,
	}
	if err := srv.Start(); err != nil {
		t.Fatalf("start proxy: %v", err)
	}
	t.Cleanup(func() { srv.Close() })
	_, portStr, _ := net.SplitHostPort(srv.Addr().String())
	port, _ := strconv.Atoi(portStr)
	return port
}

func proxyClient(t *testing.T, proxyURL string) *http.Client {
	t.Helper()
	pu, err := url.Parse(proxyURL)
	if err != nil {
		t.Fatalf("parse proxy url: %v", err)
	}
	tr := &http.Transport{Proxy: http.ProxyURL(pu)}
	t.Cleanup(tr.CloseIdleConnections)
	return &http.Client{Transport: tr, Timeout: 5 * time.Second}
}

func fetchBody(t *testing.T, c *http.Client, target string) (int, string) {
	t.Helper()
	resp, err := c.Get(target)
	if err != nil {
		t.Fatalf("GET %s: %v", target, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

// TestBindIPEgressAndPoolIsolation proves (a) outbound connections are bound
// to the customer's bind IP for both 127.0.0.1 and 127.0.0.2 independently,
// and (b) two bind IPs never share a pooled upstream connection even when
// hitting the same destination.
func TestBindIPEgressAndPoolIsolation(t *testing.T) {
	origin, conns := startOrigin(t)
	port := startProxy(t)

	c1 := proxyClient(t, fmt.Sprintf("http://user1:pass1@127.0.0.1:%d", port))
	c2 := proxyClient(t, fmt.Sprintf("http://user2:pass2@127.0.0.2:%d", port))

	code, body := fetchBody(t, c1, origin.URL)
	if code != 200 || !strings.Contains(body, "remote=127.0.0.1:") {
		t.Fatalf("bind 127.0.0.1: code=%d body=%q", code, body)
	}
	code, body = fetchBody(t, c2, origin.URL)
	if code != 200 || !strings.Contains(body, "remote=127.0.0.2:") {
		t.Fatalf("bind 127.0.0.2: code=%d body=%q", code, body)
	}
	if got := atomic.LoadInt64(conns); got != 2 {
		t.Fatalf("expected 2 upstream conns (one per bind IP), got %d", got)
	}

	// Same customers again: pooled conns must be reused within the same
	// bind IP (no new upstream connections).
	for i := 0; i < 3; i++ {
		if code, body = fetchBody(t, c1, origin.URL); code != 200 || !strings.Contains(body, "remote=127.0.0.1:") {
			t.Fatalf("reuse bind 127.0.0.1: code=%d body=%q", code, body)
		}
		if code, body = fetchBody(t, c2, origin.URL); code != 200 || !strings.Contains(body, "remote=127.0.0.2:") {
			t.Fatalf("reuse bind 127.0.0.2: code=%d body=%q", code, body)
		}
	}
	if got := atomic.LoadInt64(conns); got != 2 {
		t.Fatalf("pooling regressed: expected still 2 upstream conns, got %d", got)
	}
}

// TestAuthScopedToBindIP proves credentials for one bind IP are rejected on
// another.
func TestAuthScopedToBindIP(t *testing.T) {
	origin, _ := startOrigin(t)
	port := startProxy(t)

	// user1 is only assigned to 127.0.0.1; via 127.0.0.2 it must be 403.
	cWrong := proxyClient(t, fmt.Sprintf("http://user1:pass1@127.0.0.2:%d", port))
	code, _ := fetchBody(t, cWrong, origin.URL)
	if code != http.StatusForbidden {
		t.Fatalf("expected 403 for user1 on 127.0.0.2, got %d", code)
	}

	// No credentials at all → 407.
	cNone := proxyClient(t, fmt.Sprintf("http://127.0.0.1:%d", port))
	code, _ = fetchBody(t, cNone, origin.URL)
	if code != http.StatusProxyAuthRequired {
		t.Fatalf("expected 407 without credentials, got %d", code)
	}
}

// TestClientKeepAlive sends two plain HTTP proxy requests over ONE client
// connection and verifies both succeed and only one upstream connection is
// used (client keep-alive + upstream pooling together).
func TestClientKeepAlive(t *testing.T) {
	origin, conns := startOrigin(t)
	port := startProxy(t)

	conn, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()
	br := bufio.NewReader(conn)
	authHdr := "Basic " + base64.StdEncoding.EncodeToString([]byte("user1:pass1"))

	for i := 0; i < 2; i++ {
		req := fmt.Sprintf("GET %s/ HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n",
			origin.URL, strings.TrimPrefix(origin.URL, "http://"), authHdr)
		if _, err := conn.Write([]byte(req)); err != nil {
			t.Fatalf("write req %d: %v", i, err)
		}
		resp, err := http.ReadResponse(br, nil)
		if err != nil {
			t.Fatalf("read resp %d (keep-alive broken?): %v", i, err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 || !strings.Contains(string(body), "remote=127.0.0.1:") {
			t.Fatalf("resp %d: code=%d body=%q", i, resp.StatusCode, body)
		}
		if resp.Close {
			t.Fatalf("resp %d asked client to close; keep-alive regressed", i)
		}
	}
	if got := atomic.LoadInt64(conns); got != 1 {
		t.Fatalf("expected 1 upstream conn for 2 keep-alive requests, got %d", got)
	}
}

// TestStalePooledConnRetry pools a conn, kills it server-side, then proves
// the next request still succeeds via a fresh dial.
func TestStalePooledConnRetry(t *testing.T) {
	origin, _ := startOrigin(t)
	port := startProxy(t)

	c := proxyClient(t, fmt.Sprintf("http://user1:pass1@127.0.0.1:%d", port))
	if code, _ := fetchBody(t, c, origin.URL); code != 200 {
		t.Fatalf("first request failed: %d", code)
	}
	origin.CloseClientConnections() // pooled upstream conn is now dead
	time.Sleep(50 * time.Millisecond)
	if code, body := fetchBody(t, c, origin.URL); code != 200 {
		t.Fatalf("request after stale pooled conn failed: code=%d body=%q", code, body)
	}
}

// TestSOCKS5ConnectAndAuth drives a raw SOCKS5 handshake (RFC 1928/1929)
// through the proxy and fetches from the origin over the tunnel, checking
// the egress bind IP. Also verifies bad credentials are rejected.
func TestSOCKS5ConnectAndAuth(t *testing.T) {
	origin, _ := startOrigin(t)
	port := startProxy(t)
	originHost, originPortStr, _ := net.SplitHostPort(strings.TrimPrefix(origin.URL, "http://"))
	originPort, _ := strconv.Atoi(originPortStr)

	body := socks5Fetch(t, fmt.Sprintf("127.0.0.2:%d", port), "user2", "pass2", originHost, originPort, true)
	if !strings.Contains(body, "remote=127.0.0.2:") {
		t.Fatalf("SOCKS5 egress not bound to 127.0.0.2: %q", body)
	}

	// Wrong credentials for this bind IP must fail the subnegotiation.
	socks5Fetch(t, fmt.Sprintf("127.0.0.2:%d", port), "user1", "pass1", originHost, originPort, false)
}

func socks5Fetch(t *testing.T, proxyAddr, user, pass, dstHost string, dstPort int, wantOK bool) string {
	t.Helper()
	conn, err := net.DialTimeout("tcp", proxyAddr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial socks5: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Greeting: version 5, one method (user/pass).
	mustWrite(t, conn, []byte{0x05, 0x01, 0x02})
	reply := mustRead(t, conn, 2)
	if reply[0] != 0x05 || reply[1] != 0x02 {
		t.Fatalf("unexpected method selection: %v", reply)
	}

	// RFC 1929 subnegotiation.
	msg := []byte{0x01, byte(len(user))}
	msg = append(msg, user...)
	msg = append(msg, byte(len(pass)))
	msg = append(msg, pass...)
	mustWrite(t, conn, msg)
	authReply := mustRead(t, conn, 2)
	if !wantOK {
		if authReply[1] == 0x00 {
			t.Fatal("SOCKS5 auth should have failed for wrong bind IP credentials")
		}
		return ""
	}
	if authReply[1] != 0x00 {
		t.Fatalf("SOCKS5 auth failed: %v", authReply)
	}

	// CONNECT to origin (IPv4 form).
	ip := net.ParseIP(dstHost).To4()
	req := []byte{0x05, 0x01, 0x00, 0x01}
	req = append(req, ip...)
	var pbuf [2]byte
	binary.BigEndian.PutUint16(pbuf[:], uint16(dstPort))
	req = append(req, pbuf[:]...)
	mustWrite(t, conn, req)
	connReply := mustRead(t, conn, 10)
	if connReply[1] != 0x00 {
		t.Fatalf("SOCKS5 connect failed: %v", connReply)
	}

	mustWrite(t, conn, []byte(fmt.Sprintf("GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", dstHost)))
	data, err := io.ReadAll(conn)
	if err != nil {
		t.Fatalf("read tunnel: %v", err)
	}
	if !strings.Contains(string(data), "200 OK") {
		t.Fatalf("tunnel fetch failed: %q", data)
	}
	return string(data)
}

func mustWrite(t *testing.T, conn net.Conn, b []byte) {
	t.Helper()
	if _, err := conn.Write(b); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func mustRead(t *testing.T, conn net.Conn, n int) []byte {
	t.Helper()
	buf := make([]byte, n)
	if _, err := io.ReadFull(conn, buf); err != nil {
		t.Fatalf("read %d bytes: %v", n, err)
	}
	return buf
}

// TestConnectTunnel exercises the HTTP CONNECT path end to end.
func TestConnectTunnel(t *testing.T) {
	origin, _ := startOrigin(t)
	port := startProxy(t)

	conn, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	hostport := strings.TrimPrefix(origin.URL, "http://")
	authHdr := "Basic " + base64.StdEncoding.EncodeToString([]byte("user1:pass1"))
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", hostport, hostport, authHdr)
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("CONNECT failed: err=%v resp=%v", err, resp)
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", hostport)
	data, _ := io.ReadAll(br)
	if !strings.Contains(string(data), "remote=127.0.0.1:") {
		t.Fatalf("tunnel body missing bind IP: %q", data)
	}
}
