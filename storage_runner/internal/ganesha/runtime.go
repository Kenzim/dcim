package ganesha

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/names"
)

const (
	LabelRole     = "rackflow.role"
	RoleValue     = "storage-nfs"
	LabelTenant   = "rackflow.tenant"
	LabelCoreHash = "rackflow.core-hash"

	// Paths inside the container.
	ConfDir     = "/etc/ganesha"
	CephDir     = "/etc/ceph"
	RecoveryDir = "/var/lib/nfs/ganesha"
	Entrypoint  = "/usr/bin/ganesha.nfsd"
)

type Mount struct {
	Source   string
	Target   string
	ReadOnly bool
}

// Spec describes a tenant's Ganesha container.
type Spec struct {
	Name     string
	TenantID int64
	Image    string
	Netns    string // path of the named network namespace
	CoreHash string
	Mounts   []Mount
}

type Container struct {
	Name     string
	TenantID int64
	Running  bool
	CoreHash string
}

type Runtime interface {
	// List returns runner-owned Ganesha containers.
	List(ctx context.Context) ([]Container, error)
	Run(ctx context.Context, spec Spec) error
	Remove(ctx context.Context, name string) error
	// Reload makes Ganesha re-read its exports.
	Reload(ctx context.Context, name string) error
}

// Podman runs Ganesha with podman. Docker cannot join a named network
// namespace by path, so podman is the only supported runtime.
type Podman struct {
	Cmd       execx.Commander
	RemoteURL string // optional podman service URL when the runner itself runs in a container
}

func (p *Podman) podman(ctx context.Context, args ...string) ([]byte, error) {
	if p.RemoteURL != "" {
		args = append([]string{"--remote", "--url", p.RemoteURL}, args...)
	}
	return p.Cmd.Run(ctx, nil, "podman", args...)
}

func (p *Podman) List(ctx context.Context) ([]Container, error) {
	out, err := p.podman(ctx, "ps", "-a", "--filter", "label="+LabelRole+"="+RoleValue, "--format", "json")
	if err != nil {
		return nil, err
	}
	var raw []struct {
		Names  []string          `json:"Names"`
		State  string            `json:"State"`
		Labels map[string]string `json:"Labels"`
	}
	if s := strings.TrimSpace(string(out)); s != "" && s != "null" {
		if err := json.Unmarshal(out, &raw); err != nil {
			return nil, fmt.Errorf("parse podman ps output: %w", err)
		}
	}
	var res []Container
	for _, c := range raw {
		if len(c.Names) == 0 {
			continue
		}
		id, ok := names.ParseContainer(c.Names[0])
		if !ok || c.Labels[LabelTenant] != strconv.FormatInt(id, 10) {
			continue
		}
		res = append(res, Container{
			Name:     c.Names[0],
			TenantID: id,
			Running:  c.State == "running",
			CoreHash: c.Labels[LabelCoreHash],
		})
	}
	sort.Slice(res, func(i, j int) bool { return res[i].TenantID < res[j].TenantID })
	return res, nil
}

// RunArgs is the podman command line for spec, without the binary.
func RunArgs(spec Spec) []string {
	args := []string{
		"run", "-d",
		"--name", spec.Name,
		"--network", "ns:" + spec.Netns,
		// The runner restarts a stopped container itself, after evicting the
		// old CephFS session; an in-place restart would stall on its caps.
		"--restart", "no",
		"--label", LabelRole + "=" + RoleValue,
		"--label", LabelTenant + "=" + strconv.FormatInt(spec.TenantID, 10),
		"--label", LabelCoreHash + "=" + spec.CoreHash,
	}
	for _, m := range spec.Mounts {
		v := m.Source + ":" + m.Target
		if m.ReadOnly {
			v += ":ro"
		}
		args = append(args, "-v", v)
	}
	args = append(args,
		"--entrypoint", Entrypoint,
		spec.Image,
		"-F", "-L", "STDERR", "-f", ConfDir+"/ganesha.conf")
	return args
}

func (p *Podman) Run(ctx context.Context, spec Spec) error {
	_, err := p.podman(ctx, RunArgs(spec)...)
	return err
}

func (p *Podman) Remove(ctx context.Context, name string) error {
	_, err := p.podman(ctx, "rm", "-f", name)
	return err
}

func (p *Podman) Reload(ctx context.Context, name string) error {
	_, err := p.podman(ctx, "kill", "--signal", "HUP", name)
	return err
}
