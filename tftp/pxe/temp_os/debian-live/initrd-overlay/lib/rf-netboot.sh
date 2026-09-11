#!/bin/sh
# Rackflow netboot helpers. Sourced from /init (PID 1) and unit-tested with fake sysfs.
# Override RF_SYSFS_NET / RF_SLEEP for tests. Do not assume bash.

rf_sysfs_net="${RF_SYSFS_NET:-/sys/class/net}"

rf_sleep() {
	${RF_SLEEP:-sleep} "$1"
}

rf_echo() {
	# Logs on stderr so $(rf_select_iface ...) only captures the iface name.
	printf 'rf-netboot: %s\n' "$*" >&2
}

rf_normalize_mac() {
	# Lowercase colon MAC (aa:bb:cc:dd:ee:ff). Empty if not 6 octets.
	raw=$(printf '%s' "$1" | tr 'A-Z' 'a-z' | sed 's/[^0-9a-f]//g')
	if [ "${#raw}" -ne 12 ]; then
		return 1
	fi
	printf '%s\n' "$raw" | sed 's/\(..\)/\1:/g; s/:$//'
}

rf_mac_from_bootif() {
	# PXELINUX BOOTIF=01-aa-bb-cc-dd-ee-ff (leading 01- optional).
	val=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
	case "$val" in
	01-*) val=${val#01-} ;;
	01:*) val=${val#01:} ;;
	esac
	rf_normalize_mac "$val"
}

rf_cmdline_text() {
	if [ -n "${RF_CMDLINE+x}" ]; then
		printf '%s' "$RF_CMDLINE"
	elif [ -r /proc/cmdline ]; then
		cat /proc/cmdline
	fi
}

rf_cmdline_get() {
	# Print first cmdline value for key= (RF_CMDLINE or /proc/cmdline).
	key="$1"
	# shellcheck disable=SC2086
	for tok in $(rf_cmdline_text); do
		case "$tok" in
		"${key}="*)
			printf '%s\n' "${tok#*=}"
			return 0
			;;
		esac
	done
	return 1
}

rf_wanted_mac() {
	mac=$(rf_cmdline_get rf_pxe_mac 2>/dev/null) || mac=""
	if [ -n "$mac" ]; then
		rf_normalize_mac "$mac" && return 0
	fi
	bootif=$(rf_cmdline_get BOOTIF 2>/dev/null) || bootif=""
	if [ -n "$bootif" ]; then
		rf_mac_from_bootif "$bootif" && return 0
	fi
	return 1
}

rf_is_skipped_iface() {
	case "$1" in
	lo|usb*)
		return 0
		;;
	esac
	return 1
}

rf_iface_address() {
	if [ -r "${rf_sysfs_net}/$1/address" ]; then
		tr -d ' \n' <"${rf_sysfs_net}/$1/address"
	fi
}

rf_iface_carrier() {
	if [ -r "${rf_sysfs_net}/$1/carrier" ]; then
		tr -d ' \n' <"${rf_sysfs_net}/$1/carrier"
	else
		printf '0'
	fi
}

rf_find_iface_by_mac() {
	want=$(rf_normalize_mac "$1") || return 1
	for dir in "${rf_sysfs_net}"/*; do
		[ -e "$dir" ] || continue
		name=${dir##*/}
		rf_is_skipped_iface "$name" && continue
		[ -f "$dir/address" ] || continue
		have=$(rf_normalize_mac "$(cat "$dir/address")") || continue
		if [ "$have" = "$want" ]; then
			printf '%s\n' "$name"
			return 0
		fi
	done
	return 1
}

rf_dump_ifaces() {
	rf_echo "wanted MAC: ${1:-unset}"
	for dir in "${rf_sysfs_net}"/*; do
		[ -e "$dir" ] || continue
		name=${dir##*/}
		addr=$(rf_iface_address "$name")
		carrier=$(rf_iface_carrier "$name")
		oper=""
		[ -r "$dir/operstate" ] && oper=$(tr -d ' \n' <"$dir/operstate")
		rf_echo "  iface=${name} addr=${addr} carrier=${carrier} operstate=${oper}"
	done
}

rf_wait_iface_by_mac() {
	mac="$1"
	timeout="$2"
	i=0
	while [ "$i" -lt "$timeout" ]; do
		if iface=$(rf_find_iface_by_mac "$mac"); then
			printf '%s\n' "$iface"
			return 0
		fi
		rf_sleep 1
		i=$((i + 1))
	done
	return 1
}

rf_bring_up() {
	iface="$1"
	if [ -n "$RF_SYSFS_NET" ]; then
		return 0
	fi
	ip link set "$iface" up 2>/dev/null || true
}

rf_wait_stable_carrier() {
	iface="$1"
	timeout="$2"
	stable="$3"
	rf_bring_up "$iface"
	good=0
	i=0
	while [ "$i" -lt "$timeout" ]; do
		carrier=$(rf_iface_carrier "$iface")
		if [ "$carrier" = "1" ]; then
			good=$((good + 1))
			rf_echo "${iface} carrier=1 (${good}/${stable})"
			if [ "$good" -ge "$stable" ]; then
				return 0
			fi
		else
			good=0
			rf_echo "${iface} carrier=${carrier:-0}"
		fi
		rf_sleep 1
		i=$((i + 1))
	done
	return 1
}

rf_fallback_iface() {
	timeout="$1"
	stable="$2"
	i=0
	while [ "$i" -lt "$timeout" ]; do
		for dir in "${rf_sysfs_net}"/*; do
			[ -e "$dir" ] || continue
			name=${dir##*/}
			rf_is_skipped_iface "$name" && continue
			rf_bring_up "$name"
			if rf_wait_stable_carrier "$name" "$stable" "$stable"; then
				printf '%s\n' "$name"
				return 0
			fi
		done
		rf_sleep 1
		i=$((i + 1))
	done
	return 1
}

rf_select_iface() {
	timeout="$1"
	stable="$2"
	mac=""
	mac=$(rf_wanted_mac) || mac=""
	if [ -n "$mac" ]; then
		rf_echo "waiting for PXE MAC ${mac} (timeout ${timeout}s, stable ${stable}s)"
		if ! iface=$(rf_wait_iface_by_mac "$mac" "$timeout"); then
			rf_echo "MAC ${mac} not found in sysfs"
			return 1
		fi
		rf_echo "matched ${iface} for ${mac}"
		if ! rf_wait_stable_carrier "$iface" "$timeout" "$stable"; then
			rf_echo "no stable carrier on ${iface}"
			return 1
		fi
		printf '%s\n' "$iface"
		return 0
	fi
	rf_echo "no rf_pxe_mac/BOOTIF; falling back to first non-usb iface with stable carrier"
	rf_fallback_iface "$timeout" "$stable"
}

rf_udhcpc_handler() {
	# Busybox udhcpc -s script. Env: interface ip mask router dns domain broadcast.
	case "$1" in
	deconfig)
		ip addr flush dev "$interface" 2>/dev/null || true
		ip link set "$interface" up 2>/dev/null || true
		;;
	bound|renew)
		ip link set "$interface" up 2>/dev/null || true
		ip addr flush dev "$interface" 2>/dev/null || true
		if [ -n "$mask" ]; then
			ip addr add "$ip/$mask" broadcast "${broadcast:-+}" dev "$interface" 2>/dev/null \
				|| ip addr add "$ip" peer "$mask" dev "$interface" 2>/dev/null \
				|| ifconfig "$interface" "$ip" netmask "$mask"
		else
			ip addr add "$ip" dev "$interface"
		fi
		if [ -n "$router" ]; then
			ip route del default 2>/dev/null || true
			for r in $router; do
				ip route add default via "$r" dev "$interface" 2>/dev/null || true
			done
		fi
		if [ -n "$dns" ]; then
			mkdir -p /etc
			: >/etc/resolv.conf
			[ -n "$domain" ] && printf 'search %s\n' "$domain" >>/etc/resolv.conf
			for d in $dns; do
				printf 'nameserver %s\n' "$d" >>/etc/resolv.conf
			done
		fi
		;;
	leasefail|nak)
		return 1
		;;
	esac
	return 0
}

rf_dhcp() {
	iface="$1"
	timeout="${2:-60}"
	ip link set "$iface" up 2>/dev/null || true
	if command -v ipconfig >/dev/null 2>&1; then
		rf_echo "DHCP ipconfig -t ${timeout} ${iface}"
		if ipconfig -t "$timeout" "$iface"; then
			if [ -f "/run/net-${iface}.conf" ]; then
				# shellcheck disable=SC1090
				. "/run/net-${iface}.conf"
				if [ -n "${IPV4ADDR:-}" ] && [ "${IPV4ADDR}" != "0.0.0.0" ]; then
					rf_echo "DHCP ${iface} ${IPV4ADDR}"
					return 0
				fi
			fi
			# ipconfig may have configured the iface without a conf file
			if ip -4 addr show dev "$iface" 2>/dev/null | grep -q 'inet '; then
				return 0
			fi
		fi
		rf_echo "ipconfig failed on ${iface}; trying udhcpc"
	fi
	script="/lib/rf-udhcpc.script"
	if [ ! -x "$script" ]; then
		script="/tmp/rf-udhcpc.script"
		# shellcheck disable=SC2016
		printf '%s\n' '#!/bin/sh' '. /lib/rf-netboot.sh' 'rf_udhcpc_handler "$@"' >"$script"
		chmod 755 "$script"
	fi
	rf_echo "DHCP udhcpc -i ${iface}"
	udhcpc -i "$iface" -n -q -t 10 -T 3 -s "$script"
}

rf_fetch_url() {
	rf_cmdline_get fetch || rf_cmdline_get FETCH
}

rf_fetch_squashfs() {
	url="$1"
	dest="$2"
	rf_echo "fetch ${url}"
	mkdir -p "$(dirname "$dest")"
	if command -v wget >/dev/null 2>&1; then
		wget -O "$dest" -T 60 --no-check-certificate "$url"
	else
		rf_echo "wget missing"
		return 1
	fi
	[ -s "$dest" ]
}

rf_mount_live_root() {
	img="$1"
	rootmnt="${2:-/root}"
	modprobe loop 2>/dev/null || true
	modprobe squashfs 2>/dev/null || true
	modprobe overlay 2>/dev/null || true
	mkdir -p /run/live/rootfs/filesystem.squashfs /run/live/overlay "$rootmnt"
	lower=/run/live/rootfs/filesystem.squashfs
	if ! mount -t squashfs -o ro,loop "$img" "$lower" 2>/dev/null; then
		mount -t squashfs -o ro "$img" "$lower" || return 1
	fi
	mount -t tmpfs -o rw,noatime,mode=755,size=50% tmpfs /run/live/overlay || return 1
	mkdir -p /run/live/overlay/rw /run/live/overlay/work
	mount -t overlay overlay \
		-o "noatime,lowerdir=${lower},upperdir=/run/live/overlay/rw,workdir=/run/live/overlay/work,redirect_dir=on" \
		"$rootmnt" || return 1
	chmod 0755 "$rootmnt"
	if [ -d "${rootmnt}/tmp" ]; then
		chmod 1777 "${rootmnt}/tmp"
	fi
	return 0
}

rf_netboot_prepare() {
	timeout=$(rf_cmdline_get rf_link_timeout 2>/dev/null) || timeout=""
	stable=$(rf_cmdline_get rf_link_stable 2>/dev/null) || stable=""
	[ -n "$timeout" ] || timeout=120
	[ -n "$stable" ] || stable=3
	mac=$(rf_wanted_mac) || mac=""

	if ! iface=$(rf_select_iface "$timeout" "$stable"); then
		rf_dump_ifaces "$mac"
		return 1
	fi
	export DEVICE="$iface"

	if ! rf_dhcp "$iface" "$timeout"; then
		rf_echo "DHCP failed on ${iface}"
		rf_dump_ifaces "$mac"
		return 1
	fi

	url=$(rf_fetch_url) || url=""
	if [ -z "$url" ]; then
		rf_echo "no fetch= on cmdline"
		return 1
	fi

	# ramfs is unbounded (tmpfs /run is often only 10%)
	mkdir -p /run/live/medium
	mount -t ramfs ram /run/live/medium 2>/dev/null || true
	mkdir -p /run/live/medium/live
	base=$(printf '%s' "$url" | sed 's|.*/||')
	[ -n "$base" ] || base="filesystem.squashfs"
	img="/run/live/medium/live/${base}"
	if ! rf_fetch_squashfs "$url" "$img"; then
		rf_echo "failed to fetch ${url}"
		return 1
	fi
	rf_echo "fetched $(wc -c <"$img") bytes"

	rootmnt="${rootmnt:-/root}"
	if ! rf_mount_live_root "$img" "$rootmnt"; then
		rf_echo "overlay mount failed"
		return 1
	fi
	rf_echo "live root mounted at ${rootmnt} via ${iface}"
	return 0
}

# Allow: sh rf-netboot.sh rf_normalize_mac aa:bb:...
if [ "${0##*/}" = "rf-netboot.sh" ] && [ -n "$1" ]; then
	cmd="$1"
	shift
	"$cmd" "$@"
fi
