// Package execx runs host commands behind a small interface so drivers can be
// tested with a scripted fake.
package execx

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

var (
	// key=..., val=..., secret=... as printed by Ceph tools, for example when a keyring fails to parse.
	keyAssignment = regexp.MustCompile(`(?i)\b(key|val|secret)(\s*=\s*)[A-Za-z0-9+/=]{16,}`)
	// A bare base64 cephx secret: they start with AQ (aes) or Ag (aes256k).
	cephxSecret = regexp.MustCompile(`\bA[Qg][A-Za-z0-9+/]{30,}={0,2}`)
)

// Redact removes Ceph key material from command output before it is logged or
// reported to Rackflow.
func Redact(s string) string {
	s = keyAssignment.ReplaceAllString(s, "${1}${2}[redacted]")
	return cephxSecret.ReplaceAllString(s, "[redacted]")
}

// Commander runs one command and returns its standard output.
type Commander interface {
	Run(ctx context.Context, stdin []byte, name string, args ...string) ([]byte, error)
}

// Exec runs real processes. Errors include the command line and its stderr,
// with Ceph key material redacted.
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
		return stdout.Bytes(), fmt.Errorf("%s", Redact(Line(name, args...)+": "+msg))
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
