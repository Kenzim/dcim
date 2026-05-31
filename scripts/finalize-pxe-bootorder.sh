#!/bin/bash
# Generic post-install boot-order finalize script.
# Runs in live OS (e.g. Debian Live) after an installation has completed and reported status.
# Reorders UEFI BootOrder so PXE/Network is first (BootCurrent first when PXE),
# and always sets BootNext to BootCurrent.
# Optional: set VALIDATE_ONLY=1 to only check which boot entries have a valid EFI file on disk (no changes, no reboot).
# When the script exits without rebooting, it creates /tmp/dcim-no-reboot so the live-image runner can skip its own reboot.

set -e

echo "=== Post-install boot order finalize ==="

if [ ! -d /sys/firmware/efi ]; then
    echo "Not UEFI; skipping boot order fix."
    exit 0
fi

mkdir -p /sys/firmware/efi/efivars 2>/dev/null || true
mount -t efivarfs efivarfs /sys/firmware/efi/efivars 2>/dev/null || true

if ! command -v efibootmgr >/dev/null 2>&1; then
    echo "efibootmgr not available; skipping."
    exit 0
fi

BOOT_INFO="$(efibootmgr -v 2>/dev/null || true)"
if [ -z "$BOOT_INFO" ]; then
    echo "Could not read UEFI boot info; skipping."
    exit 0
fi

# Classify each boot number: 1 = PXE/Network, 2 = UEFI Shell, 0 = other (used below)
is_pxe() {
    local bn="$1"
    echo "$BOOT_INFO" | awk -v bn="$bn" '
        BEGIN { inblock=0; out=0 }
        $0 ~ "^Boot" bn "[^0-9A-Fa-f]" { inblock=1; next }
        inblock && $0 ~ /^Boot[0-9A-Fa-f]{4}/ { inblock=0; next }
        inblock && tolower($0) ~ /(pxe|http|network)/ { out=1 }
        END { print out }
    ' | grep -q 1
}
is_shell() {
    local bn="$1"
    echo "$BOOT_INFO" | awk -v bn="$bn" '
        BEGIN { inblock=0; out=0 }
        $0 ~ "^Boot" bn "[^0-9A-Fa-f]" { inblock=1; next }
        inblock && $0 ~ /^Boot[0-9A-Fa-f]{4}/ { inblock=0; next }
        inblock && tolower($0) ~ /shell/ { out=1 }
        END { print out }
    ' | grep -q 1
}

# VALIDATE_ONLY: check which entries point to an existing EFI file on disk, then exit
if [ "${VALIDATE_ONLY:-0}" = "1" ]; then
    echo "--- Validation mode: checking which boot entries have a valid EFI file on disk ---"
    # Find ESPs: block devices with vfat (or type C12A7328-...); prefer by PARTUUID from blkid
    CHECK_MOUNTS=()
    for dev in /sys/block/*/dev; do
        [ -e "$dev" ] || continue
        bname="${dev%/dev}"; bname="${bname##*/}"
        for part in "/sys/block/$bname"/*/dev; do
            [ -e "$part" ] || continue
            pname="${part%/dev}"; pname="${pname##*/}"
            blk="/dev/$pname"
            [ -b "$blk" ] || continue
            fstype="$(blkid -o value -s TYPE "$blk" 2>/dev/null)" || true
            if [ "$fstype" = "vfat" ]; then
                mnt="/mnt/efi-check-$pname"
                mkdir -p "$mnt"
                if mount -o ro "$blk" "$mnt" 2>/dev/null; then
                    CHECK_MOUNTS+=("$mnt")
                fi
            fi
        done
    done
    # Also check already-mounted ESPs
    while read -r _ mnt _; do
        [ -d "$mnt" ] || continue
        case "$mnt" in /mnt/efi-check-*) continue ;; esac
        if [ -d "$mnt/EFI" ]; then
            CHECK_MOUNTS+=("$mnt")
        fi
    done < /proc/mounts 2>/dev/null || true
    # For each boot entry (non-PXE, non-Shell), extract File(\path) and check existence
    while read -r bootline; do
        bn="${bootline#Boot}"
        [ -n "$bn" ] || continue
        if is_pxe "$bn"; then echo "Boot${bn}: PXE/Network (skipped)"; continue; fi
        if is_shell "$bn"; then echo "Boot${bn}: UEFI Shell (skipped)"; continue; fi
        # Extract path from File(\...); path may be on same line or continuation
        efipath="$(echo "$BOOT_INFO" | awk -v bn="$bn" '
            $0 ~ "^Boot" bn "[^0-9A-Fa-f]" { inblock=1 }
            inblock && /File\\(\\\\/ { sub(/.*File\\(\\\\/, ""); sub(/).*/, ""); gsub(/\\\\/, "/"); print; exit }
        ')"
        [ -n "$efipath" ] || efipath=""
        if [ -z "$efipath" ]; then
            echo "Boot${bn}: (no File path in entry)"
            continue
        fi
        found=""
        for m in "${CHECK_MOUNTS[@]}"; do
            [ -d "$m" ] || continue
            if [ -f "$m/$efipath" ]; then
                found="$m/$efipath"
                break
            fi
        done
        if [ -n "$found" ]; then
            echo "Boot${bn}: VALID  -> $found"
        else
            echo "Boot${bn}: INVALID (file not found: $efipath)"
        fi
    done < <(echo "$BOOT_INFO" | grep -oE 'Boot[0-9A-Fa-f]{4}' | sort -u)
    for m in "${CHECK_MOUNTS[@]}"; do
        [[ "$m" == /mnt/efi-check-* ]] && umount "$m" 2>/dev/null || true
    done
    echo "--- End validation ---"
    exit 0
fi

# Parse BootOrder (e.g. "BootOrder: 0003,0002,0001")
CURRENT_ORDER="$(echo "$BOOT_INFO" | awk -F: '/^BootOrder:/ {gsub(/^[^0-9]*/, "", $2); print $2; exit}')"
if [ -z "$CURRENT_ORDER" ]; then
    echo "Could not parse BootOrder; skipping."
    exit 0
fi

# Read BootCurrent once; this should be PXE in this live boot flow.
BOOT_CURRENT="$(echo "$BOOT_INFO" | awk -F: '/^BootCurrent:/ {gsub(/[^0-9A-Fa-f]/, "", $2); print $2; exit}')"

# Build new order:
# 1) BootCurrent first
# 2) other PXE entries
# 3) non-PXE/non-Shell
# 4) Shell
CURRENT_FIRST=""
PXE_OTHER=""
OTHER_LIST=""
SHELL_LIST=""

for bn in $(echo "$CURRENT_ORDER" | tr ',' ' '); do
    [ -z "$bn" ] && continue
    if [ -n "$BOOT_CURRENT" ] && [ "$bn" = "$BOOT_CURRENT" ]; then
        CURRENT_FIRST="$bn"
    elif is_pxe "$bn"; then
        PXE_OTHER="${PXE_OTHER:+$PXE_OTHER,}$bn"
    elif is_shell "$bn"; then
        SHELL_LIST="${SHELL_LIST:+$SHELL_LIST,}$bn"
    else
        OTHER_LIST="${OTHER_LIST:+$OTHER_LIST,}$bn"
    fi
done

NEW_ORDER="$CURRENT_FIRST"
[ -n "$PXE_OTHER" ] && NEW_ORDER="${NEW_ORDER:+$NEW_ORDER,}$PXE_OTHER"
[ -n "$OTHER_LIST" ] && NEW_ORDER="${NEW_ORDER:+$NEW_ORDER,}$OTHER_LIST"
[ -n "$SHELL_LIST" ] && NEW_ORDER="${NEW_ORDER:+$NEW_ORDER,}$SHELL_LIST"

# Fallback: if BootCurrent wasn't in BootOrder for some reason, keep PXE-first behavior.
if [ -z "$NEW_ORDER" ]; then
    for bn in $(echo "$CURRENT_ORDER" | tr ',' ' '); do
        [ -z "$bn" ] && continue
        if is_pxe "$bn"; then
            PXE_OTHER="${PXE_OTHER:+$PXE_OTHER,}$bn"
        elif is_shell "$bn"; then
            SHELL_LIST="${SHELL_LIST:+$SHELL_LIST,}$bn"
        else
            OTHER_LIST="${OTHER_LIST:+$OTHER_LIST,}$bn"
        fi
    done
    NEW_ORDER="$PXE_OTHER"
    [ -n "$OTHER_LIST" ] && NEW_ORDER="${NEW_ORDER:+$NEW_ORDER,}$OTHER_LIST"
    [ -n "$SHELL_LIST" ] && NEW_ORDER="${NEW_ORDER:+$NEW_ORDER,}$SHELL_LIST"
fi

if [ -n "$NEW_ORDER" ]; then
    echo "Setting BootOrder to: $NEW_ORDER"
    efibootmgr -o "$NEW_ORDER" 2>/dev/null || true
fi

# Always set one-time next boot to BootCurrent (expected PXE in this context).
if [ -n "$BOOT_CURRENT" ]; then
    echo "Setting BootNext to current entry Boot${BOOT_CURRENT}."
    efibootmgr -n "$BOOT_CURRENT" 2>/dev/null || true
else
    echo "Could not read BootCurrent; leaving BootNext unchanged."
fi

echo "Done. Inspect with efibootmgr -v, then reboot manually when ready."
touch /tmp/dcim-no-reboot 2>/dev/null || true

sleep 25