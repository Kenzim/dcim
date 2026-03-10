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
    local payload
    if [ "$status" = "failed" ] && [ -n "$error_msg" ]; then
        payload=$(printf '{"logs":"","status":"failed","error_message":"%s"}' "$(echo "$error_msg" | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/ /g')")
    else
        payload=$(printf '{"logs":"","status":"%s"}' "$status")
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
    # Check exit codes
    CURL_EXIT=${PIPESTATUS[0]}
    DD_EXIT=${PIPESTATUS[1]}
    if [ "$CURL_EXIT" -ne 0 ]; then
        echo "  curl failed with exit code: $CURL_EXIT"
    fi
    if [ "$DD_EXIT" -ne 0 ]; then
        echo "  dd failed with exit code: $DD_EXIT"
    fi
    exit 1
fi
sync
echo "✓ Windows image written successfully"
echo ""

# Stream EFI image directly to partition (UEFI only)
if [ "$BOOT_MODE" = "uefi" ]; then
    echo "Streaming EFI image to $EFI_PART..."
    echo "This will stream directly from the server to disk..."
    if ! curl -fL "${EFI_IMG_URL}" | dd of="$EFI_PART" bs=4M status=progress; then
        echo "ERROR: Failed to stream/write EFI image"
        # Check exit codes
        CURL_EXIT=${PIPESTATUS[0]}
        DD_EXIT=${PIPESTATUS[1]}
        if [ "$CURL_EXIT" -ne 0 ]; then
            echo "  curl failed with exit code: $CURL_EXIT"
        fi
        if [ "$DD_EXIT" -ne 0 ]; then
            echo "  dd failed with exit code: $DD_EXIT"
        fi
        exit 1
    fi
    sync
    echo "✓ EFI image written successfully"
    echo ""
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
        if command -v blkdiscard >/dev/null 2>&1; then
            blkdiscard -f "$disk" 2>/dev/null || true
        else
            dd if=/dev/zero of="$disk" bs=4M count=100 2>/dev/null || true
            sync
        fi
        parted -s "$disk" mklabel gpt
        parted -s "$disk" mkpart primary ntfs 1MiB 100%
        EXTRA_PART=$(partpath "$disk" 1)
        mkfs.ntfs -f -L "Data" "$EXTRA_PART"
        echo "✓ $disk formatted (partition $EXTRA_PART)"
    done
    echo ""
fi

# Write password file (mount Windows partition temporarily)
echo "Writing password configuration file..."
WINDOWS_MOUNT="/mnt/windows"
mkdir -p "$WINDOWS_MOUNT"

# Mount Windows partition read-write to write password file
if mount -t ntfs-3g "$WINDOWS_PART" "$WINDOWS_MOUNT" -o rw 2>/dev/null || \
   mount -t ntfs "$WINDOWS_PART" "$WINDOWS_MOUNT" -o rw 2>/dev/null; then
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
    umount "$WINDOWS_MOUNT" 2>/dev/null || true
else
    echo "WARNING: Could not mount Windows partition to write password file"
    echo "Password will need to be set manually"
fi

echo ""

# Terminate download token (explicit cleanup)
if [ -n "${DOWNLOAD_TOKEN:-}" ]; then
    echo "Terminating download token..."
    TERMINATE_URL="${API_BASE}/api/servers/interaction/download-token/${DOWNLOAD_TOKEN}/terminate"
    curl -X POST "$TERMINATE_URL" -f -s >/dev/null 2>&1 || {
        echo "WARNING: Failed to terminate download token (will expire automatically)"
    }
fi

echo ""
echo "=== Installation Complete ==="
echo "Windows Server 2022 installed to $TARGET_DISK"
echo "Boot mode: $BOOT_MODE"
echo "Password written to disk (Windows will read it on first boot)"
echo ""

# Upload logs and report success to DCIM API if INSTALLATION_TASK_ID is set
if [ -n "${INSTALLATION_TASK_ID:-}" ] && [ -n "${SERVER_ID:-}" ]; then
    DCIM_STATUS_REPORTED=1
    echo "Uploading installation logs and reporting success..."
    
    # Read log file content
    if [ -f "$LOG_FILE" ]; then
        LOG_CONTENT=$(cat "$LOG_FILE" 2>/dev/null || echo "")
        
        # Upload logs via API
        UPLOAD_URL="${API_BASE}/api/servers/${SERVER_ID}/installation-tasks/${INSTALLATION_TASK_ID}/logs"
        
        # Use Python to properly JSON-encode the logs if available, otherwise use simple escaping
        if command -v python3 >/dev/null 2>&1; then
            # Use Python to properly encode JSON
            JSON_PAYLOAD=$(python3 -c "
import json
import sys
logs = sys.stdin.read()
payload = {'logs': logs, 'status': 'completed'}
print(json.dumps(payload))
" <<< "$LOG_CONTENT" 2>/dev/null || echo '{"logs": "", "status": "completed"}')
        else
            # Fallback: simple escaping (may break with special characters)
            ESCAPED_LOGS=$(echo "$LOG_CONTENT" | sed 's/\\/\\\\/g' | sed 's/"/\\"/g' | sed ':a;N;$!ba;s/\n/\\n/g')
            JSON_PAYLOAD="{\"logs\": \"${ESCAPED_LOGS}\", \"status\": \"completed\"}"
        fi
        
        if command -v curl >/dev/null 2>&1; then
            curl -X POST "$UPLOAD_URL" \
                -H "Content-Type: application/json" \
                -d "$JSON_PAYLOAD" \
                >/dev/null 2>&1 || echo "Warning: Failed to upload logs"
        elif command -v wget >/dev/null 2>&1; then
            echo "$JSON_PAYLOAD" | wget --method=POST \
                --header="Content-Type: application/json" \
                --body-data="$JSON_PAYLOAD" \
                "$UPLOAD_URL" \
                -O /dev/null 2>&1 || echo "Warning: Failed to upload logs"
        else
            echo "Warning: curl or wget not available, cannot upload logs"
        fi
    fi
fi

echo "Rebooting in 5 seconds..."
sleep 5
#reboot
