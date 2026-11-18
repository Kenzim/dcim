package reconcile

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"rackflow/storage_runner/internal/api"
	"rackflow/storage_runner/internal/sftp"
)

type nopBackend struct{}

func (nopBackend) Serve(ctx context.Context, _ int64, _ io.Reader, _, _ io.Writer) error {
	<-ctx.Done()
	return nil
}

type sftpEnv struct {
	*env
	gw     *sftp.Gateway
	mounts *sftp.FakeMounter
	rt     *sftp.FakeRuntime
	key    ssh.Signer
	port   int
}

func signer(t *testing.T) ssh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	s, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func newSFTPEnv(t *testing.T) *sftpEnv {
	e := &sftpEnv{env: newEnv(t), mounts: &sftp.FakeMounter{}, rt: &sftp.FakeRuntime{}, key: signer(t), port: freePort(t)}
	e.gw = sftp.NewGateway(signer(t), nopBackend{}, nil)
	t.Cleanup(e.gw.Close)
	e.r.Host.SFTPImage = "sftp:test"
	e.r.Host.SFTPListenAddr = "127.0.0.1"
	e.r.SFTP = e.gw
	e.r.SFTPMounts = e.mounts
	e.r.SFTPRuntime = e.rt
	e.r.SFTPMountRoot = "/run/rf-test"
	return e
}

func (e *sftpEnv) cfg(version string, tenants ...api.Tenant) *api.Config {
	c := cfgOf(version, tenants...)
	c.SFTP = &api.SFTPSettings{Enabled: true, Port: e.port, MaxAuthFailures: 50}
	return c
}

func (e *sftpEnv) user(tenantID int64) api.SFTPUser {
	line := strings.TrimSpace(string(ssh.MarshalAuthorizedKey(e.key.PublicKey())))
	return api.SFTPUser{ID: tenantID * 10, Username: "web." + itoa(tenantID), PublicKeys: []string{line}}
}

func itoa(n int64) string { b, _ := json.Marshal(n); return string(b) }

func (e *sftpEnv) login(tenantID int64) error {
	c, err := ssh.Dial("tcp", net.JoinHostPort("127.0.0.1", itoa(int64(e.port))), &ssh.ClientConfig{
		User:            "web." + itoa(tenantID),
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(e.key)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         5 * time.Second,
	})
	if err != nil {
		return err
	}
	return c.Close()
}

func sftpTenant(e *sftpEnv, id int64, vlan int, mode string) api.Tenant {
	s := share(id*10+5, "data", 100*gib, "10.0.0.5")
	s.SFTP = mode
	t := tenant(id, vlan, s)
	t.SFTPUsers = []api.SFTPUser{e.user(id)}
	return t
}

func sftpState(t *testing.T, st api.Status, id int64) api.TenantSFTP {
	t.Helper()
	ts := tenantStatus(t, st, id)
	if ts.SFTP == nil {
		t.Fatalf("tenant %d has no sftp status", id)
	}
	return *ts.SFTP
}

func TestOldConfigWithoutSFTPIsUnchanged(t *testing.T) {
	e := newSFTPEnv(t)
	expect(t, e.apply(cfgOf("v1", basic())), firstApply())
	st := e.status()
	if got := sftpState(t, st, 12).State; got != api.SFTPStateOff {
		t.Fatalf("sftp state %s", got)
	}
	if st.SFTP == nil || st.SFTP.Listening || st.SFTP.Error != nil {
		t.Fatalf("node sftp %+v", st.SFTP)
	}
	if len(e.mounts.Mounts) != 0 || len(e.rt.Containers) != 0 {
		t.Fatal("sftp resources created without sftp config")
	}
	b, _ := json.Marshal(st)
	if !strings.Contains(string(b), `"sftp_logins":[]`) {
		t.Fatalf("status json %s", b)
	}
}

func TestSFTPServesActiveTenantWithItsOwnKey(t *testing.T) {
	e := newSFTPEnv(t)
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	st := e.status()
	if got := sftpState(t, st, 12); got.State != api.SFTPStateReady {
		t.Fatalf("sftp %+v", got)
	}
	if !st.SFTP.Listening || !strings.HasPrefix(st.SFTP.HostKeyFingerprint, "SHA256:") || st.SFTP.Port != e.port {
		t.Fatalf("node sftp %+v", st.SFTP)
	}
	m, ok := e.mounts.Mounts["/run/rf-test/t12/data"]
	if !ok || m.ReadOnly || m.Entity != "client.rf-t12" {
		t.Fatalf("mounts %+v", e.mounts.Mounts)
	}
	secret, err := os.ReadFile(m.SecretFile)
	if err != nil {
		t.Fatal(err)
	}
	user, _ := e.ceph.User("client.rf-t12")
	if string(secret) != user.Key || strings.Contains(strings.Join(sftp.MountArgs(m), " "), user.Key) {
		t.Fatal("tenant key not in the secret file, or on the command line")
	}
	if fi, _ := os.Stat(m.SecretFile); fi.Mode().Perm() != 0o600 {
		t.Fatalf("secret mode %v", fi.Mode())
	}
	spec := e.rt.Specs["rf-sftp-t12"]
	if len(spec.Binds) != 1 || spec.Binds[0].Target != "/data" || spec.Binds[0].ReadOnly {
		t.Fatalf("container %+v", spec)
	}
	if err := e.login(12); err != nil {
		t.Fatalf("login: %v", err)
	}

	// The login is reported once, then acknowledged.
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	if logins := e.status().SFTPLogins; len(logins) != 1 || logins[0].UserID != 120 {
		t.Fatalf("logins %+v", logins)
	}
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	if logins := e.status().SFTPLogins; len(logins) != 0 {
		t.Fatalf("logins kept after acknowledgement %+v", logins)
	}
	if e.rt.Runs != 1 {
		t.Fatalf("container restarted %d times on reapply", e.rt.Runs)
	}
}

func TestSFTPModeChangeRecreatesTheBackEnd(t *testing.T) {
	e := newSFTPEnv(t)
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	e.apply(e.cfg("v2", sftpTenant(e, 12, 1234, api.SFTPReadOnly)))
	if e.rt.Runs != 2 || !e.rt.Specs["rf-sftp-t12"].Binds[0].ReadOnly {
		t.Fatalf("runs %d spec %+v", e.rt.Runs, e.rt.Specs["rf-sftp-t12"])
	}
	if !e.mounts.Mounts["/run/rf-test/t12/data"].ReadOnly {
		t.Fatal("read-only share mounted read-write")
	}
	e.apply(e.cfg("v3", sftpTenant(e, 12, 1234, api.SFTPOff)))
	if len(e.rt.Containers) != 0 || len(e.mounts.Mounts) != 0 {
		t.Fatal("sftp off left the back end running")
	}
	if got := sftpState(t, e.status(), 12).State; got != api.SFTPStateOff {
		t.Fatalf("state %s", got)
	}
}

func TestSuspendAndTerminateStopSFTP(t *testing.T) {
	e := newSFTPEnv(t)
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	if err := e.login(12); err != nil {
		t.Fatal(err)
	}
	suspended := sftpTenant(e, 12, 1234, api.SFTPReadWrite)
	suspended.State = api.TenantSuspended
	e.apply(e.cfg("v2", suspended))
	if len(e.rt.Containers) != 0 || len(e.mounts.Mounts) != 0 {
		t.Fatal("suspended tenant still has an sftp back end")
	}
	if e.login(12) == nil {
		t.Fatal("suspended tenant can still log in")
	}

	e.apply(e.cfg("v3", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	if err := e.login(12); err != nil {
		t.Fatalf("unsuspended login: %v", err)
	}

	gone := sftpTenant(e, 12, 1234, api.SFTPReadWrite)
	gone.State = api.TenantTerminated
	gone.TerminatedAt = ts(e.now)
	e.apply(e.cfg("v4", gone))
	if len(e.rt.Containers) != 0 || len(e.mounts.Mounts) != 0 || e.login(12) == nil {
		t.Fatal("terminated tenant still served")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "tenants", "12", "sftp")); !os.IsNotExist(err) {
		t.Fatalf("sftp state left behind: %v", err)
	}
}

func TestOneTenantsSFTPFailureLeavesOthersAndNFSAlone(t *testing.T) {
	e := newSFTPEnv(t)
	e.rt.FailRun = map[int64]error{12: errors.New("image pull failed")}
	b := sftpTenant(e, 13, 1235, api.SFTPReadWrite)
	b.NetworkCIDR, b.EndpointIP = "10.0.1.0/24", "10.0.1.254"
	b.Shares[0].Clients = []string{"10.0.1.5"}
	e.apply(e.cfg("v1", sftpTenant(e, 12, 1234, api.SFTPReadWrite), b))
	st := e.status()
	if got := sftpState(t, st, 12); got.State != api.SFTPStateError || got.Error == nil {
		t.Fatalf("tenant 12 sftp %+v", got)
	}
	if got := sftpState(t, st, 13).State; got != api.SFTPStateReady {
		t.Fatalf("tenant 13 sftp %s", got)
	}
	if ts := tenantStatus(t, st, 12); ts.State != api.StateReady {
		t.Fatalf("tenant 12 nfs state %s", ts.State)
	}
	if e.login(12) == nil {
		t.Fatal("login served without a back end")
	}
	if err := e.login(13); err != nil {
		t.Fatalf("tenant 13 login: %v", err)
	}
}

func TestSFTPNeedsImageAndUsers(t *testing.T) {
	e := newSFTPEnv(t)
	noUsers := sftpTenant(e, 12, 1234, api.SFTPReadWrite)
	noUsers.SFTPUsers = nil
	e.apply(e.cfg("v1", noUsers))
	if len(e.rt.Containers) != 0 || sftpState(t, e.status(), 12).State != api.SFTPStateOff {
		t.Fatal("back end started without users")
	}
	e.r.Host.SFTPImage = ""
	e.apply(e.cfg("v2", sftpTenant(e, 12, 1234, api.SFTPReadWrite)))
	if st := e.status(); st.SFTP.Error == nil || !strings.Contains(*st.SFTP.Error, "SFTP_IMAGE") {
		t.Fatalf("node sftp %+v", st.SFTP)
	}
}

func TestSFTPOrphansAreReportedNotRemoved(t *testing.T) {
	e := newSFTPEnv(t)
	e.rt.Run(context.Background(), sftp.ContainerSpec{Name: "rf-sftp-t99", TenantID: 99})
	e.mounts.Mount(context.Background(), sftp.MountSpec{TenantID: 99, Share: "x", Target: "/run/rf-test/t99/x"})
	e.apply(e.cfg("v1", basic()))
	orphans := strings.Join(e.status().Orphans, "|")
	if !strings.Contains(orphans, "sftp container rf-sftp-t99") || !strings.Contains(orphans, "sftp mount /run/rf-test/t99/x") {
		t.Fatalf("orphans %s", orphans)
	}
	if _, ok := e.rt.Containers["rf-sftp-t99"]; !ok || len(e.mounts.Mounts) != 1 {
		t.Fatal("orphan removed")
	}
}
