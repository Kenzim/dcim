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

	mu    sync.Mutex
	specs map[int64]*Spec // nil spec: namespace exists with unknown contents
	down  map[int64]bool
}

func NewFake(rec *record.Recorder) *Fake {
	return &Fake{Rec: rec, Fail: map[int64]error{}, specs: map[int64]*Spec{}}
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
