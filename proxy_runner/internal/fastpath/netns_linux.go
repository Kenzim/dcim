//go:build linux && (amd64 || arm64)

package fastpath

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
)

// NetnsDir is where named network namespaces are mounted.
var NetnsDir = "/run/netns"

func setns(fd uintptr) error {
	if _, _, errno := syscall.Syscall(sysSetns, fd, syscall.CLONE_NEWNET, 0); errno != 0 {
		return errno
	}
	return nil
}

// ListenInNetns opens a TCP listener on 0.0.0.0:port inside the named
// namespace. The socket keeps its namespace after the thread switches back,
// so accepting and serving happen on ordinary goroutines.
func ListenInNetns(namespace string, port int) (net.Listener, error) {
	var ln net.Listener
	err := InNetns(namespace, func() error {
		var err error
		ln, err = net.Listen("tcp4", fmt.Sprintf("0.0.0.0:%d", port))
		return err
	})
	if err != nil && ln != nil {
		_ = ln.Close()
		ln = nil
	}
	return ln, err
}

// InNetns runs fn with the calling thread in the named namespace. Sockets
// fn creates stay in that namespace. The switch runs on a goroutine of its
// own: if switching back fails, that goroutine exits with its thread still
// locked and the runtime discards the thread instead of reusing it.
func InNetns(namespace string, fn func() error) error {
	done := make(chan error, 1)
	go func() {
		done <- inNetnsLocked(namespace, fn)
	}()
	return <-done
}

func inNetnsLocked(namespace string, fn func() error) error {
	runtime.LockOSThread()
	origin, err := os.Open("/proc/thread-self/ns/net")
	if err != nil {
		runtime.UnlockOSThread()
		return err
	}
	defer origin.Close()
	target, err := os.Open(filepath.Join(NetnsDir, namespace))
	if err != nil {
		runtime.UnlockOSThread()
		return err
	}
	defer target.Close()
	if err := setns(target.Fd()); err != nil {
		runtime.UnlockOSThread()
		return fmt.Errorf("enter %s: %w", namespace, err)
	}
	fnErr := fn()
	if err := setns(origin.Fd()); err != nil {
		// Left locked on purpose: see InNetns.
		return fmt.Errorf("leave %s: %w", namespace, err)
	}
	runtime.UnlockOSThread()
	return fnErr
}
