package lease

import (
	"bufio"
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"
	"time"
)

//go:embed rados_helper.py
var helperScript string

// Errno values the helper reports.
const (
	errnoNOENT     = 2
	errnoINTR      = 4
	errnoBUSY      = 16
	errnoEXIST     = 17
	errnoSHUTDOWN  = 108 // EBLOCKLISTED
	errnoTIMEDOUT  = 110
	helperOpTimout = 8
)

// Helper is the real Store: a python3-rados child process spoken to with
// JSON lines. One process is one RADOS client instance; a process that does
// not answer within CallTimeout is killed and the next call starts another.
type Helper struct {
	Python  string // default python3
	Script  string // default the embedded rados_helper.py
	Conf    string // ceph.conf, empty for the default search path
	Keyring string
	Name    string // cephx entity, empty for client.admin
	Pool    string

	CallTimeout  time.Duration // default 10 s, above the helper's own 8 s
	StartTimeout time.Duration // default 10 s
	RetryAfter   time.Duration // wait after a failed start, default 5 s
	Logf         func(format string, args ...any)

	mu        sync.Mutex
	proc      *helperProc
	failedAt  time.Time
	failedErr error
}

type helperProc struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
	ident Identity

	mu      sync.Mutex
	nextID  int64
	pending map[int64]chan helperReply
	dead    chan struct{}
	deadErr error
}

type helperReply struct {
	ID     *int64          `json:"id"`
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result"`
	Errno  int             `json:"errno"`
	Error  string          `json:"error"`

	Ready  *bool  `json:"ready"`
	Client string `json:"client"`
	Addr   string `json:"addr"`
}

func (h *Helper) logf(format string, args ...any) {
	if h.Logf != nil {
		h.Logf(format, args...)
	}
}

func dur(d, def time.Duration) time.Duration {
	if d > 0 {
		return d
	}
	return def
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func (h *Helper) get(ctx context.Context) (*helperProc, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if p := h.proc; p != nil {
		select {
		case <-p.dead:
			h.proc = nil
		default:
			return p, nil
		}
	}
	if h.failedErr != nil && time.Since(h.failedAt) < dur(h.RetryAfter, 5*time.Second) {
		return nil, h.failedErr
	}
	p, err := h.start(ctx)
	if err != nil {
		h.failedAt, h.failedErr = time.Now(), err
		return nil, err
	}
	h.failedErr = nil
	h.proc = p
	return p, nil
}

func (h *Helper) start(ctx context.Context) (*helperProc, error) {
	cfg, _ := json.Marshal(map[string]any{
		"conf": h.Conf, "keyring": h.Keyring, "name": h.Name,
		"pool": h.Pool, "namespace": Namespace, "op_timeout": helperOpTimout,
	})
	python := h.Python
	if python == "" {
		python = "python3"
	}
	script := h.Script
	if script == "" {
		script = helperScript
	}
	cmd := exec.Command(python, "-c", script, string(cfg))
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start lease helper: %w", err)
	}
	p := &helperProc{cmd: cmd, stdin: stdin, pending: map[int64]chan helperReply{}, dead: make(chan struct{})}
	go func() {
		sc := bufio.NewScanner(stderr)
		for sc.Scan() {
			h.logf("lease helper: %s", sc.Text())
		}
	}()
	ready := make(chan helperReply, 1)
	go p.read(stdout, ready)

	timer := time.NewTimer(dur(h.StartTimeout, 10*time.Second))
	defer timer.Stop()
	select {
	case r := <-ready:
		if r.Ready == nil || !*r.Ready {
			p.kill()
			return nil, fmt.Errorf("lease helper could not connect to Ceph: %s", r.Error)
		}
		p.ident = Identity{Instance: randomHex(4), Client: r.Client, Addr: strings.TrimSpace(r.Addr)}
		h.logf("lease helper started: %s at %s", p.ident.Client, p.ident.Addr)
		return p, nil
	case <-p.dead:
		return nil, fmt.Errorf("lease helper exited at start: %v", p.deadErr)
	case <-timer.C:
		p.kill()
		return nil, fmt.Errorf("lease helper did not connect within %s: %w", dur(h.StartTimeout, 10*time.Second), ErrTimeout)
	case <-ctx.Done():
		p.kill()
		return nil, ctx.Err()
	}
}

func (p *helperProc) read(stdout io.Reader, ready chan<- helperReply) {
	sc := bufio.NewScanner(stdout)
	sc.Buffer(make([]byte, 64<<10), 8<<20)
	first := true
	for sc.Scan() {
		var r helperReply
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			continue
		}
		if first {
			first = false
			ready <- r
			continue
		}
		if r.ID == nil {
			continue
		}
		p.mu.Lock()
		ch := p.pending[*r.ID]
		delete(p.pending, *r.ID)
		p.mu.Unlock()
		if ch != nil {
			ch <- r
		}
	}
	err := sc.Err()
	if werr := p.cmd.Wait(); err == nil {
		err = werr
	}
	if err == nil {
		err = errors.New("exited")
	}
	p.mu.Lock()
	p.deadErr = err
	p.mu.Unlock()
	close(p.dead)
}

func (p *helperProc) kill() {
	if p.cmd.Process != nil {
		p.cmd.Process.Kill()
	}
}

// Reset kills the current helper; the next call starts a new instance.
func (h *Helper) Reset() {
	h.mu.Lock()
	p := h.proc
	h.proc = nil
	h.mu.Unlock()
	if p != nil {
		p.kill()
	}
}

// Close stops the helper.
func (h *Helper) Close() { h.Reset() }

func mapErrno(op string, errno int, msg string) error {
	var base error
	switch errno {
	case errnoBUSY:
		base = ErrBusy
	case errnoNOENT:
		base = ErrNotFound
	case errnoEXIST:
		base = ErrExists
	case errnoSHUTDOWN:
		base = ErrBlocklisted
	case errnoTIMEDOUT, errnoINTR:
		base = ErrTimeout
	default:
		return fmt.Errorf("%s: %s (errno %d)", op, msg, errno)
	}
	return fmt.Errorf("%s: %s: %w", op, msg, base)
}

func (h *Helper) call(ctx context.Context, op string, args map[string]any, out any) error {
	p, err := h.get(ctx)
	if err != nil {
		return err
	}
	ch := make(chan helperReply, 1)
	p.mu.Lock()
	p.nextID++
	id := p.nextID
	p.pending[id] = ch
	if args == nil {
		args = map[string]any{}
	}
	args["id"], args["op"] = id, op
	line, _ := json.Marshal(args)
	_, werr := p.stdin.Write(append(line, '\n'))
	p.mu.Unlock()
	forget := func() {
		p.mu.Lock()
		delete(p.pending, id)
		p.mu.Unlock()
	}
	if werr != nil {
		forget()
		p.kill()
		return fmt.Errorf("%s: write to lease helper: %v: %w", op, werr, ErrTimeout)
	}
	timer := time.NewTimer(dur(h.CallTimeout, 10*time.Second))
	defer timer.Stop()
	select {
	case r := <-ch:
		if !r.OK {
			return mapErrno(op, r.Errno, r.Error)
		}
		if out != nil && len(r.Result) > 0 {
			if err := json.Unmarshal(r.Result, out); err != nil {
				return fmt.Errorf("%s: bad helper reply: %w", op, err)
			}
		}
		return nil
	case <-p.dead:
		forget()
		return fmt.Errorf("%s: lease helper exited: %v: %w", op, p.deadErr, ErrTimeout)
	case <-timer.C:
		forget()
		h.logf("lease helper did not answer %s within %s; restarting it", op, dur(h.CallTimeout, 10*time.Second))
		h.mu.Lock()
		if h.proc == p {
			h.proc = nil
		}
		h.mu.Unlock()
		p.kill()
		return fmt.Errorf("%s: %w", op, ErrTimeout)
	case <-ctx.Done():
		forget()
		return fmt.Errorf("%s: %v: %w", op, ctx.Err(), ErrTimeout)
	}
}

func (h *Helper) Identity(ctx context.Context) (Identity, error) {
	p, err := h.get(ctx)
	if err != nil {
		return Identity{}, err
	}
	return p.ident, nil
}

func (h *Helper) LockShared(ctx context.Context, oid, cookie, tag string, duration time.Duration, flags int) error {
	return h.call(ctx, "lock_shared", map[string]any{
		"oid": oid, "cookie": cookie, "tag": tag, "duration": int64(duration / time.Second), "flags": flags,
	}, nil)
}

func (h *Helper) Unlock(ctx context.Context, oid, cookie string) error {
	return h.call(ctx, "unlock", map[string]any{"oid": oid, "cookie": cookie}, nil)
}

func (h *Helper) BreakLock(ctx context.Context, oid, client, cookie string) error {
	return h.call(ctx, "break_lock", map[string]any{"oid": oid, "client": client, "cookie": cookie}, nil)
}

func (h *Helper) ListLockers(ctx context.Context, oid string) (Lockers, error) {
	var out Lockers
	err := h.call(ctx, "list_lockers", map[string]any{"oid": oid}, &out)
	return out, err
}

func (h *Helper) GetXattrs(ctx context.Context, oid string) (map[string]string, error) {
	out := map[string]string{}
	err := h.call(ctx, "get_xattrs", map[string]any{"oid": oid}, &out)
	return out, err
}

func (h *Helper) SetXattr(ctx context.Context, oid, name, value string) error {
	return h.call(ctx, "set_xattr", map[string]any{"oid": oid, "name": name, "value": value}, nil)
}

func (h *Helper) WriteFull(ctx context.Context, oid string, data []byte) error {
	return h.call(ctx, "write_full", map[string]any{"oid": oid, "data": base64.StdEncoding.EncodeToString(data)}, nil)
}

func (h *Helper) Read(ctx context.Context, oid string) ([]byte, error) {
	var s string
	if err := h.call(ctx, "read", map[string]any{"oid": oid}, &s); err != nil {
		return nil, err
	}
	return base64.StdEncoding.DecodeString(s)
}

func (h *Helper) Remove(ctx context.Context, oid string) error {
	return h.call(ctx, "remove", map[string]any{"oid": oid}, nil)
}

func (h *Helper) PurgeNamespace(ctx context.Context, ns string) error {
	return h.call(ctx, "purge", map[string]any{"ns": ns}, nil)
}

func (h *Helper) BlocklistAdd(ctx context.Context, addr string, expire time.Duration) error {
	return h.call(ctx, "blocklist_add", map[string]any{"addr": addr, "expire": int64(expire / time.Second)}, nil)
}

// blocklistTimeLayouts are the forms Ceph prints a blocklist expiry in.
var blocklistTimeLayouts = []string{
	"2006-01-02T15:04:05.000000-0700",
	"2006-01-02T15:04:05.999999-0700",
	"2006-01-02 15:04:05.999999",
	time.RFC3339Nano,
}

func (h *Helper) BlocklistLs(ctx context.Context) ([]BlockEntry, error) {
	var raw []struct {
		Addr  string `json:"addr"`
		Until string `json:"until"`
	}
	if err := h.call(ctx, "blocklist_ls", nil, &raw); err != nil {
		return nil, err
	}
	out := make([]BlockEntry, 0, len(raw))
	for _, e := range raw {
		be := BlockEntry{Addr: e.Addr}
		for _, layout := range blocklistTimeLayouts {
			if t, err := time.Parse(layout, e.Until); err == nil {
				be.Until = t.UTC()
				break
			}
		}
		out = append(out, be)
	}
	return out, nil
}
