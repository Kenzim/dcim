package reconcile

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/crypto/ssh"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/lease"
	"rackflow/storage_runner/internal/names"
)

// HostKeyFile is the SFTP host key's file name under STATE_DIR.
const HostKeyFile = "sftp_host_ed25519_key"

// sftpMode is how this pass serves SFTP.
type sftpMode int

const (
	// sftpPerNode serves the tenants of this node on its own address (no ha
	// block, or no virtual IP).
	sftpPerNode sftpMode = iota
	// sftpMaster serves every tenant on the location's virtual IP.
	sftpMaster
	// sftpStandby serves nothing: another node is the SFTP master.
	sftpStandby
)

// sftpVIP returns the location's SFTP virtual IP, if the config has one.
func sftpVIP(cfg *api.Config) (netip.Prefix, bool, error) {
	if cfg.HA == nil || cfg.SFTP == nil || !cfg.SFTP.Enabled || cfg.SFTP.VirtualIP == nil || *cfg.SFTP.VirtualIP == "" {
		return netip.Prefix{}, false, nil
	}
	pfx, err := netip.ParsePrefix(*cfg.SFTP.VirtualIP)
	if err != nil || !pfx.Addr().Is4() {
		return netip.Prefix{}, false, fmt.Errorf("sftp.virtual_ip %q is not an IPv4 address with prefix", *cfg.SFTP.VirtualIP)
	}
	if cfg.SFTP.LocationID <= 0 {
		return netip.Prefix{}, false, fmt.Errorf("sftp.location_id is not set")
	}
	return pfx, true, nil
}

func (r *Reconciler) vipInterface(ctx context.Context) (string, error) {
	if r.Host.SFTPVIPInterface != "" {
		return r.Host.SFTPVIPInterface, nil
	}
	if r.vipIface != "" {
		return r.vipIface, nil
	}
	if r.VIP == nil {
		return "", fmt.Errorf("this runner was started without virtual IP support")
	}
	iface, err := r.VIP.DefaultInterface(ctx)
	if err != nil {
		return "", fmt.Errorf("SFTP_VIP_INTERFACE is not set and the default route is unknown: %w", err)
	}
	r.vipIface = iface
	return iface, nil
}

// sftpWant is the SFTP master lease of the location, when SFTP runs on a
// virtual IP. A node that may not be master releases the lease.
func (p *pass) sftpWant(cfg *api.Config) (lease.Want, bool) {
	vip, ok, err := sftpVIP(cfg)
	if err != nil {
		p.nodeErr("sftp: %v", err)
		return lease.Want{}, false
	}
	if !ok {
		return lease.Want{}, false
	}
	s := cfg.SFTP
	w := lease.Want{Key: lease.SFTPKey(s.LocationID), Rank: -1}
	iface, err := p.r.vipInterface(p.ctx)
	if err != nil {
		p.nodeErr("sftp: %v", err)
		w.Release = true
		return w, true
	}
	w.Guard = lease.GuardInfo{VIP: vip.String(), VIPInterface: iface}
	if s.MasterEligible {
		w.Rank = indexOf(s.MasterOrder, cfg.Node.ID)
		w.Acquire = w.Rank >= 0
	}
	w.Release = !w.Acquire
	// A new master evicts the old master's kernel mounts: the sessions of
	// every tenant user from the old node's host name.
	for _, t := range cfg.Tenants {
		if t.ID > 0 {
			w.FenceEntities = append(w.FenceEntities, names.CephEntity(t.ID))
		}
	}
	return w, true
}

// sftpModeFor decides how this pass serves SFTP.
func (p *pass) sftpModeFor(cfg *api.Config) sftpMode {
	if p.ha == nil {
		return sftpPerNode
	}
	if _, ok, _ := sftpVIP(cfg); !ok {
		return sftpPerNode
	}
	if p.r.Lease.Holds(lease.SFTPKey(cfg.SFTP.LocationID)) {
		return sftpMaster
	}
	return sftpStandby
}

// sftpServes says whether this pass may serve a tenant's SFTP.
func (p *pass) sftpServes(mode sftpMode, w *tenantWork) bool {
	switch mode {
	case sftpMaster:
		return true
	case sftpStandby:
		return false
	}
	return w.held
}

// sftpListenAddr is where the gateway listens in this pass, empty for not
// at all.
func (p *pass) sftpListenAddr(cfg *api.Config, mode sftpMode, port int) string {
	switch mode {
	case sftpStandby:
		return ""
	case sftpMaster:
		vip, _, _ := sftpVIP(cfg)
		return net.JoinHostPort(vip.Addr().String(), strconv.Itoa(port))
	}
	host := p.r.Host.SFTPListenAddr
	if host == "" {
		host = "0.0.0.0"
	}
	return net.JoinHostPort(host, strconv.Itoa(port))
}

// ensureVIP puts the virtual IP on its interface and announces it, while
// this node is the SFTP master.
func (p *pass) ensureVIP(cfg *api.Config) error {
	r := p.r
	vip, _, _ := sftpVIP(cfg)
	iface, err := r.vipInterface(p.ctx)
	if err != nil {
		return err
	}
	return p.whileHolding(lease.SFTPKey(cfg.SFTP.LocationID), func(ctx context.Context) error {
		has, err := r.VIP.Has(ctx, iface, vip)
		if err != nil {
			return err
		}
		if has {
			return nil
		}
		if err := r.VIP.Add(ctx, iface, vip); err != nil {
			return err
		}
		r.logf("sftp: virtual ip %s added on %s", vip, iface)
		if err := r.VIP.Announce(ctx, iface, vip.Addr()); err != nil {
			r.logf("sftp: gratuitous arp for %s: %v", vip.Addr(), err)
		}
		return nil
	})
}

// removeVIP takes the virtual IP off its interface when this node is not
// the SFTP master.
func (r *Reconciler) removeVIP(ctx context.Context, cfg *api.Config) {
	if r.VIP == nil || cfg == nil {
		return
	}
	vip, ok, _ := sftpVIP(cfg)
	if !ok {
		return
	}
	iface, err := r.vipInterface(ctx)
	if err != nil {
		return
	}
	has, err := r.VIP.Has(ctx, iface, vip)
	if err != nil || !has {
		return
	}
	if err := r.VIP.Remove(ctx, iface, vip); err != nil {
		r.logf("sftp: remove virtual ip %s: %v", vip, err)
		return
	}
	r.logf("sftp: virtual ip %s removed from %s", vip, iface)
}

// stopSFTPMaster stops serving SFTP as master: the gateway stops listening,
// the virtual IP goes, and every back end and mount is removed.
func (r *Reconciler) stopSFTPMaster(ctx context.Context, g lease.GuardInfo) {
	if r.SFTP != nil {
		if err := r.SFTP.Listen(""); err != nil {
			r.logf("sftp: stop gateway: %v", err)
		}
	}
	if vip, err := netip.ParsePrefix(g.VIP); err == nil && g.VIPInterface != "" && r.VIP != nil {
		if err := r.VIP.Remove(ctx, g.VIPInterface, vip); err != nil {
			r.logf("sftp: remove virtual ip %s: %v", vip, err)
		} else {
			r.logf("sftp: virtual ip %s removed from %s", vip, g.VIPInterface)
		}
	}
	if r.SFTPRuntime != nil {
		if ctrs, err := r.SFTPRuntime.List(ctx); err == nil {
			for _, c := range ctrs {
				if r.SFTP != nil {
					r.SFTP.CloseTenant(c.TenantID)
				}
				if err := r.SFTPRuntime.Remove(ctx, c.Name); err != nil {
					r.logf("sftp: remove %s: %v", c.Name, err)
				}
			}
		} else {
			r.logf("sftp: list back ends: %v", err)
		}
	}
	if r.SFTPMounts != nil {
		if mounts, err := r.SFTPMounts.List(ctx); err == nil {
			for _, m := range mounts {
				if err := r.SFTPMounts.Unmount(ctx, m.Target); err != nil {
					r.logf("sftp: unmount %s: %v", m.Target, err)
				}
			}
		} else {
			r.logf("sftp: list mounts: %v", err)
		}
	}
	r.logf("sftp: master role ended; gateway, virtual ip and back ends stopped")
}

// applyHostKey switches the gateway to the location's host key when the
// config carries one, so every master presents the same key. Without one
// the node keeps its own key.
func (p *pass) applyHostKey(cfg *api.Config) {
	r := p.r
	if r.SFTP == nil || cfg.SFTP == nil || strings.TrimSpace(cfg.SFTP.HostKey) == "" {
		return
	}
	pem := []byte(strings.TrimSpace(cfg.SFTP.HostKey) + "\n")
	signer, err := ssh.ParsePrivateKey(pem)
	if err != nil {
		p.sftp.errs = append(p.sftp.errs, fmt.Sprintf("sftp.host_key: %v", err))
		return
	}
	if cur := r.SFTP.HostKey(); cur != nil && bytes.Equal(cur.Marshal(), signer.PublicKey().Marshal()) {
		return
	}
	path := filepath.Join(r.Host.StateDir, HostKeyFile)
	if err := os.MkdirAll(r.Host.StateDir, 0o700); err != nil {
		p.sftp.errs = append(p.sftp.errs, fmt.Sprintf("sftp host key: %v", err))
		return
	}
	if _, err := writeIfChanged(path, pem, 0o600); err != nil {
		p.sftp.errs = append(p.sftp.errs, fmt.Sprintf("sftp host key: %v", err))
		return
	}
	r.SFTP.SetHostKey(signer)
	r.logf("sftp: using the location host key %s", ssh.FingerprintSHA256(signer.PublicKey()))
}
