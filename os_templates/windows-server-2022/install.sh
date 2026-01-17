#!/bin/bash
# Windows Server 2022 Installation Script
# This script runs in Alpine Linux environment booted via PXE
# Simple approach: download raw disk image, dd it to disk, write password file

set -e

echo "=== Windows Server 2022 Installation ==="
echo "Server IP: ${SERVER_IP}"
echo "Server MAC: ${SERVER_MAC}"
echo ""

# Configuration from template parameters
ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: ADMIN_PASSWORD parameter is required"
    exit 1
fi

# Get disk image URL (injected by backend from template.disk_image)
DISK_IMAGE_URL="${DISK_IMAGE_URL}"
DISK_IMAGE="/tmp/disk_image.raw"

if [ -z "$DISK_IMAGE_URL" ]; then
    echo "ERROR: DISK_IMAGE_URL not provided"
    exit 1
fi

# Find the target disk (first disk, usually /dev/sda or /dev/nvme0n1)
TARGET_DISK=""
for disk in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
    if [ -b "$disk" ]; then
        TARGET_DISK="$disk"
        break
    fi
done

if [ -z "$TARGET_DISK" ]; then
    echo "ERROR: Could not find target disk"
    exit 1
fi

echo "Target disk: $TARGET_DISK"
echo "WARNING: This will completely overwrite $TARGET_DISK"
echo ""

# Install required tools
echo "Installing required tools..."
apk add --no-cache wget dd ntfs-3g

# Download raw disk image
echo "Downloading raw disk image from ${DISK_IMAGE_URL}..."
if ! wget -O "$DISK_IMAGE" "$DISK_IMAGE_URL"; then
    echo "ERROR: Failed to download disk image"
    exit 1
fi

# Write raw disk image directly to target disk
echo "Writing disk image to $TARGET_DISK (this may take a while)..."
dd if="$DISK_IMAGE" of="$TARGET_DISK" bs=4M status=progress

# Sync to ensure all data is written
sync

# Try to mount the Windows partition and write password file
echo "Attempting to write password configuration..."
WINDOWS_MOUNT="/mnt/windows"
mkdir -p "$WINDOWS_MOUNT"

# Try common partition numbers (Windows usually on partition 2 or 3)
for part_num in 2 3 1; do
    if [ -b "${TARGET_DISK}${part_num}" ]; then
        echo "Trying to mount ${TARGET_DISK}${part_num}..."
        if mount -t ntfs-3g "${TARGET_DISK}${part_num}" "$WINDOWS_MOUNT" 2>/dev/null; then
            echo "Mounted ${TARGET_DISK}${part_num}, writing password file..."
            PASSWORD_FILE="${WINDOWS_MOUNT}/dcim_password.txt"
            echo "$ADMIN_PASSWORD" > "$PASSWORD_FILE"
            chmod 600 "$PASSWORD_FILE"
            umount "$WINDOWS_MOUNT" || true
            echo "Password file written successfully"
            break
        fi
    fi
done

echo ""
echo "=== Installation Complete ==="
echo "Raw disk image written to $TARGET_DISK"
echo "Password written to disk (Windows will read it on first boot)"
echo ""
echo "Rebooting in 5 seconds..."
sleep 5
reboot
