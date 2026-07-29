package proxy

import (
	"errors"
	"io"
	"net"
	"os"
	"syscall"
	"time"
)

// ErrDestinationBlocked is returned when a dial target resolves to an
// address forbidden by the destination policy (loopback, link-local,
// private/ULA ranges, cloud metadata, etc). Without this check an
// authenticated proxy customer could pivot into internal infrastructure
// reachable from the proxy-runner host/network namespace ("SSRF as a
// service"), since this proxy otherwise dials whatever host:port the client
// requests with no destination filtering at all.
var ErrDestinationBlocked = errors.New("proxy: destination address is not allowed")

// allowPrivateDestinations lets an operator explicitly opt out of the
// destination policy for trusted, fully isolated lab/test deployments. Off
// (secure) by default.
func allowPrivateDestinations() bool {
	return os.Getenv("PROXY_ALLOW_PRIVATE_DESTINATIONS") == "true"
}

// isBlockedDestination reports whether ip must never be reachable through
// the proxy: loopback, link-local (incl. IPv6 link-local -- this also
// covers the 169.254.169.254 cloud metadata address, which lives in the
// 169.254.0.0/16 link-local block), unspecified, multicast, and
// private/ULA ranges (RFC1918 / fc00::/7).
func isBlockedDestination(ip net.IP) bool {
	if ip == nil {
		return true
	}
	if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified() || ip.IsMulticast() {
		return true
	}
	return ip.IsPrivate()
}

func splice(a, b net.Conn, idle time.Duration) {
	done := make(chan struct{}, 2)
	copySide := func(dst, src net.Conn) {
		defer func() { done <- struct{}{} }()
		buf := make([]byte, 32*1024)
		for {
			if idle > 0 {
				_ = src.SetReadDeadline(time.Now().Add(idle))
			}
			n, err := src.Read(buf)
			if n > 0 {
				if idle > 0 {
					_ = dst.SetWriteDeadline(time.Now().Add(idle))
				}
				if _, werr := dst.Write(buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				return
			}
		}
	}
	go copySide(a, b)
	go copySide(b, a)
	<-done
	_ = a.Close()
	_ = b.Close()
	<-done
}

// dialTCP dials addr, binding the outbound socket to bindIP when set so
// egress traffic actually leaves via the customer's assigned IP instead of
// the host's default route — the entire point of a bind-IP proxy. If bindIP
// is empty or unparsable, falls back to the OS's normal source selection.
//
// A Control hook rejects the dial if the destination address is in a
// blocked range (see isBlockedDestination). Control runs after DNS
// resolution but immediately before the OS-level connect() call, so a
// domain name is checked using the actual IP it resolved to -- closing the
// TOCTOU window a naive "resolve, check, dial separately" approach would
// leave open to DNS-rebinding.
func dialTCP(addr, bindIP string, timeout time.Duration) (net.Conn, error) {
	allowPrivate := allowPrivateDestinations()
	d := net.Dialer{
		Timeout: timeout,
		Control: func(_, address string, c syscall.RawConn) error {
			if allowPrivate {
				return nil
			}
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return err
			}
			ip := net.ParseIP(host)
			if isBlockedDestination(ip) {
				return ErrDestinationBlocked
			}
			return nil
		},
	}
	if bindIP != "" {
		if ip := net.ParseIP(bindIP); ip != nil {
			d.LocalAddr = &net.TCPAddr{IP: ip}
		}
	}
	return d.Dial("tcp", addr)
}

// discard closes and drains a bit so the peer sees FIN promptly.
func discard(c net.Conn) {
	_ = c.Close()
}

var _ = io.Copy
