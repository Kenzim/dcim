package ganesha

import (
	"context"
	"fmt"
	"sort"
	"sync"

	"rackflow/storage_runner/internal/record"
)

// Fake keeps containers in memory and records changes.
type Fake struct {
	Rec        *record.Recorder
	Fail       map[string]error // Run and Remove errors keyed by container name
	ReloadFail map[string]error // Reload errors keyed by container name

	mu         sync.Mutex
	containers map[string]Container
	specs      map[string]Spec
}

func NewFake(rec *record.Recorder) *Fake {
	return &Fake{Rec: rec, Fail: map[string]error{}, ReloadFail: map[string]error{}, containers: map[string]Container{}, specs: map[string]Spec{}}
}

// Seed adds a container that was not started through this fake.
func (f *Fake) Seed(c Container) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.containers[c.Name] = c
}

// Stop marks a container as exited.
func (f *Fake) Stop(name string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.containers[name]
	c.Running = false
	f.containers[name] = c
}

func (f *Fake) Spec(name string) (Spec, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.specs[name]
	return s, ok
}

func (f *Fake) Has(name string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.containers[name]
	return ok
}

func (f *Fake) List(context.Context) ([]Container, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []Container
	for _, c := range f.containers {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TenantID < out[j].TenantID })
	return out, nil
}

func (f *Fake) Run(_ context.Context, s Spec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[s.Name]; err != nil {
		return err
	}
	if _, ok := f.containers[s.Name]; ok {
		return fmt.Errorf("container %s already exists", s.Name)
	}
	f.containers[s.Name] = Container{Name: s.Name, TenantID: s.TenantID, Running: true, CoreHash: s.CoreHash}
	f.specs[s.Name] = s
	f.Rec.Add("nfs run %s netns=%s image=%s", s.Name, s.Netns, s.Image)
	return nil
}

func (f *Fake) Remove(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[name]; err != nil {
		return err
	}
	delete(f.containers, name)
	delete(f.specs, name)
	f.Rec.Add("nfs rm %s", name)
	return nil
}

func (f *Fake) Reload(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.containers[name]; !ok {
		return fmt.Errorf("container %s not found", name)
	}
	if err := f.ReloadFail[name]; err != nil {
		return err
	}
	f.Rec.Add("nfs reload %s", name)
	return nil
}
