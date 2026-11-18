// Package execx runs host commands behind a small interface so drivers can be
// tested with a scripted fake.
package execx

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Commander runs one command and returns its standard output.
type Commander interface {
	Run(ctx context.Context, stdin []byte, name string, args ...string) ([]byte, error)
}

// Exec runs real processes. Errors include the command line and its stderr.
type Exec struct {
	Timeout time.Duration
}

func (e Exec) Run(ctx context.Context, stdin []byte, name string, args ...string) ([]byte, error) {
	if e.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, e.Timeout)
		defer cancel()
	}
	cmd := exec.CommandContext(ctx, name, args...)
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		} else {
			msg += " (" + err.Error() + ")"
		}
		return stdout.Bytes(), fmt.Errorf("%s: %s", Line(name, args...), msg)
	}
	return stdout.Bytes(), nil
}

// Line renders a command for logs and test expectations. Arguments that are
// empty or contain spaces are quoted.
func Line(name string, args ...string) string {
	parts := make([]string, 0, len(args)+1)
	parts = append(parts, name)
	for _, a := range args {
		if a == "" || strings.ContainsAny(a, " \t\n'\"") {
			parts = append(parts, fmt.Sprintf("%q", a))
		} else {
			parts = append(parts, a)
		}
	}
	return strings.Join(parts, " ")
}
