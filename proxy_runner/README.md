# Rackflow proxy runner

Go dual-protocol forwarder for customer HTTP proxy / SOCKS5 services.

- Listens on one port (`PROXY_LISTEN`, default `:8080`): SOCKS5 if the first byte is `0x05`, otherwise HTTP proxy (`CONNECT` + absolute-URI).
- Syncs assignments from Rackflow `GET /api/runner/proxy/config` using `RUNNER_API_KEY` (also acts as the 30s health phone-home).
- Authenticates with `(bind IP, username, password)`. Bind IP is `LocalAddr` of the accepted connection (use host networking / host VIPs), or `X-Bind-IP` for HTTP.
- Health: `GET` on `HEALTH_LISTEN` (default `:8081`) path `/health`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `RACKFLOW_BASE_URL` | (required) | App base URL, no trailing slash |
| `RUNNER_API_KEY` | (required) | Bearer key from Admin → Proxy Runners (generated once) |
| `SYNC_INTERVAL_SECONDS` | `30` | Config poll / phone-home interval |
| `PROXY_LISTEN` | `:8080` | Client proxy listen address |
| `HEALTH_LISTEN` | `:8081` | Health HTTP listen address |
| `DIAL_TIMEOUT` | `15s` | Upstream dial timeout |
| `IDLE_TIMEOUT` | `5m` | Idle splice timeout |

## Deploy

Prefer `network_mode: host` so customer bind IPs on the host are the connection `LocalAddr`.

1. In Rackflow Admin → **Proxy Runners**, create a runner and copy the generated API key (shown once).
2. Set `RACKFLOW_BASE_URL` and `RUNNER_API_KEY` on the runner.
3. The runner phones home every 30s via the config endpoint; Rackflow shows Online when `last_seen` is within 90s.

Proxy runners are **not** bound to bare-metal locations. Config includes all active IPAM proxy assignments; the process only serves credentials for IPs present on the host.
