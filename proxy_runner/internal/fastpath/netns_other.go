//go:build !(linux && (amd64 || arm64))

package fastpath

import (
	"errors"
	"net"
)

// ListenInNetns needs Linux network namespaces.
func ListenInNetns(namespace string, port int) (net.Listener, error) {
	return nil, errors.New("the proxy fast path needs Linux network namespaces on amd64 or arm64")
}
