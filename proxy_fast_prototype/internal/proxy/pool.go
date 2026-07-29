package proxy

import (
	"bufio"
	"net"
	"sync"
	"time"
)

// poolKey scopes idle upstream connections by BOTH the customer bind IP and
// the destination address. Pooling across bind IPs would hand one customer's
// egress path to another — a correctness bug, not just a perf concern.
type poolKey struct {
	bindIP string
	addr   string // destination host:port
}

// pooledConn is an idle upstream connection plus its buffered reader (the
// reader must travel with the conn: it may hold bytes already read from the
// socket).
type pooledConn struct {
	c      net.Conn
	br     *bufio.Reader
	idleAt time.Time
}

func newPooledConn(c net.Conn) *pooledConn {
	return &pooledConn{c: c, br: bufio.NewReaderSize(c, 8*1024)}
}

func (pc *pooledConn) Close() { _ = pc.c.Close() }

// connPool keeps idle upstream connections for plain-HTTP forwarding.
// LIFO reuse (most recently used first) keeps hot connections hot and lets
// the idle tail expire. Stale conns are handled two ways: lazily on get
// (expired entries are closed) and by the caller retrying one fresh dial if
// a reused conn fails mid-roundtrip.
type connPool struct {
	mu          sync.Mutex
	idle        map[poolKey][]*pooledConn
	maxPerKey   int
	idleTimeout time.Duration
	stop        chan struct{}
	stopOnce    sync.Once
	closed      bool
}

func newConnPool(maxPerKey int, idleTimeout time.Duration) *connPool {
	if maxPerKey <= 0 {
		maxPerKey = 256
	}
	if idleTimeout <= 0 {
		idleTimeout = 30 * time.Second
	}
	p := &connPool{
		idle:        make(map[poolKey][]*pooledConn),
		maxPerKey:   maxPerKey,
		idleTimeout: idleTimeout,
		stop:        make(chan struct{}),
	}
	go p.janitor()
	return p
}

// get pops the most recently used idle conn for (bindIP, addr), or nil.
// Entries are appended in idleAt order, so if the newest (tail) is expired
// the whole list is expired.
func (p *connPool) get(bindIP, addr string) *pooledConn {
	k := poolKey{bindIP: bindIP, addr: addr}
	now := time.Now()
	p.mu.Lock()
	list := p.idle[k]
	if len(list) == 0 {
		p.mu.Unlock()
		return nil
	}
	newest := list[len(list)-1]
	if now.Sub(newest.idleAt) > p.idleTimeout {
		delete(p.idle, k)
		p.mu.Unlock()
		for _, pc := range list {
			pc.Close()
		}
		return nil
	}
	p.idle[k] = list[:len(list)-1]
	p.mu.Unlock()
	return newest
}

// put returns a healthy conn to the pool; drops it if the pool is full or
// closed.
func (p *connPool) put(bindIP, addr string, pc *pooledConn) {
	pc.idleAt = time.Now()
	k := poolKey{bindIP: bindIP, addr: addr}
	p.mu.Lock()
	if p.closed || len(p.idle[k]) >= p.maxPerKey {
		p.mu.Unlock()
		pc.Close()
		return
	}
	p.idle[k] = append(p.idle[k], pc)
	p.mu.Unlock()
}

// janitor evicts idle-expired conns so sockets don't linger after bursts.
func (p *connPool) janitor() {
	interval := p.idleTimeout / 2
	if interval < time.Second {
		interval = time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-p.stop:
			return
		case now := <-t.C:
			var expired []*pooledConn
			p.mu.Lock()
			for k, list := range p.idle {
				i := 0
				for ; i < len(list); i++ {
					if now.Sub(list[i].idleAt) <= p.idleTimeout {
						break
					}
				}
				if i == 0 {
					continue
				}
				expired = append(expired, list[:i]...)
				rest := list[i:]
				if len(rest) == 0 {
					delete(p.idle, k)
				} else {
					p.idle[k] = append(list[:0], rest...)
				}
			}
			p.mu.Unlock()
			for _, pc := range expired {
				pc.Close()
			}
		}
	}
}

func (p *connPool) Close() {
	p.stopOnce.Do(func() { close(p.stop) })
	p.mu.Lock()
	p.closed = true
	all := p.idle
	p.idle = make(map[poolKey][]*pooledConn)
	p.mu.Unlock()
	for _, list := range all {
		for _, pc := range list {
			pc.Close()
		}
	}
}
