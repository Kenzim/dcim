#!/bin/bash
cd /root/dcim
# Simple HTTP server on port 8080, serving the tftp directory
python3 -m http.server 8080 --bind 192.168.12.74 --directory tftp

