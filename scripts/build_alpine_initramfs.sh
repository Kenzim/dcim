#!/bin/bash
set -e

# Build script for custom Alpine initramfs with extra packages
# This creates an Alpine initramfs that boots, gets network, downloads and runs a script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/alpine-initramfs"
OUTPUT_DIR="$PROJECT_ROOT/tftp/pxe/temp_os/alpine-script"

echo "Building custom Alpine initramfs with script support..."

# Clean and create build directory
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

cd "$BUILD_DIR"

# Download Alpine's standard initramfs as base
echo "Downloading Alpine LTS initramfs..."
if [ ! -f "$PROJECT_ROOT/tftp/pxe/temp_os/alpine/initrd/initramfs-lts" ]; then
    echo "ERROR: Alpine initramfs not found. Please download it first."
    exit 1
fi

# Extract Alpine initramfs
echo "Extracting Alpine initramfs..."
cd "$BUILD_DIR"
zcat "$PROJECT_ROOT/tftp/pxe/temp_os/alpine/initrd/initramfs-lts" | cpio -idmv

# Download and extract additional packages
echo "Downloading additional packages..."
ALPINE_REPO="https://dl-cdn.alpinelinux.org/alpine/latest-stable/main/x86_64"
PACKAGES="wget curl 7zip zip zstd bash openssh openssh-client-default ntfs-3g nano"

mkdir -p "$BUILD_DIR/tmp/packages"
cd "$BUILD_DIR/tmp/packages"

# Function to download and extract Alpine package
download_package() {
    local pkg=$1
    echo "Downloading $pkg..."
    
    # Get HTML directory listing and find package
    HTML=$(wget -qO- "${ALPINE_REPO}/" 2>/dev/null || echo "")
    if [ -z "$HTML" ]; then
        echo "  Warning: Could not fetch package directory"
        return 1
    fi
    
    # Find package file in HTML (format: href="package-version.apk")
    PKG_FILE=$(echo "$HTML" | grep -oP "href=\"${pkg}-[0-9][^\"]+\.apk\"" | head -1 | cut -d'"' -f2)
    if [ -z "$PKG_FILE" ]; then
        echo "  Warning: Package $pkg not found in repository"
        return 1
    fi
    
    echo "  Found: $PKG_FILE"
    
    # Download package
    if wget -q "${ALPINE_REPO}/${PKG_FILE}"; then
        echo "  Downloaded: $PKG_FILE"
        
        # Extract .apk directly (it's a tar.gz containing files directly)
        # Alpine .apk files contain: .PKGINFO, .SIGN.RSA.*, and actual files (usr/, etc/, etc.)
        if tar -xzf "$PKG_FILE" -C "$BUILD_DIR" 2>/dev/null; then
            # Clean up metadata files
            rm -f "$BUILD_DIR/.PKGINFO" "$BUILD_DIR/.SIGN.RSA"* 2>/dev/null || true
            rm -f "$PKG_FILE" 2>/dev/null || true
            echo "  Successfully installed $pkg"
            return 0
        else
            echo "  Warning: Failed to extract $PKG_FILE"
            rm -f "$PKG_FILE" 2>/dev/null || true
            return 1
        fi
    else
        echo "  Warning: Failed to download $PKG_FILE"
        return 1
    fi
}

# Download each package
for pkg in $PACKAGES; do
    download_package "$pkg" || echo "  Skipping $pkg"
done

# Clean up
cd "$BUILD_DIR"
rm -rf tmp

# Backup Alpine's original init
echo "Backing up Alpine's original init..."
if [ -f init ]; then
    cp init init.orig
else
    echo "ERROR: No original init found"
    exit 1
fi

# Modify Alpine's init to inject our script execution before switch_root
echo "Modifying Alpine's init to inject script execution before switch_root..."
python3 << 'PYTHON_EOF'
import sys

with open('init.orig', 'r') as f:
    lines = f.readlines()

# Find the exec switch_root line and inject our hook before it
hook_code = '''# Custom script execution hook (injected by build script)
run_api_script() {
    NETIF=$(ip link | grep -E '^[0-9]+:' | grep -v lo | head -1 | cut -d: -f2 | tr -d ' ')
    [ -z "$NETIF" ] && return 1
    MAC=$(ip link show "$NETIF" 2>/dev/null | grep -oP 'link/ether \\K[^ ]+' | tr '[:lower:]' '[:upper:]' || echo "")
    [ -z "$MAC" ] && return 1
    SCRIPT_URL="http://192.168.12.74:8000/api/servers/interaction/pxe?mac=${MAC}&script=true"
    # Try to download script - ignore 404 errors (no script available)
    if wget -O /tmp/run.sh "$SCRIPT_URL" 2>/dev/null || curl -f -o /tmp/run.sh "$SCRIPT_URL" 2>/dev/null; then
        if [ -s /tmp/run.sh ]; then
            chmod +x /tmp/run.sh
            echo "Running script from API..."
            sh /tmp/run.sh
            echo "Script completed, dropping to shell"
            exec /bin/sh
        fi
    fi
    # If we get here, no script was available - return 0 to continue normal boot
    return 0
}
# Try to run script - if no script available, continue with normal Alpine boot
if ! run_api_script; then
    echo "No script available, continuing with normal Alpine boot"
fi
'''

new_lines = []
injected = False
for i, line in enumerate(lines):
    # Inject before exec switch_root (look for the line that does exec switch_root)
    if 'exec switch_root' in line and not injected:
        new_lines.append(hook_code)
        new_lines.append('\n')
        injected = True
    new_lines.append(line)

# If we didn't find exec switch_root, append at the end (shouldn't happen)
if not injected:
    print("WARNING: Could not find 'exec switch_root' line, appending hook at end", file=sys.stderr)
    new_lines.append('\n')
    new_lines.append(hook_code)

with open('init', 'w') as f:
    f.writelines(new_lines)

print(f"Injected script hook into init (found switch_root: {injected})")
PYTHON_EOF

if [ ! -f init ] || [ ! -s init ]; then
    echo "ERROR: Failed to create modified init script"
    exit 1
fi

chmod +x init
echo "Modified init script created successfully"

# Pack the initramfs
echo "Packing initramfs..."
find . -print0 | cpio --null -ov --format=newc | gzip -9 > "$OUTPUT_DIR/initrd/alpine-script.cpio.gz"

echo "Initramfs built successfully: $OUTPUT_DIR/initrd/alpine-script.cpio.gz"
ls -lh "$OUTPUT_DIR/initrd/alpine-script.cpio.gz"

# Copy kernel
mkdir -p "$OUTPUT_DIR/kernel"
if [ -f "$PROJECT_ROOT/tftp/pxe/temp_os/alpine/kernel/vmlinuz-lts" ]; then
    cp "$PROJECT_ROOT/tftp/pxe/temp_os/alpine/kernel/vmlinuz-lts" "$OUTPUT_DIR/kernel/vmlinuz-lts"
    echo "Kernel copied: $OUTPUT_DIR/kernel/vmlinuz-lts"
else
    echo "WARNING: Alpine kernel not found"
fi

echo ""
echo "Build complete!"
echo "Initramfs: $OUTPUT_DIR/initrd/alpine-script.cpio.gz"
echo "Kernel: $OUTPUT_DIR/kernel/vmlinuz-lts"
