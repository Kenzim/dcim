package firewall

import (
	"context"
	"sync"

	"rackflow/storage_runner/internal/record"
)

// Fake records rulesets that differ from the last one applied.
type Fake struct {
	Rec *record.Recorder
	Err error

	mu   sync.Mutex
	last string
	set  bool
}

func NewFake(rec *record.Recorder) *Fake { return &Fake{Rec: rec} }

func (f *Fake) Apply(_ context.Context, rs Ruleset) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return false, f.Err
	}
	s := rs.String()
	if f.set && s == f.last {
		return false, nil
	}
	f.last, f.set = s, true
	f.Rec.Add("fw apply %s", s)
	return true, nil
}
