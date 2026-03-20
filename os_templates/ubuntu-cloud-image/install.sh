#!/bin/bash
# Ubuntu Cloud Image Installation Script
# Runs inside temporary Debian Live environment.

set -euo pipefail

LOG_FILE="/tmp/dcim-installation.log"
exec > >(tee -a "$LOG_FILE") 2>&1

DCIM_STATUS_REPORTED=0

json_escape() {
  sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g'
}

report_installation_status() {
  local status="$1"
  local error_msg="${2:-}"
  [ -n "${INSTALLATION_TASK_ID:-}" ] || return 0
  [ -n "${SERVER_ID:-}" ] || return 0
  [ -n "${API_BASE:-}" ] || return 0

  local url="${API_BASE}/api/servers/${SERVER_ID}/installation-tasks/${INSTALLATION_TASK_ID}/logs"
  [ -n "${DOWNLOAD_TOKEN:-}" ] && url="${url}?token=${DOWNLOAD_TOKEN}"

  local logs=""
  if [ -f "$LOG_FILE" ]; then
    # Send the tail of the log file (up to ~50KB) to avoid huge payloads
    logs="$(tail -c 50000 "$LOG_FILE" | json_escape)"
  fi

  local payload
  if [ "$status" = "failed" ] && [ -n "$error_msg" ]; then
    payload=$(printf '{"logs":"%s","status":"failed","error_message":"%s"}' "$logs" "$(echo "$error_msg" | json_escape)")
  else
    payload=$(printf '{"logs":"%s","status":"%s"}' "$logs" "$status")
  fi
  curl -X POST "$url" -H "Content-Type: application/json" -d "$payload" -f -s >/dev/null 2>&1 || true
}

fail() {
  local msg="$1"
  echo "ERROR: $msg"
  report_installation_status failed "$msg"
  DCIM_STATUS_REPORTED=1
  exit 1
}

trap 'rc=$?; if [ "$DCIM_STATUS_REPORTED" = "0" ] && [ "$rc" -ne 0 ]; then report_installation_status failed "Ubuntu cloud image install failed"; fi' EXIT

echo "=== Ubuntu Cloud Image Install ==="
echo "Server ID: ${SERVER_ID:-<unknown>}"
echo "Installation Task ID: ${INSTALLATION_TASK_ID:-<unknown>}"
echo "Selected release: ${PARAM_UBUNTU_RELEASE:-jammy}"
echo

API_BASE="${API_BASE_URL:-http://127.0.0.1:8000}"
UBUNTU_RELEASE="${PARAM_UBUNTU_RELEASE:-jammy}"
DEFAULT_USER="${PARAM_USERNAME:-rackflow}"
DEFAULT_PASS="${PARAM_PASSWORD:-}"
DEFAULT_SSH_KEY="${PARAM_SSH_PUBLIC_KEY:-}"

[ -n "$DEFAULT_PASS" ] || fail "Template parameter 'password' is required"

partpath() {
  if [[ "$1" =~ nvme ]]; then
    echo "${1}p$2"
  else
    echo "${1}$2"
  fi
}

get_disk_serial() {
  local disk="$1"
  lsblk -dno SERIAL "$disk" 2>/dev/null | head -1 | tr -d '[:space:]'
}

get_disk_size_gb() {
  local disk="$1"
  local size_bytes
  size_bytes=$(lsblk -bdno SIZE "$disk" 2>/dev/null | head -1 || true)
  [ -n "$size_bytes" ] && echo $((size_bytes / 1024 / 1024 / 1024))
}

get_disk_type() {
  local disk="$1"
  if [ -f "/sys/block/$(basename "$disk")/queue/rotational" ]; then
    local rotational
    rotational=$(cat "/sys/block/$(basename "$disk")/queue/rotational" 2>/dev/null || echo "1")
    [ "$rotational" = "0" ] && echo "ssd" || echo "hdd"
  else
    echo "hdd"
  fi
}

TARGET_DISK=""
OS_DISK_SERIAL="${OS_DISK_SERIAL:-}"
OS_DISK_SIZE_GB="${OS_DISK_SIZE_GB:-}"
OS_DISK_TYPE="${OS_DISK_TYPE:-}"

AVAILABLE_DISKS=()
for disk in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
  [ -b "$disk" ] || continue
  info=$(lsblk -dn -o TYPE,TRAN,RM "$disk" 2>/dev/null | head -1 || true)
  disk_type_field=$(echo "$info" | awk '{print $1}')
  disk_tran_field=$(echo "$info" | awk '{print $2}')
  disk_rm_field=$(echo "$info" | awk '{print $3}')
  [ "$disk_type_field" = "disk" ] || continue
  if [ "$disk_tran_field" = "usb" ] || [ "$disk_rm_field" = "1" ]; then
    continue
  fi
  AVAILABLE_DISKS+=("$disk")
done

[ ${#AVAILABLE_DISKS[@]} -gt 0 ] || fail "No candidate disks found"

if [ -n "$OS_DISK_SERIAL" ]; then
  for disk in "${AVAILABLE_DISKS[@]}"; do
    if [ "$(get_disk_serial "$disk")" = "$OS_DISK_SERIAL" ]; then
      TARGET_DISK="$disk"
      break
    fi
  done
fi

if [ -z "$TARGET_DISK" ] && [ -n "$OS_DISK_SIZE_GB" ] && [ -n "$OS_DISK_TYPE" ]; then
  BEST_MATCH=""
  BEST_DIFF=999999
  for disk in "${AVAILABLE_DISKS[@]}"; do
    disk_size=$(get_disk_size_gb "$disk")
    disk_type=$(get_disk_type "$disk")
    [ -n "$disk_size" ] || continue
    [ "$disk_type" = "$OS_DISK_TYPE" ] || continue
    diff=$((disk_size > OS_DISK_SIZE_GB ? disk_size - OS_DISK_SIZE_GB : OS_DISK_SIZE_GB - disk_size))
    if [ "$diff" -lt "$BEST_DIFF" ]; then
      BEST_DIFF="$diff"
      BEST_MATCH="$disk"
    fi
  done
  [ -n "$BEST_MATCH" ] && TARGET_DISK="$BEST_MATCH"
fi

if [ -z "$TARGET_DISK" ]; then
  TARGET_DISK="${AVAILABLE_DISKS[0]}"
fi

echo "Selected target disk: $TARGET_DISK"

WORKDIR="/tmp/rackflow-ubuntu-cloud-image"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

RELEASE_URL="https://cloud-images.ubuntu.com/releases/${UBUNTU_RELEASE}/release/"
LOCAL_IMAGE_DIR="/os_templates/ubuntu-cloud-image"
LOCAL_IMAGE_CANDIDATE="${LOCAL_IMAGE_DIR}/${UBUNTU_RELEASE}-server-cloudimg-amd64.img"

IMAGE_FILE=""

echo "Checking network connectivity to Ubuntu cloud image repository..."
if curl -fsS --head --connect-timeout 5 --max-time 10 "$RELEASE_URL" >/dev/null 2>&1; then
  echo "Network available. Querying release index: $RELEASE_URL"

  HTML_INDEX="$(mktemp)"
  if curl -fsSL "$RELEASE_URL" -o "$HTML_INDEX"; then
    IMAGE_NAME=$(grep -Eo 'ubuntu-[0-9]+\.[0-9]+(\.[0-9]+)?-server-cloudimg-amd64\.img' "$HTML_INDEX" | sort -Vu | tail -1)
    if [ -n "${IMAGE_NAME:-}" ]; then
      IMAGE_URL="${RELEASE_URL}${IMAGE_NAME}"
      IMAGE_FILE="${WORKDIR}/${IMAGE_NAME}"
      echo "Downloading image: $IMAGE_URL"
      if ! curl -fSL "$IMAGE_URL" -o "$IMAGE_FILE"; then
        echo "WARNING: Failed to download cloud image from network, will try local fallback if available."
        IMAGE_FILE=""
      fi
    else
      echo "WARNING: Could not determine image filename from release index, will try local fallback if available."
    fi
  else
    echo "WARNING: Failed to read Ubuntu release index, will try local fallback if available."
  fi
else
  echo "No network connectivity to $RELEASE_URL. Will try local fallback image if available."
fi

if [ -z "$IMAGE_FILE" ]; then
  echo "Attempting to use local image: $LOCAL_IMAGE_CANDIDATE"
  if [ -f "$LOCAL_IMAGE_CANDIDATE" ]; then
    IMAGE_FILE="$LOCAL_IMAGE_CANDIDATE"
    echo "Using local Ubuntu cloud image: $IMAGE_FILE"
  else
    fail "No network access to Ubuntu cloud images and no local image found at '$LOCAL_IMAGE_CANDIDATE'"
  fi
fi

echo "Writing image to disk: $TARGET_DISK"
if command -v qemu-img >/dev/null 2>&1; then
  qemu-img convert -p -f qcow2 -O raw "$IMAGE_FILE" "$TARGET_DISK" || fail "qemu-img convert failed"
else
  if command -v qemu-nbd >/dev/null 2>&1; then
    modprobe nbd max_part=8 || true
    qemu-nbd --disconnect /dev/nbd0 >/dev/null 2>&1 || true
    qemu-nbd --connect=/dev/nbd0 "$IMAGE_FILE" || fail "Failed to attach cloud image via qemu-nbd"
    dd if=/dev/nbd0 of="$TARGET_DISK" bs=64M status=progress conv=fsync || fail "Disk clone via qemu-nbd failed"
    qemu-nbd --disconnect /dev/nbd0 || true
  else
    fail "Neither qemu-img nor qemu-nbd is available to write qcow2 image"
  fi
fi
sync

echo "Injecting cloud-init datasource config into image"
modprobe nbd max_part=8 || true
qemu-nbd --disconnect /dev/nbd0 >/dev/null 2>&1 || true
qemu-nbd --connect=/dev/nbd0 "$TARGET_DISK" || fail "Failed to expose installed disk via qemu-nbd"
sleep 2

ROOT_PART="$(lsblk -nrpo NAME,FSTYPE /dev/nbd0 | awk '$2 ~ /ext4|xfs|btrfs/ {print $1}' | tail -1)"
[ -n "$ROOT_PART" ] || fail "Could not detect Linux root partition on installed image"

mkdir -p /mnt/ubuntu-root
mount "$ROOT_PART" /mnt/ubuntu-root || fail "Failed to mount root partition"
mkdir -p /mnt/ubuntu-root/etc/cloud/cloud.cfg.d

cat > /mnt/ubuntu-root/etc/cloud/cloud.cfg.d/99-rackflow-datasource.cfg <<EOF
datasource_list: [ NoCloud, None ]
datasource:
  NoCloud:
    seedfrom: "${API_BASE}/api/servers/interaction/cloud-init/"
EOF

cat > /mnt/ubuntu-root/etc/cloud/cloud.cfg.d/99-rackflow-user.cfg <<EOF
# Written by Rackflow install template
users:
  - default
EOF

umount /mnt/ubuntu-root || true
qemu-nbd --disconnect /dev/nbd0 || true
sync

echo "Ubuntu cloud image deployed successfully."
report_installation_status completed
DCIM_STATUS_REPORTED=1
exit 0
