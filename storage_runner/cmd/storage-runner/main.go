package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/ceph"
	"rackflow/storage_runner/internal/config"
	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/firewall"
	"rackflow/storage_runner/internal/ganesha"
	"rackflow/storage_runner/internal/netns"
	"rackflow/storage_runner/internal/reconcile"
	"rackflow/storage_runner/internal/state"
)

// version is overridden at build time with -ldflags "-X main.version=...".
var version = "0.1.0"

func main() {
	host, err := config.FromEnv(os.Getenv)
	if err != nil {
		log.Fatalf("storage runner config: %v", err)
	}
	store, err := state.Open(host.StateDir, host.LinkCIDR)
	if err != nil {
		log.Fatalf("state: %v", err)
	}
	cmd := execx.Exec{Timeout: 2 * time.Minute}

	r := &reconcile.Reconciler{
		Host:     host,
		Source:   &api.Client{BaseURL: host.RackflowURL, Key: host.RunnerKey},
		Net:      &netns.IPRoute{Cmd: cmd, ParentIface: host.ParentIface},
		Firewall: &firewall.NFT{Cmd: cmd},
		Ceph: &ceph.CLI{
			Cmd:            cmd,
			FS:             host.CephFSName,
			SubvolumeGroup: host.CephSubvolumeGroup,
			ECPool:         host.CephECPool,
			Conf:           host.CephConf,
			Keyring:        host.CephKeyring,
		},
		NFS:           &ganesha.Podman{Cmd: cmd, RemoteURL: host.PodmanRemoteURL},
		State:         store,
		RunnerVersion: version,
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	log.Printf("storage runner %s: node interface %s, fs %s, EC pool %s, polling every %s",
		version, host.ParentIface, host.CephFSName, host.CephECPool, host.PollInterval)
	r.Loop(ctx, host.PollInterval)
	log.Printf("storage runner stopped")
}
