package proxy

import (
	"bufio"
	"log"
	"net"
	"sync"
	"time"

	"rackflow/proxy_runner/internal/auth"
)

type Server struct {
	Listen      string
	Store       *auth.Store
	DialTimeout time.Duration
	IdleTimeout time.Duration

	ln net.Listener
	wg sync.WaitGroup
}

func (s *Server) Start() error {
	if s.DialTimeout <= 0 {
		s.DialTimeout = 15 * time.Second
	}
	if s.IdleTimeout <= 0 {
		s.IdleTimeout = 5 * time.Minute
	}
	ln, err := net.Listen("tcp", s.Listen)
	if err != nil {
		return err
	}
	s.ln = ln
	s.wg.Add(1)
	go s.acceptLoop()
	log.Printf("proxy listening on %s (HTTP + SOCKS5)", s.Listen)
	return nil
}

func (s *Server) Close() error {
	if s.ln != nil {
		_ = s.ln.Close()
	}
	s.wg.Wait()
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
	defer discard(conn)
	_ = conn.SetDeadline(time.Now().Add(s.DialTimeout + 30*time.Second))

	bindIP := localIP(conn)
	br := bufio.NewReader(conn)
	first, err := br.ReadByte()
	if err != nil {
		return
	}
	// Clear the accept-phase deadline; splice sets idle deadlines.
	_ = conn.SetDeadline(time.Time{})

	if first == socks5Version {
		handleSOCKS5(conn, br, first, bindIP, s.Store, s.DialTimeout, s.IdleTimeout)
		return
	}
	handleHTTP(conn, br, first, bindIP, s.Store, s.DialTimeout, s.IdleTimeout)
}

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
