package fastpath

import (
	"bufio"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func init() {
	// The end-to-end test proxies to a loopback target.
	os.Setenv("PROXY_ALLOW_PRIVATE_DESTINATIONS", "true")
}

type fakeLink struct {
	vlan  int
	addrs map[string]bool
	up    bool
}

// fakeNet stands in for ip(8) and nft(8), keeping the state they would change.
type fakeNet struct {
	mu         sync.Mutex
	namespaces map[string]map[string]*fakeLink
	host       map[string]*fakeLink
	log        []string
	rulesets   map[string]string
	fail       map[string]error
}

func newFakeNet(namespaces ...string) *fakeNet {
	f := &fakeNet{
		namespaces: map[string]map[string]*fakeLink{},
		host:       map[string]*fakeLink{},
		rulesets:   map[string]string{},
		fail:       map[string]error{},
	}
	for _, ns := range namespaces {
		f.namespaces[ns] = map[string]*fakeLink{}
	}
	return f
}

func (f *fakeNet) record(entry string) {
	f.mu.Lock()
	f.log = append(f.log, entry)
	f.mu.Unlock()
}

func (f *fakeNet) calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.log...)
}

func (f *fakeNet) reset() {
	f.mu.Lock()
	f.log = nil
	f.mu.Unlock()
}

var errMissing = errors.New("does not exist")

func (f *fakeNet) Run(stdin string, name string, args ...string) (string, error) {
	line := name + " " + strings.Join(args, " ")
	f.record(line)
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail[line]; err != nil {
		return "", err
	}
	a := args
	switch {
	case line == "ip netns list":
		names := make([]string, 0, len(f.namespaces))
		for ns := range f.namespaces {
			names = append(names, ns+" (id: 1)")
		}
		sort.Strings(names)
		return strings.Join(names, "\n") + "\n", nil
	case len(a) == 3 && a[0] == "netns" && a[1] == "add":
		f.namespaces[a[2]] = map[string]*fakeLink{}
		return "", nil
	case len(a) == 3 && a[0] == "netns" && a[1] == "del":
		if _, ok := f.namespaces[a[2]]; !ok {
			return "", errMissing
		}
		delete(f.namespaces, a[2])
		return "", nil
	case len(a) >= 4 && a[0] == "netns" && a[1] == "exec":
		f.rulesets[a[2]] = stdin
		return "", nil
	case a[0] == "-n":
		links, ok := f.namespaces[a[1]]
		if !ok {
			return "", errMissing
		}
		return f.inNamespace(links, a[2:])
	case a[0] == "link" && a[1] == "del":
		if _, ok := f.host[a[2]]; !ok {
			return "", errMissing
		}
		delete(f.host, a[2])
		return "", nil
	case a[0] == "link" && a[1] == "add":
		vlan, _ := strconv.Atoi(a[len(a)-1])
		f.host[a[5]] = &fakeLink{vlan: vlan, addrs: map[string]bool{}}
		return "", nil
	case a[0] == "link" && a[1] == "set" && a[3] == "netns":
		link, ok := f.host[a[2]]
		target, nsOK := f.namespaces[a[4]]
		if !ok || !nsOK {
			return "", errMissing
		}
		delete(f.host, a[2])
		target[a[2]] = link
		return "", nil
	}
	return "", fmt.Errorf("fake: unexpected command %q", line)
}

func (f *fakeNet) inNamespace(links map[string]*fakeLink, a []string) (string, error) {
	switch {
	case strings.Join(a, " ") == "link set lo up":
		return "", nil
	case a[0] == "-d":
		link, ok := links[a[len(a)-1]]
		if !ok {
			return "", errMissing
		}
		return fmt.Sprintf("7: %s@if2: <BROADCAST,UP> mtu 1500\\    vlan protocol 802.1Q id %d <REORDER_HDR>\n", a[len(a)-1], link.vlan), nil
	case a[0] == "-4":
		link, ok := links[a[len(a)-1]]
		if !ok {
			return "", errMissing
		}
		var out strings.Builder
		for addr := range link.addrs {
			fmt.Fprintf(&out, "7: %s    inet %s scope global %s\n", a[len(a)-1], addr, a[len(a)-1])
		}
		return out.String(), nil
	case a[0] == "link" && a[1] == "del":
		if _, ok := links[a[2]]; !ok {
			return "", errMissing
		}
		delete(links, a[2])
		return "", nil
	case a[0] == "link" && a[1] == "set":
		link, ok := links[a[2]]
		if !ok {
			return "", errMissing
		}
		link.up = true
		return "", nil
	case a[0] == "addr":
		link, ok := links[a[4]]
		if !ok {
			return "", errMissing
		}
		if a[1] == "add" {
			link.addrs[a[2]] = true
		} else {
			delete(link.addrs, a[2])
		}
		return "", nil
	}
	return "", fmt.Errorf("fake: unexpected namespace command %v", a)
}

// fakeListeners opens loopback listeners in place of namespace listeners.
type fakeListeners struct {
	net    *fakeNet
	host   string
	mu     sync.Mutex
	opened []string
}

type loggedListener struct {
	net.Listener
	net  *fakeNet
	name string
	once sync.Once
}

func (l *loggedListener) Close() error {
	l.once.Do(func() { l.net.record("close listener " + l.name) })
	return l.Listener.Close()
}

func (f *fakeListeners) listen(ns string, port int) (net.Listener, error) {
	host := f.host
	if host == "" {
		host = "127.0.0.1"
	}
	ln, err := net.Listen("tcp", host+":0")
	if err != nil {
		return nil, err
	}
	name := fmt.Sprintf("%s:%d", ns, port)
	f.net.record("listen " + name)
	f.mu.Lock()
	f.opened = append(f.opened, name)
	f.mu.Unlock()
	return &loggedListener{Listener: ln, net: f.net, name: name}, nil
}

func newManager(f *fakeNet, parent string) (*Manager, *fakeListeners) {
	listeners := &fakeListeners{net: f}
	return &Manager{
		Parent:      parent,
		Cmd:         f,
		Listen:      listeners.listen,
		DialTimeout: 2 * time.Second,
		IdleTimeout: 5 * time.Second,
	}, listeners
}

func oneTenant(port int, vlan int, addrs ...string) Config {
	return Config{CIDR: "100.80.0.0/16", Port: port, Tenants: []Tenant{{NetworkID: 7, VLAN: vlan, Addresses: addrs}}}
}

func assertCalls(t *testing.T, got, want []string) {
	t.Helper()
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("calls:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestDisabledManagerTouchesNothing(t *testing.T) {
	f := newFakeNet("pf-n3")
	m, _ := newManager(f, "")
	m.Apply(oneTenant(11080, 2001, "100.80.0.1"))
	m.Retry()
	m.Close()
	if len(f.calls()) != 0 {
		t.Fatalf("disabled manager ran %v", f.calls())
	}
	if m.Status().Enabled {
		t.Fatal("status must report disabled")
	}
}

func TestCreateTenant(t *testing.T) {
	f := newFakeNet()
	m, _ := newManager(f, "bond0")
	defer m.Close()
	m.Apply(oneTenant(11080, 2001, "100.80.0.2", "100.80.0.1"))
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"ip netns add pf-n7",
		"ip -n pf-n7 link set lo up",
		"ip -n pf-n7 -d -o link show dev pf-v7",
		"ip link del pf-v7",
		"ip link add link bond0 name pf-v7 type vlan id 2001",
		"ip link set pf-v7 netns pf-n7",
		"ip -n pf-n7 -4 -o addr show dev pf-v7",
		"ip -n pf-n7 addr add 100.80.0.1/16 dev pf-v7",
		"ip -n pf-n7 addr add 100.80.0.2/16 dev pf-v7",
		"ip -n pf-n7 link set pf-v7 up",
		"ip netns exec pf-n7 nft -f -",
		"listen pf-n7:11080",
	})
	if f.rulesets["pf-n7"] != RenderRuleset(11080) {
		t.Fatalf("ruleset not applied: %q", f.rulesets["pf-n7"])
	}
	if s := m.Status(); !s.Enabled || s.Tenants != 1 || len(s.Errors) != 0 {
		t.Fatalf("status %+v", s)
	}

	f.reset()
	m.Apply(oneTenant(11080, 2001, "100.80.0.2", "100.80.0.1"))
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"ip -n pf-n7 link set lo up",
		"ip -n pf-n7 -d -o link show dev pf-v7",
		"ip -n pf-n7 -4 -o addr show dev pf-v7",
		"ip -n pf-n7 link set pf-v7 up",
	})
}

func TestAddressVLANAndPortChanges(t *testing.T) {
	f := newFakeNet()
	m, _ := newManager(f, "bond0")
	defer m.Close()
	m.Apply(oneTenant(11080, 2001, "100.80.0.1", "100.80.0.2"))

	f.reset()
	m.Apply(oneTenant(11080, 2001, "100.80.0.3", "100.80.0.2"))
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"ip -n pf-n7 link set lo up",
		"ip -n pf-n7 -d -o link show dev pf-v7",
		"ip -n pf-n7 -4 -o addr show dev pf-v7",
		"ip -n pf-n7 addr add 100.80.0.3/16 dev pf-v7",
		"ip -n pf-n7 addr del 100.80.0.1/16 dev pf-v7",
		"ip -n pf-n7 link set pf-v7 up",
	})

	f.reset()
	m.Apply(oneTenant(11080, 2002, "100.80.0.3"))
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"ip -n pf-n7 link set lo up",
		"ip -n pf-n7 -d -o link show dev pf-v7",
		"ip -n pf-n7 link del pf-v7",
		"ip link del pf-v7",
		"ip link add link bond0 name pf-v7 type vlan id 2002",
		"ip link set pf-v7 netns pf-n7",
		"ip -n pf-n7 -4 -o addr show dev pf-v7",
		"ip -n pf-n7 addr add 100.80.0.3/16 dev pf-v7",
		"ip -n pf-n7 link set pf-v7 up",
	})

	f.reset()
	m.Apply(oneTenant(11081, 2002, "100.80.0.3"))
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"ip -n pf-n7 link set lo up",
		"ip -n pf-n7 -d -o link show dev pf-v7",
		"ip -n pf-n7 -4 -o addr show dev pf-v7",
		"ip -n pf-n7 link set pf-v7 up",
		"ip netns exec pf-n7 nft -f -",
		"close listener pf-n7:11080",
		"listen pf-n7:11081",
	})
	if !strings.Contains(f.rulesets["pf-n7"], "tcp dport 11081 ct state new accept") {
		t.Fatalf("ruleset not updated: %s", f.rulesets["pf-n7"])
	}
}

func TestRemovalClosesListenerFirstAndOnlyTouchesOwnNamespaces(t *testing.T) {
	f := newFakeNet("pf-n99", "rf-t5", "pf-x", "pf-n0", "pf-n07", "other")
	m, _ := newManager(f, "bond0")
	defer m.Close()
	m.Apply(oneTenant(11080, 2001, "100.80.0.1"))
	if _, ok := f.namespaces["pf-n99"]; ok {
		t.Fatal("leftover pf-n99 must be removed")
	}
	for _, foreign := range []string{"rf-t5", "pf-x", "pf-n0", "pf-n07", "other"} {
		if _, ok := f.namespaces[foreign]; !ok {
			t.Fatalf("foreign namespace %s was removed", foreign)
		}
	}
	for _, call := range f.calls() {
		for _, foreign := range []string{"rf-t5", "pf-x", "pf-n0 ", "pf-n07", "other"} {
			if strings.Contains(call+" ", foreign+" ") {
				t.Fatalf("command touched foreign namespace: %s", call)
			}
		}
	}

	f.reset()
	m.Apply(Config{CIDR: "100.80.0.0/16", Port: 11080})
	assertCalls(t, f.calls(), []string{
		"ip netns list",
		"close listener pf-n7:11080",
		"ip -n pf-n7 link del pf-v7",
		"ip netns del pf-n7",
	})
	if s := m.Status(); s.Tenants != 0 || len(s.Errors) != 0 {
		t.Fatalf("status %+v", s)
	}
}

func TestOneFailingTenantDoesNotBlockOthersAndIsRetried(t *testing.T) {
	f := newFakeNet()
	broken := "ip link add link bond0 name pf-v7 type vlan id 2001"
	f.fail[broken] = errors.New("RTNETLINK answers: File exists")
	m, _ := newManager(f, "bond0")
	defer m.Close()
	cfg := Config{CIDR: "100.80.0.0/16", Port: 11080, Tenants: []Tenant{
		{NetworkID: 7, VLAN: 2001, Addresses: []string{"100.80.0.1"}},
		{NetworkID: 8, VLAN: 2002, Addresses: []string{"100.80.0.1"}},
		{NetworkID: 9, VLAN: 5000},
		{NetworkID: 10, VLAN: 2003, Addresses: []string{"10.0.0.1"}},
	}}
	m.Apply(cfg)
	status := m.Status()
	if len(status.Errors) != 3 || status.Errors["pf-n7"] == "" || status.Errors["pf-n9"] == "" || status.Errors["pf-n10"] == "" {
		t.Fatalf("status %+v", status)
	}
	if _, ok := f.namespaces["pf-n8"]["pf-v8"]; !ok {
		t.Fatal("healthy tenant was not set up")
	}

	delete(f.fail, broken)
	cfg.Tenants = cfg.Tenants[:2]
	m.Apply(cfg)
	if s := m.Status(); len(s.Errors) != 0 || s.Tenants != 2 {
		t.Fatalf("after fix: %+v", s)
	}

	f.fail["ip -n pf-n8 link set lo up"] = errors.New("boom")
	m.Apply(cfg)
	f.reset()
	m.Retry()
	if len(f.calls()) == 0 {
		t.Fatal("retry must reconcile after a failure")
	}
	delete(f.fail, "ip -n pf-n8 link set lo up")
	m.Retry()
	f.reset()
	m.Retry()
	if len(f.calls()) != 0 {
		t.Fatalf("retry without failures must do nothing, ran %v", f.calls())
	}
}

func TestBadPortRefusesAllTenants(t *testing.T) {
	f := newFakeNet()
	m, _ := newManager(f, "bond0")
	m.Apply(oneTenant(0, 2001, "100.80.0.1"))
	if s := m.Status(); s.Errors["fast_path"] == "" {
		t.Fatalf("status %+v", s)
	}
	if len(f.namespaces) != 0 {
		t.Fatal("nothing must be created with a bad port")
	}
}

func sourceTarget(t *testing.T) (string, func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				if _, err := http.ReadRequest(bufio.NewReader(c)); err != nil {
					return
				}
				host, _, _ := net.SplitHostPort(c.RemoteAddr().String())
				fmt.Fprintf(c, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(host), host)
			}(c)
		}
	}()
	return ln.Addr().String(), func() { _ = ln.Close() }
}

func proxyGet(t *testing.T, proxyAddr, target, user, pass string) (int, string) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", proxyAddr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	creds := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
	fmt.Fprintf(conn, "GET http://%s/ HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Basic %s\r\nConnection: close\r\n\r\n", target, target, creds)
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := make([]byte, 64)
	n, _ := resp.Body.Read(buf)
	return resp.StatusCode, string(buf[:n])
}

func TestTenantListenerEgressesByCredentialsAndPicksUpNewOnes(t *testing.T) {
	target, closeTarget := sourceTarget(t)
	defer closeTarget()
	f := newFakeNet()
	m, listeners := newManager(f, "bond0")
	listeners.host = "127.0.0.6"
	defer m.Close()

	// 127.0.0.6 stands in for the subnet's private IP, 127.0.0.7 and .8 for public IPs.
	cfg := Config{CIDR: "127.0.0.0/8", Port: 11080, Tenants: []Tenant{{
		NetworkID: 7, VLAN: 2001, Addresses: []string{"127.0.0.6"},
		Assignments: []Assignment{{ServiceID: 1, PrivateIP: "127.0.0.6", BindIP: "127.0.0.7", Username: "alice", Password: "pa"}},
	}}}
	m.Apply(cfg)
	addr := m.tenants[7].server.Addr()

	if code, body := proxyGet(t, addr, target, "alice", "pa"); code != 200 || body != "127.0.0.7" {
		t.Fatalf("alice: %d %q", code, body)
	}
	if code, _ := proxyGet(t, addr, target, "bob", "pb"); code != http.StatusForbidden {
		t.Fatalf("unknown user: %d", code)
	}

	cfg.Tenants[0].Assignments = append(cfg.Tenants[0].Assignments,
		Assignment{ServiceID: 2, PrivateIP: "127.0.0.6", BindIP: "127.0.0.8", Username: "bob", Password: "pb"})
	m.Apply(cfg)
	if m.tenants[7].server.Addr() != addr {
		t.Fatal("a credential change must not reopen the listener")
	}
	if code, body := proxyGet(t, addr, target, "bob", "pb"); code != 200 || body != "127.0.0.8" {
		t.Fatalf("bob after update: %d %q", code, body)
	}
}
