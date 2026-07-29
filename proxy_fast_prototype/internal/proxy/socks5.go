package proxy

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"net"
)

const (
	socks5Version          = 0x05
	socks5AuthUserPass     = 0x02
	socks5AuthNoAcceptable = 0xFF
	socks5CmdConnect       = 0x01
	socks5AddrIPv4         = 0x01
	socks5AddrDomain       = 0x03
	socks5AddrIPv6         = 0x04
	socks5UserPassVer      = 0x01
)

// handleSOCKS5 implements SOCKS5 CONNECT with mandatory username/password
// auth (RFC 1928 + 1929), scoped to the bind IP the client connected to.
// The version byte (0x05) was already consumed by the protocol sniffer.
func (s *Server) handleSOCKS5(conn net.Conn, br *bufio.Reader, bindIP string) {
	nmethods, err := br.ReadByte()
	if err != nil {
		return
	}
	methods := make([]byte, int(nmethods))
	if _, err := io.ReadFull(br, methods); err != nil {
		return
	}
	supportsUserPass := false
	for _, m := range methods {
		if m == socks5AuthUserPass {
			supportsUserPass = true
			break
		}
	}
	if !supportsUserPass {
		_, _ = conn.Write([]byte{socks5Version, socks5AuthNoAcceptable})
		return
	}
	_, _ = conn.Write([]byte{socks5Version, socks5AuthUserPass})

	// Username/password subnegotiation (RFC 1929).
	ver, err := br.ReadByte()
	if err != nil || ver != socks5UserPassVer {
		return
	}
	ulen, err := br.ReadByte()
	if err != nil {
		return
	}
	ubuf := make([]byte, int(ulen))
	if _, err := io.ReadFull(br, ubuf); err != nil {
		return
	}
	plen, err := br.ReadByte()
	if err != nil {
		return
	}
	pbuf := make([]byte, int(plen))
	if _, err := io.ReadFull(br, pbuf); err != nil {
		return
	}
	if _, allowed := s.Store.Allow(bindIP, string(ubuf), string(pbuf)); !allowed {
		_, _ = conn.Write([]byte{socks5UserPassVer, 0x01})
		return
	}
	_, _ = conn.Write([]byte{socks5UserPassVer, 0x00})

	// Request.
	hdr := make([]byte, 4)
	if _, err := io.ReadFull(br, hdr); err != nil {
		return
	}
	if hdr[0] != socks5Version || hdr[1] != socks5CmdConnect {
		_ = writeSocks5Reply(conn, 0x07) // command not supported
		return
	}
	host, port, err := readSocks5Addr(br, hdr[3])
	if err != nil {
		_ = writeSocks5Reply(conn, 0x01)
		return
	}
	target := net.JoinHostPort(host, fmt.Sprintf("%d", port))
	upstream, err := dialTCP(target, bindIP, s.DialTimeout)
	if err != nil {
		_ = writeSocks5Reply(conn, 0x05) // connection refused
		return
	}
	if err := writeSocks5Reply(conn, 0x00); err != nil {
		upstream.Close()
		return
	}
	// Bytes the client pipelined behind the request belong to the tunnel.
	if n := br.Buffered(); n > 0 {
		pending, _ := br.Peek(n)
		if _, err := upstream.Write(pending); err != nil {
			upstream.Close()
			return
		}
		_, _ = br.Discard(n)
	}
	splice(conn, upstream, s.IdleTimeout)
}

func readSocks5Addr(br *bufio.Reader, atyp byte) (host string, port uint16, err error) {
	switch atyp {
	case socks5AddrIPv4:
		addr := make([]byte, 4)
		if _, err = io.ReadFull(br, addr); err != nil {
			return
		}
		host = net.IP(addr).String()
	case socks5AddrDomain:
		l, e := br.ReadByte()
		if e != nil {
			err = e
			return
		}
		domain := make([]byte, int(l))
		if _, err = io.ReadFull(br, domain); err != nil {
			return
		}
		host = string(domain)
	case socks5AddrIPv6:
		addr := make([]byte, 16)
		if _, err = io.ReadFull(br, addr); err != nil {
			return
		}
		host = net.IP(addr).String()
	default:
		err = fmt.Errorf("unsupported atyp %d", atyp)
		return
	}
	var pbuf [2]byte
	if _, err = io.ReadFull(br, pbuf[:]); err != nil {
		return
	}
	port = binary.BigEndian.Uint16(pbuf[:])
	return
}

func writeSocks5Reply(conn net.Conn, rep byte) error {
	// VER REP RSV ATYP BND.ADDR BND.PORT — 0.0.0.0:0
	_, err := conn.Write([]byte{socks5Version, rep, 0x00, socks5AddrIPv4, 0, 0, 0, 0, 0, 0})
	return err
}
