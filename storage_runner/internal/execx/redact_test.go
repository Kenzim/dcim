package execx

import (
	"context"
	"strings"
	"testing"
)

// fakeKey has the shape of an aes256k cephx secret; it is not a real key.
const fakeKey = "AgBkZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZWtleWZha2Vr0123abcd="

func TestRedactRemovesCephKeyMaterial(t *testing.T) {
	cases := []string{
		"auth: error setting modifier for [client.admin] type=key val=" + fakeKey + ": Malformed input [buffer:3]",
		"[client.rf-t2]\n\tkey = " + fakeKey,
		"mount error: secret=" + fakeKey + ",name=rf-t2",
		"unexpected token " + fakeKey + " in keyring",
		"old style key AQBkZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZQ== leaked",
	}
	for _, in := range cases {
		out := Redact(in)
		if strings.Contains(out, fakeKey) || strings.Contains(out, "AQBkZmFr") {
			t.Errorf("key material left in %q", out)
		}
		if !strings.Contains(out, "[redacted]") {
			t.Errorf("nothing redacted in %q", out)
		}
	}
}

func TestRedactKeepsOrdinaryErrors(t *testing.T) {
	in := "mount -t ceph :/volumes/rackflow/t2-s3/78da4eb2-32d7-4ee5-ac86-762e2b33549c /run/x -o name=rf-t2,secretfile=/var/lib/x/secret: mount error 13 = Permission denied"
	if out := Redact(in); out != in {
		t.Errorf("ordinary error changed:\n%s\n%s", in, out)
	}
}

func TestExecErrorsAreRedacted(t *testing.T) {
	_, err := Exec{}.Run(context.Background(), nil, "sh", "-c", "echo 'type=key val="+fakeKey+"' >&2; exit 3")
	if err == nil {
		t.Fatal("expected an error")
	}
	if strings.Contains(err.Error(), fakeKey) {
		t.Errorf("key in error: %v", err)
	}
}
