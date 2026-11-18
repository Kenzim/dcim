package execx

import (
	"context"
	"sync"
)

// Script is a fake Commander. Commands are matched by their Line rendering;
// unknown commands succeed with empty output. Every call is recorded.
type Script struct {
	mu        sync.Mutex
	responses map[string]response
	calls     []Call
}

type Call struct {
	Line  string
	Stdin string
}

type response struct {
	out string
	err error
}

func NewScript() *Script {
	return &Script{responses: map[string]response{}}
}

// On sets the output returned for a command line.
func (s *Script) On(line, out string) *Script {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.responses[line] = response{out: out}
	return s
}

// Fail makes a command line return err.
func (s *Script) Fail(line string, err error) *Script {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.responses[line] = response{err: err}
	return s
}

func (s *Script) Run(_ context.Context, stdin []byte, name string, args ...string) ([]byte, error) {
	line := Line(name, args...)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, Call{Line: line, Stdin: string(stdin)})
	r := s.responses[line]
	if r.err != nil {
		return nil, r.err
	}
	return []byte(r.out), nil
}

// Lines returns the command lines run so far.
func (s *Script) Lines() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, len(s.calls))
	for i, c := range s.calls {
		out[i] = c.Line
	}
	return out
}

// Calls returns every call with its stdin.
func (s *Script) Calls() []Call {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Call(nil), s.calls...)
}

// Reset forgets recorded calls but keeps responses.
func (s *Script) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = nil
}
