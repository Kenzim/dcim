package ceph

import (
	"context"
	"fmt"
	"net/netip"
	"sort"
	"strings"
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
	Fail      map[string]error // create errors keyed by subvolume name or entity
	InfoFail  map[string]error // SubvolumeInfo errors keyed by subvolume name
	// Daemons are the Ceph daemon addresses DaemonAddrs returns.
	Daemons    []netip.Addr
	DaemonsErr error
	EvictErr   error
	// Scoped makes Users return only the entities asked for, as a scoped
	// key's auth get does.
	Scoped bool

	mu         sync.Mutex
	authorized map[string]bool // entity+"/"+subvolume known to the mgr
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
		InfoFail:   map[string]error{},
		subvolumes: map[string]*SubvolumeInfo{},
		users:      map[string]User{},
		authorized: map[string]bool{},
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
	if err := f.InfoFail[name]; err != nil {
		return SubvolumeInfo{}, err
	}
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

func (f *Fake) Users(_ context.Context, entities []string) (map[string]User, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]User, len(f.users))
	if f.Scoped {
		for _, e := range entities {
			if u, ok := f.users[e]; ok {
				out[e] = u
			}
		}
		return out, nil
	}
	for k, v := range f.users {
		out[k] = v
	}
	return out, nil
}

// Authorize adds the subvolume's grants the way the mgr does: appended
// with "," and not repeated, creating the user with mon "allow r".
func (f *Fake) Authorize(_ context.Context, entity, subvolume string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.Fail[entity]; err != nil {
		return err
	}
	sv, ok := f.subvolumes[subvolume]
	if !ok {
		return fmt.Errorf("subvolume %s not found", subvolume)
	}
	u, ok := f.users[entity]
	if !ok {
		f.keys++
		u = User{Key: fmt.Sprintf("KEY%d", f.keys), Caps: Caps{Mon: "allow r"}}
	}
	u.Caps.MDS = addGrant(u.Caps.MDS, "allow rw path="+sv.Path)
	u.Caps.OSD = addGrant(u.Caps.OSD, "allow rw pool="+sv.DataPool+" namespace="+sv.PoolNamespace)
	f.users[entity] = u
	f.authorized[entity+"/"+subvolume] = true
	f.Rec.Add("ceph authorize %s %s", entity, subvolume)
	return nil
}

// Deauthorize removes the subvolume's grants; like the mgr it refuses a
// subvolume it did not authorize and removes a user left with no caps
// beyond mon.
func (f *Fake) Deauthorize(_ context.Context, entity, subvolume string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	sv, ok := f.subvolumes[subvolume]
	if !ok {
		return fmt.Errorf("subvolume %s not found", subvolume)
	}
	if !f.authorized[entity+"/"+subvolume] {
		return fmt.Errorf("Error ENOENT: auth ID: %s doesn't exist", entity)
	}
	delete(f.authorized, entity+"/"+subvolume)
	u := f.users[entity]
	u.Caps.MDS = dropGrant(u.Caps.MDS, "allow rw path="+sv.Path)
	u.Caps.OSD = dropGrant(u.Caps.OSD, "allow rw pool="+sv.DataPool+" namespace="+sv.PoolNamespace)
	if u.Caps.MDS == "" && u.Caps.OSD == "" {
		delete(f.users, entity)
	} else {
		f.users[entity] = u
	}
	f.Rec.Add("ceph deauthorize %s %s", entity, subvolume)
	return nil
}

func splitGrants(caps string) []string {
	var out []string
	for _, g := range strings.Split(caps, ",") {
		if g = strings.TrimSpace(g); g != "" {
			out = append(out, g)
		}
	}
	return out
}

func addGrant(caps, grant string) string {
	gs := splitGrants(caps)
	for _, g := range gs {
		if g == grant {
			return caps
		}
	}
	return strings.Join(append(gs, grant), ",")
}

func dropGrant(caps, grant string) string {
	var keep []string
	for _, g := range splitGrants(caps) {
		if g != grant {
			keep = append(keep, g)
		}
	}
	return strings.Join(keep, ",")
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

func (f *Fake) EvictSessions(_ context.Context, entity, hostname string) error {
	f.mu.Lock()
	err := f.EvictErr
	f.mu.Unlock()
	if err != nil {
		return err
	}
	if hostname == "" {
		f.Rec.Add("ceph evict %s", entity)
		return nil
	}
	f.Rec.Add("ceph evict %s hostname=%s", entity, hostname)
	return nil
}

func (f *Fake) EvictAddrSessions(_ context.Context, ip netip.Addr) (int, error) {
	f.mu.Lock()
	err := f.EvictErr
	f.mu.Unlock()
	if err != nil {
		return 0, err
	}
	f.Rec.Add("ceph evict addr=%s", ip)
	return 0, nil
}

func (f *Fake) DaemonAddrs(context.Context) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]netip.Addr(nil), f.Daemons...), f.DaemonsErr
}

// SetEvictErr makes every eviction fail with err (nil to succeed again).
func (f *Fake) SetEvictErr(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.EvictErr = err
}

func (f *Fake) MinimalConf(context.Context) (string, error) {
	return "[global]\n\tfsid = 00000000-0000-0000-0000-000000000000\n\tmon_host = [v2:10.10.0.5:3300/0,v1:10.10.0.5:6789/0]\n", nil
}
