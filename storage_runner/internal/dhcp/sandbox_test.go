//go:build sandbox

// Opens the real server socket inside a namespace and talks DHCP to it over
// a veth pair. Only run it inside a throwaway namespace, never on a real
// host:
//
//	unshare -rnm sh -c 'mount -t tmpfs none /run && go test -tags sandbox ./internal/dhcp/'
package dhcp

import (
	"context"
	"net"
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func sh(t *testing.T, args ...string) {
	t.Helper()
	if out, err := exec.Command(args[0], args[1:]...).CombinedOutput(); err != nil {
		t.Fatalf("%v: %v\n%s", args, err, out)
	}
}

func TestSandboxServesInsideNamespace(t *testing.T) {
	sh(t, "ip", "netns", "add", "rf-p6")
	defer exec.Command("ip", "netns", "del", "rf-p6").Run()
	sh(t, "ip", "link", "add", "cl0", "type", "veth", "peer", "name", "rf-w6", "netns", "rf-p6")
	// Real clients send from 0.0.0.0 on a raw socket; a UDP socket needs
	// an address to send a broadcast from.
	sh(t, "ip", "addr", "add", "10.0.0.50/24", "dev", "cl0")
	sh(t, "ip", "link", "set", "cl0", "up")
	sh(t, "ip", "-n", "rf-p6", "addr", "add", "10.0.0.254/24", "dev", "rf-w6")
	sh(t, "ip", "-n", "rf-p6", "link", "set", "rf-w6", "up")
	sh(t, "ip", "link", "set", "lo", "up")

	conn, err := OpenInNamespace("/run/netns/rf-p6", "rf-w6")
	if err != nil {
		t.Fatal(err)
	}
	if la := conn.LocalAddr().String(); la != "0.0.0.0:67" {
		t.Fatalf("bound to %s", la)
	}
	srv := NewServer(table(t, reservedCfg()), "", nil, nil)
	srv.Logf = t.Logf
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { srv.Serve(ctx, conn); close(done) }()
	defer func() { cancel(); <-done }()

	// The client side: port 68 on the host end of the veth, broadcast.
	lc := net.ListenConfig{Control: func(_, _ string, c syscall.RawConn) error {
		var serr error
		c.Control(func(fd uintptr) {
			serr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
			if serr == nil {
				serr = syscall.BindToDevice(int(fd), "cl0")
			}
		})
		return serr
	}}
	client, err := lc.ListenPacket(context.Background(), "udp4", "0.0.0.0:68")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err := client.WriteTo(msg{typ: Discover, mac: macA}.bytes(), &net.UDPAddr{IP: net.IPv4bcast, Port: 67}); err != nil {
		t.Fatal(err)
	}
	client.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, 1500)
	n, from, err := client.ReadFrom(buf)
	if err != nil {
		t.Fatal(err)
	}
	p, err := Parse(buf[:n])
	if err != nil || p.MessageType() != Offer || p.YIAddr != ip("10.0.0.1") {
		t.Fatalf("reply from %s: %+v %v", from, p, err)
	}
	if from.(*net.UDPAddr).IP.String() != "10.0.0.254" {
		t.Fatalf("reply from %s", from)
	}

	// The namespace's own address answers; the runner's thread is back in
	// the original namespace.
	if _, err := net.InterfaceByName("rf-w6"); err == nil {
		t.Fatal("rf-w6 visible from the original namespace")
	}
}
