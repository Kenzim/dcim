package ceph

import (
	"context"
	"fmt"
	"sort"
	"sync"

	"rackflow/storage_runner/internal/record"
)

// Fake is an in-memory cluster that records changes.
type Fake struct {
	Rec       *record.Recorder
	GroupName string
	ECPool    string
	CheckErr  error
	FreeBytes int64
	Fail      map[string]error // errors keyed by subvolume name or entity

	mu         sync.Mutex
	groupPool  string
	groupSet   bool
	subvolumes map[string]*SubvolumeInfo
	users      map[string]User
	keys       int
}

func NewFake(rec *record.Recorder, group, pool string) *Fake {
	return &Fake{
		Rec:        rec,
		GroupName:  group,
		ECPool:     pool,
		FreeBytes:  1 << 40,
		Fail:       map[string]error{},
		subvolumes: map[string]*SubvolumeInfo{},
		users:      map[string]User{},
	}
}

// SetGroup pre-creates the subvolume group with the given data pool.
func (f *Fake) SetGroup(pool string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.groupPool, f.groupSet = pool, true
}

// SeedSubvolume adds a subvolume without recording an action.
func (f *Fake) SeedSubvolume(name string, size int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.subvolumes[name] = f.newInfo(name, size)
}

// SetUsed sets a subvolume's used bytes.
func (f *Fake) SetUsed(name string, used int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.subvolumes[name].BytesUsed = used
}

func (f *Fake) HasSubvolume(name string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.subvolumes[name]
	return ok
}

func (f *Fake) User(entity string) (User, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	u, ok := f.users[entity]
	return u, ok
}

// SeedUser adds a cephx user without recording an action.
func (f *Fake) SeedUser(entity string, caps Caps) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.keys++
	f.users[entity] = User{Key: fmt.Sprintf("KEY%d", f.keys), Caps: caps}
}

func (f *Fake) newInfo(name string, size int64) *SubvolumeInfo {
	return &SubvolumeInfo{
		Path:          fmt.Sprintf("/volumes/%s/%s/uuid-%s", f.GroupName, name, name),
		DataPool:      f.ECPool,
		PoolNamespace: "fsvolumens_" + name,
		BytesQuota:    size,
	}
}

func (f *Fake) Check(context.Context) (int64, error) {
	if f.CheckErr != nil {
		return 0, f.CheckErr
	}
	return f.FreeBytes, nil
}

func (f *Fake) Group(context.Context) (bool, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.groupSet, f.groupPool, nil
}

func (f *Fake) CreateGroup(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.groupPool, f.groupSet = f.ECPool, true
	f.Rec.Add("ceph group create %s pool=%s", f.GroupName, f.ECPool)
	return nil
}

func (f *Fake) Subvolumes(context.Context) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, 0, len(f.subvolumes))
	for n := range f.subvolumes {
		out = append(out, n)
	}
	sort.Strings(out)
	return out, nil
}

func (f *Fake) CreateSubvolume(_ context.Context, name string, size int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[name]; err != nil {
		return err
	}
	f.subvolumes[name] = f.newInfo(name, size)
	f.Rec.Add("ceph subvolume create %s size=%d", name, size)
	return nil
}

func (f *Fake) ResizeSubvolume(_ context.Context, name string, size int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	sv, ok := f.subvolumes[name]
	if !ok {
		return fmt.Errorf("subvolume %s not found", name)
	}
	sv.BytesQuota = size
	f.Rec.Add("ceph subvolume resize %s size=%d", name, size)
	return nil
}

func (f *Fake) SubvolumeInfo(_ context.Context, name string) (SubvolumeInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	sv, ok := f.subvolumes[name]
	if !ok {
		return SubvolumeInfo{}, fmt.Errorf("subvolume %s not found", name)
	}
	return *sv, nil
}

func (f *Fake) RemoveSubvolume(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.subvolumes[name]; !ok {
		return fmt.Errorf("subvolume %s not found", name)
	}
	delete(f.subvolumes, name)
	f.Rec.Add("ceph subvolume rm %s", name)
	return nil
}

func (f *Fake) Users(context.Context) (map[string]User, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]User, len(f.users))
	for k, v := range f.users {
		out[k] = v
	}
	return out, nil
}

func (f *Fake) CreateUser(_ context.Context, entity string, caps Caps) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[entity]; err != nil {
		return "", err
	}
	f.keys++
	u := User{Key: fmt.Sprintf("KEY%d", f.keys), Caps: caps}
	f.users[entity] = u
	f.Rec.Add("ceph user create %s %s", entity, caps)
	return u.Key, nil
}

func (f *Fake) SetCaps(_ context.Context, entity string, caps Caps) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	u, ok := f.users[entity]
	if !ok {
		return fmt.Errorf("user %s not found", entity)
	}
	u.Caps = caps
	f.users[entity] = u
	f.Rec.Add("ceph user caps %s %s", entity, caps)
	return nil
}

func (f *Fake) RemoveUser(_ context.Context, entity string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.users[entity]; !ok {
		return fmt.Errorf("user %s not found", entity)
	}
	delete(f.users, entity)
	f.Rec.Add("ceph user rm %s", entity)
	return nil
}

func (f *Fake) MinimalConf(context.Context) (string, error) {
	return "[global]\n\tfsid = 00000000-0000-0000-0000-000000000000\n\tmon_host = [v2:10.10.0.5:3300/0,v1:10.10.0.5:6789/0]\n", nil
}
