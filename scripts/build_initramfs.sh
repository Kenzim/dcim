#!/bin/bash
set -e

# Build script for custom initramfs
# This creates a minimal initramfs that boots, gets network, downloads and runs a script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/initramfs"
OUTPUT_DIR="$PROJECT_ROOT/tftp/pxe/temp_os/custom-initramfs"

echo "Building custom initramfs..."

# Clean and create build directory
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"/{bin,sbin,etc,proc,sys,dev,tmp}

cd "$BUILD_DIR"

# Check if busybox-static is installed
if ! command -v busybox &> /dev/null; then
    echo "Installing busybox-static..."
    apt-get update && apt-get install -y busybox-static
fi

# Copy busybox
echo "Copying BusyBox..."
cp /bin/busybox ./bin/

# Create busybox applets
echo "Creating BusyBox applets..."
cd bin
./busybox --install .
cd ..

# Create /init script
echo "Creating /init script..."
cat > init << 'INIT_EOF'
#!/bin/sh
set -e

# Mount essential filesystems first
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev || mount -t tmpfs dev /dev

# Ensure input devices are available (for keyboard)
mkdir -p /dev/input
if [ ! -e /dev/input/mice ]; then
    mknod -m 666 /dev/input/mice c 13 63 2>/dev/null || true
fi

# Ensure TTY is available before setting up console
if [ ! -e /dev/tty ]; then
    mknod -m 666 /dev/tty c 5 0 2>/dev/null || true
fi
if [ ! -e /dev/tty0 ]; then
    mknod -m 666 /dev/tty0 c 4 0 2>/dev/null || true
fi

# Set up console properly - use cttyhack to make console the controlling terminal
# This ensures keyboard input works properly
if [ -c /dev/console ]; then
    # Use cttyhack to wrap the rest of the init script
    # This makes /dev/console the controlling terminal
    exec /bin/cttyhack /bin/sh -c '
        echo "== Custom initramfs booting =="
        
        # Redirect stdio to console
        exec 0</dev/console
        exec 1>/dev/console
        exec 2>/dev/console

# Ensure TTY is available
if [ ! -e /dev/tty ]; then
    mknod -m 666 /dev/tty c 5 0 2>/dev/null || true
fi
if [ ! -e /dev/tty0 ]; then
    mknod -m 666 /dev/tty0 c 4 0 2>/dev/null || true
fi

# Determine network interface (try common names)
NETIF=""
for iface in eth0 ens18 ens3 enp0s3; do
    if ip link show "$iface" > /dev/null 2>&1; then
        NETIF="$iface"
        break
    fi
done

if [ -z "$NETIF" ]; then
    # Fallback: use first interface
    NETIF=$(ip link show | grep -E "^[0-9]+:" | head -1 | cut -d: -f2 | tr -d ' ')
fi

if [ -z "$NETIF" ]; then
    echo "ERROR: No network interface found"
    sleep 10
    reboot -f
fi

echo "Using network interface: $NETIF"

# Start network (DHCP)
echo "Starting network (DHCP)..."
udhcpc -i "$NETIF" -q || {
    echo "DHCP failed, trying static config..."
    ip link set "$NETIF" up
    sleep 2
}

# Get server MAC address for script URL (normalize to colon format)
MAC=$(ip link show "$NETIF" | grep -oP 'link/ether \K[^ ]+' | tr '[:lower:]' '[:upper:]')
echo "MAC address: $MAC"

# Fetch script from API (use script=true to get script content, not iPXE script)
SCRIPT_URL="http://192.168.12.74:8000/api/servers/interaction/pxe?mac=${MAC}&script=true"
echo "Fetching script from: $SCRIPT_URL"

# Try to fetch the script
if wget -O /tmp/run.sh "$SCRIPT_URL" 2>/dev/null; then
    echo "Script downloaded successfully"
    chmod +x /tmp/run.sh
    echo "Running script..."
    sh /tmp/run.sh || {
        echo "Script execution failed"
        sleep 10
    }
else
    echo "Failed to download script, entering shell..."
    echo "You can manually download and run:"
    echo "  wget -O /tmp/run.sh $SCRIPT_URL"
    echo "  sh /tmp/run.sh"
    # Use cttyhack to ensure keyboard works in shell
    exec /bin/cttyhack /bin/sh
fi

        echo "Done, rebooting..."
        sync
        sleep 2
        reboot -f
    '
fi
INIT_EOF

chmod +x init

# Create device nodes
echo "Creating device nodes..."
sudo mknod -m 600 dev/console c 5 1 2>/dev/null || mknod -m 600 dev/console c 5 1
sudo mknod -m 666 dev/null c 1 3 2>/dev/null || mknod -m 666 dev/null c 1 3
sudo mknod -m 666 dev/tty c 5 0 2>/dev/null || mknod -m 666 dev/tty c 5 0
sudo mknod -m 666 dev/tty0 c 4 0 2>/dev/null || mknod -m 666 dev/tty0 c 4 0

# Create input directory for keyboard/mouse
mkdir -p dev/input
sudo mknod -m 666 dev/input/mice c 13 63 2>/dev/null || mknod -m 666 dev/input/mice c 13 63

# Pack the initramfs
echo "Packing initramfs..."
find . -print0 | cpio --null -ov --format=newc | gzip -9 > "$OUTPUT_DIR/initrd/custom-initramfs.cpio.gz"

echo "Initramfs built successfully: $OUTPUT_DIR/initrd/custom-initramfs.cpio.gz"
ls -lh "$OUTPUT_DIR/initrd/custom-initramfs.cpio.gz"

# Copy kernel (use Alpine kernel as default, or Debian if available)
KERNEL_SOURCE=""
if [ -f "/boot/vmlinuz-$(uname -r)" ]; then
    KERNEL_SOURCE="/boot/vmlinuz-$(uname -r)"
    echo "Using Debian kernel: $KERNEL_SOURCE"
elif [ -f "$PROJECT_ROOT/tftp/pxe/temp_os/alpine/kernel/vmlinuz-virt" ]; then
    KERNEL_SOURCE="$PROJECT_ROOT/tftp/pxe/temp_os/alpine/kernel/vmlinuz-virt"
    echo "Using Alpine kernel: $KERNEL_SOURCE"
else
    echo "WARNING: No kernel found. Please copy a kernel to: $OUTPUT_DIR/kernel/vmlinuz-virt"
    echo "You can download one from Alpine or use a Debian kernel."
    exit 1
fi

cp "$KERNEL_SOURCE" "$OUTPUT_DIR/kernel/vmlinuz-virt"
echo "Kernel copied: $OUTPUT_DIR/kernel/vmlinuz-virt"

echo ""
echo "Build complete!"
echo "Initramfs: $OUTPUT_DIR/initrd/custom-initramfs.cpio.gz"
echo "Kernel: $OUTPUT_DIR/kernel/vmlinuz-virt"
