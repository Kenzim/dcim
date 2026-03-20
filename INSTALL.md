# Rackflow Installation Guide

## System Requirements

- Python 3.11+
- MySQL/MariaDB database

## Installation Steps

### 1. Install Python Dependencies

```bash
cd /root/dcim
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Database

Edit `app/core/config.py` or set environment variables for database connection.

### 3. Run Database Migrations

```bash
alembic upgrade head
```

### 4. Start the Application

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use a process manager like systemd, supervisor, or PM2.

## Service Management

DHCP and TFTP are controlled from the web UI (Services tab). The app runs them as subprocesses, or calls a remote runner container API when `DHCP_TFTP_SERVICE_URL` is set (see Docker).

## Docker

The app can run as Docker containers: the main FastAPI app (with static frontend) and the SNMP bandwidth poller as separate services, with MySQL and Redis.

### Build and run with Docker Compose

```bash
docker compose up -d --build
```

This starts:

- **app** – FastAPI on port 8000 (frontend is built and served from `/app/static`)
- **bandwidth-poller** – SNMP poller (same DB, poll interval 60s)
- **dhcp-runner** – optional container that runs `dhcpd`; app calls its API (port 9080) to start/stop/restart.
- **tftp-runner** – optional container that runs `in.tftpd`; app calls its API (port 9081), TFTP on 69/udp. Config is shared via volume `/shared`.
- **mysql** – MariaDB 11 (database `dcim`, user `dcim`/`dcim`)
- **redis** – Redis 7

When `DHCP_RUNNER_URL` and `TFTP_RUNNER_URL` are set, the UI’s DHCP/TFTP controls talk to those containers. The app writes `dhcpd.conf` and TFTP files to the shared volume; the runners read them. For DHCP to serve a real LAN you may need `network_mode: host` and `DHCP_INTERFACES` set (e.g. `eth0`).

### First run: migrations and initial admin

Migrations run automatically on app startup. To create an initial admin user when the database has no users, set:

- `INITIAL_ADMIN_USERNAME` – admin username (e.g. `admin`)
- `INITIAL_ADMIN_PASSWORD` – admin password (required; do not leave empty)
- `INITIAL_ADMIN_EMAIL` – optional (defaults to `{username}@localhost`)

Example:

```bash
export INITIAL_ADMIN_USERNAME=admin
export INITIAL_ADMIN_PASSWORD=your-secure-password
docker compose up -d --build
```

Or in `docker-compose.yml` under `app.environment`, or via a `.env` file in the project root.

### Overriding configuration

Set `DATABASE_URL`, `REDIS_HOST`, etc. in the environment or via a `.env` file in the project root. Example for custom DB credentials:

```bash
# .env (do not commit real credentials)
DATABASE_URL=mysql+pymysql://user:pass@mysql:3306/dcim
REDIS_HOST=redis
```

Then in `docker-compose.yml` under `app` and `bandwidth-poller` you can use `env_file: .env` or pass variables in the `environment` section.

### Building images only

- Main app: `docker build --target app -t dcim-app .` (required: default is last stage, bandwidth-poller)
- Bandwidth poller only: `docker build --target bandwidth-poller -t dcim-bandwidth-poller .`

## Service Configuration

DHCP and TFTP configuration is stored in the **database** and managed via the web UI (Services tab). No JSON config files are used. After saving configuration, the generated config files (e.g. `dhcpd.conf`) are written to the shared volume and the runner services are started or restarted automatically when using the Docker setup.

## SNMP bandwidth poller (optional)

A separate app polls SNMP port counters (IF-MIB) from switches that use a monitoring-capable plugin (e.g. SNMPv3) and stores samples in the database for bandwidth history.

- **One-shot (e.g. cron):** `python3 -m scripts.snmp_bandwidth_poller --once`
- **Long-lived (default 60s interval):** `python3 -m scripts.snmp_bandwidth_poller --interval 60`
- **Environment:** Uses the same `DATABASE_URL` as the main app. Optional `POLL_INTERVAL` (seconds).

To run as a systemd service, copy the unit and enable it (adjust paths if needed):

```bash
sudo cp systemd/dcim-snmp-bandwidth-poller.service /etc/systemd/system/
# Edit /etc/systemd/system/dcim-snmp-bandwidth-poller.service to set WorkingDirectory and EnvironmentFile
sudo systemctl daemon-reload
sudo systemctl enable --now dcim-snmp-bandwidth-poller.service
```

## Notes

- Services are created dynamically when started through the GUI if they don't exist
- Service files are automatically updated when configuration changes
- The installation script uses the current configuration files to generate service files
- Services run independently of the main application and persist across app restarts
