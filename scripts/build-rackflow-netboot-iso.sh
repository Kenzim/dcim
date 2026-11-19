#!/bin/bash
# Build the Rackflow iPXE ISOs (both gitignored):
#   boot_media/rackflow-vmedia.iso  Template for per-boot virtual-media ISOs. Contains
#                                   boot_media/placeholder.ipxe as autoexec.ipxe (BIOS
#                                   ISO9660 + UEFI ESP); Rackflow stamps each boot's
#                                   nonce/URL over the placeholder at serve time. HTTPS,
#                                   trusting ISRG Root X1/X2 (+ RACKFLOW_IPXE_EXTRA_TRUST).
#   isos/rackflow-netboot.iso       Legacy netboot ISO that DHCP/autoboots into PXE.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NETBOOT_SCRIPT="${ROOT}/scripts/rackflow-netboot.ipxe"
NETBOOT_OUT="${ROOT}/isos/rackflow-netboot.iso"
PLACEHOLDER="${ROOT}/boot_media/placeholder.ipxe"
VMEDIA_OUT="${ROOT}/boot_media/rackflow-vmedia.iso"
PLACEHOLDER_MARKER="#RACKFLOW-VMEDIA-CONFIG-v1"
WORKDIR="${ROOT}/.cursor/ipxe-build"
SRC="${WORKDIR}/ipxe/src"
TRUSTDIR="${WORKDIR}/trust"

# SHA-256 fingerprints of the DER certificates (letsencrypt.org/certificates).
ISRG_X1_SHA256="96:BC:EC:06:26:49:76:F3:74:60:77:9A:CF:28:C5:A7:CF:E8:A3:C0:AA:E1:1A:8F:FC:EE:05:C0:BD:DF:08:C6"
ISRG_X2_SHA256="69:72:9B:8E:15:A8:6E:FC:17:7A:57:AF:B7:17:1D:FC:64:AD:D2:8C:2F:CA:8C:F1:50:7E:34:45:3C:CB:14:70"

for f in "$NETBOOT_SCRIPT" "$PLACEHOLDER"; do
    if [ ! -f "$f" ]; then
        echo "missing $f" >&2
        exit 1
    fi
done
if [ "$(wc -c <"$PLACEHOLDER")" -ne 2048 ]; then
    echo "$PLACEHOLDER must be exactly 2048 bytes" >&2
    exit 1
fi

mkdir -p "${ROOT}/isos" "${ROOT}/boot_media" "$WORKDIR" "$TRUSTDIR"
if [ ! -d "$SRC" ]; then
    git clone --depth 1 https://github.com/ipxe/ipxe.git "${WORKDIR}/ipxe"
fi

fetch_root() {
    local name="$1" want="$2" dest="${TRUSTDIR}/$1.pem" got
    [ -f "$dest" ] || curl -fsS -o "$dest" "https://letsencrypt.org/certs/${name}.pem"
    got="$(openssl x509 -in "$dest" -noout -fingerprint -sha256 | cut -d= -f2)"
    if [ "$got" != "$want" ]; then
        echo "fingerprint mismatch for ${name}: ${got}" >&2
        rm -f "$dest"
        exit 1
    fi
    echo "$dest"
}

TRUST="$(fetch_root isrgrootx1 "$ISRG_X1_SHA256"),$(fetch_root isrg-root-x2 "$ISRG_X2_SHA256")"
if [ -n "${RACKFLOW_IPXE_EXTRA_TRUST:-}" ]; then
    TRUST="${TRUST},${RACKFLOW_IPXE_EXTRA_TRUST}"
fi

cat >"${SRC}/config/local/general.h" <<'EOF'
/* Written by scripts/build-rackflow-netboot-iso.sh */
#define DOWNLOAD_PROTO_HTTPS
EOF

# Virtual-media template: no embedded script; autoexec.ipxe placeholder is stamped per boot.
make -C "$SRC" -j"$(nproc)" bin/ipxe.lkrn bin-x86_64-efi/ipxe.efi TRUST="$TRUST" EMBED=
"$SRC/util/genfsimg" -o "$VMEDIA_OUT" -s "$PLACEHOLDER" "${SRC}/bin/ipxe.lkrn" "${SRC}/bin-x86_64-efi/ipxe.efi"
copies="$(grep -c -a -F "$PLACEHOLDER_MARKER" "$VMEDIA_OUT" || true)"
if [ "$copies" -lt 2 ]; then
    echo "expected the placeholder in both the BIOS and UEFI images, found ${copies}" >&2
    exit 1
fi
ls -l "$VMEDIA_OUT"

# Legacy netboot ISO: same binaries with the netboot script embedded.
make -C "$SRC" -j"$(nproc)" bin/ipxe.lkrn bin-x86_64-efi/ipxe.efi TRUST="$TRUST" EMBED="$NETBOOT_SCRIPT"
"$SRC/util/genfsimg" -o "$NETBOOT_OUT" "${SRC}/bin/ipxe.lkrn" "${SRC}/bin-x86_64-efi/ipxe.efi"
ls -l "$NETBOOT_OUT"
echo "Wrote $VMEDIA_OUT and $NETBOOT_OUT"
