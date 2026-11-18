package fastpath

import (
	"bytes"
	"fmt"
	"log"
	"net"
	"os/exec"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"rackflow/proxy_runner/internal/auth"
	"rackflow/proxy_runner/internal/proxy"
)

// Commander runs a command with optional standard input and returns its output.
type Commander interface {
	Run(stdin string, name string, args ...string) (string, error)
}

// ExecCommander runs real commands.
type ExecCommander struct{}

func (ExecCommander) Run(stdin string, name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		return out.String(), fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(out.String()))
	}
	return out.String(), nil
}

// ListenFunc opens a TCP listener on 0.0.0.0:port inside a named namespace.
type ListenFunc func(namespace string, port int) (net.Listener, error)

// Manager keeps the tenant namespaces and listeners in line with the config.
// It only creates, changes or removes namespaces named pf-n<id>, interfaces
// named pf-v<id> and the rf-proxy-fast table inside its own namespaces.
type Manager struct {
	// Parent is the host interface that carries the client VLANs. Empty
	// turns the fast path off: the config is ignored and nothing is touched.
	Parent      string
	Cmd         Commander
	Listen      ListenFunc
	DialTimeout time.Duration
	IdleTimeout time.Duration

	mu      sync.Mutex
	tenants map[int]*tenantState
	last    *Config
	errors  map[int]string
	global  string
}

type tenantState struct {
	port    int
	ruleset string
	server  *proxy.Server
	store   atomic.Pointer[auth.TenantStore]
}

// Status is reported on the health endpoint.
type Status struct {
	Enabled bool              `json:"enabled"`
	Tenants int               `json:"tenants"`
	Errors  map[string]string `json:"errors,omitempty"`
}

// Enabled reports whether the fast path is configured on this host.
func (m *Manager) Enabled() bool { return m != nil && m.Parent != "" }

// Apply reconciles to a new config.
func (m *Manager) Apply(cfg Config) {
	if !m.Enabled() {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.last = &cfg
	m.reconcile(cfg)
}

// Retry reconciles the last config again when something failed.
func (m *Manager) Retry() {
	if !m.Enabled() {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.last == nil || (len(m.errors) == 0 && m.global == "") {
		return
	}
	m.reconcile(*m.last)
}

// Close stops every tenant listener. Namespaces are kept for the next start.
func (m *Manager) Close() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, st := range m.tenants {
		if st.server != nil {
			st.server.Stop()
			st.server = nil
		}
	}
}

func (m *Manager) Status() Status {
	if !m.Enabled() {
		return Status{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s := Status{Enabled: true, Tenants: len(m.tenants)}
	if len(m.errors) > 0 || m.global != "" {
		s.Errors = map[string]string{}
		for id, e := range m.errors {
			s.Errors[NamespaceName(id)] = e
		}
		if m.global != "" {
			s.Errors["fast_path"] = m.global
		}
	}
	return s
}

func (m *Manager) run(stdin string, name string, args ...string) (string, error) {
	return m.Cmd.Run(stdin, name, args...)
}

func (m *Manager) reconcile(cfg Config) {
	if m.tenants == nil {
		m.tenants = map[int]*tenantState{}
	}
	m.errors = map[int]string{}
	m.global = ""

	existing, err := m.listNamespaces()
	if err != nil {
		m.global = err.Error()
		log.Printf("fast path: %v", err)
		return
	}
	desired := map[int]Tenant{}
	for _, t := range cfg.Tenants {
		desired[t.NetworkID] = t
	}
	for _, id := range sortedKeys(existing) {
		if _, keep := desired[id]; !keep {
			m.remove(id, true)
		}
	}
	for id := range m.tenants {
		if _, keep := desired[id]; !keep {
			m.remove(id, existing[id])
		}
	}
	if len(desired) > 0 && (cfg.Port < 1 || cfg.Port > 65535) {
		m.global = fmt.Sprintf("fast path port %d is out of range", cfg.Port)
		log.Printf("fast path: %s", m.global)
		return
	}
	for _, id := range sortedKeys(desired) {
		if err := m.ensure(cfg, desired[id], existing[id]); err != nil {
			m.errors[id] = err.Error()
			log.Printf("fast path: tenant %d: %v", id, err)
		}
	}
}

func (m *Manager) listNamespaces() (map[int]bool, error) {
	out, err := m.run("", "ip", "netns", "list")
	if err != nil {
		return nil, err
	}
	found := map[int]bool{}
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		if id, ok := ParseNamespace(fields[0]); ok {
			found[id] = true
		}
	}
	return found, nil
}

func (m *Manager) remove(id int, hasNamespace bool) {
	ns, link := NamespaceName(id), LinkName(id)
	if st := m.tenants[id]; st != nil && st.server != nil {
		st.server.Stop()
	}
	delete(m.tenants, id)
	if !hasNamespace {
		return
	}
	// Delete the VLAN interface first so its VLAN is free at once, even if
	// open connections keep the namespace alive for a while.
	_, _ = m.run("", "ip", "-n", ns, "link", "del", link)
	if _, err := m.run("", "ip", "netns", "del", ns); err != nil {
		m.errors[id] = err.Error()
		log.Printf("fast path: remove %s: %v", ns, err)
		return
	}
	log.Printf("fast path: removed %s", ns)
}

var (
	vlanIDPattern = regexp.MustCompile(`vlan protocol \S+ id (\d+)`)
	inetPattern   = regexp.MustCompile(`\binet (\S+)`)
)

func (m *Manager) ensure(cfg Config, t Tenant, hasNamespace bool) error {
	addrs, err := cfg.validate(t)
	if err != nil {
		return err
	}
	ns, link := NamespaceName(t.NetworkID), LinkName(t.NetworkID)
	st := m.tenants[t.NetworkID]
	if st == nil {
		st = &tenantState{}
		st.store.Store(auth.NewTenantStore(nil))
		m.tenants[t.NetworkID] = st
	}
	st.store.Store(auth.NewTenantStore(tenantEntries(t)))

	if !hasNamespace {
		if _, err := m.run("", "ip", "netns", "add", ns); err != nil {
			return err
		}
		st.ruleset = ""
	}
	if _, err := m.run("", "ip", "-n", ns, "link", "set", "lo", "up"); err != nil {
		return err
	}
	if err := m.ensureLink(ns, link, t.VLAN); err != nil {
		return err
	}
	if err := m.ensureAddresses(ns, link, addrs); err != nil {
		return err
	}
	if _, err := m.run("", "ip", "-n", ns, "link", "set", link, "up"); err != nil {
		return err
	}
	ruleset := RenderRuleset(cfg.Port)
	if st.ruleset != ruleset {
		if _, err := m.run(ruleset, "ip", "netns", "exec", ns, "nft", "-f", "-"); err != nil {
			return err
		}
		st.ruleset = ruleset
	}
	if st.server != nil && st.port != cfg.Port {
		st.server.Stop()
		st.server = nil
	}
	if st.server == nil {
		ln, err := m.Listen(ns, cfg.Port)
		if err != nil {
			return fmt.Errorf("listen in %s: %w", ns, err)
		}
		srv := &proxy.Server{
			Listener:    ln,
			Auth:        proxy.TenantAuth{Store: st.store.Load},
			DialTimeout: m.DialTimeout,
			IdleTimeout: m.IdleTimeout,
		}
		if err := srv.Start(); err != nil {
			_ = ln.Close()
			return err
		}
		st.server, st.port = srv, cfg.Port
		log.Printf("fast path: %s listening on port %d (vlan %d, %d addresses)", ns, cfg.Port, t.VLAN, len(addrs))
	}
	return nil
}

func (m *Manager) ensureLink(ns, link string, vlan int) error {
	out, err := m.run("", "ip", "-n", ns, "-d", "-o", "link", "show", "dev", link)
	if err == nil {
		if match := vlanIDPattern.FindStringSubmatch(out); match != nil {
			if id, _ := strconv.Atoi(match[1]); id == vlan {
				return nil
			}
		}
		if _, err := m.run("", "ip", "-n", ns, "link", "del", link); err != nil {
			return err
		}
	}
	// A half-made interface may be left in the host namespace by a crash.
	_, _ = m.run("", "ip", "link", "del", link)
	if _, err := m.run("", "ip", "link", "add", "link", m.Parent, "name", link, "type", "vlan", "id", strconv.Itoa(vlan)); err != nil {
		return err
	}
	if _, err := m.run("", "ip", "link", "set", link, "netns", ns); err != nil {
		_, _ = m.run("", "ip", "link", "del", link)
		return err
	}
	return nil
}

func (m *Manager) ensureAddresses(ns, link string, want []string) error {
	out, err := m.run("", "ip", "-n", ns, "-4", "-o", "addr", "show", "dev", link)
	if err != nil {
		return err
	}
	have := map[string]bool{}
	for _, match := range inetPattern.FindAllStringSubmatch(out, -1) {
		have[match[1]] = true
	}
	wanted := map[string]bool{}
	for _, addr := range want {
		wanted[addr] = true
		if !have[addr] {
			if _, err := m.run("", "ip", "-n", ns, "addr", "add", addr, "dev", link); err != nil {
				return err
			}
		}
	}
	for _, addr := range sortedStrings(have) {
		if !wanted[addr] {
			if _, err := m.run("", "ip", "-n", ns, "addr", "del", addr, "dev", link); err != nil {
				return err
			}
		}
	}
	return nil
}

func tenantEntries(t Tenant) []auth.TenantEntry {
	rows := make([]auth.TenantEntry, 0, len(t.Assignments))
	for _, a := range t.Assignments {
		rows = append(rows, auth.TenantEntry{
			PrivateIP: a.PrivateIP,
			BindIP:    a.BindIP,
			Username:  a.Username,
			Password:  a.Password,
			ServiceID: a.ServiceID,
		})
	}
	return rows
}

func sortedKeys[V any](m map[int]V) []int {
	keys := make([]int, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Ints(keys)
	return keys
}

func sortedStrings(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
