package main

import (
	"context"
	"log"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
	"rackflow/storage_runner/internal/config"
	"rackflow/storage_runner/internal/dhcp"
	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/firewall"
	"rackflow/storage_runner/internal/ganesha"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
	"rackflow/storage_runner/internal/netns"
	"rackflow/storage_runner/internal/reconcile"
	"rackflow/storage_runner/internal/sftp"
	"rackflow/storage_runner/internal/state"
	"rackflow/storage_runner/internal/vip"
)

// version is overridden at build time with -ldflags "-X main.version=...".
var version = "0.4.1"

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "guard":
			runGuard()
			return
		case "version", "--version":
			os.Stdout.WriteString(version + "\n")
			return
		default:
			log.Fatalf("unknown mode %q; run without arguments for the runner, or with \"guard\"", os.Args[1])
		}
	}
	runRunner()
}

func runRunner() {
	host, err := config.FromEnv(os.Getenv)
	if err != nil {
		log.Fatalf("storage runner config: %v", err)
	}
	names.SetCephUserPrefix(host.CephUserPrefix)
	store, err := state.Open(host.StateDir, host.LinkCIDR)
	if err != nil {
		log.Fatalf("state: %v", err)
	}
	cmd := execx.Exec{Timeout: 2 * time.Minute}
	cephCLI := &ceph.CLI{
		Cmd:            cmd,
		FS:             host.CephFSName,
		SubvolumeGroup: host.CephSubvolumeGroup,
		ECPool:         host.CephECPool,
		Conf:           host.CephConf,
		Keyring:        host.CephKeyring,
		Name:           host.CephName,
		Scoped:         host.CephScopedKey,
	}
	hostname, _ := os.Hostname()

	helper := &lease.Helper{Conf: host.CephConf, Keyring: host.CephKeyring, Name: host.CephName, Pool: host.CephHAPool, Logf: log.Printf}
	defer helper.Close()
	notifier := lease.NotifierFromEnv()
	leases := &lease.Manager{
		Store:       helper,
		Fencer:      cephCLI,
		Clock:       lease.SystemClock{},
		Version:     version,
		RangeFence:  host.CephScopedKey,
		DeadlineDir: lease.DefaultDeadlineDir,
		Logf:        log.Printf,
		Alive:       func() { _ = notifier.Notify("WATCHDOG=1") },
	}

	r := &reconcile.Reconciler{
		Host:          host,
		Source:        &api.Client{BaseURL: host.RackflowURL, Key: host.RunnerKey},
		Net:           &netns.IPRoute{Cmd: cmd, ParentIface: host.ParentIface},
		Firewall:      &firewall.NFT{Cmd: cmd},
		Ceph:          cephCLI,
		NFS:           &ganesha.Podman{Cmd: cmd, RemoteURL: host.PodmanRemoteURL},
		State:         store,
		RunnerVersion: version,
		Lease:         leases,
		RunLease:      true,
		Hostname:      hostname,
		VIP:           &vip.IPRoute{Cmd: execx.Exec{Timeout: 15 * time.Second}},
		DHCP: &dhcp.Pool{
			StateDir: host.StateDir,
			Open: func(ns, iface string) (net.PacketConn, error) {
				return dhcp.OpenInNamespace(filepath.Join(names.NetnsDir, ns), iface)
			},
			Prober: func(ns, iface string) dhcp.Prober {
				return dhcp.ARPProbe{Cmd: execx.Exec{Timeout: 5 * time.Second}, Netns: ns, Iface: iface}
			},
			Logf: log.Printf,
		},
	}
	defer r.DHCP.StopAll()

	// The location host key, once Rackflow sends one, replaces this file's
	// contents; until then the node's own key is used.
	hostKey, err := sftp.LoadOrCreateHostKey(filepath.Join(host.StateDir, reconcile.HostKeyFile))
	if err != nil {
		log.Fatalf("sftp host key: %v", err)
	}
	gateway := sftp.NewGateway(hostKey, &sftp.PodmanBackend{RemoteURL: host.PodmanRemoteURL, ServerPath: host.SFTPServerPath}, log.Printf)
	defer gateway.Close()
	r.SFTP = gateway
	r.SFTPMounts = &sftp.KernelMounter{Cmd: cmd}
	r.SFTPRuntime = &sftp.Podman{Cmd: cmd, RemoteURL: host.PodmanRemoteURL}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// The watchdog is fed by the lease loop once it runs (the config has an
	// ha block); until then, and in legacy mode, by this loop.
	_ = notifier.Notify("READY=1")
	go func() {
		t := time.NewTicker(5 * time.Second)
		defer t.Stop()
		for {
			if !leases.Running() {
				_ = notifier.Notify("WATCHDOG=1")
			}
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
		}
	}()

	log.Printf("storage runner %s: node interface %s, fs %s, EC pool %s, HA pool %s, cephx users client.%s<id>, polling every %s",
		version, host.ParentIface, host.CephFSName, host.CephECPool, host.CephHAPool, host.CephUserPrefix, host.PollInterval)
	r.Loop(ctx, host.PollInterval)
	_ = notifier.Notify("STOPPING=1")
	log.Printf("storage runner stopped")
}

// guardActions stops serving with ip, podman and the VIP manager only.
type guardActions struct {
	cmd    execx.Commander
	podman *ganesha.Podman
	vip    *vip.IPRoute
}

func (g guardActions) LinkDown(ctx context.Context, ns, link string) error {
	_, err := g.cmd.Run(ctx, nil, "ip", "-n", ns, "link", "set", link, "down")
	return err
}

func (g guardActions) StopContainer(ctx context.Context, name string) error {
	return g.podman.Stop(ctx, name)
}

func (g guardActions) RemoveVIP(ctx context.Context, iface, prefix string) error {
	p, err := netip.ParsePrefix(prefix)
	if err != nil {
		return err
	}
	return g.vip.Remove(ctx, iface, p)
}

// runGuard is `storage-runner guard`: it needs no Rackflow or Ceph access,
// only the deadline files and PODMAN_REMOTE_URL if the runner uses one.
func runGuard() {
	cmd := execx.Exec{Timeout: 15 * time.Second}
	dir := os.Getenv("LEASE_DEADLINE_DIR")
	if dir == "" {
		dir = lease.DefaultDeadlineDir
	}
	g := &lease.Guard{
		Dir:   dir,
		Clock: lease.SystemClock{},
		Actions: guardActions{
			cmd:    cmd,
			podman: &ganesha.Podman{Cmd: cmd, RemoteURL: os.Getenv("PODMAN_REMOTE_URL")},
			vip:    &vip.IPRoute{Cmd: cmd},
		},
		Logf: log.Printf,
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	log.Printf("storage runner %s guard: watching %s", version, dir)
	g.Run(ctx)
}
