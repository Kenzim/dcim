#!/bin/bash
# Windows Server 2022 Installation Script
# This script runs in Debian Live OS booted via PXE
# Uses pre-created image files (windows.img, efi.img) to deploy Windows

set -e
set -o pipefail  # Make pipes fail if any command in the pipeline fails

# Capture all output to log file for upload
LOG_FILE="/tmp/dcim-installation.log"
exec > >(tee -a "$LOG_FILE") 2>&1

# Report installation status to DCIM API (success or failure)
DCIM_STATUS_REPORTED=0
report_installation_status() {
    local status="$1"
    local error_msg="${2:-}"
    [ -n "${INSTALLATION_TASK_ID:-}" ] || return 0
    [ -n "${SERVER_ID:-}" ] || return 0
    [ -n "${API_BASE:-}" ] || return 0
    local url="${API_BASE}/api/servers/${SERVER_ID}/installation-tasks/${INSTALLATION_TASK_ID}/logs"
    [ -n "${DOWNLOAD_TOKEN:-}" ] && url="${url}?token=${DOWNLOAD_TOKEN}"

    # Read tail of log file (up to ~50KB) so failures show context in the UI
    local logs=""
    if [ -f "$LOG_FILE" ]; then
        logs="$(tail -c 50000 "$LOG_FILE" 2>/dev/null | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/ /g; :a;N;$!ba;s/\n/\\n/g')"
    fi

    local payload
    if [ "$status" = "failed" ] && [ -n "$error_msg" ]; then
        local escaped_error
        escaped_error="$(echo "$error_msg" | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/ /g')"
        payload=$(printf '{"logs":"%s","status":"failed","error_message":"%s"}' "$logs" "$escaped_error")
    else
        payload=$(printf '{"logs":"%s","status":"%s"}' "$logs" "$status")
    fi

    curl -X POST "$url" -H "Content-Type: application/json" -d "$payload" -f -s >/dev/null 2>&1 || true
}
trap 'e=$?; if [ "$DCIM_STATUS_REPORTED" = 0 ] && [ "$e" -ne 0 ]; then report_installation_status failed "Installation script failed"; fi' EXIT

echo "=== Windows Server 2022 Installation ==="
echo "Server IP: ${SERVER_IP}"
echo "Server MAC: ${SERVER_MAC}"
echo "Installation Task ID: ${INSTALLATION_TASK_ID:-<not set>}"
echo "Boot Mode: ${OS_BOOT_MODE:-uefi}"
echo ""

# Configuration from template parameters
ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: ADMIN_PASSWORD parameter is required"
    exit 1
fi

# Boot mode detection (default to UEFI if not set)
BOOT_MODE="${OS_BOOT_MODE:-uefi}"
BOOT_MODE=$(echo "$BOOT_MODE" | tr '[:upper:]' '[:lower:]')

if [ "$BOOT_MODE" != "uefi" ] && [ "$BOOT_MODE" != "bios" ]; then
    echo "WARNING: Invalid boot mode '$BOOT_MODE', defaulting to UEFI"
    BOOT_MODE="uefi"
fi

echo "Using boot mode: $BOOT_MODE"
echo ""

# Get image file URLs (injected by system from template configuration)
API_BASE="${API_BASE_URL:-http://192.168.12.74:8000}"

if [ -z "${WINDOWS_IMG_URL:-}" ]; then
    echo "ERROR: WINDOWS_IMG_URL is required (should be provided by template configuration)"
    exit 1
fi

if [ "$BOOT_MODE" = "uefi" ] && [ -z "${EFI_IMG_URL:-}" ]; then
    echo "ERROR: EFI_IMG_URL is required for UEFI boot mode"
    exit 1
fi

echo "Windows image URL: ${WINDOWS_IMG_URL}"
if [ "$BOOT_MODE" = "uefi" ]; then
    echo "EFI image URL: ${EFI_IMG_URL}"
fi
echo ""

# Find the target disk
# Priority: 1) Match by serial number, 2) Match by size/type, 3) First available disk
TARGET_DISK=""
OS_DISK_SERIAL="${OS_DISK_SERIAL:-}"
OS_DISK_SIZE_GB="${OS_DISK_SIZE_GB:-}"
OS_DISK_TYPE="${OS_DISK_TYPE:-}"

echo "Looking for OS disk..."
echo "  Serial: ${OS_DISK_SERIAL:-<not specified>}"
echo "  Size: ${OS_DISK_SIZE_GB:-<not specified>} GB"
echo "  Type: ${OS_DISK_TYPE:-<not specified>}"
echo ""

# Helper function for partition naming (handles NVMe vs SATA)
partpath() {
    if [[ "$1" =~ nvme ]]; then
        echo "${1}p$2"
    else
        echo "${1}$2"
    fi
}

# Function to get disk serial number
get_disk_serial() {
    local disk=$1
    # Try different methods to get serial
    if command -v lsblk >/dev/null 2>&1; then
        lsblk -dno SERIAL "$disk" 2>/dev/null | head -1 | tr -d '[:space:]'
    elif [ -f "/sys/block/$(basename $disk)/serial" ]; then
        cat "/sys/block/$(basename $disk)/serial" 2>/dev/null | tr -d '[:space:]'
    else
        # Try smartctl if available
        if command -v smartctl >/dev/null 2>&1; then
            smartctl -i "$disk" 2>/dev/null | grep -i "serial number" | awk '{print $3}' | tr -d '[:space:]'
        fi
    fi
}

# Function to get disk size in GB
get_disk_size_gb() {
    local disk=$1
    if command -v lsblk >/dev/null 2>&1; then
        # Get size in bytes, convert to GB
        local size_bytes=$(lsblk -bdno SIZE "$disk" 2>/dev/null | head -1)
        if [ -n "$size_bytes" ]; then
            echo $((size_bytes / 1024 / 1024 / 1024))
        fi
    elif [ -f "/sys/block/$(basename $disk)/size" ]; then
        # Get size in 512-byte sectors, convert to GB
        local sectors=$(cat "/sys/block/$(basename $disk)/size" 2>/dev/null)
        if [ -n "$sectors" ]; then
            echo $((sectors * 512 / 1024 / 1024 / 1024))
        fi
    fi
}

# Function to get disk type (SSD/HDD)
get_disk_type() {
    local disk=$1
    if [ -f "/sys/block/$(basename $disk)/queue/rotational" ]; then
        local rotational=$(cat "/sys/block/$(basename $disk)/queue/rotational" 2>/dev/null)
        if [ "$rotational" = "0" ]; then
            echo "ssd"
        else
            echo "hdd"
        fi
    else
        # Fallback: try to detect from model name
        if command -v smartctl >/dev/null 2>&1; then
            local model=$(smartctl -i "$disk" 2>/dev/null | grep -i "model" | head -1)
            if echo "$model" | grep -qi "ssd\|solid"; then
                echo "ssd"
            else
                echo "hdd"
            fi
        fi
    fi
}

# Collect all available disks
AVAILABLE_DISKS=()
for disk in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
    if [ -b "$disk" ]; then
        # Only consider real disks (lsblk TYPE=disk) and skip obvious virtual media
        if command -v lsblk >/dev/null 2>&1; then
            info=$(lsblk -dn -o TYPE,TRAN,RM "$disk" 2>/dev/null | head -1)
            disk_type_field=$(echo "$info" | awk '{print $1}')
            disk_tran_field=$(echo "$info" | awk '{print $2}')
            disk_rm_field=$(echo "$info" | awk '{print $3}')
            # TYPE must be "disk"
            if [ "$disk_type_field" != "disk" ]; then
                echo "Skipping $disk (lsblk TYPE=$disk_type_field, not a real disk)"
                continue
            fi
            # Skip removable/USB devices (typically IPMI virtual media, ISO bridges, etc.)
            if [ "$disk_tran_field" = "usb" ] || [ "$disk_rm_field" = "1" ]; then
                echo "Skipping $disk (TRAN=$disk_tran_field, RM=$disk_rm_field – likely virtual/removable media)"
                continue
            fi
        fi
        AVAILABLE_DISKS+=("$disk")
    fi
done

if [ ${#AVAILABLE_DISKS[@]} -eq 0 ]; then
    echo "ERROR: Could not find any disks"
    exit 1
fi

echo "Found ${#AVAILABLE_DISKS[@]} disk(s):"
for disk in "${AVAILABLE_DISKS[@]}"; do
    echo "  - $disk"
done
echo ""

# Priority 1: Match by serial number if provided
if [ -n "$OS_DISK_SERIAL" ]; then
    echo "Matching by serial number: $OS_DISK_SERIAL"
    for disk in "${AVAILABLE_DISKS[@]}"; do
        disk_serial=$(get_disk_serial "$disk")
        if [ "$disk_serial" = "$OS_DISK_SERIAL" ]; then
            TARGET_DISK="$disk"
            echo "Found matching disk by serial: $TARGET_DISK"
            break
        fi
    done
fi

# Priority 2: Match by size and type if serial didn't match and size/type are provided
if [ -z "$TARGET_DISK" ] && [ -n "$OS_DISK_SIZE_GB" ] && [ -n "$OS_DISK_TYPE" ]; then
    echo "Matching by size ($OS_DISK_SIZE_GB GB) and type ($OS_DISK_TYPE)..."
    BEST_MATCH=""
    BEST_DIFF=999999
    
    for disk in "${AVAILABLE_DISKS[@]}"; do
        disk_size=$(get_disk_size_gb "$disk")
        disk_type=$(get_disk_type "$disk")
        
        if [ -n "$disk_size" ] && [ -n "$disk_type" ]; then
            # Check if type matches
            if [ "$disk_type" = "$OS_DISK_TYPE" ]; then
                # Calculate size difference
                diff=$((disk_size > OS_DISK_SIZE_GB ? disk_size - OS_DISK_SIZE_GB : OS_DISK_SIZE_GB - disk_size))
                if [ $diff -lt $BEST_DIFF ]; then
                    BEST_DIFF=$diff
                    BEST_MATCH="$disk"
                fi
            fi
        fi
    done
    
    if [ -n "$BEST_MATCH" ]; then
        TARGET_DISK="$BEST_MATCH"
        echo "Found closest matching disk: $TARGET_DISK (size diff: ${BEST_DIFF}GB)"
    fi
fi

# Priority 3: Use first available disk if no match
if [ -z "$TARGET_DISK" ]; then
    TARGET_DISK="${AVAILABLE_DISKS[0]}"
    echo "Using first available disk: $TARGET_DISK"
fi

if [ -z "$TARGET_DISK" ]; then
    echo "ERROR: Could not determine target disk"
    exit 1
fi

echo ""
echo "Target disk: $TARGET_DISK"
echo "WARNING: This will completely overwrite $TARGET_DISK"
echo ""

# Check for required tools
echo "Checking for required tools..."
MISSING_TOOLS=()

if ! command -v curl >/dev/null 2>&1; then
    MISSING_TOOLS+=("curl")
fi

if ! command -v parted >/dev/null 2>&1; then
    MISSING_TOOLS+=("parted")
fi

if ! command -v mkfs.ntfs >/dev/null 2>&1; then
    MISSING_TOOLS+=("ntfs-3g")
fi

if [ "$BOOT_MODE" = "bios" ]; then
    if ! command -v ms-sys >/dev/null 2>&1; then
        MISSING_TOOLS+=("ms-sys")
    fi
fi

if [ ${#MISSING_TOOLS[@]} -gt 0 ]; then
    echo "ERROR: Missing required tools: ${MISSING_TOOLS[*]}"
    echo "Attempting to install..."
    apt-get update -qq
    apt-get install -y "${MISSING_TOOLS[@]}" || {
        echo "ERROR: Failed to install required tools"
        exit 1
    }
fi

echo "All required tools are available"
echo ""

# Wipe the target disk
echo "=========================================="
echo "Wiping target disk: $TARGET_DISK"
echo "=========================================="

# Try blkdiscard first (fastest for SSDs)
if command -v blkdiscard >/dev/null 2>&1; then
    echo "Attempting blkdiscard (TRIM) for fast wipe..."
    if blkdiscard -f "$TARGET_DISK" 2>/dev/null; then
        echo "✓ Disk wiped using blkdiscard"
    else
        echo "  blkdiscard not supported, using dd..."
        # Fallback to dd
        dd if=/dev/zero of="$TARGET_DISK" bs=4M count=100 2>/dev/null || true
        sync
    fi
else
    # Fallback to partial dd wipe (just first 400MB to clear partition table)
    echo "Using dd to wipe partition table..."
    dd if=/dev/zero of="$TARGET_DISK" bs=4M count=100 2>/dev/null || true
    sync
fi

echo ""

# Create partition layout based on boot mode
echo "=========================================="
echo "Creating partition layout ($BOOT_MODE mode)"
echo "=========================================="

if [ "$BOOT_MODE" = "uefi" ]; then
    echo "Creating UEFI GPT partition layout..."
    # UEFI layout: GPT with EFI system partition (100MB) + Windows partition (rest)
    parted -s "$TARGET_DISK" mklabel gpt
    parted -s "$TARGET_DISK" mkpart primary fat32 1MiB 101MiB
    parted -s "$TARGET_DISK" set 1 esp on
    parted -s "$TARGET_DISK" mkpart primary ntfs 101MiB 100%
    
    # Get correct partition paths (handles NVMe vs SATA)
    EFI_PART=$(partpath "$TARGET_DISK" 1)
    WINDOWS_PART=$(partpath "$TARGET_DISK" 2)
    
    echo "EFI partition: $EFI_PART"
    echo "Windows partition: $WINDOWS_PART"
    
else
    echo "Creating BIOS MBR partition layout..."
    # BIOS layout: MBR with single Windows partition
    parted -s "$TARGET_DISK" mklabel msdos
    parted -s "$TARGET_DISK" mkpart primary ntfs 1MiB 100%
    parted -s "$TARGET_DISK" set 1 boot on
    
    # Get correct partition path (handles NVMe vs SATA)
    WINDOWS_PART=$(partpath "$TARGET_DISK" 1)
    
    echo "Windows partition: $WINDOWS_PART"
fi

echo "Partition layout created successfully"
echo ""

# Download and write image files directly to disk (streaming)
echo "=========================================="
echo "Downloading and writing image files"
echo "=========================================="
echo "This may take a while..."
echo "Streaming directly to disk (no temporary files)"
echo ""

# Stream Windows image directly to partition
echo "Streaming Windows image to $WINDOWS_PART..."
echo "This will stream directly from the server to disk..."
if ! curl -fL "${WINDOWS_IMG_URL}" | dd of="$WINDOWS_PART" bs=4M status=progress; then
    echo "ERROR: Failed to stream/write Windows image"
    # Check exit codes (PIPESTATUS is bash-only; guard so we don't get 'integer expression expected' under sh)
    CURL_EXIT=${PIPESTATUS[0]:-}
    DD_EXIT=${PIPESTATUS[1]:-}
    if [ -n "${CURL_EXIT}" ] && [ "${CURL_EXIT}" -ne 0 ] 2>/dev/null; then
        echo "  curl failed with exit code: $CURL_EXIT"
    fi
    if [ -n "${DD_EXIT}" ] && [ "${DD_EXIT}" -ne 0 ] 2>/dev/null; then
        echo "  dd failed with exit code: $DD_EXIT"
    fi
    exit 1
fi
sync
echo "✓ Windows image written successfully"
echo ""

# Expand NTFS filesystem to fill the partition (image may be smaller than partition)
if command -v ntfsresize >/dev/null 2>&1; then
    echo "Expanding NTFS filesystem to fill partition..."
    if ntfsresize --expand "$WINDOWS_PART" 2>/dev/null; then
        echo "✓ NTFS filesystem expanded to fill partition"
    else
        echo "WARNING: ntfsresize failed (partition may already be full or image size matches); continuing"
    fi
    echo ""
else
    echo "Note: ntfsresize not found; NTFS partition not expanded (install ntfs-3g for ntfsresize)"
    echo ""
fi

# Stream EFI image directly to partition (UEFI only)
if [ "$BOOT_MODE" = "uefi" ]; then
    echo "Streaming EFI image to $EFI_PART..."
    echo "This will stream directly from the server to disk..."
    if ! curl -fL "${EFI_IMG_URL}" | dd of="$EFI_PART" bs=4M status=progress; then
        echo "ERROR: Failed to stream/write EFI image"
        # Check exit codes (PIPESTATUS is bash-only; guard so we don't get 'integer expression expected' under sh)
        CURL_EXIT=${PIPESTATUS[0]:-}
        DD_EXIT=${PIPESTATUS[1]:-}
        if [ -n "${CURL_EXIT}" ] && [ "${CURL_EXIT}" -ne 0 ] 2>/dev/null; then
            echo "  curl failed with exit code: $CURL_EXIT"
        fi
        if [ -n "${DD_EXIT}" ] && [ "${DD_EXIT}" -ne 0 ] 2>/dev/null; then
            echo "  dd failed with exit code: $DD_EXIT"
        fi
        exit 1
    fi
    sync
    echo "✓ EFI image written successfully"
    echo ""
fi

# UEFI: clean up stale boot entries and keep PXE automatic.
if [ "$BOOT_MODE" = "uefi" ]; then
    echo "=========================================="
    echo "Removing all non-PXE EFI boot entries"
    echo "=========================================="

    mkdir -p /sys/firmware/efi/efivars 2>/dev/null || true
    mount -t efivarfs efivarfs /sys/firmware/efi/efivars 2>/dev/null || true

    BOOT_INFO="$(efibootmgr -v 2>/dev/null || true)"
    BOOT_CURRENT="$(echo "$BOOT_INFO" | awk -F: '/BootCurrent/ {gsub(/[^0-9A-Fa-f]/, "", $2); print $2; exit}')"

    while read -r bn; do
        [ -n "$bn" ] || continue
        # Never delete the entry we're currently booted from
        [ "$bn" = "$BOOT_CURRENT" ] && continue
        IS_PXE="$(echo "$BOOT_INFO" | awk -v bn="$bn" '
            BEGIN{inblock=0;px=0}
            $0 ~ "^Boot"bn"[^0-9A-Fa-f]" {inblock=1; next}
            inblock && $0 ~ /^Boot[0-9A-Fa-f]{4}/ {inblock=0; next}
            inblock && tolower($0) ~ /(pxe|http|network)/ {px=1}
            END{print px}
        ' | head -n1 || true)"
        if [ "$IS_PXE" != "1" ]; then
            echo "Deleting Boot${bn}"
            efibootmgr -b "$bn" -B 2>/dev/null || true
        fi
    done < <(echo "$BOOT_INFO" | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\).*/\1/p')

    echo "Remaining entries:"
    efibootmgr -v 2>/dev/null || true

    echo "=========================================="
    echo "Setting BootNext to keep PXE automatic"
    echo "=========================================="

    if [ -n "$BOOT_CURRENT" ]; then
        echo "Setting BootNext to current boot entry Boot${BOOT_CURRENT}."
        efibootmgr -n "$BOOT_CURRENT" 2>/dev/null || true
    else
        echo "WARNING: Could not read BootCurrent; cannot set BootNext."
    fi
fi

# Set up bootloader based on boot mode
echo "=========================================="
echo "Setting up bootloader ($BOOT_MODE mode)"
echo "=========================================="

if [ "$BOOT_MODE" = "bios" ]; then
    echo "Setting up BIOS boot..."
    
    # Install BIOS boot code using ms-sys
    if ! command -v ms-sys >/dev/null 2>&1; then
        echo "ERROR: ms-sys not available for BIOS boot setup"
        exit 1
    fi
    
    echo "Installing BIOS boot code (MBR + PBR) using ms-sys..."
    
    # Write Windows 7/8/10-style MBR
    if ! ms-sys -7 "$TARGET_DISK" 2>/dev/null; then
        # Fallback to generic Microsoft MBR
        if ! ms-sys -m "$TARGET_DISK" 2>/dev/null; then
            echo "ERROR: Failed to write MBR boot code"
            exit 1
        fi
    fi
    
    # Write partition boot record (PBR/VBR) to Windows partition
    if ! ms-sys -p "$WINDOWS_PART" 2>/dev/null; then
        echo "ERROR: Failed to write partition boot sector"
        exit 1
    fi
    
    echo "✓ BIOS boot code written successfully"
    echo "NOTE: BIOS boot requires MBR partitioning + active NTFS partition."
fi

sync
echo ""

# Format all extra disks (non-OS disks) as NTFS
EXTRA_DISKS=()
for disk in "${AVAILABLE_DISKS[@]}"; do
    [ "$disk" = "$TARGET_DISK" ] && continue
    EXTRA_DISKS+=("$disk")
done

if [ ${#EXTRA_DISKS[@]} -gt 0 ]; then
    echo "=========================================="
    echo "Formatting ${#EXTRA_DISKS[@]} extra disk(s) as NTFS"
    echo "=========================================="
    for disk in "${EXTRA_DISKS[@]}"; do
        echo "Formatting $disk as NTFS..."

        # Safety: re-check that this really looks like a local data disk
        if command -v lsblk >/dev/null 2>&1; then
            info=$(lsblk -dn -o TYPE,TRAN,RM "$disk" 2>/dev/null | head -1)
            disk_type_field=$(echo "$info" | awk '{print $1}')
            disk_tran_field=$(echo "$info" | awk '{print $2}')
            disk_rm_field=$(echo "$info" | awk '{print $3}')
            if [ "$disk_type_field" != "disk" ] || [ "$disk_tran_field" = "usb" ] || [ "$disk_rm_field" = "1" ]; then
                echo "  [SKIP] $disk is TYPE=$disk_type_field, TRAN=$disk_tran_field, RM=$disk_rm_field (likely virtual/removable); not formatting."
                continue
            fi
        fi

        # Never let a failure here abort the whole install; just warn and continue.
        {
            if command -v blkdiscard >/dev/null 2>&1; then
                blkdiscard -f "$disk" 2>/dev/null || echo "  Warning: blkdiscard failed on $disk (continuing)..."
            else
                dd if=/dev/zero of="$disk" bs=4M count=100 2>/dev/null || echo "  Warning: dd wipe failed on $disk (continuing)..."
                sync
            fi

            if ! parted -s "$disk" mklabel gpt; then
                echo "  [SKIP] Failed to create GPT label on $disk; leaving disk unchanged."
                continue
            fi
            if ! parted -s "$disk" mkpart primary ntfs 1MiB 100%; then
                echo "  [SKIP] Failed to create partition on $disk; leaving disk unchanged."
                continue
            fi

            EXTRA_PART=$(partpath "$disk" 1)
            if ! mkfs.ntfs -f -L "Data" "$EXTRA_PART"; then
                echo "  [SKIP] Failed to format $EXTRA_PART as NTFS; leaving disk unchanged."
                continue
            fi

            echo "✓ $disk formatted (partition $EXTRA_PART)"
        } || {
            # Catch-all in case anything above unexpectedly propagates an error
            echo "  [SKIP] An error occurred while handling $disk; continuing without failing installation."
            continue
        }
    done
    echo ""
fi

# Write password file and PowerShell scripts (mount Windows partition temporarily)
echo "Writing password configuration file and PowerShell scripts..."
WINDOWS_MOUNT="/mnt/windows"
mkdir -p "$WINDOWS_MOUNT"

# Mount Windows partition read-write to write password + scripts
if mount -t ntfs-3g "$WINDOWS_PART" "$WINDOWS_MOUNT" -o rw 2>/dev/null || \
   mount -t ntfs "$WINDOWS_PART" "$WINDOWS_MOUNT" -o rw 2>/dev/null; then
    # 1) Password file for firstboot.ps1
    PASSWORD_FILE="${WINDOWS_MOUNT}/dcim_password.txt"
    echo -n "$ADMIN_PASSWORD" > "$PASSWORD_FILE" 2>/dev/null || true
    chmod 600 "$PASSWORD_FILE" 2>/dev/null || true
    sync
    
    if [ -f "$PASSWORD_FILE" ]; then
        FILE_SIZE=$(stat -c%s "$PASSWORD_FILE" 2>/dev/null || stat -f%z "$PASSWORD_FILE" 2>/dev/null || echo "0")
        echo "Password file written successfully (size: ${FILE_SIZE} bytes)"
    else
        echo "WARNING: Password file verification failed"
    fi

    # 2) Overwrite Windows Setup PowerShell scripts from API template-files (if TEMPLATE_ID + token set)
    WIN_SETUP_SCRIPTS_DIR="${WINDOWS_MOUNT}/Windows/Setup/Scripts"
    mkdir -p "$WIN_SETUP_SCRIPTS_DIR"

    if [ -n "${TEMPLATE_ID:-}" ] && [ -n "${DOWNLOAD_TOKEN:-}" ] && [ -n "${API_BASE_URL:-}" ]; then
        # Fetch template files we need; API serves any file under the template at template-files/{template_id}/{path}
        for script in firstboot.ps1 user-login.ps1; do
            echo "Updating Windows $script from API..."
            url="${API_BASE_URL}/api/servers/interaction/template-files/${TEMPLATE_ID}/${script}?token=${DOWNLOAD_TOKEN}"
            if ! curl -fsSL "$url" -o "${WIN_SETUP_SCRIPTS_DIR}/${script}"; then
                echo "WARNING: Failed to download $script from API"
            fi
        done
    else
        echo "WARNING: TEMPLATE_ID or DOWNLOAD_TOKEN not set; leaving existing scripts in image"
        echo "  (Create a new install/boot task from the API so the script gets these variables)"
    fi

    sync
    umount "$WINDOWS_MOUNT" 2>/dev/null || true
else
    echo "WARNING: Could not mount Windows partition to write password or scripts"
    echo "Password and script updates will need to be handled manually"
fi

echo ""

echo "=== Installation Complete ==="
echo "Windows Server 2022 installed to $TARGET_DISK"
echo "Boot mode: $BOOT_MODE"
echo "Password written to disk (Windows will read it on first boot)"
echo ""

# Upload logs and report success to DCIM API if INSTALLATION_TASK_ID is set
if [ -n "${INSTALLATION_TASK_ID:-}" ] && [ -n "${SERVER_ID:-}" ] && [ -n "${API_BASE:-}" ]; then
    DCIM_STATUS_REPORTED=1
    echo "Uploading installation logs and reporting success to API..."

    # Read log file content (or empty if file missing)
    LOG_CONTENT=""
    [ -f "$LOG_FILE" ] && LOG_CONTENT=$(cat "$LOG_FILE" 2>/dev/null || echo "")

    UPLOAD_URL="${API_BASE}/api/servers/${SERVER_ID}/installation-tasks/${INSTALLATION_TASK_ID}/logs"
    [ -n "${DOWNLOAD_TOKEN:-}" ] && UPLOAD_URL="${UPLOAD_URL}?token=${DOWNLOAD_TOKEN}"

    if command -v python3 >/dev/null 2>&1; then
        JSON_PAYLOAD=$(python3 -c "
import json
import sys
logs = sys.stdin.read()
payload = {'logs': logs, 'status': 'completed'}
print(json.dumps(payload))
" <<< "$LOG_CONTENT" 2>/dev/null || echo '{"logs": "", "status": "completed"}')
    else
        ESCAPED_LOGS=$(echo "$LOG_CONTENT" | sed 's/\\/\\\\/g' | sed 's/"/\\"/g' | sed ':a;N;$!ba;s/\n/\\n/g')
        JSON_PAYLOAD="{\"logs\": \"${ESCAPED_LOGS}\", \"status\": \"completed\"}"
    fi

    if command -v curl >/dev/null 2>&1; then
        HTTP_RESP=$(curl -s -w "\n%{http_code}" -X POST "$UPLOAD_URL" \
            -H "Content-Type: application/json" \
            -d "$JSON_PAYLOAD" 2>/dev/null) || true
        HTTP_CODE=$(echo "$HTTP_RESP" | tail -n1)
        if [ "$HTTP_CODE" = "200" ]; then
            echo "Logs uploaded and installation marked complete (HTTP 200)"
        else
            echo "WARNING: Log upload returned HTTP $HTTP_CODE (response: $(echo "$HTTP_RESP" | head -n-1 | head -c200))"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if echo "$JSON_PAYLOAD" | wget --method=POST \
            --header="Content-Type: application/json" \
            --body-data="$JSON_PAYLOAD" \
            "$UPLOAD_URL" \
            -O /tmp/upload_response.txt 2>/dev/null; then
            echo "Logs uploaded and installation marked complete"
        else
            echo "WARNING: Failed to upload logs (check /tmp/upload_response.txt)"
        fi
    else
        echo "WARNING: curl or wget not available, cannot upload logs"
    fi
else
    echo "Skipping log upload: INSTALLATION_TASK_ID, SERVER_ID, or API_BASE not set"
fi

# Terminate download token after log upload so the token is still valid for the logs API
if [ -n "${DOWNLOAD_TOKEN:-}" ] && [ -n "${API_BASE:-}" ]; then
    echo "Terminating download token..."
    TERMINATE_URL="${API_BASE}/api/servers/interaction/download-token/${DOWNLOAD_TOKEN}/terminate"
    curl -X POST "$TERMINATE_URL" -f -s >/dev/null 2>&1 || {
        echo "WARNING: Failed to terminate download token (will expire automatically)"
    }
fi

echo "Rebooting in 5 seconds..."
sleep 5
#reboot
