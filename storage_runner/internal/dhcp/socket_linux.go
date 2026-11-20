//go:build linux

package dhcp

import (
	"fmt"
	"net"
	"os"
	"runtime"
	"syscall"

	"golang.org/x/sys/unix"
)

// OpenInNamespace opens the server socket inside the network namespace at
// nsPath: UDP 0.0.0.0:67, bound to iface, broadcast allowed. A socket keeps
// the namespace it was created in, so only its creation runs on a locked
// thread that has entered the namespace.
func OpenInNamespace(nsPath, iface string) (net.PacketConn, error) {
	type result struct {
		pc  net.PacketConn
		err error
	}
	ch := make(chan result, 1)
	go func() {
		runtime.LockOSThread()
		orig, err := os.Open("/proc/thread-self/ns/net")
		if err != nil {
			runtime.UnlockOSThread()
			ch <- result{err: err}
			return
		}
		defer orig.Close()
		target, err := os.Open(nsPath)
		if err != nil {
			runtime.UnlockOSThread()
			ch <- result{err: err}
			return
		}
		defer target.Close()
		if err := setns(target); err != nil {
			runtime.UnlockOSThread()
			ch <- result{err: fmt.Errorf("enter %s: %w", nsPath, err)}
			return
		}
		pc, err := bindDHCP(iface)
		if rerr := setns(orig); rerr != nil {
			// The thread stays locked, so it ends with this goroutine
			// instead of running other code in the wrong namespace.
			ch <- result{pc, err}
			return
		}
		runtime.UnlockOSThread()
		ch <- result{pc, err}
	}()
	r := <-ch
	return r.pc, r.err
}

func setns(f *os.File) error {
	return unix.Setns(int(f.Fd()), unix.CLONE_NEWNET)
}

func bindDHCP(iface string) (net.PacketConn, error) {
	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_DGRAM|syscall.SOCK_CLOEXEC|syscall.SOCK_NONBLOCK, syscall.IPPROTO_UDP)
	if err != nil {
		return nil, fmt.Errorf("socket: %w", err)
	}
	fail := func(what string, err error) (net.PacketConn, error) {
		syscall.Close(fd)
		return nil, fmt.Errorf("%s on %s: %w", what, iface, err)
	}
	if err := syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 1); err != nil {
		return fail("SO_REUSEADDR", err)
	}
	if err := syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1); err != nil {
		return fail("SO_BROADCAST", err)
	}
	if err := syscall.BindToDevice(fd, iface); err != nil {
		return fail("SO_BINDTODEVICE", err)
	}
	if err := syscall.Bind(fd, &syscall.SockaddrInet4{Port: 67}); err != nil {
		return fail("bind :67", err)
	}
	f := os.NewFile(uintptr(fd), "dhcp-"+iface)
	defer f.Close()
	pc, err := net.FilePacketConn(f)
	if err != nil {
		return nil, fmt.Errorf("socket on %s: %w", iface, err)
	}
	return pc, nil
}
