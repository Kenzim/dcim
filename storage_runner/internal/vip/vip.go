// Package vip adds and removes the location's SFTP virtual IP on a host
// interface and announces it with gratuitous ARP.
package vip

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"sync"

	"rackflow/storage_runner/internal/execx"
	"rackflow/storage_runner/internal/record"
)

// Manager is the host side of the virtual IP.
type Manager interface {
	// DefaultInterface is the interface of the IPv4 default route.
	DefaultInterface(ctx context.Context) (string, error)
	// Has reports whether iface carries addr.
	Has(ctx context.Context, iface string, addr netip.Prefix) (bool, error)
	Add(ctx context.Context, iface string, addr netip.Prefix) error
	Remove(ctx context.Context, iface string, addr netip.Prefix) error
	// Announce sends gratuitous ARP for addr from iface.
	Announce(ctx context.Context, iface string, addr netip.Addr) error
}

// IPRoute drives `ip` and `arping` on the host.
type IPRoute struct {
	Cmd execx.Commander
}

func (m *IPRoute) DefaultInterface(ctx context.Context) (string, error) {
	out, err := m.Cmd.Run(ctx, nil, "ip", "-j", "-4", "route", "show", "default")
	if err != nil {
		return "", err
	}
	var routes []struct {
		Dev string `json:"dev"`
	}
	if err := json.Unmarshal(out, &routes); err != nil {
		return "", fmt.Errorf("parse default route: %w", err)
	}
	for _, r := range routes {
		if r.Dev != "" {
			return r.Dev, nil
		}
	}
	return "", fmt.Errorf("no IPv4 default route")
}

func (m *IPRoute) Has(ctx context.Context, iface string, addr netip.Prefix) (bool, error) {
	out, err := m.Cmd.Run(ctx, nil, "ip", "-j", "-4", "addr", "show", "dev", iface)
	if err != nil {
		return false, err
	}
	var links []struct {
		AddrInfo []struct {
			Local     string `json:"local"`
			PrefixLen int    `json:"prefixlen"`
		} `json:"addr_info"`
	}
	if err := json.Unmarshal(out, &links); err != nil {
		return false, fmt.Errorf("parse addresses of %s: %w", iface, err)
	}
	for _, l := range links {
		for _, a := range l.AddrInfo {
			if a.Local == addr.Addr().String() && a.PrefixLen == addr.Bits() {
				return true, nil
			}
		}
	}
	return false, nil
}

func (m *IPRoute) Add(ctx context.Context, iface string, addr netip.Prefix) error {
	_, err := m.Cmd.Run(ctx, nil, "ip", "addr", "replace", addr.String(), "dev", iface)
	return err
}

func (m *IPRoute) Remove(ctx context.Context, iface string, addr netip.Prefix) error {
	ok, err := m.Has(ctx, iface, addr)
	if err != nil || !ok {
		return err
	}
	_, err = m.Cmd.Run(ctx, nil, "ip", "addr", "del", addr.String(), "dev", iface)
	return err
}

func (m *IPRoute) Announce(ctx context.Context, iface string, addr netip.Addr) error {
	_, err := m.Cmd.Run(ctx, nil, "arping", "-U", "-c", "3", "-I", iface, addr.String())
	return err
}

// Fake is an in-memory Manager for tests.
type Fake struct {
	Rec   *record.Recorder
	Iface string

	mu    sync.Mutex
	addrs map[string]map[netip.Prefix]bool
}

func NewFake(rec *record.Recorder) *Fake {
	return &Fake{Rec: rec, Iface: "eno1", addrs: map[string]map[netip.Prefix]bool{}}
}

// Present reports whether iface carries addr.
func (f *Fake) Present(iface string, addr netip.Prefix) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.addrs[iface][addr]
}

func (f *Fake) DefaultInterface(context.Context) (string, error) { return f.Iface, nil }

func (f *Fake) Has(_ context.Context, iface string, addr netip.Prefix) (bool, error) {
	return f.Present(iface, addr), nil
}

func (f *Fake) Add(_ context.Context, iface string, addr netip.Prefix) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.addrs[iface] == nil {
		f.addrs[iface] = map[netip.Prefix]bool{}
	}
	f.addrs[iface][addr] = true
	f.Rec.Add("vip add %s dev %s", addr, iface)
	return nil
}

func (f *Fake) Remove(_ context.Context, iface string, addr netip.Prefix) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.addrs[iface][addr] {
		delete(f.addrs[iface], addr)
		f.Rec.Add("vip del %s dev %s", addr, iface)
	}
	return nil
}

func (f *Fake) Announce(_ context.Context, iface string, addr netip.Addr) error {
	f.Rec.Add("vip arping %s dev %s", addr, iface)
	return nil
}
