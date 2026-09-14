#!/bin/bash
# Ubuntu Cloud Image Installation Script
# Runs inside temporary Debian Live environment.

set -euo pipefail

LOG_FILE="/tmp/dcim-installation.log"
exec > >(tee -a "$LOG_FILE") 2>&1

DCIM_STATUS_REPORTED=0

json_escape() {
  python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])' 2>/dev/null || \
    sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g; s/\r//g; s/$/\\n/' | tr -d '\n'
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

configure_rackflow_serial() {
  local root="$1"
  install -d "$root/usr/local/sbin"
  install -d "$root/etc/systemd/system/serial-getty@.service.d"
  install -d "$root/etc/systemd/system/getty.target.wants"
  install -d "$root/etc/default/grub.d"

  cat > "$root/usr/local/sbin/rackflow-serial-sh" <<'SERIALSH'
#!/bin/bash
# Line-oriented serial admin shell. Host does not echo; keep SOL Local echo on.
# Prompt "RF> " is the delimiter for programmatic SOL send.
# SOL Enter is CR; encode_sol_stdin turns that into CRLF. Ignore CR so one
# Enter is one command, not an extra empty read that reprints the prompt.
exec >/dev/tty 2>&1
stty sane 2>/dev/null || true
stty 115200 cs8 -parenb -cstopb -crtscts -ixon -ixoff 2>/dev/null || true
stty -echo icanon -icrnl igncr 2>/dev/null || true
export HOME=/root USER=root LOGNAME=root
cd /root 2>/dev/null || cd /
printf '\r\nRackflow serial console - root@%s\r\n' "$(hostname 2>/dev/null || echo host)"
prompt() { printf 'RF> '; }
prompt
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  case "$line" in
    exit|logout|quit) printf '\r\n'; exit 0 ;;
  esac
  if [[ "$line" == cd || "$line" == cd[[:space:]]* ]]; then
    eval "$line" || true
  else
    bash -lc "$line" || true
  fi
  prompt
done
SERIALSH
  chmod 0755 "$root/usr/local/sbin/rackflow-serial-sh"

  cat > "$root/etc/systemd/system/serial-getty@.service.d/rackflow.conf" <<'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,57600,38400,9600 -n -l /usr/local/sbin/rackflow-serial-sh %I $TERM
GETTY

  cat > "$root/etc/default/grub.d/99-rackflow-serial.cfg" <<'GRUB'
GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX console=tty0 console=ttyS0,115200n8 console=ttyS1,115200n8"
GRUB

  for tty in ttyS0 ttyS1 ttyS2; do
    ln -sf /lib/systemd/system/serial-getty@.service \
      "$root/etc/systemd/system/getty.target.wants/serial-getty@${tty}.service"
  done

  if command -v chroot >/dev/null 2>&1; then
    chroot "$root" usermod -s /bin/bash root >/dev/null 2>&1 || true
    chroot "$root" usermod -U root >/dev/null 2>&1 || true
  fi

  local grubcfg
  for grubcfg in "$root/boot/grub/grub.cfg" "$root/boot/grub2/grub.cfg"; do
    [ -f "$grubcfg" ] || continue
    if grep -q 'console=ttyS1,115200' "$grubcfg"; then
      continue
    fi
    if grep -q 'console=ttyS0' "$grubcfg"; then
      sed -i 's/console=ttyS0/console=ttyS0,115200n8 console=ttyS1,115200n8/g' "$grubcfg" || true
    else
      sed -i '/^[[:space:]]*linux/ s/$/ console=tty0 console=ttyS0,115200n8 console=ttyS1,115200n8/' "$grubcfg" || true
    fi
  done
}

echo "Injecting cloud-init datasource config into image"
partprobe "$TARGET_DISK" >/dev/null 2>&1 || true
sleep 2

ROOT_PART="$(lsblk -nrpo NAME,FSTYPE "$TARGET_DISK" | awk '$2 ~ /ext4|xfs|btrfs/ {print $1}' | tail -1)"
if [ -z "$ROOT_PART" ]; then
  echo "Partitions not visible on $TARGET_DISK, trying qemu-nbd -f raw"
  modprobe nbd max_part=8 || true
  qemu-nbd --disconnect /dev/nbd0 >/dev/null 2>&1 || true
  qemu-nbd --connect=/dev/nbd0 -f raw "$TARGET_DISK" || fail "Failed to expose installed disk via qemu-nbd"
  sleep 2
  ROOT_PART="$(lsblk -nrpo NAME,FSTYPE /dev/nbd0 | awk '$2 ~ /ext4|xfs|btrfs/ {print $1}' | tail -1)"
  NBD_USED=1
else
  NBD_USED=0
fi
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

echo "Configuring Rackflow serial console (ttyS0/ttyS1/ttyS2, 115200, autologin root)"
configure_rackflow_serial /mnt/ubuntu-root

umount /mnt/ubuntu-root || true
if [ "${NBD_USED:-0}" = "1" ]; then
  qemu-nbd --disconnect /dev/nbd0 || true
fi
sync

echo "Ubuntu cloud image deployed successfully."
report_installation_status completed
DCIM_STATUS_REPORTED=1
exit 0
