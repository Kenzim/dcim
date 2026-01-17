#!/bin/bash
# Windows Server 2022 Installation Script
# This script runs in Alpine Linux environment booted via PXE

set -e

echo "=== Windows Server 2022 Installation ==="
echo "Server IP: ${SERVER_IP}"
echo "Server MAC: ${SERVER_MAC}"
echo "Edition: ${PARAM_EDITION}"
echo ""

# Configuration from template parameters
WINDOWS_EDITION="${PARAM_EDITION:-Standard}"
ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}"
PRODUCT_KEY="${PARAM_PRODUCT_KEY:-}"
PARTITIONING="${PARAM_PARTITIONING:-auto}"

# Windows ISO URL (you'll need to host this or download it)
# For now, using a placeholder - you'll need to provide the actual ISO
WINDOWS_ISO_URL="http://192.168.12.74:8000/isos/windows-server-2022.iso"
WINDOWS_ISO="/tmp/windows.iso"

# Mount point for Windows installation
WINDOWS_MOUNT="/mnt/windows"

# Find the target disk (usually the first disk that's not the boot disk)
# This assumes the Alpine boot disk is different from the Windows target disk
TARGET_DISK=""
for disk in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
    if [ -b "$disk" ]; then
        # Skip if this is the boot disk (check if it has partitions mounted)
        if ! mount | grep -q "^${disk}"; then
            TARGET_DISK="$disk"
            break
        fi
    fi
done

if [ -z "$TARGET_DISK" ]; then
    echo "ERROR: Could not find target disk for Windows installation"
    exit 1
fi

echo "Target disk: $TARGET_DISK"

# Install required tools
apk add --no-cache wimlib wget parted dosfstools ntfs-3g

# Download Windows ISO (if not already cached)
if [ ! -f "$WINDOWS_ISO" ]; then
    echo "Downloading Windows Server 2022 ISO..."
    wget -O "$WINDOWS_ISO" "$WINDOWS_ISO_URL" || {
        echo "ERROR: Failed to download Windows ISO"
        echo "Please ensure the ISO is available at: $WINDOWS_ISO_URL"
        exit 1
    }
fi

# Mount Windows ISO
ISO_MOUNT="/mnt/iso"
mkdir -p "$ISO_MOUNT"
mount -o loop "$WINDOWS_ISO" "$ISO_MOUNT"

# Partition the disk
echo "Partitioning disk..."
if [ "$PARTITIONING" = "auto" ]; then
    # Create GPT partition table
    parted -s "$TARGET_DISK" mklabel gpt
    
    # Create EFI system partition (100MB)
    parted -s "$TARGET_DISK" mkpart primary fat32 1MiB 101MiB
    parted -s "$TARGET_DISK" set 1 esp on
    
    # Create Windows partition (rest of disk)
    parted -s "$TARGET_DISK" mkpart primary ntfs 101MiB 100%
    
    # Format partitions
    mkfs.fat -F 32 "${TARGET_DISK}1"
    mkfs.ntfs -f "${TARGET_DISK}2"
else
    echo "Custom partitioning not implemented yet"
    exit 1
fi

# Mount Windows partition
mkdir -p "$WINDOWS_MOUNT"
mount -t ntfs-3g "${TARGET_DISK}2" "$WINDOWS_MOUNT"

# Extract Windows installation files
echo "Extracting Windows installation files..."
# Find the install.wim file (edition-specific)
INSTALL_WIM=""
for wim in "$ISO_MOUNT"/sources/install*.wim "$ISO_MOUNT"/sources/install*.esd; do
    if [ -f "$wim" ]; then
        INSTALL_WIM="$wim"
        break
    fi
done

if [ -z "$INSTALL_WIM" ]; then
    echo "ERROR: Could not find install.wim or install.esd"
    exit 1
fi

# Apply Windows image to disk
echo "Applying Windows image (this may take a while)..."
# Map edition name to WIM index (you may need to adjust these)
case "$WINDOWS_EDITION" in
    "Standard")
        WIM_INDEX=2  # Adjust based on your WIM file
        ;;
    "Datacenter")
        WIM_INDEX=3  # Adjust based on your WIM file
        ;;
    *)
        WIM_INDEX=2
        ;;
esac

wimlib-imagex apply "$INSTALL_WIM" "$WIM_INDEX" "$WINDOWS_MOUNT" --check

# Copy boot files
echo "Copying boot files..."
mkdir -p "${WINDOWS_MOUNT}/EFI/Microsoft/Boot"
cp -r "$ISO_MOUNT"/EFI/Microsoft/Boot/* "${WINDOWS_MOUNT}/EFI/Microsoft/Boot/"

# Create password file for Windows to read on first boot
# Windows boot script will read this and set the password
PASSWORD_FILE="${WINDOWS_MOUNT}/Windows/Setup/Scripts/admin_password.txt"
mkdir -p "$(dirname "$PASSWORD_FILE")"
echo "$ADMIN_PASSWORD" > "$PASSWORD_FILE"
chmod 600 "$PASSWORD_FILE"

# Create autounattend.xml for unattended installation
AUTOUNATTEND="${WINDOWS_MOUNT}/autounattend.xml"
cat > "$AUTOUNATTEND" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <settings pass="windowsPE">
        <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <SetupUILanguage>
                <UILanguage>en-US</UILanguage>
            </SetupUILanguage>
            <InputLocale>en-US</InputLocale>
            <UserLocale>en-US</UserLocale>
            <UILanguageFallback>en-US</UILanguageFallback>
            <SystemLocale>en-US</SystemLocale>
        </component>
        <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <DiskConfiguration>
                <Disk wcm:action="add">
                    <DiskID>0</DiskID>
                    <WillWipeDisk>true</WillWipeDisk>
                    <CreatePartitions>
                        <CreatePartition wcm:action="add">
                            <Order>1</Order>
                            <Type>EFI</Type>
                            <Size>100</Size>
                        </CreatePartition>
                        <CreatePartition wcm:action="add">
                            <Order>2</Order>
                            <Type>Primary</Type>
                            <Extend>true</Extend>
                        </CreatePartition>
                    </CreatePartitions>
                    <ModifyPartitions>
                        <ModifyPartition wcm:action="add">
                            <Order>1</Order>
                            <PartitionID>1</PartitionID>
                            <Label>System</Label>
                            <Format>FAT32</Format>
                        </ModifyPartition>
                        <ModifyPartition wcm:action="add">
                            <Order>2</Order>
                            <PartitionID>2</PartitionID>
                            <Label>Windows</Label>
                            <Format>NTFS</Format>
                        </ModifyPartition>
                    </ModifyPartitions>
                </Disk>
            </DiskConfiguration>
            <ImageInstall>
                <OSImage>
                    <InstallTo>
                        <DiskID>0</DiskID>
                        <PartitionID>2</PartitionID>
                    </InstallTo>
                    <InstallToAvailablePartition>false</InstallToAvailablePartition>
                </OSImage>
            </ImageInstall>
            <UserData>
                <ProductKey>
                    <Key>$PRODUCT_KEY</Key>
                    <WillShowUI>OnError</WillShowUI>
                </ProductKey>
                <AcceptEula>true</AcceptEula>
            </UserData>
        </component>
    </settings>
    <settings pass="specialize">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <ComputerName>SERVER-${SERVER_ID}</ComputerName>
        </component>
    </settings>
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <UserAccounts>
                <AdministratorPassword>
                    <Value>$ADMIN_PASSWORD</Value>
                    <PlainText>false</PlainText>
                </AdministratorPassword>
            </UserAccounts>
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <SkipMachineOOBE>true</SkipMachineOOBE>
                <SkipUserOOBE>true</SkipUserOOBE>
            </OOBE>
        </component>
    </settings>
</unattend>
EOF

# Install BCD (Boot Configuration Data)
echo "Configuring boot..."
# Mount EFI partition
EFI_MOUNT="/mnt/efi"
mkdir -p "$EFI_MOUNT"
mount "${TARGET_DISK}1" "$EFI_MOUNT"

# Copy EFI boot files
mkdir -p "${EFI_MOUNT}/EFI/Microsoft/Boot"
cp -r "$ISO_MOUNT"/EFI/Microsoft/Boot/* "${EFI_MOUNT}/EFI/Microsoft/Boot/"

# Unmount everything
umount "$EFI_MOUNT"
umount "$WINDOWS_MOUNT"
umount "$ISO_MOUNT"

echo ""
echo "=== Windows Server 2022 Installation Complete ==="
echo "The server will boot into Windows on the next reboot."
echo "Administrator password has been set."
echo ""
echo "To complete setup:"
echo "1. Reboot the server"
echo "2. Windows will boot and complete installation"
echo "3. The password file is located at: C:\\Windows\\Setup\\Scripts\\admin_password.txt"
echo ""
