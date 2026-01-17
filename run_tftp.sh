#!/bin/bash
# Start TFTP server for PXE boot
# Must use absolute path for -s option (secure/chroot mode)

cd /root/dcim
/usr/sbin/in.tftpd -4 -l -s /root/dcim/tftp -a 192.168.12.74:69 -c -v
