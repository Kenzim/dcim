// Package state persists what the runner must remember across restarts: the
// /30 link block of each tenant, the Ganesha export id of each share, and
// when each share was first seen deleting.
package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

const fileName = "state.json"

// Link is a tenant's veth /30: Host is .1 on the host side, NS is .2 in the namespace.
type Link struct {
	Block netip.Prefix
	Host  netip.Prefix
	NS    netip.Prefix
}

type data struct {
	Links         map[string]string            `json:"links"`
	Exports       map[string]map[string]uint16 `json:"export_ids"`
	ExportNext    map[string]int               `json:"export_next"`
	DeletingSince map[string]time.Time         `json:"share_deleting_since"`
}

// MaxExportID is the largest NFS-Ganesha export id; 0 is the pseudo root.
const MaxExportID = 65535

type Store struct {
	path     string
	linkCIDR netip.Prefix
	d        data
}

// Open loads STATE_DIR/state.json, creating the directory if needed.
func Open(dir string, linkCIDR netip.Prefix) (*Store, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	s := &Store{path: filepath.Join(dir, fileName), linkCIDR: linkCIDR.Masked()}
	b, err := os.ReadFile(s.path)
	switch {
	case errors.Is(err, os.ErrNotExist):
	case err != nil:
		return nil, err
	default:
		if err := json.Unmarshal(b, &s.d); err != nil {
			return nil, fmt.Errorf("%s: %w", s.path, err)
		}
	}
	if s.d.Links == nil {
		s.d.Links = map[string]string{}
	}
	if s.d.DeletingSince == nil {
		s.d.DeletingSince = map[string]time.Time{}
	}
	if s.d.Exports == nil {
		s.d.Exports = map[string]map[string]uint16{}
	}
	if s.d.ExportNext == nil {
		s.d.ExportNext = map[string]int{}
	}
	return s, nil
}

func (s *Store) save() error {
	b, err := json.MarshalIndent(s.d, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func key(id int64) string { return strconv.FormatInt(id, 10) }

func linkFor(block netip.Prefix) Link {
	base := block.Addr()
	host := base.Next()
	return Link{
		Block: block,
		Host:  netip.PrefixFrom(host, 30),
		NS:    netip.PrefixFrom(host.Next(), 30),
	}
}

func (s *Store) validBlock(raw string) (netip.Prefix, bool) {
	p, err := netip.ParsePrefix(raw)
	if err != nil || p.Bits() != 30 || p.Masked() != p || !s.linkCIDR.Contains(p.Addr()) {
		return netip.Prefix{}, false
	}
	return p, true
}

// Link returns the tenant's allocated link, if any.
func (s *Store) Link(id int64) (Link, bool) {
	p, ok := s.validBlock(s.d.Links[key(id)])
	if !ok {
		return Link{}, false
	}
	return linkFor(p), true
}

// LinkTenants lists tenants with an allocated link.
func (s *Store) LinkTenants() []int64 {
	var out []int64
	for k := range s.d.Links {
		if id, err := strconv.ParseInt(k, 10, 64); err == nil {
			out = append(out, id)
		}
	}
	return out
}

// AllocateLink returns the tenant's link, allocating the lowest free /30 of
// LINK_CIDR the first time. An allocation stays put until ReleaseLink.
func (s *Store) AllocateLink(id int64) (Link, error) {
	if l, ok := s.Link(id); ok {
		return l, nil
	}
	used := map[netip.Addr]bool{}
	for k, raw := range s.d.Links {
		if k == key(id) {
			continue
		}
		if p, ok := s.validBlock(raw); ok {
			used[p.Addr()] = true
		}
	}
	for a := s.linkCIDR.Addr(); s.linkCIDR.Contains(a); {
		if !used[a] {
			block := netip.PrefixFrom(a, 30)
			s.d.Links[key(id)] = block.String()
			if err := s.save(); err != nil {
				delete(s.d.Links, key(id))
				return Link{}, err
			}
			return linkFor(block), nil
		}
		for i := 0; i < 4; i++ {
			a = a.Next()
		}
		if !a.IsValid() {
			break
		}
	}
	return Link{}, fmt.Errorf("LINK_CIDR %s has no free /30", s.linkCIDR)
}

func (s *Store) ReleaseLink(id int64) error {
	if _, ok := s.d.Links[key(id)]; !ok {
		return nil
	}
	delete(s.d.Links, key(id))
	return s.save()
}

// ExportID returns the share's export id within its tenant's Ganesha,
// allocating the next one the first time. Ids are stable because NFS file
// handles carry them, and never reused within a tenant so a stale handle
// cannot land on another share's export.
func (s *Store) ExportID(tenant, share int64) (uint16, error) {
	m := s.d.Exports[key(tenant)]
	if id, ok := m[key(share)]; ok && id > 0 {
		return id, nil
	}
	next := s.d.ExportNext[key(tenant)]
	for _, id := range m {
		if int(id) > next {
			next = int(id)
		}
	}
	next++
	if next > MaxExportID {
		return 0, fmt.Errorf("tenant %d has used all %d export ids", tenant, MaxExportID)
	}
	if m == nil {
		m = map[string]uint16{}
		s.d.Exports[key(tenant)] = m
	}
	prev, hadPrev := s.d.ExportNext[key(tenant)]
	m[key(share)] = uint16(next)
	s.d.ExportNext[key(tenant)] = next
	if err := s.save(); err != nil {
		delete(m, key(share))
		if hadPrev {
			s.d.ExportNext[key(tenant)] = prev
		} else {
			delete(s.d.ExportNext, key(tenant))
		}
		return 0, err
	}
	return uint16(next), nil
}

// PruneExports drops export ids of shares no longer listed for tenants in
// listed; tenants not in listed are left alone.
func (s *Store) PruneExports(listed map[int64]map[int64]bool) error {
	changed := false
	for t, shares := range listed {
		m := s.d.Exports[key(t)]
		for k := range m {
			id, err := strconv.ParseInt(k, 10, 64)
			if err != nil || !shares[id] {
				delete(m, k)
				changed = true
			}
		}
		if m != nil && len(m) == 0 {
			delete(s.d.Exports, key(t))
			changed = true
		}
	}
	if !changed {
		return nil
	}
	return s.save()
}

// MarkDeleting records when a share was first seen in the deleting state and
// returns that time.
func (s *Store) MarkDeleting(share int64, now time.Time) (time.Time, error) {
	if t, ok := s.d.DeletingSince[key(share)]; ok {
		return t, nil
	}
	s.d.DeletingSince[key(share)] = now.UTC()
	return now.UTC(), s.save()
}

// PruneDeleting drops marks for shares that are no longer deleting.
func (s *Store) PruneDeleting(keep map[int64]bool) error {
	changed := false
	for k := range s.d.DeletingSince {
		id, err := strconv.ParseInt(k, 10, 64)
		if err != nil || !keep[id] {
			delete(s.d.DeletingSince, k)
			changed = true
		}
	}
	if !changed {
		return nil
	}
	return s.save()
}
