package lease

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// FakeCluster is an in-memory RADOS pool for tests. It models the OSD clock,
// cls_lock shared locks (tags, cookies, expiry, MUST_RENEW), xattrs, objects
// and the OSD blocklist. Clients are made with NewClient.
type FakeCluster struct {
	now func() time.Time

	mu        sync.Mutex
	objects   map[string]*fakeObject // "<ns>/<oid>"
	blocklist map[string]time.Time   // "ip:port/nonce" -> until
	nextGID   int64
}

type fakeObject struct {
	data   []byte
	xattrs map[string]string
	tag    string
	locks  map[lockerID]fakeEntry
}

type lockerID struct{ client, cookie string }

type fakeEntry struct {
	addr    string
	expires time.Time
}

// NewFakeCluster returns an empty pool whose OSDs use now as their clock.
func NewFakeCluster(now func() time.Time) *FakeCluster {
	return &FakeCluster{now: now, objects: map[string]*fakeObject{}, blocklist: map[string]time.Time{}, nextGID: 4100}
}

// Partition modes of a FakeClient.
const (
	Connected = iota
	// PartitionFail makes every call fail with ErrTimeout at once.
	PartitionFail
	// PartitionHang makes every call block until its context ends or Heal.
	PartitionHang
)

// FakeClient is one node's lease client. Reset gives it a new instance, as a
// helper restart does.
type FakeClient struct {
	c  *FakeCluster
	ip string

	// Before, if set, runs before every call with the operation name; tests
	// use it to advance clocks (a slow call) or to inject an error.
	Before func(op, oid string) error

	mu        sync.Mutex
	ident     Identity
	partition int
	healed    chan struct{}
	calls     map[string]int
}

// NewClient returns a client whose address is ip.
func (c *FakeCluster) NewClient(ip string) *FakeClient {
	f := &FakeClient{c: c, ip: ip, healed: make(chan struct{}), calls: map[string]int{}}
	f.ident = c.newIdentity(ip)
	return f
}

func (c *FakeCluster) newIdentity(ip string) Identity {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.nextGID++
	return Identity{
		Instance: fmt.Sprintf("i%d", c.nextGID),
		Client:   fmt.Sprintf("client.%d", c.nextGID),
		Addr:     fmt.Sprintf("v1:%s:0/%d", ip, 1000+c.nextGID),
	}
}

// SetPartition cuts the client off from Ceph (PartitionFail or
// PartitionHang) or reconnects it (Connected).
func (f *FakeClient) SetPartition(mode int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.partition == PartitionHang && mode != PartitionHang {
		close(f.healed)
		f.healed = make(chan struct{})
	}
	f.partition = mode
}

// Calls is how many times op was called.
func (f *FakeClient) Calls(op string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[op]
}

func (f *FakeClient) enter(ctx context.Context, op, oid string) (Identity, error) {
	f.mu.Lock()
	f.calls[op]++
	mode, healed, ident, before := f.partition, f.healed, f.ident, f.Before
	f.mu.Unlock()
	switch mode {
	case PartitionFail:
		return ident, fmt.Errorf("%s %s: %w", op, oid, ErrTimeout)
	case PartitionHang:
		select {
		case <-ctx.Done():
		case <-healed:
		}
		return ident, fmt.Errorf("%s %s: %w", op, oid, ErrTimeout)
	}
	if before != nil {
		if err := before(op, oid); err != nil {
			return ident, err
		}
	}
	return ident, nil
}

func (c *FakeCluster) blocked(addr string) bool {
	ip, rest, ok := strings.Cut(strings.TrimPrefix(strings.TrimPrefix(addr, "v1:"), "v2:"), ":")
	if !ok {
		return false
	}
	now := c.now()
	for _, k := range []string{ip + ":0/0", ip + "/32", ip + ":" + rest} {
		if until, ok := c.blocklist[k]; ok && now.Before(until) {
			return true
		}
	}
	return false
}

// osd runs fn as an OSD operation of ident under the cluster lock.
func (f *FakeClient) osd(ctx context.Context, op, oid string, fn func(Identity) error) error {
	ident, err := f.enter(ctx, op, oid)
	if err != nil {
		return err
	}
	c := f.c
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.blocked(ident.Addr) {
		return fmt.Errorf("%s %s: %w", op, oid, ErrBlocklisted)
	}
	return fn(ident)
}

func objKey(ns, oid string) string { return ns + "/" + oid }

func (c *FakeCluster) expire(o *fakeObject) {
	now := c.now()
	for id, e := range o.locks {
		if !now.Before(e.expires) {
			delete(o.locks, id)
		}
	}
	if len(o.locks) == 0 {
		o.tag = ""
	}
}

func (c *FakeCluster) obj(oid string, create bool) *fakeObject {
	k := objKey(Namespace, oid)
	o, ok := c.objects[k]
	if !ok && create {
		o = &fakeObject{xattrs: map[string]string{}, locks: map[lockerID]fakeEntry{}}
		c.objects[k] = o
	}
	return o
}

func (f *FakeClient) Identity(ctx context.Context) (Identity, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.ident, nil
}

func (f *FakeClient) Reset() {
	ident := f.c.newIdentity(f.ip)
	f.mu.Lock()
	f.ident = ident
	f.mu.Unlock()
}

func (f *FakeClient) LockShared(ctx context.Context, oid, cookie, tag string, duration time.Duration, flags int) error {
	return f.osd(ctx, "lock", oid, func(ident Identity) error {
		failIfExists := flags&1 == 0
		failIfMissing := flags&FlagMustRenew != 0
		if !failIfExists && failIfMissing {
			return fmt.Errorf("lock %s: EINVAL", oid)
		}
		c := f.c
		o := c.obj(oid, !failIfMissing)
		if o == nil {
			return fmt.Errorf("lock %s: %w", oid, ErrNotFound)
		}
		c.expire(o)
		id := lockerID{ident.Client, cookie}
		_, found := o.locks[id]
		if !found && failIfMissing {
			return fmt.Errorf("lock %s: %w", oid, ErrNotFound)
		}
		if found && failIfExists && !failIfMissing {
			return fmt.Errorf("lock %s: %w", oid, ErrExists)
		}
		others := 0
		for other := range o.locks {
			if other != id {
				others++
			}
		}
		if others > 0 && o.tag != tag {
			return fmt.Errorf("lock %s: %w", oid, ErrBusy)
		}
		o.tag = tag
		o.locks[id] = fakeEntry{addr: ident.Addr, expires: c.now().Add(duration)}
		return nil
	})
}

func (f *FakeClient) Unlock(ctx context.Context, oid, cookie string) error {
	return f.osd(ctx, "unlock", oid, func(ident Identity) error {
		return f.c.unlock(oid, lockerID{ident.Client, cookie})
	})
}

func (f *FakeClient) BreakLock(ctx context.Context, oid, client, cookie string) error {
	return f.osd(ctx, "break", oid, func(Identity) error {
		return f.c.unlock(oid, lockerID{client, cookie})
	})
}

func (c *FakeCluster) unlock(oid string, id lockerID) error {
	o := c.obj(oid, false)
	if o == nil {
		return fmt.Errorf("unlock %s: %w", oid, ErrNotFound)
	}
	c.expire(o)
	if _, ok := o.locks[id]; !ok {
		return fmt.Errorf("unlock %s: %w", oid, ErrNotFound)
	}
	delete(o.locks, id)
	if len(o.locks) == 0 {
		o.tag = ""
	}
	return nil
}

func (f *FakeClient) ListLockers(ctx context.Context, oid string) (Lockers, error) {
	var out Lockers
	err := f.osd(ctx, "list", oid, func(Identity) error {
		var err error
		out, err = f.c.lockers(oid)
		return err
	})
	return out, err
}

func (c *FakeCluster) lockers(oid string) (Lockers, error) {
	o := c.obj(oid, false)
	if o == nil {
		return Lockers{}, fmt.Errorf("list %s: %w", oid, ErrNotFound)
	}
	c.expire(o)
	out := Lockers{Tag: o.tag}
	for id, e := range o.locks {
		out.Lockers = append(out.Lockers, Locker{Client: id.client, Cookie: id.cookie, Addr: e.addr})
	}
	sort.Slice(out.Lockers, func(i, j int) bool {
		return out.Lockers[i].Cookie+out.Lockers[i].Client < out.Lockers[j].Cookie+out.Lockers[j].Client
	})
	return out, nil
}

func (f *FakeClient) GetXattrs(ctx context.Context, oid string) (map[string]string, error) {
	out := map[string]string{}
	err := f.osd(ctx, "getxattrs", oid, func(Identity) error {
		o := f.c.obj(oid, false)
		if o == nil {
			return fmt.Errorf("getxattrs %s: %w", oid, ErrNotFound)
		}
		for k, v := range o.xattrs {
			out[k] = v
		}
		return nil
	})
	return out, err
}

func (f *FakeClient) SetXattr(ctx context.Context, oid, name, value string) error {
	return f.osd(ctx, "setxattr", oid, func(Identity) error {
		f.c.obj(oid, true).xattrs[name] = value
		return nil
	})
}

func (f *FakeClient) WriteFull(ctx context.Context, oid string, data []byte) error {
	return f.osd(ctx, "write", oid, func(Identity) error {
		f.c.obj(oid, true).data = append([]byte(nil), data...)
		return nil
	})
}

func (f *FakeClient) Read(ctx context.Context, oid string) ([]byte, error) {
	var out []byte
	err := f.osd(ctx, "read", oid, func(Identity) error {
		o := f.c.obj(oid, false)
		if o == nil {
			return fmt.Errorf("read %s: %w", oid, ErrNotFound)
		}
		out = append([]byte(nil), o.data...)
		return nil
	})
	return out, err
}

func (f *FakeClient) Remove(ctx context.Context, oid string) error {
	return f.osd(ctx, "remove", oid, func(Identity) error {
		k := objKey(Namespace, oid)
		if _, ok := f.c.objects[k]; !ok {
			return fmt.Errorf("remove %s: %w", oid, ErrNotFound)
		}
		delete(f.c.objects, k)
		return nil
	})
}

func (f *FakeClient) PurgeNamespace(ctx context.Context, ns string) error {
	return f.osd(ctx, "purge", ns, func(Identity) error {
		for k := range f.c.objects {
			if strings.HasPrefix(k, ns+"/") {
				delete(f.c.objects, k)
			}
		}
		return nil
	})
}

func (f *FakeClient) BlocklistAdd(ctx context.Context, addr string, expire time.Duration) error {
	if _, err := f.enter(ctx, "blocklist_add", addr); err != nil {
		return err
	}
	c := f.c
	c.mu.Lock()
	defer c.mu.Unlock()
	c.blocklist[addr] = c.now().Add(expire)
	return nil
}

func (f *FakeClient) BlocklistLs(ctx context.Context) ([]BlockEntry, error) {
	if _, err := f.enter(ctx, "blocklist_ls", ""); err != nil {
		return nil, err
	}
	c := f.c
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.blockEntries(), nil
}

func (c *FakeCluster) blockEntries() []BlockEntry {
	now := c.now()
	var out []BlockEntry
	for a, until := range c.blocklist {
		if now.Before(until) {
			out = append(out, BlockEntry{Addr: a, Until: until})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Addr < out[j].Addr })
	return out
}

// --- inspection for tests ---

// Lockers returns a lock's current entries (expired ones dropped).
func (c *FakeCluster) Lockers(oid string) Lockers {
	c.mu.Lock()
	defer c.mu.Unlock()
	l, _ := c.lockers(oid)
	return l
}

// Xattr returns an xattr of an object in the lease namespace.
func (c *FakeCluster) Xattr(oid, name string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if o := c.obj(oid, false); o != nil {
		return o.xattrs[name]
	}
	return ""
}

// SetXattrRaw sets an xattr without a client, for seeding.
func (c *FakeCluster) SetXattrRaw(oid, name, value string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.obj(oid, true).xattrs[name] = value
}

// Exists reports whether an object exists in namespace ns.
func (c *FakeCluster) Exists(ns, oid string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.objects[objKey(ns, oid)]
	return ok
}

// Put creates an object in namespace ns, for seeding.
func (c *FakeCluster) Put(ns, oid string, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.objects[objKey(ns, oid)] = &fakeObject{data: data, xattrs: map[string]string{}, locks: map[lockerID]fakeEntry{}}
}

// Blocklist returns the current blocklist entries.
func (c *FakeCluster) Blocklist() []BlockEntry {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.blockEntries()
}

// FakeClock is a test clock. Advance moves both clocks; StepWall moves only
// the wall clock (an NTP step, or a paused VM whose boot time does not count
// the pause); AdvanceBoot moves only the boot clock.
type FakeClock struct {
	mu   sync.Mutex
	wall time.Time
	boot time.Duration
}

func NewFakeClock(start time.Time) *FakeClock { return &FakeClock{wall: start, boot: time.Hour} }

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.wall
}

func (c *FakeClock) Boot() time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.boot
}

func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall = c.wall.Add(d)
	c.boot += d
}

func (c *FakeClock) StepWall(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall = c.wall.Add(d)
}

func (c *FakeClock) AdvanceBoot(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.boot += d
}
