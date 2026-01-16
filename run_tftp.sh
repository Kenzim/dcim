#!/bin/bash
cd /root/dcim
/usr/sbin/in.tftpd -l -s /root/dcim/tftp -a 192.168.12.74:69 -c -v -4