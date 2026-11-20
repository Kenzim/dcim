package dhcp

import (
	"context"
	"net/netip"
	"regexp"
	"strconv"

	"rackflow/storage_runner/internal/execx"
)

// ARPProbe runs duplicate address detection from inside a namespace:
// `arping -D -c 2 -w 1 -I <iface> <ip>`.
type ARPProbe struct {
	Cmd   execx.Commander
	Netns string
	Iface string
}

var receivedRe = regexp.MustCompile(`Received (\d+) response`)

// Probe reports whether ip answered. arping exits non-zero both on a reply
// and on a failure, so its summary line decides.
func (p ARPProbe) Probe(ctx context.Context, ip netip.Addr) (bool, error) {
	out, err := p.Cmd.Run(ctx, nil, "ip", "netns", "exec", p.Netns,
		"arping", "-D", "-c", "2", "-w", "1", "-I", p.Iface, ip.String())
	if m := receivedRe.FindSubmatch(out); m != nil {
		n, _ := strconv.Atoi(string(m[1]))
		return n > 0, nil
	}
	return false, err
}
