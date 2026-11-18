package ganesha

import (
	"context"
	"net/netip"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
)

func TestRender(t *testing.T) {
	core, exports := Render(Config{
		TenantID:   12,
		BindAddr:   netip.MustParseAddr("10.0.0.254"),
		UserID:     "rf-t12",
		Filesystem: "cephfs",
		Exports: []Export{
			{ID: 6, Path: "/volumes/rackflow/t12-s6/b", Pseudo: "/logs", ReadOnly: true, Clients: []string{"10.0.0.6"}},
			{ID: 5, Path: "/volumes/rackflow/t12-s5/a", Pseudo: "/data", Clients: []string{"10.0.0.5", "10.0.0.32/28"}},
			{ID: 7, Path: "/volumes/rackflow/t12-s7/c", Pseudo: "/closed"},
		},
	})
	for _, want := range []string{"Protocols = 4;", "Bind_addr = 10.0.0.254;", "Enable_NLM = false;", "Access_Type = None;", "Squash = root_squash;"} {
		if !strings.Contains(core, want) {
			t.Errorf("core lacks %q", want)
		}
	}
	if strings.Contains(core, "EXPORT {") {
		t.Error("exports in core part")
	}
	want := `
EXPORT {
	Export_Id = 5;
	Path = "/volumes/rackflow/t12-s5/a";
	Pseudo = "/data";
	Protocols = 4;
	Transports = TCP;
	Access_Type = None;
	Squash = root_squash;
	SecType = sys;
	FSAL {
		Name = CEPH;
		User_Id = "rf-t12";
		Filesystem = "cephfs";
	}
	CLIENT {
		Clients = 10.0.0.5, 10.0.0.32/28;
		Access_Type = RW;
		Squash = root_squash;
	}
}
`
	if !strings.HasPrefix(exports, want) {
		t.Fatalf("first export:\n%s", exports)
	}
	if !strings.Contains(exports, "Access_Type = RO;") {
		t.Error("read-only share not RO")
	}
	closed := exports[strings.Index(exports, "Export_Id = 7;"):]
	if strings.Contains(closed, "CLIENT") {
		t.Error("export without clients has a CLIENT block")
	}
}

func TestPodmanRunArgs(t *testing.T) {
	s := execx.NewScript()
	p := &Podman{Cmd: s}
	err := p.Run(context.Background(), Spec{
		Name: "rf-t12-nfs", TenantID: 12, Image: "quay.io/ceph/ceph:v18", Netns: "/run/netns/rf-t12", CoreHash: "abc",
		Mounts: []Mount{
			{Source: "/var/lib/r/tenants/t12/ganesha", Target: ConfDir, ReadOnly: true},
			{Source: "/var/lib/r/tenants/t12/recovery", Target: RecoveryDir},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := "podman run -d --name rf-t12-nfs --network ns:/run/netns/rf-t12 --restart on-failure" +
		" --label rackflow.role=storage-nfs --label rackflow.tenant=12 --label rackflow.core-hash=abc" +
		" -v /var/lib/r/tenants/t12/ganesha:/etc/ganesha:ro -v /var/lib/r/tenants/t12/recovery:/var/lib/nfs/ganesha" +
		" --entrypoint /usr/bin/ganesha.nfsd quay.io/ceph/ceph:v18 -F -L STDERR -f /etc/ganesha/ganesha.conf"
	if got := s.Lines(); len(got) != 1 || got[0] != want {
		t.Fatalf("got %v\nwant %s", got, want)
	}
}

func TestPodmanListRemoveReload(t *testing.T) {
	s := execx.NewScript().On("podman --remote --url unix:///run/podman/podman.sock ps -a --filter label=rackflow.role=storage-nfs --format json", `[
		{"Names":["rf-t12-nfs"],"State":"running","Labels":{"rackflow.role":"storage-nfs","rackflow.tenant":"12","rackflow.core-hash":"h1"}},
		{"Names":["rf-t13-nfs"],"State":"exited","Labels":{"rackflow.role":"storage-nfs","rackflow.tenant":"13"}},
		{"Names":["someone-else"],"State":"running","Labels":{"rackflow.role":"storage-nfs","rackflow.tenant":"14"}},
		{"Names":["rf-t15-nfs"],"State":"running","Labels":{"rackflow.role":"storage-nfs","rackflow.tenant":"99"}}]`)
	p := &Podman{Cmd: s, RemoteURL: "unix:///run/podman/podman.sock"}
	got, err := p.List(context.Background())
	if err != nil || len(got) != 2 {
		t.Fatalf("got %+v err %v", got, err)
	}
	if got[0] != (Container{Name: "rf-t12-nfs", TenantID: 12, Running: true, CoreHash: "h1"}) || got[1].Running {
		t.Fatalf("got %+v", got)
	}
	s.Reset()
	_ = p.Remove(context.Background(), "rf-t12-nfs")
	_ = p.Reload(context.Background(), "rf-t12-nfs")
	want := []string{
		"podman --remote --url unix:///run/podman/podman.sock rm -f rf-t12-nfs",
		"podman --remote --url unix:///run/podman/podman.sock kill --signal HUP rf-t12-nfs",
	}
	if strings.Join(s.Lines(), "\n") != strings.Join(want, "\n") {
		t.Fatalf("got %v", s.Lines())
	}
}

func TestPodmanListEmpty(t *testing.T) {
	s := execx.NewScript().On("podman ps -a --filter label=rackflow.role=storage-nfs --format json", "[]\n")
	got, err := (&Podman{Cmd: s}).List(context.Background())
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v err %v", got, err)
	}
}
