package ceph

import (
	"context"
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/names"
)

const pre = "ceph --conf /etc/ceph/ceph.conf --keyring /etc/ceph/ceph.client.admin.keyring "

func cli(s *execx.Script) *CLI {
	return &CLI{Cmd: s, FS: "cephfs", SubvolumeGroup: "rackflow", ECPool: "ec", Conf: "/etc/ceph/ceph.conf", Keyring: "/etc/ceph/ceph.client.admin.keyring"}
}

func healthy() *execx.Script {
	return execx.NewScript().
		On(pre+"osd pool get ec allow_ec_overwrites --format json", `{"pool":"ec","pool_id":3,"allow_ec_overwrites":true}`).
		On(pre+"fs ls --format json", `[{"name":"cephfs","metadata_pool":"cephfs_metadata","data_pools":["cephfs_data","ec"]}]`).
		On(pre+"df detail --format json", `{"pools":[{"name":"cephfs_data","stats":{"max_avail":1}},{"name":"ec","stats":{"max_avail":5000,"stored":100,"quota_bytes":0}}]}`)
}

func TestNamedEntity(t *testing.T) {
	named := "ceph --conf /etc/ceph/ceph.conf --name client.rackflow-dev-runner --keyring /etc/ceph/dev.keyring "
	s := execx.NewScript().
		On(named+"osd pool get ec allow_ec_overwrites --format json", `{"allow_ec_overwrites":true}`).
		On(named+"fs ls --format json", `[{"name":"cephfs","data_pools":["ec"]}]`).
		On(named+"df detail --format json", `{"pools":[{"name":"ec","stats":{"max_avail":7,"stored":0,"quota_bytes":0}}]}`)
	c := &CLI{Cmd: s, FS: "cephfs", SubvolumeGroup: "rackflow", ECPool: "ec", Conf: "/etc/ceph/ceph.conf",
		Keyring: "/etc/ceph/dev.keyring", Name: "client.rackflow-dev-runner"}
	if free, err := c.Check(context.Background()); err != nil || free != 7 {
		t.Fatalf("free=%d err=%v", free, err)
	}
}

func TestCheckPoolQuota(t *testing.T) {
	cases := map[string]struct {
		stats string
		want  int64
	}{
		"quota below max_avail": {`{"max_avail":5000,"stored":400,"quota_bytes":1000}`, 600},
		"quota above max_avail": {`{"max_avail":5000,"stored":400,"quota_bytes":100000}`, 5000},
		"quota used up":         {`{"max_avail":5000,"stored":1200,"quota_bytes":1000}`, 0},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			s := healthy().On(pre+"df detail --format json", `{"pools":[{"name":"ec","stats":`+c.stats+`}]}`)
			free, err := cli(s).Check(context.Background())
			if err != nil || free != c.want {
				t.Fatalf("free=%d err=%v, want %d", free, err, c.want)
			}
		})
	}
}

func TestCheckHealthy(t *testing.T) {
	free, err := cli(healthy()).Check(context.Background())
	if err != nil || free != 5000 {
		t.Fatalf("free=%d err=%v", free, err)
	}
}

func TestCheckFailures(t *testing.T) {
	cases := map[string]struct {
		line, out string
		fail      bool
		want      string
	}{
		"pool missing":       {line: pre + "osd pool get ec allow_ec_overwrites --format json", fail: true, want: "missing or not erasure-coded"},
		"overwrites off":     {line: pre + "osd pool get ec allow_ec_overwrites --format json", out: `{"allow_ec_overwrites":false}`, want: "allow_ec_overwrites"},
		"not a data pool":    {line: pre + "fs ls --format json", out: `[{"name":"cephfs","data_pools":["cephfs_data"]}]`, want: "not a data pool"},
		"file system absent": {line: pre + "fs ls --format json", out: `[{"name":"other","data_pools":["ec"]}]`, want: "not found"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			s := healthy()
			if c.fail {
				s.Fail(c.line, errors.New("ENOENT"))
			} else {
				s.On(c.line, c.out)
			}
			_, err := cli(s).Check(context.Background())
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("err %v, want %q", err, c.want)
			}
		})
	}
}

func TestGroup(t *testing.T) {
	s := execx.NewScript().
		On(pre+"fs subvolumegroup ls cephfs --format json", `[{"name":"csi"},{"name":"rackflow"}]`).
		On(pre+"fs subvolumegroup info cephfs rackflow --format json", `{"data_pool":"ec","mode":"755"}`)
	ok, pool, err := cli(s).Group(context.Background())
	if err != nil || !ok || pool != "ec" {
		t.Fatalf("ok=%v pool=%q err=%v", ok, pool, err)
	}
	s = execx.NewScript().On(pre+"fs subvolumegroup ls cephfs --format json", `[]`)
	ok, _, err = cli(s).Group(context.Background())
	if err != nil || ok {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
}

func TestMutatingCommands(t *testing.T) {
	s := execx.NewScript().
		On(pre+"auth get-or-create client.rf-t12 mon \"allow r\" mds \"allow rw path=/volumes/rackflow/t12-s5/u\" osd \"allow rw pool=ec namespace=fsvolumens_t12-s5\" --format json",
			`[{"entity":"client.rf-t12","key":"AQB=="}]`)
	c := cli(s)
	ctx := context.Background()
	caps := TenantCaps([]Access{{Path: "/volumes/rackflow/t12-s5/u", Pool: "ec", Namespace: "fsvolumens_t12-s5"}})
	if err := c.CreateGroup(ctx); err != nil {
		t.Fatal(err)
	}
	if err := c.CreateSubvolume(ctx, "t12-s5", 1073741824); err != nil {
		t.Fatal(err)
	}
	if err := c.ResizeSubvolume(ctx, "t12-s5", 2147483648); err != nil {
		t.Fatal(err)
	}
	key, err := c.CreateUser(ctx, "client.rf-t12", caps)
	if err != nil || key != "AQB==" {
		t.Fatalf("key=%q err=%v", key, err)
	}
	if err := c.SetCaps(ctx, "client.rf-t12", TenantCaps(nil)); err != nil {
		t.Fatal(err)
	}
	if err := c.RemoveSubvolume(ctx, "t12-s5"); err != nil {
		t.Fatal(err)
	}
	if err := c.RemoveUser(ctx, "client.rf-t12"); err != nil {
		t.Fatal(err)
	}
	want := []string{
		pre + "fs subvolumegroup create cephfs rackflow --pool_layout ec",
		pre + "fs subvolume create cephfs t12-s5 --size 1073741824 --group_name rackflow --pool_layout ec --mode 777 --namespace-isolated",
		pre + "fs subvolume resize cephfs t12-s5 2147483648 --group_name rackflow",
		pre + "auth get-or-create client.rf-t12 mon \"allow r\" mds \"allow rw path=/volumes/rackflow/t12-s5/u\" osd \"allow rw pool=ec namespace=fsvolumens_t12-s5\" --format json",
		pre + "auth caps client.rf-t12 mon \"allow r\"",
		pre + "fs subvolume rm cephfs t12-s5 --group_name rackflow",
		pre + "auth rm client.rf-t12",
	}
	if got := s.Lines(); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("commands\n got:\n  %s\nwant:\n  %s", strings.Join(got, "\n  "), strings.Join(want, "\n  "))
	}
}

func TestSubvolumeInfo(t *testing.T) {
	s := execx.NewScript().
		On("ceph fs subvolume info cephfs t12-s5 --group_name rackflow --format json",
			`{"path":"/volumes/rackflow/t12-s5/abc","data_pool":"ec","pool_namespace":"fsvolumens_t12-s5","bytes_used":42,"bytes_quota":1073741824}`).
		On("ceph fs subvolume info cephfs t12-s6 --group_name rackflow --format json",
			`{"path":"/volumes/rackflow/t12-s6/abc","data_pool":"ec","bytes_used":0,"bytes_quota":"infinite"}`)
	c := &CLI{Cmd: s, FS: "cephfs", SubvolumeGroup: "rackflow", ECPool: "ec"}
	info, err := c.SubvolumeInfo(context.Background(), "t12-s5")
	if err != nil || info.BytesUsed != 42 || info.BytesQuota != 1073741824 || info.PoolNamespace != "fsvolumens_t12-s5" {
		t.Fatalf("info=%+v err=%v", info, err)
	}
	info, err = c.SubvolumeInfo(context.Background(), "t12-s6")
	if err != nil || info.BytesQuota != 0 {
		t.Fatalf("infinite quota: info=%+v err=%v", info, err)
	}
}

func TestUsersOnlyRunnerOwned(t *testing.T) {
	s := execx.NewScript().On("ceph auth ls --format json", `{"auth_dump":[
		{"entity":"client.admin","key":"A","caps":{"mon":"allow *"}},
		{"entity":"client.rf-t12","key":"B","caps":{"mon":"allow r","mds":"allow rw path=/x","osd":"allow rw pool=ec"}},
		{"entity":"osd.0","key":"C","caps":{}}]}`)
	users, err := (&CLI{Cmd: s}).Users(context.Background(), nil)
	if err != nil || len(users) != 1 || users["client.rf-t12"].Caps.MDS != "allow rw path=/x" {
		t.Fatalf("users=%+v err=%v", users, err)
	}
}

func TestUsersWithPrefixSkipOtherInstallation(t *testing.T) {
	names.SetCephUserPrefix("rfdev-t")
	defer names.SetCephUserPrefix(names.DefaultCephUserPrefix)
	s := execx.NewScript().On("ceph auth ls --format json", `{"auth_dump":[
		{"entity":"client.rf-t12","key":"B","caps":{"mon":"allow r"}},
		{"entity":"client.rfdev-t12","key":"D","caps":{"mon":"allow r"}}]}`)
	users, err := (&CLI{Cmd: s}).Users(context.Background(), nil)
	if err != nil || len(users) != 1 || users["client.rfdev-t12"].Key != "D" {
		t.Fatalf("users=%+v err=%v", users, err)
	}
}

func TestScopedUsersReadEachEntity(t *testing.T) {
	s := execx.NewScript().
		On(pre+"auth get client.rf-t12 --format json", `[{"entity":"client.rf-t12","key":"B","caps":{"mon":"allow r","mds":"allow rw path=/x"}}]`).
		Fail(pre+"auth get client.rf-t13 --format json", errors.New("ceph auth get client.rf-t13: Error ENOENT: failed to find client.rf-t13 in keyring"))
	c := cli(s)
	c.Scoped = true
	users, err := c.Users(context.Background(), []string{"client.rf-t12", "client.admin", "client.rf-t13", "client.rf-t12"})
	if err != nil || len(users) != 1 || users["client.rf-t12"].Key != "B" || users["client.rf-t12"].Caps.MDS != "allow rw path=/x" {
		t.Fatalf("users=%+v err=%v", users, err)
	}
	for _, l := range s.Lines() {
		if strings.Contains(l, "auth ls") || strings.Contains(l, "auth get client.admin") {
			t.Fatalf("scoped key ran %q", l)
		}
	}
	if n := len(s.Lines()); n != 2 {
		t.Fatalf("calls %v", s.Lines())
	}

	s = execx.NewScript().Fail(pre+"auth get client.rf-t12 --format json", errors.New("Error EACCES: access denied"))
	c = cli(s)
	c.Scoped = true
	if _, err := c.Users(context.Background(), []string{"client.rf-t12"}); err == nil {
		t.Fatal("EACCES taken for a missing user")
	}
}

func TestScopedAuthorizeAndNoEvictions(t *testing.T) {
	s := execx.NewScript().
		On(pre+"fs subvolume authorize cephfs t12-s5 rf-t12 --group_name rackflow --access_level rw --allow_existing_id", "").
		On(pre+"fs subvolume deauthorize cephfs t12-s5 rf-t12 --group_name rackflow", "")
	c := cli(s)
	c.Scoped = true
	ctx := context.Background()
	if err := c.Authorize(ctx, "client.rf-t12", "t12-s5"); err != nil {
		t.Fatal(err)
	}
	if err := c.Deauthorize(ctx, "client.rf-t12", "t12-s5"); err != nil {
		t.Fatal(err)
	}
	if err := c.EvictSessions(ctx, "client.rf-t12", "rf-t12-nfs"); err != nil {
		t.Fatal(err)
	}
	if n, err := c.EvictAddrSessions(ctx, netip.MustParseAddr("10.0.0.5")); n != 0 || err != nil {
		t.Fatalf("n=%d err=%v", n, err)
	}
	if n := len(s.Lines()); n != 2 {
		t.Fatalf("calls %v", s.Lines())
	}
}

func TestAuthorizedPathsAndReadKeyring(t *testing.T) {
	got := AuthorizedPaths("allow rw path=/volumes/rackflow/t3-s1/a,allow rw path=/volumes/rackflow/t3-s2/b, allow r")
	if strings.Join(got, " ") != "/volumes/rackflow/t3-s1/a /volumes/rackflow/t3-s2/b" {
		t.Fatalf("paths %v", got)
	}
	path := filepath.Join(t.TempDir(), "k")
	if err := os.WriteFile(path, []byte("[client.a]\n\tkey = AAA\n[client.rec]\n\tkey = BBB==\n\tcaps osd = \"allow\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if k, err := ReadKeyring(path, "client.rec"); err != nil || k != "BBB==" {
		t.Fatalf("key %q err %v", k, err)
	}
	if _, err := ReadKeyring(path, "client.other"); err == nil {
		t.Fatal("missing entity read")
	}
}

func TestTenantCaps(t *testing.T) {
	c := TenantCaps([]Access{
		{Path: "/volumes/rackflow/t12-s6/b", Pool: "ec", Namespace: "fsvolumens_t12-s6"},
		{Path: "/volumes/rackflow/t12-s5/a", Pool: "ec", Namespace: "fsvolumens_t12-s5"},
	})
	if c.MDS != "allow rw path=/volumes/rackflow/t12-s5/a, allow rw path=/volumes/rackflow/t12-s6/b" ||
		c.OSD != "allow rw pool=ec namespace=fsvolumens_t12-s5, allow rw pool=ec namespace=fsvolumens_t12-s6" ||
		c.Mon != "allow r" {
		t.Fatalf("caps %+v", c)
	}
}

func TestEvictSessionsEveryActiveRank(t *testing.T) {
	s := execx.NewScript().
		On(pre+"fs get cephfs --format json", `{"mdsmap":{"in":[0,1],"max_mds":2}}`).
		On(pre+"tell mds.cephfs:0 client evict auth_name=rf-t12 client_metadata.hostname=rf-t12-nfs", "").
		On(pre+"tell mds.cephfs:1 client evict auth_name=rf-t12 client_metadata.hostname=rf-t12-nfs", "")
	if err := cli(s).EvictSessions(context.Background(), "client.rf-t12", "rf-t12-nfs"); err != nil {
		t.Fatal(err)
	}
	want := []string{
		pre + "fs get cephfs --format json",
		pre + "tell mds.cephfs:0 client evict auth_name=rf-t12 client_metadata.hostname=rf-t12-nfs",
		pre + "tell mds.cephfs:1 client evict auth_name=rf-t12 client_metadata.hostname=rf-t12-nfs",
	}
	if got := s.Lines(); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("calls:\n%s", strings.Join(got, "\n"))
	}

	s = execx.NewScript().
		On(pre+"fs get cephfs --format json", `{"mdsmap":{"in":[0]}}`).
		Fail(pre+"tell mds.cephfs:0 client evict auth_name=rf-t12 client_metadata.hostname=rf-t12-nfs", errors.New("EPERM"))
	if err := cli(s).EvictSessions(context.Background(), "client.rf-t12", "rf-t12-nfs"); err == nil || !strings.Contains(err.Error(), "mds.cephfs:0") {
		t.Fatalf("err %v", err)
	}
}

func TestEvictAddrSessions(t *testing.T) {
	s := execx.NewScript().
		On(pre+"fs get cephfs --format json", `{"mdsmap":{"in":[0]}}`).
		On(pre+"tell mds.cephfs:0 client ls --format json", `[
			{"id":4101,"inst":"client.4101 v1:10.0.0.5:0/111"},
			{"id":4102,"inst":"client.4102 v1:10.0.0.6:0/222"},
			{"id":4103,"inst":"client.4103 10.0.0.5:0/333"}]`)
	n, err := cli(s).EvictAddrSessions(context.Background(), netip.MustParseAddr("10.0.0.5"))
	if err != nil || n != 2 {
		t.Fatalf("n=%d err=%v", n, err)
	}
	lines := strings.Join(s.Lines(), "\n")
	if !strings.Contains(lines, "client evict id=4101") || !strings.Contains(lines, "client evict id=4103") || strings.Contains(lines, "id=4102") {
		t.Fatalf("calls:\n%s", lines)
	}
}

func TestDaemonAddrs(t *testing.T) {
	s := execx.NewScript().
		On(pre+"mon dump --format json", `{"mons":[{"name":"a","public_addrs":{"addrvec":[{"type":"v2","addr":"10.1.0.1:3300","nonce":0}]},"addr":"10.1.0.1:6789/0"}]}`).
		On(pre+"mgr dump --format json", `{"active_addrs":{"addrvec":[{"addr":"10.1.0.2:6800"}]},"standbys":[]}`).
		On(pre+"fs dump --format json", `{"standbys":[{"addr":"10.1.0.3:6801/9"}],"filesystems":[{"mdsmap":{"info":{"gid_1":{"addrs":{"addrvec":[{"addr":"10.1.0.4:6800"}]}}}}}]}`).
		On(pre+"osd dump --format json", `{"epoch":7,"osds":[{"osd":0,"public_addr":"10.1.0.5:6802/1","cluster_addr":"10.2.0.5:6803/1","heartbeat_front_addr":"0.0.0.0:0/0"}],"blocklist":{"10.0.0.9:0/0":"x"}}`)
	addrs, err := cli(s).DaemonAddrs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, a := range addrs {
		got = append(got, a.String())
	}
	if strings.Join(got, ",") != "10.1.0.1,10.1.0.2,10.1.0.3,10.1.0.4,10.1.0.5,10.2.0.5" {
		t.Fatalf("addrs %v", got)
	}
}
