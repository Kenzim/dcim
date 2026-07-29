package proxy

import (
	"net"
	"time"
)

// splice bidirectionally copies between two conns until either side ends,
// enforcing an idle deadline per read/write.
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

// dialTCP dials addr binding the outbound socket to bindIP so egress traffic
// leaves via the customer's assigned IP — the entire point of a bind-IP
// proxy. Falls back to normal source selection only if bindIP is unset or
// unparsable.
func dialTCP(addr, bindIP string, timeout time.Duration) (net.Conn, error) {
	d := net.Dialer{Timeout: timeout}
	if bindIP != "" {
		if ip := net.ParseIP(bindIP); ip != nil {
			d.LocalAddr = &net.TCPAddr{IP: ip}
		}
	}
	return d.Dial("tcp", addr)
}
