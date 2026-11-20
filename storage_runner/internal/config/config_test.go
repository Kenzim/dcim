package config

import (
	"strings"
	"testing"
	"time"
)

func envOf(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func minimal() map[string]string {
	return map[string]string{
		"RACKFLOW_URL":         "https://rackflow.example.com/",
		"RUNNER_KEY":           "k",
		"PARENT_IFACE":         "eth1",
		"CEPH_FS_NAME":         "cephfs",
		"CEPH_EC_POOL":         "cephfs_ec",
		"CEPH_PUBLIC_NETWORKS": "10.20.0.0/24, 10.10.0.5/24",
		"GANESHA_IMAGE":        "quay.io/ceph/ceph:v20.2.5",
	}
}

func TestDefaults(t *testing.T) {
	h, err := FromEnv(envOf(minimal()))
	if err != nil {
		t.Fatal(err)
	}
	if h.RackflowURL != "https://rackflow.example.com" || h.PollInterval != 30*time.Second ||
		h.CephSubvolumeGroup != "rackflow" || h.LinkCIDR.String() != "169.254.128.0/17" ||
		h.StateDir != DefaultStateDir || h.CephUserPrefix != "rf-t" {
		t.Fatalf("host %+v", h)
	}
	if len(h.CephPublicNetworks) != 2 || h.CephPublicNetworks[0].String() != "10.10.0.0/24" {
		t.Fatalf("networks %v", h.CephPublicNetworks)
	}
}

func TestOverrides(t *testing.T) {
	m := minimal()
	m["POLL_INTERVAL"] = "1m"
	m["LINK_CIDR"] = "100.64.0.0/22"
	h, err := FromEnv(envOf(m))
	if err != nil || h.PollInterval != time.Minute || h.LinkCIDR.String() != "100.64.0.0/22" {
		t.Fatalf("h=%+v err=%v", h, err)
	}
	m["POLL_INTERVAL"] = "45"
	if h, _ := FromEnv(envOf(m)); h.PollInterval != 45*time.Second {
		t.Fatalf("interval %s", h.PollInterval)
	}
	m["CEPH_USER_PREFIX"] = "rfdev-t"
	m["CEPH_HA_POOL"] = "rackflow_dev_ha"
	if h, err := FromEnv(envOf(m)); err != nil || h.CephUserPrefix != "rfdev-t" || h.CephHAPool != "rackflow_dev_ha" {
		t.Fatalf("h=%+v err=%v", h, err)
	}
	m["CEPH_NAME"] = "client.rackflow-dev-runner"
	if h, err := FromEnv(envOf(m)); err != nil || h.CephName != "client.rackflow-dev-runner" {
		t.Fatalf("h=%+v err=%v", h, err)
	}
}

func TestErrors(t *testing.T) {
	cases := map[string]func(map[string]string){
		"RUNNER_KEY is required":     func(m map[string]string) { delete(m, "RUNNER_KEY") },
		"CEPH_PUBLIC_NETWORKS":       func(m map[string]string) { m["CEPH_PUBLIC_NETWORKS"] = "fd00::/64" },
		"overlaps Ceph":              func(m map[string]string) { m["LINK_CIDR"] = "10.10.0.0/16" },
		"PARENT_IFACE":               func(m map[string]string) { m["PARENT_IFACE"] = "eth1; reboot" },
		"CEPH_EC_POOL":               func(m map[string]string) { m["CEPH_EC_POOL"] = "ec pool" },
		"POLL_INTERVAL":              func(m map[string]string) { m["POLL_INTERVAL"] = "soon" },
		"/30 or larger":              func(m map[string]string) { m["LINK_CIDR"] = "169.254.128.0/31" },
		"GANESHA_IMAGE is required":  func(m map[string]string) { delete(m, "GANESHA_IMAGE") },
		"PARENT_IFACE is required":   func(m map[string]string) { delete(m, "PARENT_IFACE") },
		"CEPH_FS_NAME is required":   func(m map[string]string) { delete(m, "CEPH_FS_NAME") },
		"RACKFLOW_URL is required":   func(m map[string]string) { delete(m, "RACKFLOW_URL") },
		"CEPH_EC_POOL is required":   func(m map[string]string) { delete(m, "CEPH_EC_POOL") },
		"POLL_INTERVAL must be posi": func(m map[string]string) { m["POLL_INTERVAL"] = "0" },
		"CEPH_USER_PREFIX \"rf-t1\"": func(m map[string]string) { m["CEPH_USER_PREFIX"] = "rf-t1" },
		"CEPH_USER_PREFIX \"Dev\"":   func(m map[string]string) { m["CEPH_USER_PREFIX"] = "Dev" },
		"its own CEPH_HA_POOL":       func(m map[string]string) { m["CEPH_USER_PREFIX"] = "rfdev-t" },
		"cephx client entity":        func(m map[string]string) { m["CEPH_NAME"] = "rackflow-runner" },
		"tenant prefix":              func(m map[string]string) { m["CEPH_NAME"] = "client.rf-t9" },
	}
	for want, mutate := range cases {
		m := minimal()
		mutate(m)
		if _, err := FromEnv(envOf(m)); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: err %v", want, err)
		}
	}
}
