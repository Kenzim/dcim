#!/bin/bash
# Test Script
# This is a basic test script to verify Alpine boot and script execution
echo "=== Test Script ==="
echo "Server IP: ${SERVER_IP}"
echo "Server MAC: ${SERVER_MAC}"
echo "Server ID: ${SERVER_ID}"
echo ""
echo "This script is running in $(uname -a)!"
echo "Current date: $(date)"
echo "Current user: $(whoami)"
echo "Hostname: $(hostname)"
echo ""
echo "Disk information:"
lsblk || true
echo ""
echo "Network information:"
ip addr show || ifconfig || true
echo ""
echo "Test script completed successfully!"
echo "Rebooting in 10 seconds..."
sleep 10
reboot
