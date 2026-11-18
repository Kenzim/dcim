package sftp

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/api"
)

func TestHostKeyIsCreatedOnceWithMode0600(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state", "sftp_host_ed25519_key")
	a, err := LoadOrCreateHostKey(path)
	if err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(path)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("key file %v %v", fi, err)
	}
	b, err := LoadOrCreateHostKey(path)
	if err != nil || HostKeyLine(a) != HostKeyLine(b) || !strings.HasPrefix(HostKeyLine(a), "ssh-ed25519 ") {
		t.Fatalf("reloaded key differs: %v", err)
	}
}

func TestMountArgsKeepTheSecretOffTheCommandLine(t *testing.T) {
	args := strings.Join(MountArgs(MountSpec{
		Target: "/run/rackflow-storage/sftp/t12/data", Path: "/volumes/rackflow/t12-s5/abc",
		Entity: "client.rf-t12", SecretFile: "/var/lib/x/secret", ConfFile: "/var/lib/x/ceph.conf",
		FSName: "cephfs", ReadOnly: true,
	}), " ")
	want := "-t ceph :/volumes/rackflow/t12-s5/abc /run/rackflow-storage/sftp/t12/data -o name=rf-t12,secretfile=/var/lib/x/secret,conf=/var/lib/x/ceph.conf,mds_namespace=cephfs,nosuid,nodev,noexec,ro"
	if args != want {
		t.Fatalf("got  %s\nwant %s", args, want)
	}
}

func TestParseMountsOnlyTakesCephUnderTheRoot(t *testing.T) {
	tab := strings.Join([]string{
		"10.0.0.1:6789:/volumes/a /run/rackflow-storage/sftp/t12/data ceph rw,relatime,name=rf-t12 0 0",
		"10.0.0.1:6789:/volumes/b /run/rackflow-storage/sftp/t12/logs ceph ro,relatime 0 0",
		"10.0.0.1:6789:/volumes/c /mnt/other ceph rw 0 0",
		"tmpfs /run/rackflow-storage/sftp/t13/x tmpfs rw 0 0",
	}, "\n")
	got, err := parseMounts(strings.NewReader(tab), MountRoot)
	if err != nil || len(got) != 2 {
		t.Fatalf("%+v %v", got, err)
	}
	if got[0].TenantID != 12 || got[0].Share != "data" || got[0].ReadOnly || !got[1].ReadOnly {
		t.Fatalf("%+v", got)
	}
}

func TestBackEndContainerIsLockedDown(t *testing.T) {
	args := strings.Join(RunArgs(ContainerSpec{
		Name: "rf-sftp-t12", TenantID: 12, Image: "sftp:1", MountsHash: "h",
		Binds: []Bind{{Source: "/run/rackflow-storage/sftp/t12/data", Target: "/data", ReadOnly: true}},
	}), " ")
	for _, part := range []string{
		"--network none", "--read-only", "--cap-drop all", "--security-opt no-new-privileges",
		"--user 65534:65534", "-v /run/rackflow-storage/sftp/t12/data:/data:ro", "sftp:1 infinity",
	} {
		if !strings.Contains(args, part) {
			t.Fatalf("missing %q in %s", part, args)
		}
	}
	if id, ok := ParseContainerName("rf-sftp-t12"); !ok || id != 12 {
		t.Fatal("container name not parsed")
	}
	for _, bad := range []string{"rf-t12", "rf-sftp-t012", "rf-sftp-tx"} {
		if _, ok := ParseContainerName(bad); ok {
			t.Fatalf("%s parsed", bad)
		}
	}
	exec := strings.Join((&PodmanBackend{}).ExecArgs(12), " ")
	if exec != "exec -i --user 65534:65534 rf-sftp-t12 /usr/lib/ssh/sftp-server -e -l INFO -d /" {
		t.Fatalf("exec %s", exec)
	}
}

func TestContractFieldsAreOptional(t *testing.T) {
	var old api.Config
	if err := json.Unmarshal([]byte(`{"version":"v","node":{"id":1,"name":"n"},"settings":{},"tenants":[{"id":1,"state":"active","shares":[{"id":2,"name":"d","state":"active"}]}]}`), &old); err != nil {
		t.Fatal(err)
	}
	if old.SFTP != nil || old.Tenants[0].SFTPUsers != nil || old.Tenants[0].Shares[0].SFTPMode() != api.SFTPOff {
		t.Fatalf("old config %+v", old)
	}
	var cfg api.Config
	if err := json.Unmarshal([]byte(`{"sftp":{"enabled":true,"port":2222,"max_auth_failures":10,"failure_window_seconds":600,"ban_seconds":900},"tenants":[{"id":1,"state":"active","sftp_users":[{"id":7,"username":"web.65","public_keys":["k"],"password_hash":"$2b$12$x","allowed_ips":["203.0.113.0/24"]}],"shares":[{"id":2,"name":"d","state":"active","sftp":"ro"}]}]}`), &cfg); err != nil {
		t.Fatal(err)
	}
	u := cfg.Tenants[0].SFTPUsers[0]
	if !cfg.SFTP.Enabled || cfg.SFTP.BanSeconds != 900 || u.Username != "web.65" || *u.PasswordHash != "$2b$12$x" || cfg.Tenants[0].Shares[0].SFTPMode() != api.SFTPReadOnly {
		t.Fatalf("new config %+v", cfg)
	}
}
