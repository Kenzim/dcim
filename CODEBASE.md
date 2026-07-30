# Rackflow / DCIM — Codebase Map

**Read this file first in every new chat.** Use it to jump straight to the right layer and files. Prefer editing the mapped paths below over broad repo searches.

Product name: **Rackflow** (repo folder: `dcim`). Stack: **FastAPI + SQLAlchemy + MySQL + Redis** backend, **Svelte 4 + Vite + Bootstrap** SPA frontend, optional **Docker runners** (DHCP/TFTP/proxy), **WHMCS** PHP module, **Alembic** migrations.

---

## Quick “where do I edit?”

| I want to… | Start here |
|---|---|
| Change an admin UI page | `frontend/src/components/<Page>.svelte` + wire route in `frontend/src/routes/Admin.svelte` + nav in `Sidebar.svelte` |
| Call a new API from the UI | Add helper in `frontend/src/lib/api.js`, then use it from the component |
| Add/change a REST endpoint | `app/api/<domain>.py` → register in `app/main.py` if new router |
| Change DB schema / model | `app/models/<entity>.py` + new file under `alembic/versions/` + matching `app/dao/<entity>_dao.py` |
| Business logic (provisioning, DHCP gen, VM place) | `app/services/` |
| Server power / BMC / Proxmox actions | `app/plugins/` (`ipmi.py`, `proxmox.py`, …) |
| Switch SNMP / bandwidth | `app/plugins/snmpv3.py`, poller `scripts/snmp_bandwidth_poller.py` |
| Auth / sessions / Redis | `app/core/auth.py`, `app/core/redis.py`, `app/core/billing_auth.py` |
| Config / env vars | `app/core/config.py` + `.env` |
| OS install templates | `os_templates/<name>/` (`template.json` + `install.sh`) |
| PXE / cloud-init / boot scripts served to bare metal | `app/api/server_interaction.py` |
| Billing / WHMCS API surface | Backend: `app/api/billing.py`; WHMCS module: `whmcs/modules/servers/rackflow/` |
| DHCP/TFTP runner containers | `dhcp_runner/main.py`, `tftp_runner/main.py`, services in `app/services/dhcp_*.py` / `tftp_*.py` |
| Proxy runner (IPAM sync) | `proxy_runner/main.py`, API `app/api/runner_proxy.py` |
| Tests | Mirror path under `tests/` (`tests/api/`, `tests/dao/`, `tests/services/`, …) |
| Install / Docker ops | `INSTALL.md`, `docker-compose.yml`, `Makefile`, `Dockerfile` |

---

## Architecture (layers)

```
Browser SPA (frontend/)
    ↓ HTTP /api/*
FastAPI routers (app/api/)          ← request validation, auth deps, HTTP shape
    ↓
Services (app/services/)            ← orchestration / domain logic
    ↓
DAOs (app/dao/)                     ← DB queries
    ↓
Models (app/models/)                ← SQLAlchemy ORM
    ↓
MySQL (Alembic migrations)

Plugins (app/plugins/)              ← BMC/hypervisor/switch adapters (loaded from disk)
Integrations (app/integrations/)    ← billing adapters (e.g. WHMCS)
Runners (dhcp_runner/, tftp_runner/, proxy_runner/)  ← optional sidecars
```

**Conventions**

- API routes live under `/api/...` (mounted in `app/main.py`).
- Frontend talks only through `frontend/src/lib/api.js` (`API_BASE = '/api'`).
- DAOs are static-method classes (`ServerDAO.create(db, ...)`).
- Plugins inherit `ServerPlugin` / switch base classes; registry loads them from disk.
- Migrations: create with Alembic; app also runs `alembic upgrade head` on startup (`app/main.py` lifespan).

---

## Top-level layout

| Path | Role |
|---|---|
| `app/` | Backend application (API, models, DAOs, services, plugins, core) |
| `frontend/` | Svelte SPA source; built assets served as static files |
| `alembic/` | DB migrations (`alembic/versions/`) |
| `tests/` | Pytest suite mirroring `app/` |
| `scripts/` | Ops helpers (admin user, SNMP poller, PXE finalize, `dev.sh`) |
| `dhcp_runner/` | Container API that runs `dhcpd` |
| `tftp_runner/` | Container API that runs `in.tftpd` |
| `proxy_runner/` | Sidecar that syncs proxy/IPAM config from Rackflow |
| `os_templates/` | Disk-based OS install templates |
| `isos/`, `disk_images/`, `tftp/` | Boot media / PXE assets |
| `whmcs/` | WHMCS provisioning module + hooks |
| `systemd/` | Host unit for SNMP bandwidth poller |
| `docker-compose*.yml`, `Dockerfile`, `Makefile` | Build & deploy |
| `INSTALL.md` | Install / Docker runbook |
| `requirements.txt`, `pytest.ini`, `alembic.ini`, `sonar-project.properties` | Tooling config |

---

## Backend entry & core

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI app, lifespan (migrations, Redis keyspace, reconciliation, seed admin/plugins), mounts all routers, SPA fallback |
| `app/core/config.py` | `Settings` from env / `.env` (`DATABASE_URL`, Redis, runner URLs, initial admin, static path, encryption key) |
| `app/core/database.py` | SQLAlchemy engine / `SessionLocal` |
| `app/core/auth.py` | Session tokens in Redis, admin/user deps |
| `app/core/billing_auth.py` | API-key auth for billing integrations |
| `app/core/redis.py` | Redis client |
| `app/core/redis_notifications.py` | Keyspace notifications / listeners |
| `app/core/plugin_capabilities.py` | Capability helpers for plugins |
| `app/core/service_instance_crypto.py` | Fernet encrypt/decrypt for runner API keys |
| `app/core/seed_categories.py` | Default category seed |

Run locally: `uvicorn app.main:app --host 0.0.0.0 --port 8000` (see `INSTALL.md`).

---

## API routers (`app/api/`)

All included from `app/main.py` under `/api`.

| Module | Mount / prefix | Domain |
|---|---|---|
| `user.py` | `/api/users` | Login, logout, me, sessions, password, CRUD users |
| `plugin.py` | `/api/plugins` | List server plugins from disk |
| `switch_plugin.py` | `/api/switch-plugins` | Switch plugins |
| `location.py` | `/api/locations` | Locations CRUD |
| `location_dhcp.py` | `/api/locations/{id}/dhcp/...` | Per-location DHCP status/settings/start/stop |
| `location_tftp.py` | `/api/locations/{id}/tftp/...` | Per-location TFTP controls |
| `service_instance.py` | `/api/service-instances` | DHCP/TFTP runner registration per location |
| `proxy_runners.py` | `/api/admin/proxy-runners` | Standalone proxy runners (generated key, phone-home health) |
| `proxy_subnet_groups.py` | `/api/admin/proxy-subnet-groups` | Named IPAM subnet pools for proxy catalog auto-assign |
| `rack.py` | `/api/racks` | Racks + servers-in-rack |
| `server.py` | `/api/servers` | Servers CRUD, power, boot, capabilities, hardware detection, activity, bandwidth |
| `server_interaction.py` | `/api/servers/interaction` | **PXE scripts, cloud-init, ISOs, temp-OS, kernels, download tokens** (bare-metal install path) |
| `installation_tasks.py` | `/api/servers/{id}/installation-tasks` | Install task history/logs |
| `server_group.py` | `/api/server-groups` | Server groups |
| `network_switch.py` | `/api/network-switches` | Switches, ports, bandwidth |
| `cable_run.py` | `/api/cable-runs` | Cable runs / cabling |
| `os_templates.py` | `/api/os-templates` | Scan/list OS templates from disk |
| `billing.py` | `/api/billing` | External billing API (register/suspend/power/reinstall/…) |
| `billing_admin.py` | (billing-admin tags) | Admin management of billing integrations |
| `product_catalog.py` | `/api/product-catalog` | Families, products, VM templates, OS profiles, VM config |
| `proxmox_inventory.py` | `/api/proxmox` | Clusters, sync, inventory, VM plan |
| `ipam.py` | `/api/ipam` | Subnets, assignments, history |
| `vm_ip_allocations.py` | `/api/vm-ip-allocations` | Preallocated VM IPs |
| `runner_proxy.py` | `/api/runner/proxy` | Config for proxy runner |
| `services_admin.py` | `/api/admin/services` | Admin service lifecycle (VM/bare-metal provision, ownership) |
| `services_client.py` | `/api/services` | Client “my services” |
| `scripts_admin.py` | (scripts-admin) | Admin scripts CRUD |
| `asset.py` | `/api/assets` | Asset manager / files / labels |
| `utils.py` | `/api/utils` | e.g. password generation |
| `dhcp.py` / `tftp.py` | **Not mounted** in `main.py` — prefer location-scoped DHCP/TFTP APIs | |

**Largest / hottest files:** `server.py`, `server_interaction.py`, `billing.py`, `network_switch.py`, `services_admin.py`.

Interactive OpenAPI docs when the app is running: `http://<host>:8000/docs`.

---

## Models (`app/models/`)

Exported from `app/models/__init__.py`. One file per entity (or small group).

| Area | Files |
|---|---|
| Identity | `user.py`, `external_user.py`, `user_external_identity_link.py` |
| Datacenter layout | `location.py`, `rack.py`, `server.py`, `server_group.py`, `network_port.py`, `disk.py` |
| Networking gear | `network_switch.py`, `switch_port.py`, `cable_run.py`, `switch_bandwidth_sample.py` |
| Boot / install | `boot_task.py`, `installation_task.py`, `server_capability.py`, `hardware_detection_report.py`, `server_activity.py` |
| DHCP/TFTP | `dhcp_config.py`, `tftp_config.py`, `service_instance.py` |
| Catalog / services | `product_catalog.py`, `service.py`, `service_bare_metal.py`, `service_vm.py`, `vm_config.py`, `script.py` |
| Proxmox / IPAM | `proxmox_inventory.py`, `ipam.py`, `vm_ip_allocation.py`, `vmid_reservation.py`, `proxy_runner.py`, `proxy_subnet_group.py` |
| Billing / assets | `billing_integration.py`, `asset.py` |
| Plugins (metadata leftovers) | `plugin.py`, `plugin_category.py`, `switch_plugin.py`, `category.py` |

Schema changes → new Alembic revision under `alembic/versions/`.

---

## DAOs (`app/dao/`)

Mirror models: `*_dao.py` (e.g. `server_dao.py`, `ipam_dao.py`, `service_instance_dao.py`). Keep SQL here; keep orchestration in services/API.

---

## Services (`app/services/`)

| File | Purpose |
|---|---|
| `dhcp_config_generator.py` / `dhcp_config_service.py` / `dhcp_service.py` | Build `dhcpd.conf`, persist settings, start/stop via runner or subprocess |
| `tftp_config_service.py` / `tftp_service.py` | TFTP root/files and runner control |
| `runner_client.py` | HTTP client to DHCP/TFTP runner containers |
| `temp_os_service.py` | Temporary OS (live) boot catalog |
| `os_template_service.py` | Scan/load `os_templates/` |
| `download_token_service.py` | One-time download tokens for boot media |
| `server_activity_logger.py` | Server activity log writes |
| `vm_provisioning_service.py` / `vm_strategy_executor.py` / `vm_os_strategy.py` / `vm_install_type_strategy.py` | VM provision strategies |
| `proxmox_placement.py` / `vmid_allocator.py` / `ip_allocation.py` | Placement, VMID, IP assignment |
| `service_resource.py` / `service_product_snapshot.py` | Service ↔ product resource helpers |
| `reconciliation_jobs.py` | Background reconciliation loop (started in lifespan) |
| `plugin_sync.py` | Plugin DB sync (mostly no-op; plugins from disk) |

---

## Plugins & integrations

### Server / switch plugins — `app/plugins/`

| File | Role |
|---|---|
| `base.py` | `ServerPlugin` ABC, categories, capabilities contract |
| `capabilities.py` | Capability + UI schema definitions |
| `registry.py` | Discover/load server plugins from disk |
| `ipmi.py` | IPMI/BMC power & related |
| `proxmox.py` | Proxmox VM provisioning / power |
| `snmpv3.py` | SNMPv3 switch plugin (ports, bandwidth) |
| `switch_base.py` / `switch_registry.py` | Switch plugin ABC + registry |

**Add a server plugin:** new module in `app/plugins/` implementing `ServerPlugin`, register via registry discovery, expose `CAPABILITIES` for UI.

### Billing integrations — `app/integrations/`

| File | Role |
|---|---|
| `base.py` | `BaseIntegration` ABC |
| `whmcs.py` | WHMCS-specific integration |
| `registry.py` | Integration type registry |

---

## Schemas & utils

- `app/schemas/` — shared Pydantic models (`user.py`, `billing.py`). Most request/response models are **defined inline** in the matching `app/api/*.py` file.
- `app/utils/` — small helpers (e.g. `ipv4_netmask.py`).
- Alembic env: `alembic/env.py` (with `alembic.ini`).

---

## Frontend (`frontend/`)

**Stack:** Svelte 4, Vite 5, Bootstrap 5, svelte-spa-router, CodeMirror.  
**Dev:** `cd frontend && npm run dev` (proxies `/api` → `VITE_PROXY_TARGET` or `localhost:8000`).  
**Build:** `npm run build` → `frontend/dist` (Docker copies to static path).

### Bootstrap & routing

| File | Role |
|---|---|
| `src/main.js` | Mounts `App.svelte` |
| `src/App.svelte` | Root shell |
| `src/routes/index.js` | Top routes: `/`, `/admin`, `/admin/*`, `/client`, `/login` |
| `src/routes/Home.svelte` | Landing |
| `src/routes/Admin.svelte` | **Admin shell + all sub-route → component mapping** |
| `src/routes/Client.svelte` | Client area shell |
| `src/lib/router.js` | SPA navigation / `currentRoute` |
| `src/lib/api.js` | **All HTTP client helpers** (~2.6k lines) |
| `src/stores/auth.js` | Auth state |
| `src/stores/theme.js` | Theme |
| `src/app.css` | Global styles / CSS variables |

Feature flags (Vite env): `VITE_ENABLE_PROXMOX`, `VITE_ENABLE_IPAM_PROXY` (used in `Admin.svelte` / `Sidebar.svelte`).

### Admin URL → component map

Defined in `Admin.svelte`; nav links in `Sidebar.svelte`.

| URL | Component |
|---|---|
| `/admin` | Dashboard placeholder (`PageHeader`) |
| `/admin/servers` | `Servers.svelte` |
| `/admin/servers/:id` | `ServerDetail.svelte` (**largest UI file**) |
| `/admin/switches` | `Switches.svelte` |
| `/admin/switches/:id` | `SwitchDetail.svelte` |
| `/admin/racks` | `Racks.svelte` |
| `/admin/racks/:id` | `RackView.svelte` |
| `/admin/racks/rows/:locationId/:row` | `RowView.svelte` |
| `/admin/locations` | `Locations.svelte` |
| `/admin/locations/:id` | `LocationDetail.svelte` (DHCP/TFTP runners) |
| `/admin/server-groups` | `ServerGroups.svelte` |
| `/admin/server-groups/:id` | `ServerGroupDetail.svelte` |
| `/admin/bare-metal-services` | `BareMetalServices.svelte` → `ServicesList` mode `bare_metal` |
| `/admin/bare-metal-services/:id` | `BareMetalServiceDetail.svelte` |
| `/admin/vm-services` | `ProxmoxServices.svelte` → `ServicesList` mode VM (flag) |
| `/admin/vm-services/:id` | `VMServiceDetail.svelte` |
| `/admin/services-list` | `UnifiedServices.svelte` → `ServicesList` mode `unified` |
| `/admin/plugins` | `Plugins.svelte` |
| `/admin/os-templates` | `OSTemplates.svelte` |
| `/admin/scripts` | `Scripts.svelte` |
| `/admin/asset-manager` | `AssetManager.svelte` |
| `/admin/product-catalog` | `ProductCatalog.svelte` (VM families/products only) |
| `/admin/vm-templates` | `VMTemplates.svelte` |
| `/admin/vm-ip-allocations` | `VMIpAllocations.svelte` |
| `/admin/proxmox-inventory` | `ProxmoxInventory.svelte` |
| `/admin/proxy-catalog` | `ProxyCatalog.svelte` (http_proxy families/products + subnet groups) |
| `/admin/proxy-ipam` | `ProxyIpam.svelte` (IPAM proxy flag) |
| `/admin/proxy-runners` | `ProxyRunners.svelte` (standalone proxy runners) |
| `/admin/billing-integrations` | `BillingIntegrations.svelte` |
| `/admin/user` | `User.svelte` |
| `/login` | `Login.svelte` |

Shared chrome: `Sidebar.svelte`, `PageHeader.svelte`, `ServerControlsPanel.svelte`, `CodeEditor.svelte`.

### UI kit — `frontend/src/components/ui/`

`Button`, `Modal`, `Alert`, `Spinner`, `FormGroup`, `FormError`, `CapabilityCard`, `StateAndActionsCard`, `TrafficGraph`, barrel `index.js`.

### Adding a new admin page

1. Create `frontend/src/components/MyPage.svelte`.
2. Import + `{#if routeName === 'my-page'}` branch in `Admin.svelte`.
3. Add `<a href="/admin/my-page">` in `Sidebar.svelte`.
4. Add API helpers in `lib/api.js` if needed.
5. Implement backend in `app/api/` + DAO/model as required.

---

## Runners & sidecars

| Component | Path | Notes |
|---|---|---|
| DHCP runner | `dhcp_runner/main.py` | FastAPI control plane for `dhcpd`; shared volume for conf/leases |
| TFTP runner | `tftp_runner/main.py` | Control plane for `in.tftpd`; shared TFTP root |
| Proxy runner | `proxy_runner/cmd/proxy-runner/` | Polls `/api/runner/proxy/config` every 30s (phone-home); register via Admin → Proxy Runners |
| Bandwidth poller | `scripts/snmp_bandwidth_poller.py` | Docker service `bandwidth-poller` / systemd unit |

App selects remote runners when `DHCP_RUNNER_URL` / `TFTP_RUNNER_URL` (or legacy `DHCP_TFTP_SERVICE_URL`) are set — see `app/core/config.py` and `INSTALL.md`.

---

## OS templates, ISOs, PXE

| Path | Role |
|---|---|
| `os_templates/README.md` | Template schema & how to add templates |
| `os_templates/<id>/template.json` | Metadata, parameters, disk image path |
| `os_templates/<id>/install.sh` | Install script (variable substitution) |
| `disk_images/` | Large disk images referenced by templates |
| `isos/` | ISO files served via interaction API |
| `tftp/pxe/` | PXE-related files (incl. temp OS) |

Serving path for bare metal: `app/api/server_interaction.py` + `app/services/os_template_service.py` / `temp_os_service.py`.

---

## WHMCS

| Path | Role |
|---|---|
| `whmcs/modules/servers/rackflow/rackflow.php` | Provisioning module (Create/Suspend/Power/Register, etc.) |
| `whmcs/modules/servers/rackflow/whmcs.json` | Module metadata |
| `whmcs/includes/hooks/` | WHMCS hooks |
| Backend counterpart | `app/api/billing.py` + `app/integrations/whmcs.py` |

---

## Tests

| Path | Covers |
|---|---|
| `tests/api/` | HTTP/API behavior (servers, PXE, hardware detection, services, assets, …) |
| `tests/dao/` | DAO unit tests |
| `tests/services/` | Service logic |
| `tests/plugins/` | IPMI, SNMPv3, … |
| `tests/core/` | Auth, billing auth, Redis notifications |
| `tests/models/`, `tests/utils/`, `tests/unit/` | Models, utils, placement, etc. |
| `tests/SECURITY_TESTS.md` | Security test notes |
| `tests/fixtures/` | Shared fixtures |

Run: `pytest` (see `pytest.ini`).

---

## Scripts & ops

| Path | Role |
|---|---|
| `scripts/dev.sh` | Local dev helper |
| `scripts/create_admin_user.py` | Create admin user |
| `scripts/snmp_bandwidth_poller.py` | Bandwidth sampler |
| `scripts/finalize-pxe-bootorder.sh` | PXE boot-order finalize |
| `hash_password.py` | Password hash utility |
| `systemd/dcim-snmp-bandwidth-poller.service` | Host systemd unit |
| `Makefile` | `frontend`, `docker-app`, runners, `build`, `clean` |
| `.forgejo/workflows/` | CI: `container.yml`, `sonarqube.yml` |

---

## Common change recipes

### New API endpoint for an existing resource
1. Handler in the matching `app/api/*.py`.
2. DAO/service as needed.
3. Export client function in `frontend/src/lib/api.js`.
4. Call from the Svelte component.
5. Add test under `tests/api/`.

### New DB column/table
1. Update `app/models/...`.
2. `alembic revision` → `alembic/versions/...`.
3. Update DAO + API schemas/responses.
4. Update UI if user-facing.

### New OS template
1. `os_templates/my-template/{template.json,install.sh}`.
2. Place disk image under `disk_images/` if needed.
3. Reload via API/UI (`os_templates` endpoints / `OSTemplates.svelte`).

### New server plugin capability
1. Implement/adjust in `app/plugins/<plugin>.py` (`CAPABILITIES`).
2. Capability UI often driven by `CapabilityCard` / `ServerDetail.svelte`.
3. Server enablement via `server_capability` model + `/api/servers/{id}/capabilities`.

### New billing action for WHMCS
1. Endpoint in `app/api/billing.py` (auth via billing API key).
2. Call site in `whmcs/.../rackflow.php`.
3. Cover with API tests where practical.

---

## Security & hardening (must-know)

Non-VM security controls that are now enforced (see `tests/SECURITY_TESTS.md`):

| Area | Rule | Where |
|---|---|---|
| Secret-bearing scripts/media | Require a valid **download token** bound to the boot task (no optional/no-token fallback). Tokens are scoped to concrete file patterns and single-use media tokens are consumed atomically. | `app/api/server_interaction.py`, `app/api/installation_tasks.py`, `app/services/download_token_service.py` |
| Temp-OS / TFTP paths | Path-traversal jail via `resolve()` + `is_relative_to()` | `app/services/temp_os_service.py`, `tftp_runner/main.py` |
| Cloud-init identity | Trust `X-Forwarded-For` only when `TRUST_X_FORWARDED_FOR=true`; context limited to active installs | `app/api/server_interaction.py`, `app/core/config.py` |
| DHCP/TFTP runners | **Fail-closed**: `503` on protected routes unless `API_KEY` set (`DHCP_RUNNER_API_KEY` / `TFTP_RUNNER_API_KEY`) | `dhcp_runner/main.py`, `tftp_runner/main.py`, compose |
| Service-instance keys | Fernet-encrypted at rest when `SERVICE_INSTANCE_ENCRYPTION_KEY` set; legacy plaintext re-encrypted on verify | `app/dao/service_instance_dao.py`, `app/core/service_instance_crypto.py` |
| Billing integration keys | Stored as SHA-256 hash; plaintext revealed once on create/rotate, masked elsewhere | `app/core/billing_auth.py`, `app/api/billing_admin.py` |
| Billing power control | `on`/`reboot`/`reset` blocked for `SUSPENDED`/`TERMINATED` | `app/api/billing.py` |
| WHMCS module | TLS verification on (`CURLOPT_SSL_VERIFYPEER`/`VERIFYHOST`) | `whmcs/modules/servers/rackflow/rackflow.php` |
| Validation errors | 422 responses/logs redact request bodies and sensitive fields | `app/main.py` |
| Assets | Auth required; SVG upload disallowed; non-raster served as attachment + `nosniff` + CSP | `app/api/asset.py` |
| IPMI | Password via `IPMI_PASSWORD` env (`-E`), never argv | `app/plugins/ipmi.py` |
| Frontend auth | Role-based routing (admin→`/admin`, else `/client`); global fetch 401 → clear auth + `/login`; secret template params masked | `frontend/src/lib/api.js`, `frontend/src/routes/`, `frontend/src/components/ServerDetail.svelte` |

Relevant settings live in `app/core/config.py`: `TRUST_X_FORWARDED_FOR`, `SERVICE_INSTANCE_ENCRYPTION_KEY`, `REQUIRE_SERVICE_INSTANCE_ENCRYPTION`. Runner keys and encryption keys are wired through `docker-compose.yml`; see `INSTALL.md`.

---

## Domain glossary

| Term | Meaning in this repo |
|---|---|
| **Server** | Bare-metal (or managed) host with a management plugin (IPMI/Proxmox) |
| **Service** | Customer-facing instance (bare-metal or VM), often linked from WHMCS |
| **Service instance** | Per-location DHCP/TFTP **runner** registration (not a customer service) |
| **Boot task** | One-shot PXE/boot instruction for a server |
| **Installation task** | Long-running OS install progress/log |
| **Product / family** | Sellable catalog SKUs; drive VM/bare-metal defaults |
| **VM IP allocation** | Pool of IPs assignable to VM services |
| **Asset** | Uploaded/managed file or label attachment |

---

## Docs already in-repo

- `INSTALL.md` — install, Docker, initial admin, DHCP/TFTP runners
- `os_templates/README.md` — template format
- `isos/README.md` — ISO notes
- `tests/SECURITY_TESTS.md` — security test guidance
- **This file (`CODEBASE.md`)** — navigation map for agents and humans

When unsure, open the UI page → find the component in the Admin map → follow its `api.js` calls → land in `app/api/` → DAO/service/plugin.
