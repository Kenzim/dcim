package netns

import (
	"context"
	"fmt"
	"net/netip"
	"reflect"
	"sort"
	"sync"

	"rackflow/storage_runner/internal/names"
	"rackflow/storage_runner/internal/record"
)

// Fake keeps tenant networks in memory and records changes.
type Fake struct {
	Rec      *record.Recorder
	CheckErr error
	Fail     map[int64]error // Ensure errors per tenant

	// PresenceFail are EnsurePresence errors per network.
	PresenceFail map[int64]error

	mu        sync.Mutex
	specs     map[int64]*Spec // nil spec: namespace exists with unknown contents
	down      map[int64]bool
	presences map[int64]*PresenceSpec
	pdown     map[int64]bool
}

func NewFake(rec *record.Recorder) *Fake {
	return &Fake{Rec: rec, Fail: map[int64]error{}, specs: map[int64]*Spec{},
		PresenceFail: map[int64]error{}, presences: map[int64]*PresenceSpec{}, pdown: map[int64]bool{}}
}

// SeedPresence adds a presence namespace the runner did not create in this
// process.
func (f *Fake) SeedPresence(network int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.presences[network] = nil
}

func (f *Fake) HasPresence(network int64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.presences[network]
	return ok
}

// PresenceDown reports whether PresenceLinkDown was called since the last
// EnsurePresence.
func (f *Fake) PresenceDown(network int64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.pdown[network]
}

func (f *Fake) Presences(context.Context) ([]int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []int64
	for id := range f.presences {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (f *Fake) EnsurePresence(_ context.Context, s PresenceSpec) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.PresenceFail[s.NetworkID]; err != nil {
		return false, err
	}
	if cur := f.presences[s.NetworkID]; cur != nil && *cur == s && !f.pdown[s.NetworkID] {
		return false, nil
	}
	delete(f.pdown, s.NetworkID)
	cp := s
	f.presences[s.NetworkID] = &cp
	f.Rec.Add("net presence ensure %s", s)
	return true, nil
}

func (f *Fake) RemovePresence(_ context.Context, network int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.presences[network]; !ok {
		return nil
	}
	delete(f.presences, network)
	delete(f.pdown, network)
	f.Rec.Add("net presence remove %s", names.Presence(network))
	return nil
}

func (f *Fake) PresenceLinkDown(_ context.Context, network int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.presences[network]; !ok {
		return fmt.Errorf("namespace %s not found", names.Presence(network))
	}
	f.pdown[network] = true
	f.Rec.Add("net presence down %s", names.Presence(network))
	return nil
}

func (f *Fake) AnnouncePresence(_ context.Context, network int64, addr netip.Addr) error {
	f.Rec.Add("net presence arping %s %s", names.Presence(network), addr)
	return nil
}

// Seed adds a namespace that the runner did not create in this process.
func (f *Fake) Seed(tenant int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.specs[tenant] = nil
}

func (f *Fake) Has(tenant int64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.specs[tenant]
	return ok
}

func (f *Fake) Check(context.Context) error { return f.CheckErr }

func (f *Fake) Tenants(context.Context) ([]int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []int64
	for id := range f.specs {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (f *Fake) Ensure(_ context.Context, s Spec) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[s.TenantID]; err != nil {
		return false, err
	}
	if cur := f.specs[s.TenantID]; cur != nil && reflect.DeepEqual(*cur, s) && !f.down[s.TenantID] {
		return false, nil
	}
	delete(f.down, s.TenantID)
	cp := s
	cp.Routes = append(cp.Routes[:0:0], s.Routes...)
	f.specs[s.TenantID] = &cp
	f.Rec.Add("net ensure %s", s)
	return true, nil
}

// Down reports whether LinkDown was called since the last Ensure.
func (f *Fake) Down(tenant int64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.down[tenant]
}

func (f *Fake) LinkDown(_ context.Context, tenant int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.specs[tenant]; !ok {
		return fmt.Errorf("namespace %s not found", names.Namespace(tenant))
	}
	if f.down == nil {
		f.down = map[int64]bool{}
	}
	f.down[tenant] = true
	f.Rec.Add("net down %s", names.Namespace(tenant))
	return nil
}

func (f *Fake) Announce(_ context.Context, tenant int64, addr netip.Addr) error {
	f.Rec.Add("net arping %s %s", names.Namespace(tenant), addr)
	return nil
}

func (f *Fake) Remove(_ context.Context, tenant int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[tenant]; err != nil {
		return fmt.Errorf("remove %s: %w", names.Namespace(tenant), err)
	}
	if _, ok := f.specs[tenant]; !ok {
		return nil
	}
	delete(f.specs, tenant)
	delete(f.down, tenant)
	f.Rec.Add("net remove %s", names.Namespace(tenant))
	return nil
}
