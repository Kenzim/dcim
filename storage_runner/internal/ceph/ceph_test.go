package ceph

import (
	"context"
	"errors"
	"strings"
	"testing"

	"rackflow/storage_runner/internal/execx"
)

const pre = "ceph --conf /etc/ceph/ceph.conf --keyring /etc/ceph/ceph.client.admin.keyring "

func cli(s *execx.Script) *CLI {
	return &CLI{Cmd: s, FS: "cephfs", SubvolumeGroup: "rackflow", ECPool: "ec", Conf: "/etc/ceph/ceph.conf", Keyring: "/etc/ceph/ceph.client.admin.keyring"}
}

func healthy() *execx.Script {
	return execx.NewScript().
		On(pre+"osd pool get ec allow_ec_overwrites --format json", `{"pool":"ec","pool_id":3,"allow_ec_overwrites":true}`).
		On(pre+"fs ls --format json", `[{"name":"cephfs","metadata_pool":"cephfs_metadata","data_pools":["cephfs_data","ec"]}]`).
		On(pre+"df --format json", `{"pools":[{"name":"cephfs_data","stats":{"max_avail":1}},{"name":"ec","stats":{"max_avail":5000}}]}`)
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
		pre + "fs subvolume create cephfs t12-s5 --size 1073741824 --group_name rackflow --pool_layout ec --namespace-isolated",
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
	users, err := (&CLI{Cmd: s}).Users(context.Background())
	if err != nil || len(users) != 1 || users["client.rf-t12"].Caps.MDS != "allow rw path=/x" {
		t.Fatalf("users=%+v err=%v", users, err)
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
