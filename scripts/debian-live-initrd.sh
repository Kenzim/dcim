#!/usr/bin/env bash
# Pack or unpack a Debian live initrd, replacing /init with Rackflow netboot.
# Preserves concatenated early uncompressed cpio (microcode/firmware) and
# recompresses the main payload with the same codec as the donor (usually zstd).
#
# Usage:
#   scripts/debian-live-initrd.sh pack --donor INITRD --overlay DIR --output INITRD
#   scripts/debian-live-initrd.sh unpack --input INITRD --dir TREE
set -euo pipefail

usage() {
	sed -n '2,12p' "$0" | sed 's/^# \?//'
	exit 2
}

need_cmd() {
	command -v "$1" >/dev/null 2>&1 || {
		echo "error: missing command: $1" >&2
		exit 1
	}
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
DEFAULT_OVERLAY="${REPO_ROOT}/tftp/pxe/temp_os/debian-live/initrd-overlay"

split_initrd_py() {
	python3 - "$1" "$2" <<'PY'
import os, sys

path, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)
with open(path, "rb") as f:
    data = f.read()

COMP = (
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"\x02\x21\x4c\x18", "lz4"),
)

def peek_comp(offset: int):
    for magic, name in COMP:
        if data.startswith(magic, offset):
            return name
    return None

def skip_cpio(offset: int) -> int:
    while offset + 110 <= len(data):
        magic = data[offset : offset + 6]
        if magic not in (b"070701", b"070702"):
            raise SystemExit(f"not a newc cpio header at {offset}")
        namesize = int(data[offset + 94 : offset + 102], 16)
        filesize = int(data[offset + 54 : offset + 62], 16)
        name_start = offset + 110
        name_end = name_start + namesize
        pad1 = (4 - ((110 + namesize) % 4)) % 4
        data_start = name_end + pad1
        data_end = data_start + filesize
        pad2 = (4 - (filesize % 4)) % 4
        offset = data_end + pad2
        name = data[name_start:name_end].split(b"\0", 1)[0]
        if name == b"TRAILER!!!":
            return offset
    raise SystemExit("truncated cpio archive")

offset = 0
archives = 0
while offset < len(data):
    while offset < len(data) and data[offset] == 0:
        offset += 1
    if offset >= len(data):
        break
    kind = peek_comp(offset)
    if kind:
        early = data[:offset]
        payload = data[offset:]
        with open(os.path.join(outdir, "early.cpio"), "wb") as f:
            f.write(early)
        with open(os.path.join(outdir, "main.bin"), "wb") as f:
            f.write(payload)
        with open(os.path.join(outdir, "codec"), "w") as f:
            f.write(kind)
        with open(os.path.join(outdir, "early_size"), "w") as f:
            f.write(str(offset))
        print(f"early_cpio_bytes={offset} codec={kind} archives={archives}")
        sys.exit(0)
    if data[offset : offset + 6] in (b"070701", b"070702"):
        offset = skip_cpio(offset)
        archives += 1
        continue
    raise SystemExit(f"unrecognized initrd data at offset {offset}")

raise SystemExit("no compressed payload found in initrd")
PY
}

decompress_main() {
	local codec="$1" src="$2" dst="$3"
	case "$codec" in
	zstd) zstd -d -c "$src" >"$dst" ;;
	gzip) gzip -d -c "$src" >"$dst" ;;
	xz) xz -d -c "$src" >"$dst" ;;
	bzip2) bunzip2 -c "$src" >"$dst" ;;
	lz4) lz4 -d -c "$src" >"$dst" ;;
	*)
		echo "error: unsupported codec $codec" >&2
		exit 1
		;;
	esac
}

compress_main() {
	local codec="$1" src="$2" dst="$3"
	case "$codec" in
	zstd) zstd -T0 -10 -c "$src" >"$dst" ;;
	gzip) gzip -n -c "$src" >"$dst" ;;
	xz) xz -c "$src" >"$dst" ;;
	bzip2) bzip2 -c "$src" >"$dst" ;;
	lz4) lz4 -c "$src" >"$dst" ;;
	*)
		echo "error: unsupported codec $codec" >&2
		exit 1
		;;
	esac
}

cmd_unpack() {
	local input="" tree=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--input)
			input="$2"
			shift 2
			;;
		--dir)
			tree="$2"
			shift 2
			;;
		-h | --help) usage ;;
		*)
			echo "unknown argument: $1" >&2
			usage
			;;
		esac
	done
	[ -n "$input" ] && [ -n "$tree" ] || usage
	[ -f "$input" ] || {
		echo "error: donor not found: $input" >&2
		exit 1
	}
	need_cmd python3
	need_cmd cpio
	need_cmd zstd

	work=$(mktemp -d)
	trap 'rm -rf "$work"' EXIT
	split_initrd_py "$input" "$work"
	codec=$(cat "$work/codec")
	need_cmd "$codec"
	decompress_main "$codec" "$work/main.bin" "$work/main.cpio"
	mkdir -p "$tree"
	(cd "$tree" && cpio -idmu --quiet <"$work/main.cpio")
	# Keep early archive next to the tree so pack can reuse it.
	cp -a "$work/early.cpio" "$tree/.rf-early.cpio"
	cp -a "$work/codec" "$tree/.rf-codec"
	echo "unpacked main initramfs into $tree"
}

cmd_pack() {
	local donor="" overlay="$DEFAULT_OVERLAY" output=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--donor)
			donor="$2"
			shift 2
			;;
		--overlay)
			overlay="$2"
			shift 2
			;;
		--output)
			output="$2"
			shift 2
			;;
		-h | --help) usage ;;
		*)
			echo "unknown argument: $1" >&2
			usage
			;;
		esac
	done
	[ -n "$donor" ] && [ -n "$output" ] || usage
	[ -f "$donor" ] || {
		echo "error: donor not found: $donor" >&2
		exit 1
	}
	[ -d "$overlay" ] || {
		echo "error: overlay not found: $overlay" >&2
		exit 1
	}
	[ -f "$overlay/init" ] || {
		echo "error: overlay missing /init: $overlay/init" >&2
		exit 1
	}
	need_cmd python3
	need_cmd cpio
	need_cmd zstd

	work=$(mktemp -d)
	trap 'rm -rf "$work"' EXIT
	split_initrd_py "$donor" "$work"
	codec=$(cat "$work/codec")
	need_cmd "$codec"
	decompress_main "$codec" "$work/main.bin" "$work/main.cpio"
	mkdir -p "$work/tree"
	(cd "$work/tree" && cpio -idmu --quiet <"$work/main.cpio")
	# Copy overlay files individually. Donor /lib is often a symlink to usr/lib;
	# a bulk cp -a of overlay/lib would fail with "cannot overwrite non-directory".
	while IFS= read -r -d '' rel; do
		dest="$work/tree/$rel"
		mkdir -p "$(dirname "$dest")"
		cp -a "$overlay/$rel" "$dest"
	done < <(cd "$overlay" && find . -mindepth 1 \( -type f -o -type l \) -print0)
	chmod 0755 "$work/tree/init"
	chmod 0755 "$work/tree/lib/rf-netboot.sh" "$work/tree/lib/rf-udhcpc.script" 2>/dev/null || true
	# Drop live-boot entry point so even a stale BOOT=live cannot call it.
	if [ -e "$work/tree/scripts/live" ]; then
		printf '%s\n' '#!/bin/sh' 'echo "rf-netboot: live-boot disabled"' >"$work/tree/scripts/live"
		chmod 0755 "$work/tree/scripts/live"
	fi
	(cd "$work/tree" && find . -print0 | cpio --null --quiet -R 0:0 -o -H newc) >"$work/new.cpio"
	compress_main "$codec" "$work/new.cpio" "$work/new.bin"
	mkdir -p "$(dirname "$output")"
	cat "$work/early.cpio" "$work/new.bin" >"$output"
	chmod 0644 "$output"
	echo "packed $output ($(wc -c <"$output") bytes, codec=$codec)"
}

main() {
	[ $# -ge 1 ] || usage
	case "$1" in
	pack)
		shift
		cmd_pack "$@"
		;;
	unpack)
		shift
		cmd_unpack "$@"
		;;
	-h | --help) usage ;;
	*)
		echo "unknown command: $1" >&2
		usage
		;;
	esac
}

main "$@"
