// Package names holds the naming scheme for every host object the runner
// owns. Everything the runner creates carries an "rf-" or "t<id>" prefix so
// objects created by anyone else are never matched.
package names

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// MaxTenantID keeps "rf-h<id>" within the 15-character Linux interface name limit.
const MaxTenantID = 99999999999

// NetnsDir is where `ip netns` keeps named namespaces.
const NetnsDir = "/run/netns"

func Namespace(tenant int64) string  { return fmt.Sprintf("rf-t%d", tenant) }
func VLANIface(tenant int64) string  { return fmt.Sprintf("rf-v%d", tenant) }
func HostVeth(tenant int64) string   { return fmt.Sprintf("rf-h%d", tenant) }
func NSVeth(tenant int64) string     { return fmt.Sprintf("rf-n%d", tenant) }
func Container(tenant int64) string  { return fmt.Sprintf("rf-t%d-nfs", tenant) }
func CephUserID(tenant int64) string { return fmt.Sprintf("%s%d", cephUserPrefix, tenant) }
func CephEntity(tenant int64) string { return "client." + CephUserID(tenant) }
func TenantDir(tenant int64) string  { return fmt.Sprintf("t%d", tenant) }
func NetnsPath(tenant int64) string  { return NetnsDir + "/" + Namespace(tenant) }

// Presence is the namespace of a network presence: a private network's
// storage address with no storage behind it.
func Presence(network int64) string { return fmt.Sprintf("rf-p%d", network) }

// PresenceIface is the VLAN interface of a network presence.
func PresenceIface(network int64) string { return fmt.Sprintf("rf-w%d", network) }

// DHCPKey names a DHCP server and its lease file: "net-<id>" for a network
// presence, "tenant-<id>" for a tenant.
func NetworkDHCPKey(network int64) string { return fmt.Sprintf("net-%d", network) }
func TenantDHCPKey(tenant int64) string   { return fmt.Sprintf("tenant-%d", tenant) }

// DefaultCephUserPrefix names tenant cephx users client.rf-t<id>.
const DefaultCephUserPrefix = "rf-t"

// cephUserPrefix starts every tenant cephx user name. cephx users are
// cluster-wide, so two Rackflow installations sharing one Ceph cluster need
// different prefixes. It is set once at start-up, before any other use.
var (
	cephUserPrefix = DefaultCephUserPrefix
	entityRe       = entityRegexp(DefaultCephUserPrefix)
)

// SetCephUserPrefix changes the tenant cephx user prefix. Call it only at
// start-up, before anything else in the runner runs.
func SetCephUserPrefix(prefix string) {
	cephUserPrefix = prefix
	entityRe = entityRegexp(prefix)
}

func entityRegexp(prefix string) *regexp.Regexp {
	return regexp.MustCompile(`^client\.` + regexp.QuoteMeta(prefix) + `([1-9][0-9]{0,10})$`)
}

// Subvolume is the CephFS subvolume name of a share.
func Subvolume(tenant, share int64) string { return fmt.Sprintf("t%d-s%d", tenant, share) }

var (
	namespaceRe = regexp.MustCompile(`^rf-t([1-9][0-9]{0,10})$`)
	presenceRe  = regexp.MustCompile(`^rf-p([1-9][0-9]{0,10})$`)
	containerRe = regexp.MustCompile(`^rf-t([1-9][0-9]{0,10})-nfs$`)
	subvolumeRe = regexp.MustCompile(`^t([1-9][0-9]{0,17})-s([1-9][0-9]{0,17})$`)
)

func parseID(re *regexp.Regexp, s string) (int64, bool) {
	m := re.FindStringSubmatch(s)
	if m == nil {
		return 0, false
	}
	id, err := strconv.ParseInt(m[1], 10, 64)
	return id, err == nil
}

// SubvolumeOfPath returns the runner-named subvolume of group whose path
// (as ceph fs subvolume getpath prints it) is path.
func SubvolumeOfPath(group, path string) (string, bool) {
	rest, ok := strings.CutPrefix(path, "/volumes/"+group+"/")
	if !ok {
		return "", false
	}
	name, uuid, ok := strings.Cut(rest, "/")
	if !ok || uuid == "" || strings.Contains(uuid, "/") {
		return "", false
	}
	if _, _, ok := ParseSubvolume(name); !ok {
		return "", false
	}
	return name, true
}

func ParseNamespace(s string) (int64, bool)  { return parseID(namespaceRe, s) }
func ParsePresence(s string) (int64, bool)   { return parseID(presenceRe, s) }
func ParseCephEntity(s string) (int64, bool) { return parseID(entityRe, s) }
func ParseContainer(s string) (int64, bool)  { return parseID(containerRe, s) }

// ParseSubvolume returns the tenant and share ids of a runner-named subvolume.
func ParseSubvolume(s string) (tenant, share int64, ok bool) {
	m := subvolumeRe.FindStringSubmatch(s)
	if m == nil {
		return 0, 0, false
	}
	t, err1 := strconv.ParseInt(m[1], 10, 64)
	sh, err2 := strconv.ParseInt(m[2], 10, 64)
	if err1 != nil || err2 != nil {
		return 0, 0, false
	}
	return t, sh, true
}
