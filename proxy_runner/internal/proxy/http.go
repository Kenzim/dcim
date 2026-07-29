package proxy

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"rackflow/proxy_runner/internal/auth"
)

// handleHTTP serves one or more proxied HTTP requests on conn. Plain
// (non-CONNECT) requests support keep-alive so a client sending many
// requests over one connection doesn't pay a fresh TCP handshake + fresh
// upstream dial per request; CONNECT tunnels (used for all HTTPS traffic)
// take the connection over completely via splice, as before.
func handleHTTP(conn net.Conn, br *bufio.Reader, first byte, bindIP string, store *auth.Store, dialTimeout, idleTimeout time.Duration) {
	reader := bufio.NewReader(io.MultiReader(strings.NewReader(string([]byte{first})), br))
	for {
		if idleTimeout > 0 {
			_ = conn.SetReadDeadline(time.Now().Add(idleTimeout))
		}
		req, err := http.ReadRequest(reader)
		if err != nil {
			return
		}
		if !serveOneHTTPRequest(conn, req, bindIP, store, dialTimeout, idleTimeout) {
			return
		}
	}
}

// serveOneHTTPRequest handles a single proxied request. It returns true if
// the connection should stay open for another request (HTTP keep-alive),
// false if the caller should stop (CONNECT tunnel, auth failure, or either
// side requesting the connection be closed).
func serveOneHTTPRequest(conn net.Conn, req *http.Request, bindIP string, store *auth.Store, dialTimeout, idleTimeout time.Duration) bool {
	defer req.Body.Close()

	username, password, ok := parseProxyBasicAuth(req)
	if !ok {
		conn.Write([]byte("HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"proxy\"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"))
		return false
	}
	effectiveBindIP := bindIP
	if hdr := req.Header.Get("X-Bind-IP"); hdr != "" {
		effectiveBindIP = hdr
	}
	if _, allowed := store.Allow(effectiveBindIP, username, password); !allowed {
		writeHTTPStatus(conn, http.StatusForbidden, "Forbidden")
		return false
	}

	if req.Method == http.MethodConnect {
		host := req.Host
		if host == "" {
			writeHTTPStatus(conn, http.StatusBadRequest, "Bad Request")
			return false
		}
		if !strings.Contains(host, ":") {
			host = net.JoinHostPort(host, "443")
		}
		upstream, err := dialTCP(host, effectiveBindIP, dialTimeout)
		if err != nil {
			writeHTTPStatus(conn, http.StatusBadGateway, "Bad Gateway")
			return false
		}
		_ = conn.SetDeadline(time.Time{})
		_, _ = conn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))
		splice(conn, upstream, idleTimeout)
		return false
	}

	// Absolute-URI form for non-CONNECT proxy requests.
	target := req.URL
	if target == nil || target.Host == "" {
		writeHTTPStatus(conn, http.StatusBadRequest, "Bad Request")
		return false
	}
	host := target.Host
	if !strings.Contains(host, ":") {
		if target.Scheme == "https" {
			host = net.JoinHostPort(host, "443")
		} else {
			host = net.JoinHostPort(host, "80")
		}
	}
	upstream, err := dialTCP(host, effectiveBindIP, dialTimeout)
	if err != nil {
		writeHTTPStatus(conn, http.StatusBadGateway, "Bad Gateway")
		return false
	}
	defer upstream.Close()

	outReq := req.Clone(req.Context())
	outReq.RequestURI = ""
	outReq.Header.Del("Proxy-Authorization")
	outReq.Header.Del("Proxy-Connection")
	if err := outReq.Write(upstream); err != nil {
		return false
	}
	resp, err := http.ReadResponse(bufio.NewReader(upstream), outReq)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if err := resp.Write(conn); err != nil {
		return false
	}
	return !req.Close && !resp.Close
}

func parseProxyBasicAuth(req *http.Request) (user, pass string, ok bool) {
	h := req.Header.Get("Proxy-Authorization")
	if h == "" {
		return "", "", false
	}
	parts := strings.SplitN(h, " ", 2)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Basic") {
		return "", "", false
	}
	decoded, err := base64.StdEncoding.DecodeString(strings.TrimSpace(parts[1]))
	if err != nil {
		return "", "", false
	}
	up := strings.SplitN(string(decoded), ":", 2)
	if len(up) != 2 {
		return "", "", false
	}
	return up[0], up[1], true
}

func writeHTTPStatus(conn net.Conn, code int, text string) {
	body := text
	msg := fmt.Sprintf("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
		code, http.StatusText(code), len(body), body)
	_, _ = conn.Write([]byte(msg))
}
