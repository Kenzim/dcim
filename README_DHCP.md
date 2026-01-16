# DHCP Server Setup

This directory contains the DHCP server configuration file for ISC DHCP (dhcpd).

## Files

- `dhcpd.conf` - Main DHCP server configuration file
- `dhcpd.leases` - DHCP lease database (auto-updated by server)

## Installation

### Debian/Ubuntu:
```bash
sudo apt-get update
sudo apt-get install isc-dhcp-server
```

### RHEL/CentOS:
```bash
sudo yum install dhcp
# or
sudo dnf install dhcp
```

## Configuration

1. Edit `dhcpd.conf` to match your network:
   - Update subnet, netmask, and IP ranges
   - Configure DNS servers
   - Set default gateway
   - Add static IP reservations if needed

2. Set the network interface in `/etc/default/isc-dhcp-server`:
   ```
   INTERFACESv4="eth1"
   ```

3. Create lease file with proper permissions:
   ```bash
   sudo touch /var/lib/dhcp/dhcpd.leases
   sudo chown dhcpd:dhcpd /var/lib/dhcp/dhcpd.leases
   ```

## Usage

### Test configuration:
```bash
sudo dhcpd -t -cf /root/dcim/dhcpd.conf
```

### Run DHCP server with custom config:
```bash
sudo dhcpd -f -cf /root/dcim/dhcpd.conf -lf /root/dcim/dhcpd.leases eth1
```

### Start as systemd service:
```bash
# Copy config to system location
sudo cp /root/dcim/dhcpd.conf /etc/dhcp/dhcpd.conf
sudo cp /root/dcim/dhcpd.leases /var/lib/dhcp/dhcpd.leases

# Start service
sudo systemctl start isc-dhcp-server
sudo systemctl enable isc-dhcp-server
```

## Configuration Notes

- **Interface**: eth1 (192.168.12.74)
- **Subnet**: 192.168.12.0/24
- **IP Range**: 192.168.12.100 - 192.168.12.200
- **Static Reservation**: MAC 00:0e:1e:6f:16:b0 -> IP 192.168.12.80
- **No Gateway**: Gateway option removed
- **No DNS**: DNS servers option removed

## Static IP Reservations

To reserve a static IP for a device, add a host declaration in `dhcpd.conf`:

```
host server1 {
    hardware ethernet 00:11:22:33:44:55;
    fixed-address 192.168.1.50;
    option host-name "server1";
}
```

## Troubleshooting

- Check logs: `sudo journalctl -u isc-dhcp-server -f`
- Verify config: `sudo dhcpd -t -cf /root/dcim/dhcpd.conf`
- Check lease file: `cat /var/lib/dhcp/dhcpd.leases`

## Security Notes

- Ensure the DHCP server only listens on the correct network interface
- Use firewall rules to restrict DHCP traffic if needed
- Consider using DHCP snooping on switches to prevent rogue DHCP servers

