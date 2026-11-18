//go:build sandbox

// Sends real traffic through the runner's nftables table and a tenant
// namespace built by the real netns driver. Only run it inside a throwaway
// namespace, never on a real host:
//
//	unshare -rnm sh -c 'mount -t tmpfs none /run && go test -tags sandbox ./internal/firewall/'
//
// Needs python3 and nsenter.
package firewall

import (
	"bufio"
	"context"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/netns"
)

func run(t *testing.T, args ...string) {
	t.Helper()
	if out, err := exec.Command(args[0], args[1:]...).CombinedOutput(); err != nil {
		t.Fatalf("%v: %v\n%s", args, err, out)
	}
}

// connect opens a TCP connection from inside a namespace.
func connect(ns, ip, port string) error {
	return exec.Command("nsenter", "--net=/run/netns/"+ns, "python3", "-c",
		"import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1).close()",
		ip, port).Run()
}

const remoteListener = `
import select, socket
srv = []
for port in (6800, 22):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("10.20.0.7", port))
    s.listen()
    srv.append(s)
print("ready", flush=True)
while True:
    ready, _, _ = select.select(srv, [], [])
    for s in ready:
        c, addr = s.accept()
        print(addr[0], flush=True)
        c.close()
`

func TestSandboxTraffic(t *testing.T) {
	ctx := context.Background()
	run(t, "ip", "link", "set", "lo", "up")
	run(t, "ip", "link", "add", "trunk0", "type", "dummy")
	run(t, "ip", "link", "set", "trunk0", "up")
	// Local Ceph: an address of this host inside the Ceph public network.
	run(t, "ip", "link", "add", "ceph0", "type", "dummy")
	run(t, "ip", "addr", "add", "10.10.0.5/24", "dev", "ceph0")
	run(t, "ip", "link", "set", "ceph0", "up")
	// Remote Ceph node with no route back to LINK_CIDR: replies only work if
	// the host masquerades.
	run(t, "ip", "netns", "add", "cephremote")
	run(t, "ip", "link", "add", "rc0", "type", "veth", "peer", "name", "rc1", "netns", "cephremote")
	run(t, "ip", "addr", "add", "10.20.0.1/24", "dev", "rc0")
	run(t, "ip", "link", "set", "rc0", "up")
	run(t, "ip", "-n", "cephremote", "addr", "add", "10.20.0.7/24", "dev", "rc1")
	run(t, "ip", "-n", "cephremote", "link", "set", "rc1", "up")
	run(t, "ip", "-n", "cephremote", "link", "set", "lo", "up")
	if err := os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644); err != nil {
		t.Fatal(err)
	}

	cephNets := []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24"), netip.MustParsePrefix("10.20.0.0/24")}
	m := &netns.IPRoute{Cmd: execx.Exec{}, ParentIface: "trunk0"}
	for i, id := range []int64{12, 13} {
		base := netip.MustParseAddr("169.254.128.0")
		for j := 0; j < 4*i; j++ {
			base = base.Next()
		}
		spec := netns.Spec{
			TenantID: id,
			VLAN:     1234 + i,
			Address:  netip.MustParsePrefix("10.0.0.254/24"),
			HostAddr: netip.PrefixFrom(base.Next(), 30),
			NSAddr:   netip.PrefixFrom(base.Next().Next(), 30),
			Routes:   cephNets,
		}
		if _, err := m.Ensure(ctx, spec); err != nil {
			t.Fatal(err)
		}
	}
	for _, port := range []string{"3300", "22"} {
		ln, err := net.Listen("tcp", "10.10.0.5:"+port)
		if err != nil {
			t.Fatal(err)
		}
		defer ln.Close()
		go func() {
			for {
				c, err := ln.Accept()
				if err != nil {
					return
				}
				c.Close()
			}
		}()
	}
	hostLn, err := net.Listen("tcp", "169.254.128.1:3300")
	if err != nil {
		t.Fatal(err)
	}
	defer hostLn.Close()

	remote := exec.Command("nsenter", "--net=/run/netns/cephremote", "python3", "-c", remoteListener)
	out, err := remote.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.Start(); err != nil {
		t.Fatal(err)
	}
	defer remote.Process.Kill()
	peers := bufio.NewScanner(out)
	if !peers.Scan() || peers.Text() != "ready" {
		t.Fatalf("remote listener did not start: %q", peers.Text())
	}

	// Control: without the table the blocked cases below are reachable, so
	// the drops are the firewall's doing.
	for _, c := range [][3]string{{"rf-t12", "10.10.0.5", "22"}, {"rf-t12", "169.254.128.1", "3300"}, {"rf-t13", "10.10.0.5", "3300"}} {
		if err := connect(c[0], c[1], c[2]); err != nil {
			t.Fatalf("control %v unreachable without the firewall: %v", c, err)
		}
	}

	// Tenant 13 has a namespace but is not allowed (suspended).
	rs := Ruleset{Links: []string{"rf-h12"}, CephNets: cephNets, LinkCIDR: netip.MustParsePrefix("169.254.128.0/17")}
	if _, err := (&NFT{Cmd: execx.Exec{}}).Apply(ctx, rs); err != nil {
		t.Fatal(err)
	}

	// Local Ceph (same host): input path, Ceph ports only.
	if err := connect("rf-t12", "10.10.0.5", "3300"); err != nil {
		t.Errorf("local mon port blocked: %v", err)
	}
	if err := connect("rf-t12", "10.10.0.5", "22"); err == nil {
		t.Error("local ssh reachable from tenant")
	}
	if err := connect("rf-t12", "169.254.128.1", "3300"); err == nil {
		t.Error("host veth address reachable from tenant")
	}
	if err := connect("rf-t13", "10.10.0.5", "3300"); err == nil {
		t.Error("tenant outside the allowed set reached local Ceph")
	}

	// Remote Ceph: forwarded and masqueraded, Ceph ports only.
	if err := connect("rf-t12", "10.20.0.7", "6800"); err != nil {
		t.Fatalf("remote OSD port blocked: %v", err)
	}
	if !peers.Scan() || peers.Text() != "10.20.0.1" {
		t.Errorf("remote saw source %q, want the host address 10.20.0.1", peers.Text())
	}
	if err := connect("rf-t12", "10.20.0.7", "22"); err == nil {
		t.Error("remote ssh reachable from tenant")
	}
	if err := connect("rf-t13", "10.20.0.7", "6800"); err == nil {
		t.Error("tenant outside the allowed set reached remote Ceph")
	}

	done := make(chan string, 1)
	go func() {
		if peers.Scan() {
			done <- peers.Text()
		}
	}()
	select {
	case p := <-done:
		if !strings.HasPrefix(p, "10.") {
			t.Errorf("unexpected connection from %s", p)
		} else {
			t.Errorf("a blocked connection reached the remote node from %s", p)
		}
	case <-time.After(300 * time.Millisecond):
	}
}
