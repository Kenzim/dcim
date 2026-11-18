package proxy

import "rackflow/proxy_runner/internal/auth"

// Authorizer checks the credentials of a request that arrived on localIP and
// returns the IP to dial out from. xBindIP is the X-Bind-IP header (HTTP
// only; empty for SOCKS5 or when absent).
type Authorizer interface {
	Authorize(localIP, username, password, xBindIP string) (egressIP string, ok bool)
}

// PublicAuth is the public path: credentials are checked against the IP the
// client connected to, or the X-Bind-IP header when present, and egress uses
// that same IP. Because the check is keyed by that IP, the header can only
// select an IP the credentials own.
type PublicAuth struct {
	Store *auth.Store
}

func (p PublicAuth) Authorize(localIP, username, password, xBindIP string) (string, bool) {
	bindIP := localIP
	if xBindIP != "" {
		bindIP = xBindIP
	}
	if _, ok := p.Store.Allow(bindIP, username, password); !ok {
		return "", false
	}
	return bindIP, true
}

// TenantAuth is the fast path for one client: credentials are checked against
// that client's own store, keyed by the private IP the client connected to,
// and egress uses the matching assignment's public IP.
type TenantAuth struct {
	Store func() *auth.TenantStore
}

func (t TenantAuth) Authorize(localIP, username, password, xBindIP string) (string, bool) {
	bindIP, _, ok := t.Store().Lookup(localIP, username, password, xBindIP)
	return bindIP, ok
}
