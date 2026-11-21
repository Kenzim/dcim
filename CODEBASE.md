# Rackflow / DCIM — Codebase Map

**Read this file first in every new chat.** Use it to jump straight to the right layer and files. Prefer editing the mapped paths below over broad repo searches.

Product name: **Rackflow** (repo folder: `dcim`). Stack: **FastAPI + SQLAlchemy + MySQL + Redis** backend, **Svelte 4 + Vite + Bootstrap** SPA frontend, optional **Docker runners** (DHCP/TFTP/media/proxy), **WHMCS** PHP module, **Alembic** migrations.

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
| Native HTML5 KVM (AMI MegaRAC / SuperMicro ATEN HTML5 / SuperMicro X9 HERMON) | `app/services/ipmi_kvm/` (`asrockrack.py`, `gigabyte.py`, `supermicro.py`, `supermicro_x9.py`) + `app/api/ipmi_kvm.py` |
| Bare-metal Serial-over-LAN | `app/services/sol/` (`ipmi_sol.py` + hub/tickets) + `app/api/sol.py` |
| BMC virtual CD (ISO mount) | `app/services/virtual_media/` + `app/api/virtual_media.py` |
| Switch SNMP / bandwidth | `app/plugins/snmpv3.py`, poller `scripts/snmp_bandwidth_poller.py` |
| Cabling map (servers, switches, patch panels, cable runs as a graph) | API `app/api/cabling.py` → `app/services/cabling_graph_service.py` → `app/dao/cabling_graph_dao.py`; UI `Cabling.svelte` (Cytoscape.js lazy-loaded, ELK layout in a web worker via `frontend/src/lib/elkLayout.js`) and the port trace strip in `ServerDetail.svelte` |
| Patch panels and multi-hop cable paths | `app/models/patch_panel.py`, API `app/api/patch_panel.py` → `app/services/patch_panel_service.py` → `app/dao/patch_panel_dao.py`; path tracing through panels in `app/services/cable_trace_service.py`; UI `PatchPanels.svelte`, `PatchPanelDetail.svelte` |
| LLDP neighbours and the recorded-versus-seen check | Collection `app/plugins/snmpv3.py` (`get_lldp_neighbors`) → `app/services/lldp_collection_service.py` → `switch_lldp_neighbors` table; comparison `app/services/lldp_compare_service.py`; both exposed under `/api/admin/cabling/lldp` |
| PDUs, rack power feeds, server PSUs | Plugins `app/plugins/pdu/`, logic `app/services/power_service.py`, API `app/api/pdu.py` / `power_feed.py` / `server_power.py`, MCP `app/mcp/tools/pdus.py`; power history (graphs, kWh, retention) `app/services/power_history_service.py` |
| Client private networks (VLAN per network) | `app/services/private_network_service.py`, `app/api/private_networks.py`, uplink planning and vPC pairs `app/services/uplink_vlan_service.py`, switch drivers `app/plugins/nxos.py` and `app/plugins/qnap_qss.py`; setup and limits in `docs/private-networks.md`, Nexus setup in `docs/nxos-vlan-management.md` |
| Cisco vPC pair detection | `app/services/switch_vpc_service.py` (CISCO-VPC-MIB via `snmpv3.get_vpc_info`, pairs by peer-keepalive addresses) → `network_switches.vpc_*`; `POST /api/admin/network-switches/vpc/detect`, MCP `switch_action` `detect_vpc` |
| Auth / sessions / Redis | `app/core/auth.py`, `app/core/redis.py`, `app/core/billing_auth.py`, `app/core/mcp_auth.py` |
| Config / env vars | `app/core/config.py` + `.env` |
| Settings an operator can change at runtime (billing and automation) | Typed schema, env defaults and `effective_config(db)` in `app/services/settings_store.py` (rows `setting:<key>` in `system_settings`); admin API `app/api/settings_admin.py`; UI `AutomationSettings.svelte`. To add a setting: a `SettingDef` in `SCHEMA` (its default is the `Settings` attribute of the same name), then read it with `get_setting(db, key)` or `effective_config(db)` instead of `settings`. Process switches such as `run_recurring_billing_worker` stay env-only |
| Admin MCP (remote AI tools) | `app/mcp/` tools/resources; keys API `app/api/mcp_keys_admin.py`; UI `McpKeys.svelte` |
| Client MCP (customers' own assistants) | `app/mcp/client/` (tools call client-portal handlers), `app/core/client_mcp_auth.py`, keys API `app/api/client_mcp_keys.py`; UI `ClientMcpKeys.svelte` |
| OS install templates | `os_templates/<name>/` (`template.json` + `install.sh`); bare-metal WHMCS OS list is the server group’s permitted templates |
| PXE / cloud-init / boot scripts served to bare metal | `app/api/server_interaction.py` |
| Billing / WHMCS API surface | Backend: `app/api/billing.py`; WHMCS module: `whmcs/modules/servers/rackflow/` |
| Invoice settlement (an invoice becomes paid) and what happens after | `app/services/billing/settlement.py` (`settle_invoice`; the billing event registry `subscribe(event, ...)`/`fire(db, event, payload)` and the internal payload dataclasses; `subscribe_invoice_paid`/`fire_invoice_paid` are the `invoice.paid` forms); default subscribers in `app/services/billing/settlement_subscribers.py`, including one `catalogue:<event>` publisher per phase 1 billing event (the only emission site of each); spec section 14 of `docs/billing-target-spec.md` |
| Billing phase 1 (invoice model v2, credit and debit notes, refunds, A3 accounts, shadow engines) | Design `docs/billing-phase1-design.md`. Models `app/models/billing_series.py` (`DocumentSeries`), `invoice_adjustment.py` (`InvoiceAdjustment`, `InvoiceAdjustmentLine`), `refund.py` (`Refund`), `billing_shadow.py` (`BillingShadowMismatch`); v2 columns on `Invoice`, `Payment`, `CreditLedgerEntry` (`reseller.py`), `InvoiceLine` (`commerce_invoice_ext.py`), `PricePlanCycle` (`storefront.py`). Services: `app/services/invoice_numbering.py` (`InvoiceNumbering.allocate`), `invoice_service.py` (`balance()`, `public_invoice_status()`, v2 transition stubs), `invoice_adjustment_service.py` and `refund_service.py` (stubs), `credit_ledger_service.py` (`*_account` stubs), `billing_shadow.py` (mismatch recording, scan results, `billing_engine_mode`), `billing_backfill.py` (batched backfills, also `python -m app.services.billing_backfill <name>`), `billing/cycles.py` (`resolve_cycle`), `billing/periodic.py` (shadow scan and refund reconciler jobs, off by default, run by the recurring billing worker), `billing_locks.py` (`LOCK_ORDER`). Settings in the `billing_engines` group of `settings_store.py`. Revisions `alembic/versions/billing_p1_*.py`; tests `tests/migrations/`, `tests/services/test_billing_p1_foundations.py`, `tests/contract/test_public_invoice_status.py` |
| Retail card payment from the client area | `app/services/payments/retail_card.py` (pending `Payment` per PaymentIntent, reused on retry), called by `POST /api/client/commerce/invoices/{id}/pay` |
| Admin mark-paid, order cancel, refund policy | `app/services/billing/manual_payments.py`; `app/services/billing/order_refunds.py` (`billing_order_refund_policy`; every policy is a recorded manual review until phase 1) |
| Renewal intervals, retail renewals, end-of-period cancel | `app/services/billing/intervals.py` (all six labels; unknown labels raise); `app/services/billing/cycles.py` (`resolve_cycle` by label or unit and count, `sellable_cycles`, `cycle_length_fields`; cycles over 12 months gated by `billing_long_cycles_enabled`); `app/services/billing/renewals.py` (`AccountFunder`, retail cycle steps, gated by `billing_retail_renewals_enabled`); worker logic in `app/services/recurring_billing_service.py` |
| Coupon limits and usage counting | `app/services/billing/coupons.py` (checked at quote, counted at settlement) |
| Credit and debit notes (A2) | `app/services/invoice_adjustment_service.py` (only writer of `invoice_adjustments`: admin credit and debit notes, refund credit notes, overpayment debit notes; `v2` invoice engine only); `app/services/adjustment_pdf_service.py` (PDF from the note's snapshots); `app/api/billing_adjustments.py` (admin `/api/admin/billing/...`, client `/api/client/commerce/adjustments`, reseller `/api/reseller-panel/adjustments`); detector `adjustment_bounds` in `app/services/alerting/billing_integrity.py`; tests `tests/services/test_billing_p1n_adjustments.py`, `tests/api/test_billing_p1n_adjustments_api.py` |
| DHCP/TFTP/media runner containers | `dhcp_runner/main.py`, `tftp_runner/main.py`, `media_runner/main.py`, uplink in `runner_common/`, hub in `app/services/runners/` |
| Proxy runner (IPAM sync) | Go runner `proxy_runner/` (see its README), API `app/api/runner_proxy.py` |
| Private network sharing (owner invites other clients; members attach their own servers) | Design `docs/private-network-sharing.md`; models `PrivateNetworkMember` and `PrivateNetworkInvitation` in `app/models/private_network.py`; service `app/services/private_network_sharing.py` (invite, respond, remove member, end sharing, repair); client API `app/api/private_network_sharing.py` (owner invitation and member routes, member routes under `/shared`); the service checks the service owner (`acting_user_id`) and the API checks owner-or-member; MCP `services view=networks` and `service_action action=network` in `app/mcp/client/networks.py`, staff `lookup kind=private_networks` and `switch_action action=network_member_remove`; notification category `network_sharing` |
| Proxy fast path (clients reach proxies over a private VLAN) | Design `docs/proxy-fast-path.md`; service `app/services/proxy_fast_path.py` (subnet pinning, `ProxyAccessService`, runner `fast_path` config, client overview); access networks are `PrivateNetwork.kind = proxy_access` in `private_network_service.py`; runner side `proxy_runner/internal/fastpath/` (namespace per client) |
| NFS storage on private networks (`nfs_storage` services, storage nodes, shares) | Models `app/models/storage.py`, DAO `app/dao/storage_dao.py`, service `app/services/storage_service.py`, provisioning `app/services/provisioning/nfs_storage.py`; API `app/api/storage.py` (staff), `app/api/storage_client.py` (client), `app/api/storage_common.py` (shared shapes), runner API `app/api/runner_storage.py`; MCP `app/mcp/tools/storage.py` and the client `storage` tool; UI `StorageNodes.svelte`, `client/ClientStorage.svelte`, `components/storage/` |
| Storage failover (three active nodes, Ceph leases, SFTP master on a virtual IP) | Design and contract `docs/nfs-storage-failover.md`; app side `app/services/storage_ha.py` (takeover order, moves, drain, rebalance, return to preferred node, HA config and status), `app/services/storage_location.py` (per-location SFTP host, virtual IP with IPAM reservation, sealed shared host key); runner side `storage_runner/internal/lease/` |
| Storage runner (per-customer NFS on CephFS) | `storage_runner/internal/reconcile/reconcile.go` (loop), failover wiring in `storage_runner/internal/reconcile/ha.go` and `sftp_master.go`, Ceph leases, fencing and the guard in `storage_runner/internal/lease/` (with the embedded `rados_helper.py`), SFTP virtual IP in `storage_runner/internal/vip/`, drivers in `storage_runner/internal/{netns,firewall,ceph,ganesha}/`, public SFTP gateway and per-tenant back ends in `storage_runner/internal/sftp/` |
| Selling storage (WHMCS, store) | `docs/nfs-storage-billing.md`; WHMCS `rackflow_storageClientArea` and `clientarea_storage.tpl`, Module Settings storage panel in `hooks.php` (`storagePanelHtml`) |
| Backup slots (`backup_slot` services holding one retained VM backup) | Models `app/models/backup_slot.py`, DAO `app/dao/backup_slot_dao.py`, services `app/services/backup_slot_service.py` (retain, release, resize, grace-period purge) and `app/services/retained_restore_service.py` (restore onto another VM), provisioning `app/services/provisioning/backup_slot.py`; API `app/api/backup_slots.py` (client and staff) plus `/api/billing/services/{id}/backup-slot*` in `billing.py`; restore job `RestoreRetainedBackupStrategy` in `app/services/deployment/strategies.py`; MCP on `restore_backup`/`service_info`/`provision` (staff) and `services`/`service_action` (client); WHMCS functions `rackflow_slot*` and `slot_action.php`; UI `client/ClientBackupSlots.svelte`, `client/BackupsPanel.svelte`; design and set-up in `docs/backup-slots.md` |
| Alerts and notifications (staff alerts, client opt-in notices) | Design `docs/alerting.md`. Models `app/models/alerting.py`; catalogue, defaults and settings `app/services/alerting/catalogue.py`; detectors (read recorded state) `alerting/detectors.py`; lifecycle (open, hold-down, resolve, flapping, silence) `alerting/engine.py`; fan-out `alerting/dispatch.py`; delivery outbox sender `alerting/sender.py`; rendering `alerting/render.py`; webhook HTTP with address checks `alerting/safe_http.py`; staff operations `alerting/admin.py`; retention `alerting/retention.py`; VM backup outcomes `alerting/backup_watch.py`; billing integrity detectors (invoice lines, paid invoices without payments, overallocation, credit ledger drift, orphaned payments, stuck refunds; five ship disabled via `AlertTypeSpec.enabled_by_default`; plus nine billing phase 1 detectors, stubs shipped disabled until their phase 1 owners fill them in) `alerting/billing_integrity.py`. Worker `app/workers/alerts.py` (in the API process, `RUN_ALERT_WORKER`). Staff API `app/api/alerts.py`, MCP tool `alerts` (`app/mcp/tools/alerts.py`), UI `Alerts.svelte` plus the count in `Sidebar.svelte`. Client side: `app/services/client_notifications.py` (hooks from `AuditService.log`, a `before_flush` listener on service status, SFTP login report, backup and install outcomes), API `app/api/client_notifications.py`, client MCP `account` with `view="notifications"`, UI `client/ClientNotifications.svelte`. To add an alert type: a `AlertTypeSpec` in the catalogue and a function in `DETECTORS` |
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
Runners (dhcp_runner/, tftp_runner/, media_runner/, proxy_runner/, storage_runner/)  ← optional sidecars
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
| `dhcp_runner/` | Container API that runs `dhcpd` + WebSocket uplink |
| `tftp_runner/` | Container API that runs `in.tftpd` + WebSocket uplink |
| `media_runner/` | Per-location ISO library (HTTP range + SMB) + WebSocket uplink |
| `runner_common/` | Shared protocol/agent used by Python runners |
| `proxy_runner/` | Go HTTP/SOCKS5 proxy runner; syncs proxy/IPAM config from Rackflow and, with `FASTPATH_PARENT_IFACE`, serves the proxy fast path in one network namespace per client |
| `storage_runner/` | Go agent on storage nodes: one isolated NFS-Ganesha per customer (own netns + VLAN) on erasure-coded CephFS |
| `os_templates/` | Disk-based OS install templates |
| `isos/`, `disk_images/`, `tftp/` | Central boot media (ISO catalog fallback); per-location libraries live on media runners |
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
| `app/core/auth.py` | Session tokens in Redis, admin/user deps (`rfmcp_` / `rsk_` rejected here) |
| `app/core/billing_auth.py` | API-key auth for billing integrations |
| `app/core/mcp_auth.py` | Dedicated `rfmcp_` Bearer keys for `/mcp` (scopes, CIDR, rate limit) |
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
| `service_instance.py` | `/api/service-instances` | Legacy DHCP/TFTP HTTP runner registration per location |
| `runners_admin.py` | `/api/admin/runners` | Unified runner enroll / rotate key / ISO library |
| `runner_ws.py` | `WS /api/runner/ws` | Runner-initiated uplink |
| `proxy_runners.py` | `/api/admin/proxy-runners` | Standalone proxy runners (generated key, phone-home health, fast path location/server/VLAN mode and hosted subnets) |
| `proxy_subnet_groups.py` | `/api/admin/proxy-subnet-groups` | Named IPAM subnet pools for proxy catalog auto-assign |
| `rack.py` | `/api/racks` | Racks + servers-in-rack |
| `server.py` | `/api/servers` | Servers CRUD, power, boot, capabilities, hardware detection, activity, bandwidth |
| `server_interaction.py` | `/api/servers/interaction` | **PXE scripts, cloud-init, ISOs, temp-OS, kernels, download tokens** (bare-metal install path) |
| `installation_tasks.py` | `/api/servers/{id}/installation-tasks` | Install task history/logs |
| `server_group.py` | `/api/server-groups` | Server groups |
| `network_switch.py` | `/api/network-switches` | Switches, ports, bandwidth |
| `cable_run.py` | `/api/cable-runs` | Cable runs / cabling. Each end is a server, switch or patch panel port (`{type, id, side}`; `side` is `front` or `rear` for patch ports). `GET /trace?type=&id=&side=` follows a port's path through patch panels |
| `patch_panel.py` | `/api/patch-panels` | Patch panel CRUD (ports created with the panel), `PUT /{id}/ports/{number}` to label a port; delete refuses panels with cables unless `?force=true`. Rack units are checked against servers, switches and other panels |
| `cabling.py` | `/api/admin/cabling` | Admin only, never returns `plugin_config` or BMC details, fixed number of queries. `GET /graph?location_id=&rack_id=`: devices (servers, switches, patch panels), racks, ports, cables (`edges`) and end-to-end `paths` through patch panels; devices outside the filter reached by a path come back with `in_scope=false`. `GET /lldp?location_id=&rack_id=`: per switch port, recorded cable versus LLDP neighbour (`match`, `mismatch`, `missing_cable`, `unknown_neighbour`, `no_lldp`, `unmapped_local_port`). `POST /lldp/switches/{id}/refresh` collects LLDP from one switch now |
| `cable_run.py` | `/api/cable-runs` | Cable runs / cabling |
| `pdu.py` | `/api/pdus` | PDUs, inlets, outlets, poll, outlet on/off/cycle (guarded) |
| `power_history.py` | `/api/pdus/{id}/history`, `/api/pdus/{id}/inlets/{inlet}/history`, `/api/power-feeds/{id}/history`, `/api/racks/{id}/power-history`, `/api/servers/{id}/power-history`, `/api/power-history[?location_id=]` | Admin power history: `?hours=` 1 to 9600, raw up to 24 h, 5-minute rollups up to 35 days, hourly beyond |
| `power_feed.py` | `/api/power-feeds` | Rack power feeds with live usage |
| `server_power.py` | `/api/servers/{id}/power-inputs` | Server PSUs, outlet links, draw, outlet action |
| `os_templates.py` | `/api/os-templates` | Scan/list OS templates from disk |
| `billing.py` | `/api/billing` | External billing API (register/suspend/power/reinstall/…) |
| `billing_admin.py` | (billing-admin tags) | Admin management of billing integrations |
| `alerts.py` | `/api/admin/alerts` | Staff alert list, acknowledge, silence, alert types and thresholds, channels, recipients, deliveries |
| `client_notifications.py` | `/api/client/notifications` | Client opt-in notification preferences, webhook and test |
| `mcp_keys_admin.py` | `/api/admin/mcp-keys` | Admin CRUD for MCP Streamable HTTP keys (`rfmcp_`) |
| `commerce_store_admin.py` | `/api/admin/store` | Frontend products, categories, plans, coupons, tax |
| `commerce_admin.py` | `/api/admin/commerce` | Orders, invoices/PDF, gateway logs, emails, webhooks, GDPR |
| `commerce_client.py` | `/api/client/commerce` | Client browse/checkout/orders/invoices/support/Discord/2FA |
| `commerce_support_admin.py` | `/api/admin/support` | Support ticket admin queue |
| `settings_admin.py` | `/api/admin/settings` | Admin only. `GET` lists the settings schema with value, default, source (`default` or `stored`), group, description, validation and `requires_restart`; `PUT /{key}` stores a validated value (422 with a message on a bad value); `DELETE /{key}` resets to the default |
| `commerce_auth_public.py` | `/api/commerce/auth` | Register, verify email, password reset |
| `product_catalog.py` | `/api/product-catalog` | Families, products, VM templates, OS profiles, VM config |
| `proxmox_inventory.py` | `/api/proxmox` | Clusters, sync, inventory, VM plan |
| `private_networks.py` | `/api/admin/private-networks`, `/api/client/private-networks` | Private network pools, networks, attachments; client `/fast-path` (proxy fast path overview, on, off); admin `DELETE /{id}/members/{user_id}` |
| `private_network_sharing.py` | `/api/client/private-networks` (mounted before `private_networks.py`) | Invitations (`/invitations`), owner invite, cancel and remove member, member routes under `/shared` (view, attach, detach, storage, leave) |
| `ipam.py` | `/api/ipam` | Subnets, assignments, history |
| `vm_ip_allocations.py` | `/api/vm-ip-allocations` | Preallocated VM IPs |
| `runner_proxy.py` | `/api/runner/proxy` | Config for proxy runner (`fast_path` block only for runners with a location whose pool offers the fast path) |
| `storage.py` | `/api/admin/storage` | Storage nodes (key shown once, VLAN mode and ports, switch port preview, drain and undrain), nfs_storage services with shares and planned moves, storage locations (SFTP host, virtual IP, host key, HA settings, rebalance) |
| `storage_client.py` | `/api/client/storage` | Client's own storage: link to a private network (or create one on link), share CRUD with SFTP access mode, SFTP users (`/sftp-users`; permission `storage.manage_shares`) |
| `backup_slots.py` | `/api/client/backup-slots`, `/api/admin/backup-slots` | Backup slots: client list and detail, retain, release, restore onto a VM (confirm_data_loss); staff create, resize, release |
| `runner_storage.py` | `/api/runner/storage` | Storage runner: `GET /config` (every tenant of the location when HA is active, otherwise only the node's own; runners older than 0.3.0 get the old shape), `POST /status` (serving flags and failover events); bearer node key, failed keys rate-limited per IP |
| `services_admin.py` | `/api/admin/services` | Admin service lifecycle (VM/bare-metal provision, ownership) |
| `services_client.py` | `/api/services` | Client “my services” |
| `scripts_admin.py` | (scripts-admin) | Admin scripts CRUD |
| `asset.py` | `/api/assets` | Asset manager / files / labels |
| `utils.py` | `/api/utils` | e.g. password generation |
| `vm_vnc.py` | `/api/vnc` | Public VM console redeem + WS bridge to Proxmox |
| `ipmi_kvm.py` | `/api/kvm`, `/api/ipmi-kvm` | HTML5 KVM redeem/asset/WS bridge + admin profile list |
| `sol.py` | `/api/sol` | SOL profile list, redeem, WS bridge; send lives on server/service/billing routes |
| `virtual_media.py` | `/api/virtual-media` | Profile list, BMC ISO fetch (path token + Range), admin server mount/eject |
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
| Networking gear | `network_switch.py`, `switch_port.py`, `cable_run.py`, `patch_panel.py`, `switch_lldp_neighbor.py`, `switch_bandwidth_sample.py` |
| Networking gear | `network_switch.py`, `switch_port.py`, `cable_run.py`, `switch_bandwidth_sample.py` |
| Power | `power_feed.py` (rack feeds), `pdu.py` (`Pdu`, `PduInlet`, `PduOutlet`), `server_power_input.py` (PSUs; new servers get `PSU1`) |
| Power history | `pdu_sample.py`: raw sample and rollup tables for PDU, inlet, bank and outlet (`SAMPLE_KINDS` describes them); DAO `pdu_sample_dao.py` |
| Boot / install | `boot_task.py`, `installation_task.py`, `server_capability.py`, `hardware_detection_report.py`, `server_activity.py` |
| DHCP/TFTP | `dhcp_config.py`, `tftp_config.py`, `service_instance.py` |
| Catalog / services | `product_catalog.py`, `service.py`, `service_bare_metal.py`, `service_vm.py`, `vm_config.py`, `script.py` |
| Proxmox / IPAM | `proxmox_inventory.py`, `ipam.py`, `vm_ip_allocation.py`, `vmid_reservation.py`, `proxy_runner.py`, `proxy_subnet_group.py`, `private_network.py` |
| Billing / assets | `billing_integration.py`, `mcp_api_key.py`, `asset.py` |
| Retail commerce | `commerce_account.py`, `commerce_order.py`, `storefront.py`, `commerce_invoice_ext.py`, `commerce_coupon_tax.py`, `commerce_email.py`, `commerce_audit.py`, `commerce_gateway_log.py`, `commerce_auth_extra.py`, `commerce_webhook.py`, `support_ticket.py` |
| Billing documents (phase 1) | `billing_series.py` (numbering series), `invoice_adjustment.py` (credit and debit notes), `refund.py`, `billing_shadow.py` (shadow mismatches) |
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
| `power_service.py` | PDU inlet/outlet generation (`sync_pdu_ports`), readings (`poll_pdu`, `apply_readings`, which also writes the history sample), feed/rack/server aggregation, outlet-control guards |
| `power_history_service.py` | PDU power history: `record_samples`, `rollup` (5 min and hourly, re-runnable), `prune`, `run_maintenance` (poller), and the per-scope views (`pdu_history`, `feed_history`, `rack_history`, `location_history`, `server_history`) with kWh from the energy counter or by integrating power |
| `vm_provisioning_service.py` / `vm_strategy_executor.py` / `vm_os_strategy.py` / `vm_install_type_strategy.py` | VM provision strategies |
| `provisioning/` | Unified service create (`ProvisioningService.create` + `ProvisionRequest`); used by billing, admin, MCP, reseller, and commerce checkout |
| `checkout_service.py` | Retail checkout quote/place + fulfillment via `ProvisioningService` |
| `proxmox_placement.py` / `vmid_allocator.py` / `ip_allocation.py` | Placement, VMID, IP assignment |
| `service_resource.py` / `service_product_snapshot.py` | Service ↔ product resource helpers |
| `reconciliation_jobs.py` | Background reconciliation loop (started in lifespan) |
| `private_network_service.py` | Private networks: VLAN allocation, VM NIC and switch port attach/detach, terminate cleanup, repair. The last usable address of every network is reserved for storage (`storage_endpoint_ip`); `attach_storage` is the only way it is handed out. Storage attachments trunk the node's `vlan_port_ids` (or the PXE uplink), or make no switch calls in `preconfigured` VLAN mode. Proxy access networks (`kind = proxy_access`) always use `settings.proxy_access_cidr` (100.80.0.0/16, refused for normal networks), keep its first /20 for proxy subnet IPs, have no storage address, and also hold proxy runner attachments (`proxy_runner_id`, no service) |
| `private_network_sharing.py` | Private network sharing: invite by email or username (same reseller scope), accept and decline, remove member and leave (detach the member's attachments first), end sharing on delete, `reconcile_sharing` repair (expiry, ineligible members, owner gone 30 days), fixed-sentence `network_sharing` notices. Roles and rules in `docs/private-network-sharing.md` |
| `proxy_fast_path.py` | Proxy fast path: `pin_subnet` (subnet to runner, allocates the subnet's private IP once), `ProxyAccessService` (client on/off per location, runners join and leave, `runner_config` builds per-client tenants, `overview` for clients), `eligibility_problem` (free for clients with an active server and fast-path proxy in the location) |
| `storage_service.py` | NFS storage: node CRUD (including the public SFTP endpoint), placement (least-loaded enabled node in the location), location chosen at order or at link (a network is created on link when none is given), share validation and quotas, terminate and share delete with `storage_destroy_grace_seconds`, combined readiness (attachment applied and runner ready), runner config and status. Failover logic lives in `storage_ha.py` and `storage_location.py` (see `docs/nfs-storage-failover.md`) |
| `storage_sftp.py` | Storage SFTP: users (bcrypt password hashes and public keys only), key and CIDR validation, logins `<name>.<service_id>`, client/staff views, the runner's `sftp`/`sftp_users` config and status. Design: `docs/nfs-storage-sftp.md`; selling and WHMCS setup: `docs/nfs-storage-billing.md` |
| `backup_slot_service.py` / `retained_restore_service.py` | Backup slots: one retained backup per slot, set under Proxmox's `protected` flag (no copy); retain, release, resize, terminate with `backup_slot_destroy_grace_seconds` then purge from `reconciliation_jobs.py`. Restore onto another VM checks owner, status, cluster, backup storage on the target's node and disk size, then queues `restore_retained_backup`, or `restore_retained_backup_macos` for macOS (steps in `deployment/macos_restore.py`: target's SMBIOS and args kept, public NIC link down until the guest agent has applied the target's address, a fresh SMBIOS identity if the target had none, `retry_restore_configuration` action to retry; the strategies' `on_job_failed` hook records any terminal job failure as a failed restore; `power_block_reason` stops the power routes starting a VM before its own identity is back) |
| `plugin_sync.py` | Plugin DB sync (mostly no-op; plugins from disk) |
| `cabling_graph_service.py` | Builds the cabling graph from batch queries in `app/dao/cabling_graph_dao.py`: nodes, ports, cables and end-to-end paths through patch panels. Link status comes from the switch end's stored `oper_status` and counts only while fresh (`STATUS_STALE_AFTER`, 15 minutes); older data is `stale`, never polled is `unknown` |
| `cable_trace_service.py` | Follows a port through patch panels (front to rear) to the server or switch port at the far end; shared by the cable-run API, server and switch port views, bandwidth views, MCP and the graph |
| `patch_panel_service.py` | Patch panel validation (rack placement, port count changes) and per-port front/rear views |
| `uplink_vlan_service.py` | Turns a server's uplink into targets (port, port-channel, or vPC port-channel on both peers), refuses peer-links, inter-switch links and unpaired vPCs, applies/removes a VLAN with rollback, and records dry-run commands (`dry_run_uplink`, `GET /api/admin/network-switches/vlan-dry-run/{server_id}`) |
| `switch_vpc_service.py` | vPC pair detection and the stored pairing summary |
| `lldp_collection_service.py` / `lldp_compare_service.py` | Store an LLDP snapshot per switch (replaced on each collection, kept on error) / compare it with recorded cables |
| `vm_vnc_ticket_service.py` | One-time VM VNC launch tickets + WS sessions |
| `ipmi_kvm_ticket_service.py` | One-time IPMI HTML5 KVM launch tickets + WS sessions |
| `ipmi_kvm/` | Vendor KVM profiles (ASRockRack/Gigabyte AMI MegaRAC IVTP, SuperMicro ATEN InsydeVNC, SuperMicro X9 ATEN HERMON on TCP 5900). X9 JNLP GET from Docker needs host TCPMSS clamp when eth0 MTU < 1500. |
| `sol/` | Bare-metal SOL profiles (`ipmi_sol` via ipmitool), shared hub, REST send; `windows_password.py` resets the Windows Administrator password through the serial-admin-cmd.ps1 prompt (`POST .../windows-password-reset` on admin service, client and billing routes) |
| `virtual_media/` | BMC virtual CD (ASRockRack/Gigabyte MegaRAC Redfish, SuperMicro Redfish, SuperMicro X9 ATEN CIFS share), shared `isos/` catalog or per-location media runner; `megarac_stream` profile (`megarac_stream.py`) streams the ISO to ASRock/Gigabyte MegaRAC BMCs over the `cd-server` websocket via `ipmi_kvm/cd_stream.py` + `iusb.py` (no NFS/SMB share, boot ISO stamped in memory, stream records in Redis, resumed on app start) |

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
| `nxos.py` | Cisco NX-OS switch plugin: VLANs, port trunks and vPC peer-links over NX-API (`add`/`remove` only, VLANs 2 to 3967, never another VLAN's name), dry run; monitoring, LLDP and vPC discovery through SNMPv3 |
| `qnap_qss.py` | QNAP QSW switches (QSS firmware): tagged VLANs per port and the port list over the web UI's `/api/v3` JSON API; VLAN IDs 2 to 4000 |
| `switch_base.py` / `switch_registry.py` | Switch plugin ABC + registry |
| `pdu/` | PDU plugins: `base.py` (`PduPlugin`, capabilities metering / outlet_metering / outlet_control), `registry.py` (auto-discovers modules in `pdu/`), `snmp.py` (shared SNMP v1/v2c/v3 client), `manual.py` (unmanaged PDU) |

**Power history:** each successful poll adds one raw row per PDU, inlet, bank and (when `outlet_metering`) outlet. The poller (`run_power_history_cycle`, every `POWER_HISTORY_INTERVAL` s, default 300) rolls raw rows up into 300 s and 3600 s buckets (avg/min/max, energy counter first/last) and deletes rows past `POWER_HISTORY_RAW_RETENTION_DAYS` (7), `POWER_HISTORY_5M_RETENTION_DAYS` (35) and `POWER_HISTORY_1H_RETENTION_DAYS` (396); a tier is only pruned once the next tier has absorbed it (and never the rows of the newest bucket). Each rollup run re-rolls the newest bucket of each tier and only rolls buckets that ended over 2 minutes ago, so a late sample from a manual poll is still counted. Summed totals (rack, feed, location, overall, server) mark a bucket `partial` when fewer series than the scope has reported in it, and the response has `partial: true`; the graphs then show an incomplete-data note. Failed polls write nothing. A server's history comes from its linked metered outlets and says when it cannot be known. MCP: `history_hours` on `pdus`, `power_feeds` (list with `feed_id`), `rack_power_summary` and `server_power_summary` returns about 24 buckets per metric.

**Power model:** rack `PowerFeed` (entered by hand) ← `PduInlet` (1, or 2 on an ATS) of a `Pdu` → `PduOutlet` ← `ServerPowerInput` (a PSU). Latest readings are stored on the PDU, inlets and outlets by the bandwidth poller (`run_pdu_cycle`) or `POST /api/pdus/{id}/poll`. Outlet switching needs a plugin with `outlet_control`, the PDU's `outlet_control_enabled` opt-in (off by default), an unlocked outlet and `confirm=true`; actions are logged to the linked server's activity.

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
| `src/routes/index.js` | Top routes: `/`, `/admin`, `/admin/*`, `/client`, `/login`, `/vnc`, `/kvm`, `/sol` |
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
| `/admin/cabling` | `Cabling.svelte` (loaded with a dynamic import; `?location=`, `?rack=`, `?focus=server:<id>`, `switch:<id>` or `patch:<id>`, `?lldp=1`) |
| `/admin/patch-panels` | `PatchPanels.svelte` (dynamic import) |
| `/admin/patch-panels/:id` | `PatchPanelDetail.svelte` (dynamic import) |
| `/admin/pdus` | `Pdus.svelte` |
| `/admin/pdus/:id` | `PduDetail.svelte` (rack power: `RackPowerPanel.svelte` in `RackView`; server PSUs: `ServerPowerCard.svelte` in `ServerDetail`) |
| Power history graphs | `PowerHistoryPanel.svelte` (range selector, kWh, charts from a loader and a builder in `lib/powerCharts.js`) is used on `PduDetail`, in `RackPowerPanel` (per feed and per rack, collapsible) and in `ServerPowerCard`; `PowerDrawPanel.svelte` is the dashboard "Power draw" panel beside the traffic panel. Charts: `ui/PowerChart.svelte` wraps `ui/AggregateTrafficChart.svelte`, which takes `format`, `referenceLines` (rated or usable current) and `showBand`; unit formatting is in `lib/powerFormat.js`. |
| `/admin/racks` | `Racks.svelte` |
| `/admin/racks/:id` | `RackView.svelte` |
| `/admin/racks/rows/:locationId/:row` | `RowView.svelte` |
| `/admin/locations` | `Locations.svelte` |
| `/admin/locations/:id` | `LocationDetail.svelte` (DHCP/TFTP/ISO library) |
| `/admin/runners` | `Runners.svelte` (unified location runners) |
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
| `/admin/product-catalog` | `ProductCatalog.svelte` (tabs: VM / Bare metal / HTTP proxy via `?type=`) |
| `/admin/bare-metal-catalog` | Redirects to `/admin/product-catalog?type=bare_metal` |
| `/admin/vm-templates` | `VMTemplates.svelte` |
| `/admin/vm-ip-allocations` | `VMIpAllocations.svelte` |
| `/admin/proxmox-inventory` | `ProxmoxInventory.svelte` |
| `/admin/private-networks` | `PrivateNetworks.svelte` |
| `/admin/storage` | `StorageNodes.svelte` (nodes with served tenants, SFTP master and drain; storage services with preferred and current node and move; `?service=ID` opens one), `storage/StorageLocationPanel.svelte` (location SFTP, virtual IP, host key, HA, rebalance); server detail has `storage/ServerStorageNodeCard.svelte` |
| `/admin/proxy-catalog` | Redirects to `/admin/product-catalog?type=http_proxy` |
| `/admin/proxy-ipam` | `ProxyIpam.svelte` (IPAM proxy flag) |
| `/admin/proxy-runners` | `ProxyRunners.svelte` (standalone proxy runners) |
| `/admin/billing-integrations` | `BillingIntegrations.svelte` |
| `/admin/mcp-keys` | `McpKeys.svelte` |
| `/admin/store/categories` | `store/StoreCategories.svelte` |
| `/admin/store/products` | `store/StoreProducts.svelte` |
| `/admin/store/coupons` | `store/StoreCoupons.svelte` |
| `/admin/commerce/orders` | `commerce/CommerceOrders.svelte` |
| `/admin/commerce/invoices` | `commerce/CommerceInvoices.svelte` |
| `/admin/commerce/transactions` | `commerce/CommerceTransactions.svelte` |
| `/admin/commerce/gateway-logs` | `commerce/CommerceGatewayLogs.svelte` |
| `/admin/commerce/email-log` | `commerce/CommerceEmailLog.svelte` |
| `/admin/commerce/audit` | `commerce/CommerceAudit.svelte` |
| `/admin/settings/automation` | `AutomationSettings.svelte` ("Automation and billing settings": grouped typed form, per-field save and reset to default) |
| `/admin/support/tickets` | `commerce/SupportTickets.svelte` |
| `/admin/user` | `User.svelte` |
| `/login` | `Login.svelte` |

Client portal (commerce always on; no public storefront): `/client/billing`, `/client/checkout`, `/client/support`, `/client/account`, `/client/networks`, `/client/storage` — see `frontend/src/components/client/Client*.svelte`. Coexistence notes: `docs/commerce-whmcs-coexistence.md`.

Client portal shell: `client/ClientShell.svelte` (left sidebar built on the admin sidebar tokens `--admin-sidebar-*`; drawer plus slim top bar under 768px; impersonation banner above both).

Shared chrome: `Sidebar.svelte`, `PageHeader.svelte`, `ServerControlsPanel.svelte`, `CodeEditor.svelte`.

### UI kit — `frontend/src/components/ui/`

`Button`, `Modal`, `Alert`, `Spinner`, `FormGroup`, `FormError`, `CapabilityCard`, `StateAndActionsCard`, `TrafficGraph`, barrel `index.js`.

### Adding a new admin page

1. Create `frontend/src/components/MyPage.svelte`.
2. Import + `{#if routeName === 'my-page'}` branch in `Admin.svelte`.
3. Add `<a href="/admin/my-page">` in `Sidebar.svelte`.
4. Add API helpers in `lib/api.js` if needed.
5. Implement backend in `app/api/` + DAO/model as required.

Heavy libraries used by one page (for example Cytoscape.js on `/admin/cabling`) should be loaded with a dynamic `import()` so they land in their own chunk; `Admin.svelte` loads `Cabling.svelte` with `{#await import(...)}` and the page imports `cytoscape` and `lib/elkLayout.js` on mount. ELK runs in a web worker (`elkjs/lib/elk-worker.min.js?url`, copied as a ready-minified asset rather than bundled), which keeps layouts off the main thread and keeps the ELK source out of Rollup, so it adds little to `vite build` memory.

---

## Runners & sidecars

| Component | Path | Notes |
|---|---|---|
| Protocol / hub | `runner_common/`, `app/services/runners/` | Runner-initiated WebSocket; state is pushed; admin reads are cache-only |
| DHCP runner | `dhcp_runner/main.py` | FastAPI control plane for `dhcpd` + uplink (`RACKFLOW_URL` + `API_KEY`) |
| TFTP runner | `tftp_runner/main.py` | Control plane for `in.tftpd` + uplink |
| Media runner | `media_runner/main.py` | HTTP ISO library + `smbd` share `isos`; enroll via Admin → Runners |
| Proxy runner | `proxy_runner/cmd/proxy-runner/` | Polls `/api/runner/proxy/config` every 30s (phone-home); register via Admin → Proxy Runners |
| Storage runner | `storage_runner/cmd/storage-runner/` | Polls `GET /api/runner/storage/config`, reconciles (netns + VLAN + veth per tenant, nftables table `inet rf-storage`, CephFS subvolumes and `client.rf-t<id>` users, podman Ganesha per tenant), posts `POST /api/runner/storage/status`. From 0.3.0 a tenant is served only while the node holds its Ceph lease (`docs/nfs-storage-failover.md`); `storage-runner guard` is the backstop unit. Never deletes anything not listed with a destroy state; see `storage_runner/README.md` |
| Bandwidth poller | `scripts/snmp_bandwidth_poller.py` | Docker service `bandwidth-poller` / systemd unit. Each cycle also stores switch port admin/oper status (`status_updated_at`); LLDP neighbours are collected every `--lldp-interval` seconds (default 600, `0` turns it off); PDU history is rolled up and pruned every `--power-history-interval` seconds (default 300, `0` turns it off) |

Python runners dial `WS /api/runner/ws` when `RACKFLOW_URL` and `API_KEY` are set. Legacy HTTP `ServiceInstance.base_url` still works if no uplink is connected. App env `DHCP_RUNNER_URL` / `TFTP_RUNNER_URL` remains for in-cluster HTTP control — see `INSTALL.md`.

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

**WHMCS OS source:** bare-metal Default OS and checkout OS come from the product's **server group** (`permitted_os_templates`, exposed as `os_templates` on `GET /api/billing/server-groups`). VM checkout OS is catalog `vm_templates` (`rfvt:{id}`). Provisioning installs `service_config.template_id` (WHMCS token `rfot:{id}`).

---

## WHMCS

| Path | Role |
|---|---|
| `whmcs/modules/servers/rackflow/rackflow.php` | Provisioning module (Create/Suspend/Power/Register, etc.). Bare-metal checkout OS tokens are `rfot:{template_id}` from the server group; VM OS tokens are `rfvt:{id}`. Service type is taken from the catalog product family. |
| `whmcs/modules/servers/rackflow/git_update.php` | Admin git-archive download + in-place module file sync |
| `whmcs/modules/servers/rackflow/module_update.php` | Admin UI to check/apply git module updates |
| `whmcs/modules/servers/rackflow/module_update_action.php` | Admin JSON API for git module updates |
| `whmcs/modules/addons/rackflow_updater/` | Addon menu entry (Addons → RackFlow Git Updates) |
| `whmcs/modules/servers/rackflow/vnc_open.php` | One-click VM VNC popup launcher |
| `whmcs/modules/servers/rackflow/kvm_open.php` | One-click IPMI HTML5 KVM popup launcher |
| `whmcs/modules/servers/rackflow/sol_open.php` | One-click Serial-over-LAN popup launcher |
| `whmcs/modules/servers/rackflow/virtual_media_action.php` | AJAX BMC virtual CD mount/eject (not a popup) |
| `whmcs/modules/servers/rackflow/ipmi_open.php` | One-click BMC web-UI proxy launcher |
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
| `tests/mcp/` | MCP scopes, key auth, confirm/audit, tool happy paths |
| `tests/contract/` | WHMCS module contract: pins request and response shapes of every `/api/billing/*` route the PHP module calls (route list in `whmcs_routes.py`, see its README); `test_public_invoice_status.py` pins the invoice `status` values of the client commerce and reseller APIs |
| `tests/migrations/` | Billing phase 1 revisions, one file per revision, driven up and down through alembic from `billing_a0_schema.sql` (the schema at `billing_a0_renewals`); `test_billing_p1_mariadb.py` runs the chain on MariaDB when `RACKFLOW_TEST_MARIADB_URL` is set (optional job `.forgejo/workflows/billing-mariadb.yml`, manual trigger) |
| `tests/models/`, `tests/utils/`, `tests/unit/` | Models, utils, placement, etc. |
| `tests/SECURITY_TESTS.md` | Security test notes |
| `tests/fixtures/` | Shared fixtures |

Run: `pytest` (see `pytest.ini`).

Go runners: `cd proxy_runner && go test -race ./...` (fast path kernel check: `-tags sandbox`, see `proxy_runner/README.md`); `cd storage_runner && go test -race ./...` (fakes only). The storage runner's kernel-level checks are behind the `sandbox` build tag and run in a throwaway namespace (see `storage_runner/README.md`).

---

## Scripts & ops

| Path | Role |
|---|---|
| `scripts/dev.sh` | Local dev helper |
| `scripts/create_admin_user.py` | Create admin user |
| `scripts/snmp_bandwidth_poller.py` | Bandwidth sampler |
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
| DHCP/TFTP/media runners | **Fail-closed** HTTP API plus outbound WebSocket uplink (`RACKFLOW_URL` + `API_KEY`) | `dhcp_runner/main.py`, `tftp_runner/main.py`, `media_runner/main.py`, `runner_common/` |
| Service-instance keys | Fernet-encrypted at rest when `SERVICE_INSTANCE_ENCRYPTION_KEY` set; legacy plaintext re-encrypted on verify | `app/dao/service_instance_dao.py`, `app/core/service_instance_crypto.py` |
| Billing integration keys | Stored as SHA-256 hash; plaintext revealed once on create/rotate, masked elsewhere | `app/core/billing_auth.py`, `app/api/billing_admin.py` |
| MCP admin keys | Prefix `rfmcp_`; SHA-256 at rest; plaintext once on create/rotate. Rejected by `get_current_user`. `/mcp` off unless `MCP_ENABLED=true`. Scope ladder `read` ⊂ `write` ⊂ `destructive`; destructive tools need `confirm=true`. | `app/core/mcp_auth.py`, `app/api/mcp_keys_admin.py`, `app/mcp/` |
| Billing power control | `on`/`reboot`/`reset` blocked for `SUSPENDED`/`TERMINATED` | `app/api/billing.py` |
| WHMCS module | TLS verification on (`CURLOPT_SSL_VERIFYPEER`/`VERIFYHOST`) | `whmcs/modules/servers/rackflow/rackflow.php` |
| Validation errors | 422 responses/logs redact request bodies and sensitive fields | `app/main.py` |
| Assets | Auth required; SVG upload disallowed; non-raster served as attachment + `nosniff` + CSP | `app/api/asset.py` |
| IPMI | Password via `IPMI_PASSWORD` env (`-E`), never argv | `app/plugins/ipmi.py` |
| Frontend auth | Role-based routing (admin→`/admin`, else `/client`); global fetch 401 → clear auth + `/login`; secret template params masked | `frontend/src/lib/api.js`, `frontend/src/routes/`, `frontend/src/components/ServerDetail.svelte` |

Relevant settings live in `app/core/config.py`: `TRUST_X_FORWARDED_FOR`, `SERVICE_INSTANCE_ENCRYPTION_KEY`, `REQUIRE_SERVICE_INSTANCE_ENCRYPTION`, `MCP_ENABLED`, `MCP_RATE_LIMIT_PER_KEY`, `MCP_RATE_LIMIT_PER_IP`. Runner keys and encryption keys are wired through `docker-compose.yml`; see `INSTALL.md`.

Client MCP (`/client-mcp`, off unless `CLIENT_MCP_ENABLED=true`): keys use prefix `rfcmcp_` in table `client_mcp_api_keys` (SHA-256 at rest), belong to a client user, are rejected by `get_current_user` and the staff `/mcp`, and are re-checked against the live user and the `account.mcp` preset permission (default on) on every call. Tools (`account`, `services`, `service_action`, `billing`, `tickets`) call `app/api/services_client.py` / `commerce_client.py` handlers with the owner's auth dict, so ownership, presets and audit match the portal. Impersonation sessions cannot create or change keys. Private networks: `services view=networks` and `service_action action=network` (attach, detach, invite, invitation_cancel, member_remove, leave, invitation_accept, invitation_decline) go through the same portal handlers as the member and owner routes (`app/mcp/client/networks.py`).

MCP tools live in `app/mcp/tools/` and call DAOs/services/plugins directly (not a REST proxy). Resources: `rackflow://server/{id}`, `rackflow://service/{id}`, `rackflow://location/{id}`.

---

## Domain glossary

| Term | Meaning in this repo |
|---|---|
| **Server** | Bare-metal (or managed) host with a management plugin (IPMI/Proxmox) |
| **Service** | Customer-facing instance (bare-metal or VM), often linked from WHMCS |
| **Service instance** | Legacy per-location DHCP/TFTP HTTP **runner** registration (not a customer service) |
| **Runner** | Unified location agent (dhcp/tftp/media) that phones home over WebSocket |
| **Boot task** | One-shot PXE/boot instruction for a server |
| **Installation task** | Long-running OS install progress/log |
| **Product / family** | Sellable catalog SKUs; drive VM/bare-metal defaults |
| **VM IP allocation** | Pool of IPs assignable to VM services |
| **Asset** | Uploaded/managed file or label attachment |

---

## Docs already in-repo

- `INSTALL.md` — install, Docker, initial admin, DHCP/TFTP runners
- `docs/deploy-runbook.md` — handing production deploys and infrastructure work to the `sauce-complex-deploy` agent: request template, deploy procedure, owner-confirmation list, how to check production when a Forge request id is unknown
- `os_templates/README.md` — template format
- `isos/README.md` — ISO notes
- `tests/SECURITY_TESTS.md` — security test guidance
- **This file (`CODEBASE.md`)** — navigation map for agents and humans

When unsure, open the UI page → find the component in the Admin map → follow its `api.js` calls → land in `app/api/` → DAO/service/plugin.
