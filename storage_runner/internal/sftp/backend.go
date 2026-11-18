package sftp

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/crypto/ssh"

	"rackflow/storage_runner/internal/execx"
)

const (
	// MountRoot holds the kernel CephFS mounts of SFTP shares, one directory per tenant.
	MountRoot = "/run/rackflow-storage/sftp"

	LabelRole       = "rackflow.role"
	RoleValue       = "storage-sftp"
	LabelTenant     = "rackflow.tenant"
	LabelMountsHash = "rackflow.sftp-mounts"

	DefaultServerPath = "/usr/lib/ssh/sftp-server"
	// NobodyID is the uid and gid sftp-server runs as, the same as squashed NFS root.
	NobodyID = "65534:65534"
)

// ContainerName is the name of a tenant's SFTP back end.
func ContainerName(tenantID int64) string { return "rf-sftp-t" + strconv.FormatInt(tenantID, 10) }

// ParseContainerName returns the tenant of an SFTP back end name.
func ParseContainerName(name string) (int64, bool) {
	rest, ok := strings.CutPrefix(strings.TrimPrefix(name, "/"), "rf-sftp-t")
	if !ok {
		return 0, false
	}
	id, err := strconv.ParseInt(rest, 10, 64)
	return id, err == nil && id > 0 && strconv.FormatInt(id, 10) == rest
}

// TenantMountDir is the directory holding a tenant's share mounts.
func TenantMountDir(root string, tenantID int64) string {
	return filepath.Join(root, "t"+strconv.FormatInt(tenantID, 10))
}

// LoadOrCreateHostKey reads the gateway's ed25519 host key, creating it (mode
// 0600) on first use.
func LoadOrCreateHostKey(path string) (ssh.Signer, error) {
	data, err := os.ReadFile(path)
	if err == nil {
		return ssh.ParsePrivateKey(data)
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	block, err := ssh.MarshalPrivateKey(priv, "rackflow storage sftp")
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, pem.EncodeToMemory(block), 0o600); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, path); err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(priv)
}

// HostKeyLine is the host key in authorized_keys format.
func HostKeyLine(s ssh.Signer) string {
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(s.PublicKey())))
}

// --- mounts ---

// MountSpec is one share mounted for a tenant's SFTP back end.
type MountSpec struct {
	TenantID   int64
	Share      string // share name, also the folder name in the container
	Target     string
	Path       string // CephFS path of the subvolume
	Entity     string // cephx user, client.rf-t<id>
	SecretFile string // file holding only the tenant's cephx key
	ConfFile   string // ceph.conf with the monitor addresses
	FSName     string
	ReadOnly   bool
}

// Mount is a mount found under the mount root.
type Mount struct {
	Target   string
	TenantID int64
	Share    string
	ReadOnly bool
}

// Mounter mounts and unmounts CephFS paths for SFTP.
type Mounter interface {
	List(ctx context.Context) ([]Mount, error)
	Mount(ctx context.Context, spec MountSpec) error
	Unmount(ctx context.Context, target string) error
}

// parseMountTarget maps <root>/t<id>/<share> to tenant and share.
func parseMountTarget(root, target string) (int64, string, bool) {
	rel, err := filepath.Rel(root, target)
	if err != nil || strings.HasPrefix(rel, "..") {
		return 0, "", false
	}
	parts := strings.Split(rel, string(filepath.Separator))
	if len(parts) != 2 || !strings.HasPrefix(parts[0], "t") {
		return 0, "", false
	}
	id, err := strconv.ParseInt(parts[0][1:], 10, 64)
	if err != nil || id <= 0 {
		return 0, "", false
	}
	return id, parts[1], true
}

// KernelMounter uses the kernel CephFS client through mount(8) and mount.ceph.
// The key is passed in a secret file, never on the command line.
type KernelMounter struct {
	Cmd       execx.Commander
	Root      string
	MountsTab string // default /proc/self/mounts
}

func (k *KernelMounter) root() string {
	if k.Root != "" {
		return k.Root
	}
	return MountRoot
}

func (k *KernelMounter) List(ctx context.Context) ([]Mount, error) {
	tab := k.MountsTab
	if tab == "" {
		tab = "/proc/self/mounts"
	}
	f, err := os.Open(tab)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return parseMounts(f, k.root())
}

func parseMounts(r io.Reader, root string) ([]Mount, error) {
	var out []Mount
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 4 || fields[2] != "ceph" {
			continue
		}
		target := strings.ReplaceAll(fields[1], `\040`, " ")
		id, share, ok := parseMountTarget(root, target)
		if !ok {
			continue
		}
		ro := false
		for _, o := range strings.Split(fields[3], ",") {
			if o == "ro" {
				ro = true
			}
		}
		out = append(out, Mount{Target: target, TenantID: id, Share: share, ReadOnly: ro})
	}
	return out, sc.Err()
}

// MountArgs is the mount(8) command line for spec, without the binary.
func MountArgs(spec MountSpec) []string {
	opts := []string{
		"name=" + strings.TrimPrefix(spec.Entity, "client."),
		"secretfile=" + spec.SecretFile,
		"conf=" + spec.ConfFile,
		"mds_namespace=" + spec.FSName,
		"nosuid", "nodev", "noexec",
	}
	if spec.ReadOnly {
		opts = append(opts, "ro")
	}
	return []string{"-t", "ceph", ":" + spec.Path, spec.Target, "-o", strings.Join(opts, ",")}
}

func (k *KernelMounter) Mount(ctx context.Context, spec MountSpec) error {
	if err := os.MkdirAll(filepath.Dir(spec.Target), 0o700); err != nil {
		return err
	}
	if err := os.MkdirAll(spec.Target, 0o700); err != nil {
		return err
	}
	_, err := k.Cmd.Run(ctx, nil, "mount", MountArgs(spec)...)
	return err
}

func (k *KernelMounter) Unmount(ctx context.Context, target string) error {
	_, err := k.Cmd.Run(ctx, nil, "umount", target)
	if err != nil {
		return err
	}
	return os.Remove(target)
}

// FakeMounter keeps mounts in memory.
type FakeMounter struct {
	Root     string
	mu       sync.Mutex
	Mounts   map[string]MountSpec
	FailOn   map[int64]error // tenant -> error returned by Mount
	MountLog []string
}

func (f *FakeMounter) List(ctx context.Context) ([]Mount, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []Mount
	for target, s := range f.Mounts {
		out = append(out, Mount{Target: target, TenantID: s.TenantID, Share: s.Share, ReadOnly: s.ReadOnly})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Target < out[j].Target })
	return out, nil
}

func (f *FakeMounter) Mount(ctx context.Context, spec MountSpec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.FailOn[spec.TenantID]; err != nil {
		return err
	}
	if f.Mounts == nil {
		f.Mounts = map[string]MountSpec{}
	}
	f.Mounts[spec.Target] = spec
	f.MountLog = append(f.MountLog, "mount "+spec.Target)
	return nil
}

func (f *FakeMounter) Unmount(ctx context.Context, target string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.Mounts, target)
	f.MountLog = append(f.MountLog, "umount "+target)
	return nil
}

// --- containers ---

// Bind is one share bind-mounted into a back end.
type Bind struct {
	Source   string
	Target   string
	ReadOnly bool
}

// ContainerSpec describes a tenant's SFTP back end.
type ContainerSpec struct {
	Name       string
	TenantID   int64
	Image      string
	MountsHash string
	Binds      []Bind
}

// Container is a back end found on the host.
type Container struct {
	Name       string
	TenantID   int64
	Running    bool
	MountsHash string
}

// Runtime runs the per-tenant back ends.
type Runtime interface {
	List(ctx context.Context) ([]Container, error)
	Run(ctx context.Context, spec ContainerSpec) error
	Remove(ctx context.Context, name string) error
}

// RunArgs is the podman command line for spec, without the binary. The back
// end has no network, no capabilities, a read-only root and runs as nobody;
// it only sees its tenant's shares.
func RunArgs(spec ContainerSpec) []string {
	args := []string{
		"run", "-d",
		"--name", spec.Name,
		"--restart", "no",
		"--network", "none",
		"--read-only",
		"--cap-drop", "all",
		"--security-opt", "no-new-privileges",
		"--user", NobodyID,
		"--pids-limit", "256",
		"--memory", "256m",
		"--tmpfs", "/tmp",
		"--label", LabelRole + "=" + RoleValue,
		"--label", LabelTenant + "=" + strconv.FormatInt(spec.TenantID, 10),
		"--label", LabelMountsHash + "=" + spec.MountsHash,
	}
	for _, b := range spec.Binds {
		mode := ":rw"
		if b.ReadOnly {
			mode = ":ro"
		}
		args = append(args, "-v", b.Source+":"+b.Target+mode)
	}
	return append(args, "--entrypoint", "sleep", spec.Image, "infinity")
}

// Podman runs back ends with podman.
type Podman struct {
	Cmd       execx.Commander
	RemoteURL string
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
		id, ok := ParseContainerName(c.Names[0])
		if !ok || c.Labels[LabelTenant] != strconv.FormatInt(id, 10) {
			continue
		}
		res = append(res, Container{Name: c.Names[0], TenantID: id, Running: c.State == "running", MountsHash: c.Labels[LabelMountsHash]})
	}
	sort.Slice(res, func(i, j int) bool { return res[i].TenantID < res[j].TenantID })
	return res, nil
}

func (p *Podman) Run(ctx context.Context, spec ContainerSpec) error {
	_, err := p.podman(ctx, RunArgs(spec)...)
	return err
}

func (p *Podman) Remove(ctx context.Context, name string) error {
	_, err := p.podman(ctx, "rm", "-f", "-t", "5", name)
	return err
}

// FakeRuntime keeps back ends in memory.
type FakeRuntime struct {
	mu         sync.Mutex
	Containers map[string]Container
	Specs      map[string]ContainerSpec
	Runs       int
	FailRun    map[int64]error
}

func (f *FakeRuntime) List(ctx context.Context) ([]Container, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []Container
	for _, c := range f.Containers {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TenantID < out[j].TenantID })
	return out, nil
}

func (f *FakeRuntime) Run(ctx context.Context, spec ContainerSpec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.FailRun[spec.TenantID]; err != nil {
		return err
	}
	if f.Containers == nil {
		f.Containers = map[string]Container{}
		f.Specs = map[string]ContainerSpec{}
	}
	if _, ok := f.Containers[spec.Name]; ok {
		return fmt.Errorf("container %s already exists", spec.Name)
	}
	f.Containers[spec.Name] = Container{Name: spec.Name, TenantID: spec.TenantID, Running: true, MountsHash: spec.MountsHash}
	f.Specs[spec.Name] = spec
	f.Runs++
	return nil
}

func (f *FakeRuntime) Remove(ctx context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.Containers, name)
	delete(f.Specs, name)
	return nil
}

// --- gateway back end ---

// PodmanBackend runs sftp-server inside the tenant's back end container with
// the SSH channel as its standard input and output.
type PodmanBackend struct {
	RemoteURL  string
	ServerPath string
}

// ExecArgs is the podman exec command line, without the binary.
func (b *PodmanBackend) ExecArgs(tenantID int64) []string {
	path := b.ServerPath
	if path == "" {
		path = DefaultServerPath
	}
	args := []string{"exec", "-i", "--user", NobodyID, ContainerName(tenantID), path, "-e", "-l", "INFO", "-d", "/"}
	if b.RemoteURL != "" {
		args = append([]string{"--remote", "--url", b.RemoteURL}, args...)
	}
	return args
}

func (b *PodmanBackend) Serve(ctx context.Context, tenantID int64, stdin io.Reader, stdout, stderr io.Writer) error {
	cmd := exec.CommandContext(ctx, "podman", b.ExecArgs(tenantID)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdin, stdout, stderr
	return cmd.Run()
}
