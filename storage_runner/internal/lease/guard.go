package lease

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DefaultDeadlineDir is where the runner writes, and the guard reads, the
// local lease deadlines.
const DefaultDeadlineDir = "/run/rackflow-storage/leases"

// GuardActions stop serving for the guard. They must not need Ceph or
// Rackflow.
type GuardActions interface {
	LinkDown(ctx context.Context, netns, link string) error
	StopContainer(ctx context.Context, name string) error
	RemoveVIP(ctx context.Context, iface, prefix string) error
}

// Guard is the second line of defence of section 5.7: a separate process
// that stops what a lease covers once the lease's guard deadline (renewal
// send time + lease_seconds - guard_margin_seconds) has passed on either
// clock, in case the runner itself is stuck.
type Guard struct {
	Dir     string
	Clock   Clock
	Actions GuardActions
	Logf    func(format string, args ...any)
}

func (g *Guard) logf(format string, args ...any) {
	if g.Logf != nil {
		g.Logf(format, args...)
	}
}

// Check acts on every expired deadline file once and removes it.
func (g *Guard) Check(ctx context.Context) {
	entries, err := os.ReadDir(g.Dir)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			g.logf("guard: read %s: %v", g.Dir, err)
		}
		return
	}
	now, boot := g.Clock.Now(), g.Clock.Boot()
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		path := filepath.Join(g.Dir, e.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var d deadlineFile
		if err := json.Unmarshal(data, &d); err != nil {
			g.logf("guard: %s is unreadable: %v", path, err)
			continue
		}
		if now.Before(d.WallDeadline) && boot < time.Duration(d.BootDeadlineNS) {
			continue
		}
		if err := g.stop(ctx, d); err != nil {
			g.logf("guard: %s passed its deadline; stopping failed: %v", d.Key, err)
			continue
		}
		g.logf("guard: %s passed its deadline without renewal; stopped it", d.Key)
		// Only remove the file if the runner did not renew it meanwhile.
		if cur, err := os.ReadFile(path); err == nil && string(cur) == string(data) {
			os.Remove(path)
		}
	}
}

func (g *Guard) stop(ctx context.Context, d deadlineFile) error {
	var errs []error
	if d.Netns != "" && d.Link != "" {
		if err := g.Actions.LinkDown(ctx, d.Netns, d.Link); err != nil {
			errs = append(errs, fmt.Errorf("interface down: %w", err))
		}
	}
	if d.Container != "" {
		if err := g.Actions.StopContainer(ctx, d.Container); err != nil {
			errs = append(errs, fmt.Errorf("stop container: %w", err))
		}
	}
	if d.VIP != "" && d.VIPInterface != "" {
		if err := g.Actions.RemoveVIP(ctx, d.VIPInterface, d.VIP); err != nil {
			errs = append(errs, fmt.Errorf("remove virtual ip: %w", err))
		}
	}
	return errors.Join(errs...)
}

// Run checks every second until ctx ends.
func (g *Guard) Run(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		g.Check(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Notifier sends sd_notify messages to systemd over NOTIFY_SOCKET; a zero
// Notifier (no socket) does nothing.
type Notifier struct {
	Socket string
}

// NotifierFromEnv uses $NOTIFY_SOCKET.
func NotifierFromEnv() Notifier { return Notifier{Socket: os.Getenv("NOTIFY_SOCKET")} }

// Notify sends state, such as "READY=1" or "WATCHDOG=1".
func (n Notifier) Notify(state string) error {
	if n.Socket == "" {
		return nil
	}
	name := n.Socket
	if strings.HasPrefix(name, "@") {
		name = "\x00" + name[1:]
	}
	conn, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: name, Net: "unixgram"})
	if err != nil {
		return err
	}
	defer conn.Close()
	_, err = conn.Write([]byte(state))
	return err
}
