#!/bin/bash
# DHCP and TFTP are no longer installed as systemd services.
# Start/stop them from the web UI (Services tab), or the app runs them as
# subprocesses when DHCP_TFTP_SERVICE_URL is not set (e.g. in Docker with run_services_direct).
# For a dedicated runner container, use docker-compose with the dhcp-tftp-runner service.
echo "Systemd service installation is no longer used."
echo "DHCP and TFTP are controlled from the app (web UI or API)."
exit 0
