// Package record collects the mutating actions of fake drivers in order, so
// reconcile tests can assert exactly what would have changed on a host.
package record

import (
	"fmt"
	"sync"
)

type Recorder struct {
	mu      sync.Mutex
	actions []string
}

func (r *Recorder) Add(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.actions = append(r.actions, fmt.Sprintf(format, args...))
}

// Take returns the recorded actions and clears the log.
func (r *Recorder) Take() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.actions
	r.actions = nil
	return out
}
