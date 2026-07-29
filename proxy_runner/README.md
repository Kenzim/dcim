# Rackflow proxy runner

Go dual-protocol forwarder for customer HTTP proxy / SOCKS5 services.

- Listens on one port (`PROXY_LISTEN`, default `:8080`): SOCKS5 if the first byte is `0x05`, otherwise HTTP proxy (`CONNECT` + absolute-URI).
- Syncs assignments from Rackflow `GET /api/runner/proxy/config` using `RUNNER_API_KEY`.
- Authenticates with `(bind IP, username, password)`. Bind IP is `LocalAddr` of the accepted connection (use host networking / host VIPs), or `X-Bind-IP` for HTTP.
- Health: `GET` on `HEALTH_LISTEN` (default `:8081`) path `/health`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `RACKFLOW_BASE_URL` | (required) | App base URL, no trailing slash |
| `RUNNER_API_KEY` | (required) | Bearer key matching a `proxy` service instance |
| `SYNC_INTERVAL_SECONDS` | `10` | Config poll interval |
| `PROXY_LISTEN` | `:8080` | Client proxy listen address |
| `HEALTH_LISTEN` | `:8081` | Health HTTP listen address |
| `DIAL_TIMEOUT` | `15s` | Upstream dial timeout |
| `IDLE_TIMEOUT` | `5m` | Idle splice timeout |

## Deploy

Prefer `network_mode: host` so customer bind IPs on the host are the connection `LocalAddr`. Register a location service instance with `service_type=proxy` and the same API key in the admin UI.
