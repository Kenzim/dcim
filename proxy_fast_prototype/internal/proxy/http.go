package proxy

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// handleHTTP serves proxied HTTP requests on conn with client-facing
// keep-alive: many requests per client connection. CONNECT tunnels take the
// connection over via splice. Plain (absolute-URI) requests are forwarded
// over pooled upstream connections keyed by (bindIP, destination).
func (s *Server) handleHTTP(conn net.Conn, br *bufio.Reader, bindIP string) {
	bw := bufio.NewWriterSize(conn, 4*1024)
	for {
		if s.IdleTimeout > 0 {
			_ = conn.SetReadDeadline(time.Now().Add(s.IdleTimeout))
		}
		req, err := http.ReadRequest(br)
		if err != nil {
			return
		}
		if !s.serveOneHTTPRequest(conn, bw, br, req, bindIP) {
			return
		}
	}
}

// serveOneHTTPRequest handles one proxied request; returns true when the
// client connection should stay open for another request (keep-alive).
func (s *Server) serveOneHTTPRequest(conn net.Conn, bw *bufio.Writer, br *bufio.Reader, req *http.Request, bindIP string) bool {
	defer req.Body.Close()

	username, password, ok := parseProxyBasicAuth(req)
	if !ok {
		writeRaw(bw, "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"proxy\"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
		return false
	}
	if _, allowed := s.Store.Allow(bindIP, username, password); !allowed {
		writeHTTPStatus(bw, http.StatusForbidden, "Forbidden")
		return false
	}

	if req.Method == http.MethodConnect {
		return s.handleConnect(conn, bw, br, req, bindIP)
	}

	if req.URL == nil || req.URL.Host == "" {
		writeHTTPStatus(bw, http.StatusBadRequest, "Bad Request")
		return false
	}
	addr := canonicalAddr(req.URL)
	clientClose := req.Close

	// Prepare the outbound request: strip proxy/hop headers and force
	// upstream keep-alive regardless of what the client asked for; the
	// client-facing and upstream connection lifetimes are independent.
	req.RequestURI = ""
	req.Close = false
	req.Header.Del("Proxy-Authorization")
	req.Header.Del("Proxy-Connection")
	req.Header.Del("Connection")

	// Only retry a failed reused conn when the request body cannot have
	// been partially consumed by the first attempt.
	retryable := req.ContentLength == 0 && len(req.TransferEncoding) == 0

	var resp *http.Response
	var pc *pooledConn
	for attempt := 0; ; attempt++ {
		reused := false
		if attempt == 0 {
			if pc = s.pool.get(bindIP, addr); pc != nil {
				reused = true
			}
		}
		if pc == nil {
			c, err := dialTCP(addr, bindIP, s.DialTimeout)
			if err != nil {
				writeHTTPStatus(bw, http.StatusBadGateway, "Bad Gateway")
				return false
			}
			pc = newPooledConn(c)
		}
		var err error
		resp, err = roundTrip(pc, req, s.IdleTimeout)
		if err == nil {
			break
		}
		pc.Close()
		pc = nil
		if reused && retryable {
			continue // one fresh-dial retry after a stale pooled conn
		}
		writeHTTPStatus(bw, http.StatusBadGateway, "Bad Gateway")
		return false
	}

	upstreamClose := resp.Close
	// A body with neither Content-Length nor chunked encoding is delimited
	// by connection close: neither side of it can be kept alive.
	closeDelimited := resp.ContentLength < 0 && len(resp.TransferEncoding) == 0 &&
		req.Method != http.MethodHead && bodyAllowedForStatus(resp.StatusCode)

	resp.Header.Del("Connection")
	resp.Close = clientClose || closeDelimited

	if s.IdleTimeout > 0 {
		_ = conn.SetWriteDeadline(time.Now().Add(s.IdleTimeout))
	}
	werr := resp.Write(bw)
	if werr == nil {
		werr = bw.Flush()
	}
	_ = resp.Body.Close()

	if werr == nil && !upstreamClose && !closeDelimited {
		s.pool.put(bindIP, addr, pc)
	} else {
		pc.Close()
	}
	return werr == nil && !clientClose && !closeDelimited
}

// roundTrip writes req on the upstream conn and reads the response header.
func roundTrip(pc *pooledConn, req *http.Request, ioTimeout time.Duration) (*http.Response, error) {
	if ioTimeout > 0 {
		_ = pc.c.SetDeadline(time.Now().Add(ioTimeout))
	}
	if err := req.Write(pc.c); err != nil {
		return nil, err
	}
	return http.ReadResponse(pc.br, req)
}

func (s *Server) handleConnect(conn net.Conn, bw *bufio.Writer, br *bufio.Reader, req *http.Request, bindIP string) bool {
	host := req.Host
	if host == "" {
		writeHTTPStatus(bw, http.StatusBadRequest, "Bad Request")
		return false
	}
	if !strings.Contains(host, ":") {
		host = net.JoinHostPort(host, "443")
	}
	upstream, err := dialTCP(host, bindIP, s.DialTimeout)
	if err != nil {
		writeHTTPStatus(bw, http.StatusBadGateway, "Bad Gateway")
		return false
	}
	_ = conn.SetDeadline(time.Time{})
	writeRaw(bw, "HTTP/1.1 200 Connection Established\r\n\r\n")
	// Bytes the client pipelined behind the CONNECT belong to the tunnel.
	if n := br.Buffered(); n > 0 {
		pending, _ := br.Peek(n)
		if _, err := upstream.Write(pending); err != nil {
			upstream.Close()
			return false
		}
		_, _ = br.Discard(n)
	}
	splice(conn, upstream, s.IdleTimeout)
	return false
}

func canonicalAddr(u *url.URL) string {
	host := u.Host
	if !strings.Contains(host, ":") {
		if u.Scheme == "https" {
			return net.JoinHostPort(host, "443")
		}
		return net.JoinHostPort(host, "80")
	}
	return host
}

func bodyAllowedForStatus(status int) bool {
	switch {
	case status >= 100 && status <= 199:
		return false
	case status == http.StatusNoContent, status == http.StatusNotModified:
		return false
	}
	return true
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

func writeHTTPStatus(bw *bufio.Writer, code int, text string) {
	writeRaw(bw, fmt.Sprintf("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
		code, http.StatusText(code), len(text), text))
}

func writeRaw(bw *bufio.Writer, s string) {
	_, _ = bw.WriteString(s)
	_ = bw.Flush()
}
