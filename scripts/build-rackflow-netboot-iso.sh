#!/bin/bash
# Build a BIOS+UEFI iPXE ISO that DHCP/autoboot into Rackflow PXE.
# Output: isos/rackflow-netboot.iso (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${ROOT}/scripts/rackflow-netboot.ipxe"
OUT="${ROOT}/isos/rackflow-netboot.iso"
WORKDIR="${ROOT}/.cursor/ipxe-build"
SRC="${WORKDIR}/ipxe/src"

if [ ! -f "$SCRIPT" ]; then
    echo "missing $SCRIPT" >&2
    exit 1
fi

mkdir -p "${ROOT}/isos" "$WORKDIR"
if [ ! -d "$SRC" ]; then
    git clone --depth 1 https://github.com/ipxe/ipxe.git "${WORKDIR}/ipxe"
fi

# BIOS kernel + UEFI EFI binary, both with the same embedded script.
make -C "$SRC" -j"$(nproc)" bin/ipxe.lkrn EMBED="$SCRIPT"
make -C "$SRC" -j"$(nproc)" bin-x86_64-efi/ipxe.efi EMBED="$SCRIPT"
"$SRC/util/genfsimg" -o "$OUT" "${SRC}/bin/ipxe.lkrn" "${SRC}/bin-x86_64-efi/ipxe.efi"
ls -l "$OUT"
echo "Wrote $OUT"
