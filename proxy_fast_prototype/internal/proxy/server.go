package proxy

import (
	"bufio"
	"log"
	"net"
	"sync"
	"time"
)

// Server is a dual-protocol (HTTP proxy + SOCKS5) forwarder on one port. The
// first byte of each connection is sniffed: 0x05 → SOCKS5, else HTTP.
type Server struct {
	Listen      string
	Store       authStore
	DialTimeout time.Duration
	IdleTimeout time.Duration
	// PoolMaxIdlePerKey caps idle upstream conns per (bindIP, dest) key.
	PoolMaxIdlePerKey int
	// PoolIdleTimeout is how long an idle upstream conn may be reused.
	PoolIdleTimeout time.Duration

	pool *connPool
	ln   net.Listener
	wg   sync.WaitGroup
}

// authStore is the subset of auth.Store the server needs.
type authStore interface {
	Allow(bindIP, username, password string) (serviceID int, ok bool)
}

func (s *Server) Start() error {
	if s.DialTimeout <= 0 {
		s.DialTimeout = 15 * time.Second
	}
	if s.IdleTimeout <= 0 {
		s.IdleTimeout = 5 * time.Minute
	}
	s.pool = newConnPool(s.PoolMaxIdlePerKey, s.PoolIdleTimeout)
	ln, err := net.Listen("tcp", s.Listen)
	if err != nil {
		return err
	}
	s.ln = ln
	s.wg.Add(1)
	go s.acceptLoop()
	log.Printf("proxy-fast listening on %s (HTTP + SOCKS5)", ln.Addr())
	return nil
}

func (s *Server) Addr() net.Addr {
	if s.ln == nil {
		return nil
	}
	return s.ln.Addr()
}

func (s *Server) Close() error {
	if s.ln != nil {
		_ = s.ln.Close()
	}
	s.wg.Wait()
	if s.pool != nil {
		s.pool.Close()
	}
	return nil
}

func (s *Server) acceptLoop() {
	defer s.wg.Done()
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.wg.Add(1)
		go func(c net.Conn) {
			defer s.wg.Done()
			s.handleConn(c)
		}(conn)
	}
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(s.DialTimeout + 30*time.Second))

	bindIP := localIP(conn)
	br := bufio.NewReaderSize(conn, 8*1024)
	first, err := br.ReadByte()
	if err != nil {
		return
	}
	_ = br.UnreadByte()
	// Clear the accept-phase deadline; handlers set per-op deadlines.
	_ = conn.SetDeadline(time.Time{})

	if first == socks5Version {
		_, _ = br.ReadByte() // SOCKS5 handler expects the version consumed
		s.handleSOCKS5(conn, br, bindIP)
		return
	}
	s.handleHTTP(conn, br, bindIP)
}

// localIP is the IP the client connected to — the customer's bind IP, which
// scopes both auth and the outbound egress binding.
func localIP(conn net.Conn) string {
	addr := conn.LocalAddr()
	if addr == nil {
		return ""
	}
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return addr.String()
	}
	return host
}
