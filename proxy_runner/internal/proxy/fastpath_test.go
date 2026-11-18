package proxy

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"testing"
	"time"

	"rackflow/proxy_runner/internal/auth"
)

// sourceTarget accepts HTTP requests and answers with the source IP it saw.
func sourceTarget(t *testing.T) (addr string, closeFn func()) {
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
			go func(c net.Conn) {
				defer c.Close()
				if _, err := http.ReadRequest(bufio.NewReader(c)); err != nil {
					return
				}
				host, _, _ := net.SplitHostPort(c.RemoteAddr().String())
				fmt.Fprintf(c, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(host), host)
			}(c)
		}
	}()
	return ln.Addr().String(), func() { _ = ln.Close() }
}

// proxyGet sends a plain proxied GET and returns the status and body.
func proxyGet(t *testing.T, proxyAddr, target, user, pass, xBindIP string) (int, string) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	header := ""
	if xBindIP != "" {
		header = "X-Bind-IP: " + xBindIP + "\r\n"
	}
	creds := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
	fmt.Fprintf(conn, "GET http://%s/ HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Basic %s\r\n%sConnection: close\r\n\r\n",
		target, target, creds, header)
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := make([]byte, 64)
	n, _ := resp.Body.Read(buf)
	return resp.StatusCode, string(buf[:n])
}

func TestPublicXBindIPOnlySelectsOwnedIPs(t *testing.T) {
	target, closeTarget := sourceTarget(t)
	defer closeTarget()
	store := auth.NewStore()
	store.Replace("v1", []auth.Entry{
		{BindIP: "127.0.0.1", Username: "u", Password: "p", ServiceID: 1},
		{BindIP: "127.0.0.3", Username: "u", Password: "p", ServiceID: 2},
		{BindIP: "127.0.0.4", Username: "other", Password: "x", ServiceID: 3},
	})
	addr, closeProxy := startProxy(t, store)
	defer closeProxy()

	if code, body := proxyGet(t, addr, target, "u", "p", ""); code != 200 || body != "127.0.0.1" {
		t.Fatalf("no header: %d %q", code, body)
	}
	if code, body := proxyGet(t, addr, target, "u", "p", "127.0.0.3"); code != 200 || body != "127.0.0.3" {
		t.Fatalf("owned header IP: %d %q", code, body)
	}
	for _, header := range []string{"127.0.0.4", "127.0.0.5"} {
		if code, _ := proxyGet(t, addr, target, "u", "p", header); code != http.StatusForbidden {
			t.Fatalf("X-Bind-IP %s: expected 403, got %d", header, code)
		}
	}
}

// startTenantProxy serves a tenant on a listener of its own, as the fast path
// does inside a namespace; 127.0.0.6 stands in for the subnet's private IP.
func startTenantProxy(t *testing.T, store *auth.TenantStore) (addr string, closeFn func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.6:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{
		Listener:    ln,
		Auth:        TenantAuth{Store: func() *auth.TenantStore { return store }},
		DialTimeout: 3 * time.Second,
		IdleTimeout: 5 * time.Second,
	}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	return ln.Addr().String(), func() { _ = srv.Close() }
}

func TestFastPathEgressFollowsCredentials(t *testing.T) {
	target, closeTarget := sourceTarget(t)
	defer closeTarget()
	tenant := auth.NewTenantStore([]auth.TenantEntry{
		{PrivateIP: "127.0.0.6", BindIP: "127.0.0.7", Username: "alice", Password: "pa", ServiceID: 1},
		{PrivateIP: "127.0.0.6", BindIP: "127.0.0.8", Username: "alice2", Password: "pb", ServiceID: 2},
		{PrivateIP: "127.0.0.6", BindIP: "127.0.0.9", Username: "alice2", Password: "pb", ServiceID: 3},
	})
	addr, closeProxy := startTenantProxy(t, tenant)
	defer closeProxy()

	if code, body := proxyGet(t, addr, target, "alice", "pa", ""); code != 200 || body != "127.0.0.7" {
		t.Fatalf("alice: %d %q", code, body)
	}
	if code, body := proxyGet(t, addr, target, "alice2", "pb", ""); code != 200 || body != "127.0.0.8" {
		t.Fatalf("alice2 default: %d %q", code, body)
	}
	if code, body := proxyGet(t, addr, target, "alice2", "pb", "127.0.0.9"); code != 200 || body != "127.0.0.9" {
		t.Fatalf("alice2 header: %d %q", code, body)
	}
	if code, _ := proxyGet(t, addr, target, "alice2", "pb", "127.0.0.7"); code != http.StatusForbidden {
		t.Fatalf("header for an IP the credentials do not own: %d", code)
	}
	if code, _ := proxyGet(t, addr, target, "alice", "wrong", ""); code != http.StatusForbidden {
		t.Fatalf("wrong password: %d", code)
	}
}

func TestFastPathSOCKS5EgressFollowsCredentials(t *testing.T) {
	target, closeTarget := sourceTarget(t)
	defer closeTarget()
	tenant := auth.NewTenantStore([]auth.TenantEntry{
		{PrivateIP: "127.0.0.6", BindIP: "127.0.0.7", Username: "su", Password: "sp", ServiceID: 1},
	})
	addr, closeProxy := startTenantProxy(t, tenant)
	defer closeProxy()

	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	host, portStr, _ := net.SplitHostPort(target)
	var port int
	fmt.Sscanf(portStr, "%d", &port)
	conn.Write([]byte{0x05, 0x01, 0x02})
	reply := make([]byte, 2)
	if _, err := conn.Read(reply); err != nil || reply[1] != 0x02 {
		t.Fatalf("method reply %v %v", reply, err)
	}
	conn.Write(append(append([]byte{0x01, 2}, "su"...), append([]byte{2}, "sp"...)...))
	if _, err := conn.Read(reply); err != nil || reply[1] != 0x00 {
		t.Fatalf("auth reply %v %v", reply, err)
	}
	ip := net.ParseIP(host).To4()
	conn.Write([]byte{0x05, 0x01, 0x00, 0x01, ip[0], ip[1], ip[2], ip[3], byte(port >> 8), byte(port)})
	head := make([]byte, 10)
	if _, err := conn.Read(head); err != nil || head[1] != 0x00 {
		t.Fatalf("connect reply %v %v", head, err)
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 32)
	n, _ := resp.Body.Read(buf)
	if string(buf[:n]) != "127.0.0.7" {
		t.Fatalf("egress from %q, want 127.0.0.7", buf[:n])
	}
}
