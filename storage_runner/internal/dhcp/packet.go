// Package dhcp is the runner's DHCPv4 server for private networks
// (docs/private-network-dhcp.md section 3). Packet parsing, the decision
// and the reply are plain functions over a Server's state; the socket
// inside a namespace is opened separately (socket_linux.go).
package dhcp

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
)

// Message types (option 53).
const (
	Discover byte = 1
	Offer    byte = 2
	Request  byte = 3
	Decline  byte = 4
	Ack      byte = 5
	Nak      byte = 6
	Release  byte = 7
	Inform   byte = 8
)

// Option codes the server reads or sends. No other option is ever sent.
const (
	OptPad          byte = 0
	OptSubnetMask   byte = 1
	OptRouter       byte = 3
	OptDNS          byte = 6
	OptHostname     byte = 12
	OptDomain       byte = 15
	OptMTU          byte = 26
	OptNTP          byte = 42
	OptRequestedIP  byte = 50
	OptLeaseTime    byte = 51
	OptMessageType  byte = 53
	OptServerID     byte = 54
	OptRenewalT1    byte = 58
	OptRebindingT2  byte = 59
	OptClasslessRts byte = 121
	OptEnd          byte = 255
)

const (
	headerLen = 236
	// minReplyLen pads replies to the BOOTP minimum some clients insist on.
	minReplyLen = 300
)

var magicCookie = [4]byte{99, 130, 83, 99}

// Packet is a parsed BOOTP/DHCP message. Options holds each option's data;
// an option that appears more than once is concatenated (RFC 3396).
type Packet struct {
	Op     byte
	HType  byte
	HLen   byte
	Hops   byte
	XID    uint32
	Secs   uint16
	Flags  uint16
	CIAddr netip.Addr
	YIAddr netip.Addr
	SIAddr netip.Addr
	GIAddr netip.Addr
	CHAddr [16]byte

	Options map[byte][]byte
}

// Option is one option of a reply, in the order it is written.
type Option struct {
	Code byte
	Data []byte
}

var errShort = errors.New("dhcp: packet too short")

// Parse reads a DHCP message. It rejects anything without the magic cookie.
func Parse(b []byte) (*Packet, error) {
	if len(b) < headerLen+4 {
		return nil, errShort
	}
	p := &Packet{
		Op: b[0], HType: b[1], HLen: b[2], Hops: b[3],
		XID:     binary.BigEndian.Uint32(b[4:8]),
		Secs:    binary.BigEndian.Uint16(b[8:10]),
		Flags:   binary.BigEndian.Uint16(b[10:12]),
		CIAddr:  addr4(b[12:16]),
		YIAddr:  addr4(b[16:20]),
		SIAddr:  addr4(b[20:24]),
		GIAddr:  addr4(b[24:28]),
		Options: map[byte][]byte{},
	}
	copy(p.CHAddr[:], b[28:44])
	if [4]byte(b[headerLen:headerLen+4]) != magicCookie {
		return nil, errors.New("dhcp: no magic cookie")
	}
	opts := b[headerLen+4:]
	for i := 0; i < len(opts); {
		code := opts[i]
		if code == OptPad {
			i++
			continue
		}
		if code == OptEnd {
			break
		}
		if i+1 >= len(opts) {
			return nil, fmt.Errorf("dhcp: option %d truncated", code)
		}
		n := int(opts[i+1])
		if i+2+n > len(opts) {
			return nil, fmt.Errorf("dhcp: option %d truncated", code)
		}
		p.Options[code] = append(p.Options[code], opts[i+2:i+2+n]...)
		i += 2 + n
	}
	return p, nil
}

func addr4(b []byte) netip.Addr { return netip.AddrFrom4([4]byte(b)) }

// MessageType is option 53, or 0 when absent (plain BOOTP).
func (p *Packet) MessageType() byte {
	if v := p.Options[OptMessageType]; len(v) == 1 {
		return v[0]
	}
	return 0
}

// MAC is the client hardware address in lower case, colon separated.
func (p *Packet) MAC() string { return net.HardwareAddr(p.CHAddr[:6]).String() }

// AddrOption reads a four-byte address option; invalid when absent.
func (p *Packet) AddrOption(code byte) netip.Addr {
	if v := p.Options[code]; len(v) == 4 {
		return addr4(v)
	}
	return netip.Addr{}
}

// Marshal writes a reply: the fixed header, the magic cookie, the options
// in order (split into 255-byte pieces when longer) and the end option.
func Marshal(p *Packet, opts []Option) []byte {
	b := make([]byte, headerLen, minReplyLen)
	b[0], b[1], b[2], b[3] = p.Op, p.HType, p.HLen, p.Hops
	binary.BigEndian.PutUint32(b[4:8], p.XID)
	binary.BigEndian.PutUint16(b[8:10], p.Secs)
	binary.BigEndian.PutUint16(b[10:12], p.Flags)
	put4(b[12:16], p.CIAddr)
	put4(b[16:20], p.YIAddr)
	put4(b[20:24], p.SIAddr)
	put4(b[24:28], p.GIAddr)
	copy(b[28:44], p.CHAddr[:])
	b = append(b, magicCookie[:]...)
	for _, o := range opts {
		data := o.Data
		for first := true; first || len(data) > 0; first = false {
			n := min(len(data), 255)
			b = append(b, o.Code, byte(n))
			b = append(b, data[:n]...)
			data = data[n:]
		}
	}
	b = append(b, OptEnd)
	for len(b) < minReplyLen {
		b = append(b, OptPad)
	}
	return b
}

func put4(dst []byte, a netip.Addr) {
	if a.Is4() {
		v := a.As4()
		copy(dst, v[:])
	}
}
