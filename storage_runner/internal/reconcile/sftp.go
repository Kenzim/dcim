package reconcile

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
	"rackflow/storage_runner/internal/sftp"
)

// sftpPass holds the SFTP phase's results for the status report.
type sftpPass struct {
	tenants map[int64]api.TenantSFTP
	errs    []string
	orphans []string
	port    int
	logins  []sftp.Login
}

func (r *Reconciler) sftpMountRoot() string {
	if r.SFTPMountRoot != "" {
		return r.SFTPMountRoot
	}
	return sftp.MountRoot
}

func tenantSFTP(state, msg string) api.TenantSFTP {
	t := api.TenantSFTP{State: state}
	if msg != "" {
		t.Error = &msg
	}
	return t
}

// sftpShares are the shares a tenant's back end should serve, by config only.
func sftpShares(w *tenantWork) []*shareWork {
	var out []*shareWork
	for _, s := range w.shares {
		if s.valid && s.s.State == api.ShareActive && s.s.SFTPMode() != api.SFTPOff {
			out = append(out, s)
		}
	}
	return out
}

// applySFTP runs after the NFS phase and before destruction, so shares are
// unmounted before their subvolumes are removed. It never touches Ganesha,
// network namespaces or Ceph data, and an SFTP failure only affects the
// tenant's SFTP state.
func (p *pass) applySFTP(cfg *api.Config, works []*tenantWork) {
	r := p.r
	sp := &sftpPass{tenants: map[int64]api.TenantSFTP{}}
	p.sftp = sp
	enabled := cfg.SFTP != nil && cfg.SFTP.Enabled
	if cfg.SFTP != nil {
		sp.port = cfg.SFTP.Port
	}
	if r.SFTP == nil || r.SFTPMounts == nil || r.SFTPRuntime == nil {
		if enabled {
			sp.errs = append(sp.errs, "this runner was started without SFTP support")
		}
		for _, w := range works {
			sp.tenants[w.t.ID] = tenantSFTP(api.SFTPStateOff, "")
		}
		return
	}

	p.applyHostKey(cfg)
	mode := p.sftpModeFor(cfg)
	p.sftpMaster = mode == sftpMaster
	mounts, err := r.SFTPMounts.List(p.ctx)
	if err == nil {
		var containers []sftp.Container
		containers, err = r.SFTPRuntime.List(p.ctx)
		if err == nil {
			p.sftpTenants(cfg, works, enabled, mode, mounts, containers)
		}
	}
	if err != nil {
		// Without an inventory nothing is changed, and no login is served.
		sp.errs = append(sp.errs, fmt.Sprintf("sftp inventory: %v", err))
		r.SFTP.SetUsers(nil, sftp.Settings{})
		for _, w := range works {
			sp.tenants[w.t.ID] = tenantSFTP(api.SFTPStateError, "sftp inventory failed")
		}
	}

	addr := ""
	if enabled && sp.port > 0 {
		addr = p.sftpListenAddr(cfg, mode, sp.port)
	} else if enabled {
		sp.errs = append(sp.errs, "sftp port is not set")
	}
	if mode == sftpMaster && addr != "" {
		if err := p.ensureVIP(cfg); err != nil {
			sp.errs = append(sp.errs, fmt.Sprintf("sftp virtual ip: %v", err))
			addr = ""
		}
	} else if p.ha != nil {
		r.removeVIP(p.ctx, cfg)
	}
	listen := func() error { return r.SFTP.Listen(addr) }
	if mode == sftpMaster && addr != "" {
		listen = func() error {
			return p.whileHolding(lease.SFTPKey(cfg.SFTP.LocationID), func() error { return r.SFTP.Listen(addr) })
		}
	}
	if err := listen(); err != nil {
		sp.errs = append(sp.errs, fmt.Sprintf("sftp listen on %s: %v", addr, err))
	}
	sp.logins = r.SFTP.Logins()
}

func (p *pass) sftpTenants(cfg *api.Config, works []*tenantWork, enabled bool, mode sftpMode, mounts []sftp.Mount, containers []sftp.Container) {
	r := p.r
	sp := p.sftp
	imageMissing := enabled && r.Host.SFTPImage == ""
	if imageMissing {
		sp.errs = append(sp.errs, "SFTP_IMAGE is not set")
	}
	mountsBy := map[int64][]sftp.Mount{}
	for _, m := range mounts {
		mountsBy[m.TenantID] = append(mountsBy[m.TenantID], m)
	}
	containersBy := map[int64]sftp.Container{}
	for _, c := range containers {
		containersBy[c.TenantID] = c
	}
	known := map[int64]bool{}
	var users []sftp.User
	for _, w := range works {
		id := w.t.ID
		known[id] = true
		parsed, userErrs := parseSFTPUsers(w.t)
		shares := sftpShares(w)
		active := w.t.State == api.TenantActive
		serve := enabled && !imageMissing && active && len(shares) > 0 && len(parsed) > 0 && p.sftpServes(mode, w)
		if serve && !w.held {
			// The SFTP master serves tenants whose NFS runs elsewhere; it
			// only reads their subvolume paths and never changes Ceph.
			for _, s := range shares {
				if s.info == nil {
					p.loadInfo(s)
				}
			}
		}
		if !serve {
			msg := strings.Join(userErrs, "; ")
			if err := p.sftpTeardown(w, mountsBy[id], containersBy[id]); err != nil {
				sp.tenants[id] = tenantSFTP(api.SFTPStateError, err.Error())
				continue
			}
			sp.tenants[id] = tenantSFTP(api.SFTPStateOff, msg)
			continue
		}
		// While shares cannot be described the back end is left as it is.
		if !w.valid || w.hold || !p.cephOK {
			c, ok := containersBy[id]
			if ok && c.Running {
				users = append(users, parsed...)
			}
			sp.tenants[id] = tenantSFTP(api.SFTPStatePending, "sftp left unchanged until the tenant's shares can be applied")
			continue
		}
		if err := p.sftpEnsure(w, shares, mountsBy[id], containersBy[id]); err != nil {
			r.logf("tenant %d: sftp: %v", id, err)
			sp.tenants[id] = tenantSFTP(api.SFTPStateError, err.Error())
			continue
		}
		users = append(users, parsed...)
		sp.tenants[id] = tenantSFTP(api.SFTPStateReady, strings.Join(userErrs, "; "))
	}
	for _, c := range containers {
		if !known[c.TenantID] {
			sp.orphans = append(sp.orphans, "sftp container "+c.Name)
		}
	}
	for _, m := range mounts {
		if !known[m.TenantID] {
			sp.orphans = append(sp.orphans, "sftp mount "+m.Target)
		}
	}
	sort.Strings(sp.orphans)

	settings := sftp.Settings{}
	if cfg.SFTP != nil {
		settings = sftp.Settings{
			MaxFailures: cfg.SFTP.MaxAuthFailures,
			Window:      time.Duration(cfg.SFTP.FailureWindowSeconds) * time.Second,
			Ban:         time.Duration(cfg.SFTP.BanSeconds) * time.Second,
		}
	}
	r.SFTP.SetUsers(users, settings)
}

func parseSFTPUsers(t api.Tenant) ([]sftp.User, []string) {
	if t.State != api.TenantActive {
		return nil, nil
	}
	var out []sftp.User
	var errs []string
	for _, u := range t.SFTPUsers {
		hash := ""
		if u.PasswordHash != nil {
			hash = *u.PasswordHash
		}
		parsed, err := sftp.ParseUser(u.ID, t.ID, u.Username, u.PublicKeys, hash, u.AllowedIPs)
		if err != nil {
			errs = append(errs, err.Error())
			continue
		}
		out = append(out, parsed)
	}
	return out, errs
}

// sftpTeardown stops a tenant's back end and releases its mounts. Directories
// are only removed with os.Remove, so nothing under a mount can be deleted.
func (p *pass) sftpTeardown(w *tenantWork, mounts []sftp.Mount, c sftp.Container) error {
	r := p.r
	id := w.t.ID
	if c.Name != "" {
		r.SFTP.CloseTenant(id)
		if err := r.SFTPRuntime.Remove(p.ctx, c.Name); err != nil {
			return fmt.Errorf("remove sftp container: %v", err)
		}
		r.logf("tenant %d: removed sftp back end", id)
	}
	for _, m := range mounts {
		if err := r.SFTPMounts.Unmount(p.ctx, m.Target); err != nil {
			return fmt.Errorf("unmount %s: %v", m.Share, err)
		}
	}
	if len(mounts) > 0 || w.t.State == api.TenantTerminated {
		os.Remove(sftp.TenantMountDir(r.sftpMountRoot(), id))
		os.RemoveAll(filepath.Join(p.tenantDir(id), "sftp"))
	}
	return nil
}

func (p *pass) sftpEnsure(w *tenantWork, shares []*shareWork, mounts []sftp.Mount, c sftp.Container) error {
	r := p.r
	id := w.t.ID
	entity := names.CephEntity(id)
	user, ok := p.users[entity]
	if !ok {
		return fmt.Errorf("cephx user missing")
	}
	conf, err := p.minimalConf()
	if err != nil {
		return err
	}
	secretDir := filepath.Join(p.tenantDir(id), "sftp")
	if err := os.MkdirAll(secretDir, 0o700); err != nil {
		return err
	}
	secretFile := filepath.Join(secretDir, "secret")
	confFile := filepath.Join(secretDir, "ceph.conf")
	if _, err := writeIfChanged(secretFile, []byte(user.Key), 0o600); err != nil {
		return err
	}
	if _, err := writeIfChanged(confFile, []byte(conf), 0o600); err != nil {
		return err
	}

	dir := sftp.TenantMountDir(r.sftpMountRoot(), id)
	want := map[string]sftp.MountSpec{}
	var binds []sftp.Bind
	for _, s := range shares {
		if s.info == nil {
			continue
		}
		spec := sftp.MountSpec{
			TenantID:   id,
			Share:      s.s.Name,
			Target:     filepath.Join(dir, s.s.Name),
			Path:       s.info.Path,
			Entity:     entity,
			SecretFile: secretFile,
			ConfFile:   confFile,
			FSName:     r.Host.CephFSName,
			ReadOnly:   s.s.SFTPMode() == api.SFTPReadOnly,
		}
		want[spec.Target] = spec
		binds = append(binds, sftp.Bind{Source: spec.Target, Target: "/" + s.s.Name, ReadOnly: spec.ReadOnly})
	}
	if len(binds) == 0 {
		return fmt.Errorf("no SFTP share is ready yet")
	}
	sort.Slice(binds, func(i, j int) bool { return binds[i].Target < binds[j].Target })
	h := sha256.New()
	h.Write([]byte(r.Host.SFTPImage))
	for _, b := range binds {
		fmt.Fprintf(h, "|%s|%s|%v", b.Source, b.Target, b.ReadOnly)
	}
	hash := hex.EncodeToString(h.Sum(nil))[:16]

	// The back end is replaced before its mounts change.
	if c.Name != "" && (c.MountsHash != hash || !c.Running) {
		r.SFTP.CloseTenant(id)
		if err := r.SFTPRuntime.Remove(p.ctx, c.Name); err != nil {
			return fmt.Errorf("remove sftp container: %v", err)
		}
		c = sftp.Container{}
	}
	have := map[string]sftp.Mount{}
	for _, m := range mounts {
		spec, ok := want[m.Target]
		if ok && spec.ReadOnly == m.ReadOnly {
			have[m.Target] = m
			continue
		}
		if c.Name != "" {
			r.SFTP.CloseTenant(id)
			if err := r.SFTPRuntime.Remove(p.ctx, c.Name); err != nil {
				return fmt.Errorf("remove sftp container: %v", err)
			}
			c = sftp.Container{}
		}
		if err := r.SFTPMounts.Unmount(p.ctx, m.Target); err != nil {
			return fmt.Errorf("unmount %s: %v", m.Share, err)
		}
	}
	targets := make([]string, 0, len(want))
	for t := range want {
		targets = append(targets, t)
	}
	sort.Strings(targets)
	for _, t := range targets {
		if _, ok := have[t]; ok {
			continue
		}
		if err := r.SFTPMounts.Mount(p.ctx, want[t]); err != nil {
			return fmt.Errorf("mount %s: %v", want[t].Share, err)
		}
	}
	if c.Name == "" {
		spec := sftp.ContainerSpec{
			Name:       sftp.ContainerName(id),
			TenantID:   id,
			Image:      r.Host.SFTPImage,
			MountsHash: hash,
			Binds:      binds,
		}
		if err := r.SFTPRuntime.Run(p.ctx, spec); err != nil {
			return fmt.Errorf("start sftp container: %v", err)
		}
		r.logf("tenant %d: started sftp back end with %d share(s)", id, len(binds))
	}
	return nil
}

// fillSFTPStatus adds the SFTP phase's results to the status report.
func (p *pass) fillSFTPStatus(st *api.Status) {
	sp := p.sftp
	if sp == nil {
		return
	}
	for i := range st.Tenants {
		if t, ok := sp.tenants[st.Tenants[i].ID]; ok {
			t := t
			st.Tenants[i].SFTP = &t
		}
	}
	st.Orphans = append(st.Orphans, sp.orphans...)
	r := p.r
	node := &api.SFTPStatus{Port: sp.port}
	if r.SFTP != nil {
		node.Listening = r.SFTP.Addr() != nil
		if key := r.SFTP.HostKey(); key != nil {
			node.HostKey = strings.TrimSpace(string(ssh.MarshalAuthorizedKey(key)))
			node.HostKeyFingerprint = ssh.FingerprintSHA256(key)
		}
	}
	if len(sp.errs) > 0 {
		msg := strings.Join(sp.errs, "; ")
		node.Error = &msg
	}
	if r.SFTP != nil || node.Error != nil {
		st.SFTP = node
	}
	for _, l := range sp.logins {
		st.SFTPLogins = append(st.SFTPLogins, api.SFTPLogin{UserID: l.UserID, At: l.At, IP: l.IP})
	}
}

// ackSFTP forgets logins Rackflow has accepted.
func (p *pass) ackSFTP() {
	if p.sftp != nil && p.r.SFTP != nil {
		p.r.SFTP.AckLogins(p.sftp.logins)
	}
}
