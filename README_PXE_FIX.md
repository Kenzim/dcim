# PXE Boot Fix - Memory Error

## Problem
Error: "PXE-E79: NBP is too big to fit in free base memory"

This happens when the iPXE binary is too large for the network card's memory.

## Solution
Use the smaller `undionly.kpxe` file instead of `ipxe.efi` or `ipxe.pxe`.

## Setup

```bash
cd /root/dcim/tftp/pxe
wget https://boot.ipxe.org/undionly.kpxe -O undionly.kpxe
```

The DHCP config is already updated to use `pxe/undionly.kpxe`.

## Alternative Files (if needed)

If `undionly.kpxe` doesn't work, try:
- `ipxe.pxe` - Standard PXE (may still be too large)
- `ipxe.lkrn` - Linux kernel format
- `snponly.efi` - UEFI with limited drivers

Download from: https://boot.ipxe.org/

