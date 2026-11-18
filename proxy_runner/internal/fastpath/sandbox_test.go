//go:build sandbox && linux && (amd64 || arm64)

package fastpath

// Kernel-level check of the fast path. It creates namespaces, a bridge and
// VLAN interfaces, so it must only run inside a throwaway network and mount
// namespace (see README, "Sandbox test"); it refuses to run otherwise.

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

const sandboxPort = 18080

func sh(t *testing.T, line string) {
	t.Helper()
	fields := strings.Fields(line)
	out, err := exec.Command(fields[0], fields[1:]...).CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v: %s", line, err, out)
	}
}

func requireSandbox(t *testing.T) {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("needs root")
	}
	if os.Getenv("PF_SANDBOX") != "1" {
		t.Skip("set PF_SANDBOX=1 and run inside unshare -nm (see README)")
	}
	out, _ := exec.Command("ip", "-o", "link", "show").Output()
	if n := strings.Count(strings.TrimSpace(string(out)), "\n") + 1; n > 1 || !strings.Contains(string(out), "lo:") {
		t.Fatalf("refusing to run: this network namespace has other interfaces than lo:\n%s", out)
	}
}

// sourceEcho answers HTTP requests with the source IP it saw.
func sourceEcho(t *testing.T, addr string) {
	t.Helper()
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				if _, err := http.ReadRequest(bufio.NewReader(c)); err != nil {
					return
				}
				host, _, _ := net.SplitHostPort(c.RemoteAddr().String())
				fmt.Fprintf(c, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(host), host)
			}(c)
		}
	}()
}

func dialFrom(ns, addr string) (net.Conn, error) {
	var conn net.Conn
	err := InNetns(ns, func() error {
		var err error
		conn, err = net.DialTimeout("tcp4", addr, time.Second)
		return err
	})
	return conn, err
}

func proxyGetFrom(t *testing.T, ns, proxyAddr, target, user, pass string) (int, string) {
	t.Helper()
	conn, err := dialFrom(ns, proxyAddr)
	if err != nil {
		t.Fatalf("%s cannot reach the proxy at %s: %v", ns, proxyAddr, err)
	}
	defer conn.Close()
	creds := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
	fmt.Fprintf(conn, "GET http://%s/ HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Basic %s\r\nConnection: close\r\n\r\n", target, target, creds)
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := make([]byte, 64)
	n, _ := resp.Body.Read(buf)
	return resp.StatusCode, string(buf[:n])
}

func TestSandboxTwoClientsSameAddresses(t *testing.T) {
	requireSandbox(t)
	t.Setenv("PROXY_ALLOW_PRIVATE_DESTINATIONS", "true")

	// The sandbox's own namespace plays the runner host: loopback holds the
	// "public" egress IPs and an echo target.
	sh(t, "ip link set lo up")
	sourceEcho(t, "127.0.0.1:19000")

	// A VLAN-filtering bridge in namespace sw plays the switch: the runner's
	// uplink is a trunk, each client's server sits on an access port.
	for _, ns := range []string{"sw", "ca", "cb"} {
		sh(t, "ip netns add "+ns)
		sh(t, "ip -n "+ns+" link set lo up")
	}
	sh(t, "ip -n sw link add br0 type bridge vlan_filtering 1")
	sh(t, "ip -n sw link set br0 up")
	sh(t, "ip link add uplink type veth peer name trunk netns sw")
	sh(t, "ip link set uplink up")
	sh(t, "ip -n sw link set trunk master br0")
	sh(t, "ip -n sw link set trunk up")
	clients := []struct {
		ns, port string
		vlan     int
	}{{"ca", "pa", 2001}, {"cb", "pb", 2002}}
	for _, c := range clients {
		sh(t, fmt.Sprintf("bridge -n sw vlan add dev trunk vid %d", c.vlan))
		sh(t, fmt.Sprintf("ip -n sw link add %s type veth peer name eth0 netns %s", c.port, c.ns))
		sh(t, fmt.Sprintf("ip -n sw link set %s master br0", c.port))
		sh(t, fmt.Sprintf("bridge -n sw vlan del dev %s vid 1", c.port))
		sh(t, fmt.Sprintf("bridge -n sw vlan add dev %s vid %d pvid untagged", c.port, c.vlan))
		sh(t, fmt.Sprintf("ip -n sw link set %s up", c.port))
		sh(t, fmt.Sprintf("ip -n %s addr add 100.80.16.1/16 dev eth0", c.ns))
		sh(t, fmt.Sprintf("ip -n %s link set eth0 up", c.ns))
	}
	// Client B has a second address with a service on it, to show A cannot reach it.
	sh(t, "ip -n cb addr add 100.80.16.2/16 dev eth0")
	var bService net.Listener
	if err := InNetns("cb", func() error {
		var err error
		bService, err = net.Listen("tcp4", "100.80.16.2:9000")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	defer bService.Close()
	go func() {
		for {
			c, err := bService.Accept()
			if err != nil {
				return
			}
			_ = c.Close()
		}
	}()

	m := &Manager{
		Parent:      "uplink",
		Cmd:         ExecCommander{},
		Listen:      ListenInNetns,
		DialTimeout: 3 * time.Second,
		IdleTimeout: 10 * time.Second,
	}
	defer m.Close()
	cfg := Config{CIDR: "100.80.0.0/16", Port: sandboxPort, Tenants: []Tenant{
		{NetworkID: 1, VLAN: 2001, Addresses: []string{"100.80.0.1"}, Assignments: []Assignment{
			{ServiceID: 11, PrivateIP: "100.80.0.1", BindIP: "127.0.0.11", Username: "alice", Password: "pa"},
		}},
		{NetworkID: 2, VLAN: 2002, Addresses: []string{"100.80.0.1"}, Assignments: []Assignment{
			{ServiceID: 12, PrivateIP: "100.80.0.1", BindIP: "127.0.0.12", Username: "bob", Password: "pb"},
		}},
	}}
	m.Apply(cfg)
	if s := m.Status(); len(s.Errors) != 0 || s.Tenants != 2 {
		t.Fatalf("manager status %+v", s)
	}

	proxyAddr := fmt.Sprintf("100.80.0.1:%d", sandboxPort)
	target := "127.0.0.1:19000"

	if code, body := proxyGetFrom(t, "ca", proxyAddr, target, "alice", "pa"); code != 200 || body != "127.0.0.11" {
		t.Fatalf("client A: %d %q", code, body)
	}
	if code, body := proxyGetFrom(t, "cb", proxyAddr, target, "bob", "pb"); code != 200 || body != "127.0.0.12" {
		t.Fatalf("client B: %d %q", code, body)
	}
	if code, _ := proxyGetFrom(t, "ca", proxyAddr, target, "bob", "pb"); code != http.StatusForbidden {
		t.Fatalf("client A with client B's credentials: %d", code)
	}
	if code, _ := proxyGetFrom(t, "cb", proxyAddr, target, "alice", "pa"); code != http.StatusForbidden {
		t.Fatalf("client B with client A's credentials: %d", code)
	}

	if conn, err := dialFrom("ca", "100.80.16.2:9000"); err == nil {
		conn.Close()
		t.Fatal("client A reached client B's server")
	}

	// Another port in the tenant namespace: the firewall must drop it.
	var other net.Listener
	if err := InNetns("pf-n1", func() error {
		var err error
		other, err = net.Listen("tcp4", "0.0.0.0:2222")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if conn, err := dialFrom("ca", "100.80.0.1:2222"); err == nil {
		conn.Close()
		t.Fatal("a port other than the proxy port answered")
	}
	// The tenant namespace has no path to the runner host: only lo and its VLAN interface.
	out, err := exec.Command("ip", "netns", "exec", "pf-n1", "ip", "-o", "link", "show").Output()
	if err != nil || strings.Count(strings.TrimSpace(string(out)), "\n") != 1 {
		t.Fatalf("tenant namespace must hold only lo and its VLAN interface: %s %v", out, err)
	}

	m.Apply(Config{CIDR: cfg.CIDR, Port: sandboxPort})
	out, _ = exec.Command("ip", "netns", "list").Output()
	if strings.Contains(string(out), "pf-n") {
		t.Fatalf("tenant namespaces left after removal: %s", out)
	}
	if conn, err := dialFrom("ca", proxyAddr); err == nil {
		conn.Close()
		t.Fatal("proxy still answers after the tenant was removed")
	}
}
