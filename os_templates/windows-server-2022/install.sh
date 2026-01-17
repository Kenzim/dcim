#!/bin/bash
# Windows Server 2022 Installation Script
# This script runs in Alpine Linux environment booted via PXE
# Simple approach: download disk image, dd it to disk, write password file

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
DISK_IMAGE="/tmp/disk_image.iso"

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
echo "WARNING: This will completely wipe $TARGET_DISK"
echo ""

# Install required tools
echo "Installing required tools..."
apk add --no-cache wget dd parted dosfstools ntfs-3g

# Download disk image
echo "Downloading disk image from ${DISK_IMAGE_URL}..."
if ! wget -O "$DISK_IMAGE" "$DISK_IMAGE_URL"; then
    echo "ERROR: Failed to download disk image"
    exit 1
fi

# Mount ISO
ISO_MOUNT="/mnt/iso"
mkdir -p "$ISO_MOUNT"
mount -o loop "$DISK_IMAGE" "$ISO_MOUNT"

# Partition disk (GPT with EFI + Windows)
echo "Partitioning disk..."
parted -s "$TARGET_DISK" mklabel gpt
parted -s "$TARGET_DISK" mkpart primary fat32 1MiB 101MiB
parted -s "$TARGET_DISK" set 1 esp on
parted -s "$TARGET_DISK" mkpart primary ntfs 101MiB 100%

# Format partitions
mkfs.fat -F 32 "${TARGET_DISK}1"
mkfs.ntfs -f "${TARGET_DISK}2"

# Mount Windows partition
WINDOWS_MOUNT="/mnt/windows"
mkdir -p "$WINDOWS_MOUNT"
mount -t ntfs-3g "${TARGET_DISK}2" "$WINDOWS_MOUNT"

# Copy all files from ISO to disk
echo "Copying files from ISO to disk (this may take a while)..."
cp -a "$ISO_MOUNT"/* "$WINDOWS_MOUNT/" || true

# Copy EFI boot files
mkdir -p "${WINDOWS_MOUNT}/EFI/Microsoft/Boot"
if [ -d "$ISO_MOUNT/EFI/Microsoft/Boot" ]; then
    cp -a "$ISO_MOUNT/EFI/Microsoft/Boot"/* "${WINDOWS_MOUNT}/EFI/Microsoft/Boot/" || true
fi

# Write password file for Windows to read on first boot
echo "Writing password configuration..."
PASSWORD_FILE="${WINDOWS_MOUNT}/dcim_password.txt"
echo "$ADMIN_PASSWORD" > "$PASSWORD_FILE"
chmod 600 "$PASSWORD_FILE"

# Unmount
umount "$WINDOWS_MOUNT" || true
umount "$ISO_MOUNT" || true

echo ""
echo "=== Installation Complete ==="
echo "Disk image written to $TARGET_DISK"
echo "Password written to disk (Windows will read it on first boot)"
echo ""
echo "Rebooting in 5 seconds..."
sleep 5
reboot
