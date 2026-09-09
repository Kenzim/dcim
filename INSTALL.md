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

#### Runner authentication (required)

The DHCP and TFTP runner APIs are **fail-closed**: if no `API_KEY` is configured they refuse every request (HTTP 503) except `/health`. The compose files therefore require `DHCP_RUNNER_API_KEY` and `TFTP_RUNNER_API_KEY` to be set (via `.env`), and `docker compose up` will error until they are. Generate strong random keys and set the matching value on each service instance in the UI (Services tab) so the app presents the same `X-API-Key`:

```bash
# .env (do not commit real secrets)
DHCP_RUNNER_API_KEY=$(openssl rand -hex 32)
TFTP_RUNNER_API_KEY=$(openssl rand -hex 32)
```

For isolated local development only, you may set `ALLOW_UNAUTHENTICATED=true` on a runner to disable the check; never do this in production.

#### Service-instance key encryption (required)

Per-location service-instance API keys are encrypted at rest with Fernet. The app compose requires `SERVICE_INSTANCE_ENCRYPTION_KEY` and enables `REQUIRE_SERVICE_INSTANCE_ENCRYPTION=true`, so creating/updating a key fails unless the encryption key is present. Generate one and add it to `.env`:

```bash
# .env (do not commit real secrets)
SERVICE_INSTANCE_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

Existing plaintext keys remain usable and are re-encrypted automatically the next time they verify successfully. Rotating the encryption key invalidates stored keys (re-enter them in the UI).

#### End-user IPMI reverse proxy (optional)

The `ipmi-proxy-runner` service gives end users authenticated, browser-based access to a bare-metal server's BMC web UI (and HTML5 KVM) without exposing the BMC publicly and without a RackFlow login. Each server is served at its own subdomain `https://{server.uuid}.ipmi.<base>`.

How it works: a client (WHMCS via the billing API, or the RackFlow admin/client UI) mints a short-lived, single-use launch ticket. The browser is redirected to the server's subdomain, where the edge runner redeems the ticket for an HMAC-signed, host-only session cookie and then reverse-proxies HTTP + WebSocket traffic to the private BMC.

Requirements:

- **Wildcard DNS**: `*.ipmi.<base>` (e.g. `*.ipmi.rackflow.com`) pointing at the host running `ipmi-proxy-runner`.
- **Wildcard TLS certificate** for `*.ipmi.<base>`, mounted into the runner. Set `IPMI_TLS_CERT_DIR` to a directory containing `tls.crt` and `tls.key` (mounted read-only at `/certs`).
- **Private BMC network reachability**: the runner host must be able to reach each server's BMC web UI; the BMC must not be publicly routable.

App and runner configuration (`.env`):

```bash
# .env (do not commit real secrets)
# App side (used to build launch URLs and authenticate the runner):
IPMI_PROXY_PUBLIC_BASE=ipmi.rackflow.com
IPMI_PROXY_RUNNER_API_KEY=$(openssl rand -hex 32)
IPMI_TICKET_TTL_SECONDS=60          # one-time launch ticket lifetime
IPMI_SESSION_TTL_SECONDS=1800       # edge session cookie lifetime

# Edge runner side:
RACKFLOW_BASE_URL=https://rackflow.example.com   # how the runner reaches the app
IPMI_COOKIE_SECRET=$(openssl rand -hex 32)       # signs edge session cookies
IPMI_TLS_CERT_DIR=./ipmi_certs                   # dir with tls.crt + tls.key
# IPMI_UPSTREAM_VERIFY_TLS=1                      # enforce BMC cert verification (default off; BMC certs are usually self-signed)
```

Per server, in the admin UI (Servers → edit → IPMI Web Management): enable the proxy, set the BMC web management URL (reachable from the runner), and optionally the read-only viewer username/password shown to the user on launch.

Notes: the runner is set to `RUNNER_API_KEY=${IPMI_PROXY_RUNNER_API_KEY}` in compose (it must match the app's `IPMI_PROXY_RUNNER_API_KEY`). Launch tickets are single-use and expire after `IPMI_TICKET_TTL_SECONDS`; if a link stops working the user simply relaunches.

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

### Admin MCP (remote AI)

Streamable HTTP MCP is **off by default**. Mint keys at **Admin → MCP keys** (`/admin/mcp-keys`) even while `/mcp` is disabled, then enable the endpoint:

```bash
MCP_ENABLED=true
MCP_RATE_LIMIT_PER_KEY=120
MCP_RATE_LIMIT_PER_KEY_WINDOW_SECONDS=60
MCP_RATE_LIMIT_PER_IP=240
MCP_RATE_LIMIT_PER_IP_WINDOW_SECONDS=60
```

When `MCP_ENABLED=true`, remote clients POST to `https://<host>/mcp` with `Authorization: Bearer rfmcp_…`. Keys are hashed (SHA-256); plaintext is shown once on create/rotate. Scopes are `read`, `write`, and `destructive` (`read` ⊂ `write` ⊂ `destructive`). Optional CIDR allowlists apply per key. Do not give billing/WHMCS keys access to MCP.

### Building images only

- Main app: `docker build --target app -t dcim-app .` (required: default is last stage, bandwidth-poller)
- Bandwidth poller only: `docker build --target bandwidth-poller -t dcim-bandwidth-poller .`

## Local development stack (Docker, bind mounts + autoreload)

For iterating on the code, `docker-compose.dev.yml` runs the app and frontend from bind-mounted source with autoreload, so editing Python or Svelte files takes effect without rebuilding images:

```bash
docker compose -f docker-compose.dev.yml up --build
```

- **Backend (`app`)** – uses the `app-dev` Dockerfile stage (`uvicorn --reload`, with `dhcpd`/`tftpd` baked in so DHCP/TFTP run as in-app subprocesses). `./app`, `./scripts`, `./alembic` are bind-mounted; edits trigger a reload.
- **Frontend (`frontend`)** – `node:20-alpine` running the Vite dev server with HMR; it proxies `/api` to the backend.
- **Database / Redis** – it does **not** start local MySQL/Redis. It reads `DATABASE_URL`, `REDIS_HOST`, etc. from `.env`, so it talks to whatever those point to (currently a shared remote host). Migrations (`alembic upgrade head`) run on startup against that DB, so this is not isolated from it.
- **Ports** – host `8000`/`8001` are used by other processes on this box, so the stack publishes on `0.0.0.0`:
  - UI (primary): `http://<host>:5173`
  - API / docs: `http://<host>:8088/docs`
- Runs under its own Compose project name (`rackflow-dev`), isolated from the production stack in `docker-compose.yml`.

Stop it with:

```bash
docker compose -f docker-compose.dev.yml down
```

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
