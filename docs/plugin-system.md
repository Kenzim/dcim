# Rackflow plugin system

Status: design for plugin API version **1.0**, core implemented in `app/plugin_host/`.
Audience: Rackflow maintainers, plugin authors (this file is the public contract) and operators.

This document is the contract between Rackflow and third-party plugins. Everything a plugin may rely on is written here: the package format, the manifest, the events, the permissions, the SDK, the UI contribution language and the HTTP shapes every surface (admin UI, client portal, WHMCS, MCP) renders from. Anything not written here is internal and may change without notice.

To write a plugin, start with the [author guide](plugins/README.md); the [API reference](plugins/api-reference.md) is generated from the code, and the [deprecation policy](plugins/deprecation-policy.md) says how this contract changes.

## 1. Goals and trust model

- **Admins only.** Plugins are installed, enabled, configured, upgraded and removed only by staff. Clients never install plugins; at most they see what a plugin shows on their own services and press the buttons it offers them.
- **Anyone may write plugins.** Rackflow is open source. The manifest, the SDK and the event names are a public, versioned contract (section 3.3).
- **Trusted code, narrow door.** A plugin is Python code that an admin chose to install, running inside the Rackflow process, like a WHMCS addon. Rackflow does not sandbox it. A plugin must reach Rackflow only through the SDK context it is handed (section 8), and the context enforces the permissions the plugin declared (section 7). Importing `app.*` from a plugin is unsupported: it works today, it will break without notice, and a plugin that does it is not a valid plugin. The admin sees the declared permissions at install and again at upgrade if they grow, and Rackflow pins the approval to a hash of the plugin's files (section 4.6).
- **Audited.** Every privileged action a plugin takes through the SDK (running a command in a VM, a power action, a client notice, an outbound HTTP call) and every admin action on a plugin is written to the audit log (section 12).
- **One implementation, every surface.** A plugin describes its user interface declaratively in its manifest: panels, read-only fields, status badges, buttons with confirmation text, and forms. Rackflow evaluates those declarations against stored state and serves a **render document** (section 10) that each surface draws with one generic renderer. No plugin JavaScript, HTML or PHP in v1. The WHMCS module has one generic renderer and never needs per-plugin code.
- **A broken plugin never breaks Rackflow.** Timeouts, a circuit breaker, automatic disabling after repeated failures, and health shown to admins (section 11). Rendering a panel never runs plugin code.

The built-in hardware adapters in `app/plugins/` (server, switch and PDU plugins) are a separate, older mechanism and are unchanged by this design. They may become one "kind" of plugin later (section 17).

## 2. Vocabulary

| Term | Meaning |
|---|---|
| Plugin | A package in the plugins directory with a `plugin.json` manifest and a Python entry module |
| Installation | The database record of an installed plugin: version, approved permissions, file hash, status, health |
| Active for a service | The plugin is enabled and its enablement rules (section 9) say it applies to that service |
| Event | A typed fact published on the event bus, such as `service.suspended` |
| Before hook | A synchronous handler that runs before an operation and may veto it (or alter declared fields) |
| After handler | A handler for an event that already happened, delivered durably through the outbox |
| Service state | The plugin's key-value store for one service |
| Render document | The JSON Rackflow serves for a service page, built from the manifest's UI declarations and service state |

## 3. Package format and manifest

### 3.1 Layout

The plugins directory is set by `PLUGINS_DIR` (default `plugins`, relative to the working directory; in Docker mount a volume there). It lives outside `app/`. Each immediate sub-directory holding a `plugin.json` is a package:

```
plugins/
  uptime_reminder/
    plugin.json        manifest (required)
    plugin.py          entry module named by "entry" (required)
    helpers.py         any other modules, imported relatively by the entry module
    README.md          shown nowhere by Rackflow; for humans
```

The directory name must equal the manifest `id`. Rackflow imports the entry module under the private name `rackflow_plugins.<id>`, so a plugin's own modules import each other relatively (`from . import helpers`). Files are never written by Rackflow; plugins keep state through the SDK.

### 3.2 Manifest schema (`plugin.json`)

```json
{
  "id": "uptime_reminder",
  "name": "Uptime reminder",
  "version": "1.0.0",
  "description": "Reminds clients to confirm their service is still needed.",
  "author": "Rackflow",
  "homepage": "https://example.org/uptime-reminder",
  "licence": "MIT",
  "rackflow_api": ">=1.0,<2.0",
  "entry": "plugin.py",
  "permissions": ["service.read", "notify.client", "http.outbound", "ui.client", "mcp.staff"],
  "services": {"types": ["vm", "bare_metal"], "enablement": "all"},
  "settings": [ SettingField, ... ],
  "events": ["service.created", "service.suspended"],
  "hooks": [],
  "schedules": [ Schedule, ... ],
  "actions": [ Action, ... ],
  "ui": {"panels": [ Panel, ... ]},
  "mcp_tools": [ McpTool, ... ]
}
```

| Key | Type | Rules |
|---|---|---|
| `id` | string | `^[a-z][a-z0-9_]{2,47}$`, unique, never changes across versions |
| `name` | string | 1 to 80 characters |
| `version` | string | Semantic version `MAJOR.MINOR.PATCH` |
| `description` | string | Up to 500 characters |
| `author`, `homepage`, `licence` | string | Optional, shown to admins |
| `rackflow_api` | string | Comma-separated comparisons on the plugin API version, for example `>=1.0,<2.0`. Operators `>=`, `>`, `<=`, `<`, `==`. Install and load refuse a plugin whose range excludes the running API version |
| `entry` | string | Path of the entry module inside the package, ending `.py`, no `..` |
| `permissions` | list | Keys from section 7. Anything the plugin does through the SDK must be covered |
| `services` | object | `types`: service types the plugin applies to (`vm`, `bare_metal`, `http_proxy`, `nfs_storage`, `backup_slot`); `enablement`: `all`, `opt_in` or `addon` (section 9). Omit for a plugin with no per-service features |
| `settings` | list | Admin settings form (section 5) |
| `events` | list | Event names from section 6 the plugin handles after they happen |
| `hooks` | list | Event names with a before hook (section 6.3) the plugin wants to run before |
| `schedules` | list | Section 9.3 |
| `actions` | list | Buttons and forms (section 10.3) |
| `ui.panels` | list | Panels (section 10.2) |
| `mcp_tools` | list | Section 13 |

Unknown top-level keys are an error, so a typo is caught at install. Every declared event, hook, schedule, action and MCP tool must have a handler registered by the entry module, and the entry module may not register a handler that the manifest does not declare. `tables` is reserved for plugin-owned tables (section 3.5) and is refused in API 1.0.

### 3.3 Versioning of the contract

- The plugin API version is `MAJOR.MINOR` (currently `1.0`), exposed as `RACKFLOW_PLUGIN_API_VERSION` and in `GET /api/admin/plugins`.
- A **minor** bump only adds: new events, new optional payload fields, new permissions, new SDK methods, new element types (old renderers skip unknown element types, section 10.5). A plugin written for 1.0 keeps working on 1.x.
- A **major** bump may remove or change things. Rackflow will support the previous major for at least one Rackflow release after the new one ships, and the admin page flags plugins whose range does not include the next major.
- Event payloads are versioned with the API: within a major version, fields are only added, never renamed, removed or retyped.

### 3.4 Entry module

The entry module defines one function, `register(plugin)`. `plugin` is a registrar; its decorators bind handlers to the names declared in the manifest:

```python
def register(plugin):
    @plugin.on("service.suspended")
    async def suspended(ctx, event): ...

    @plugin.before("service.power_requested")
    async def check_power(ctx, event): ...      # return None, ctx.veto("reason") or ctx.alter({...})

    @plugin.schedule("check")
    async def check(ctx, service): ...          # service is None for a global schedule

    @plugin.action("acknowledge")
    async def acknowledge(ctx, request): ...    # return {"message": "..."} or raise ctx.ActionError("...")

    @plugin.mcp_tool("status")
    async def status(ctx, params): ...          # return JSON-serialisable data

    @plugin.lifecycle("install")                # install, enable, disable, upgrade, uninstall
    async def installed(ctx, info): ...
```

Handlers may be `async def` or plain `def`. Plain functions run in a small dedicated thread pool so they may block; async handlers run on Rackflow's event loop and must never block (no `time.sleep`, no blocking sockets). SDK methods that do I/O over the network (`ctx.guest.exec`, `ctx.vm.power`, `ctx.http.*`) are coroutines and can only be awaited from async handlers. Storage, settings, logging and notices are plain methods usable from both.

### 3.5 Plugin-owned tables (reserved, not in API 1.0)

Key-value state (section 8.3) covers v1 plugins, including the Windows licence example. When a plugin needs real tables, API 1.1 will add:

- a `tables` permission and a `migrations/` directory of numbered files `0001_<name>.py`, each with `upgrade(op)` and `downgrade(op)` receiving Alembic operations;
- table names `plg_<plugin id>_<name>`, at most 64 characters, created only through those migrations; Rackflow refuses a migration touching any other table;
- the applied migration number kept on the installation row, upgrades run on plugin upgrade, `downgrade` to zero run on uninstall with data purge;
- the plugin's migrations never appear in Rackflow's own Alembic history, so the single-head rule for `alembic/versions/` is unaffected.

## 4. Lifecycle

States of an installation: `disabled`, `enabled`, `failed` (auto-disabled or failed to load), `files_changed` (the files on disk no longer match what the admin approved).

### 4.1 Discover

`GET /api/admin/plugins` lists installed plugins and every package found in the plugins directory, each validated. Invalid packages are listed with their errors and cannot be installed.

### 4.2 Install

The admin picks an available package. The UI shows its name, version, author, description and the declared permissions with plain descriptions and a risk level. The request must repeat the permission list (`accept_permissions`); Rackflow refuses the install unless it covers every declared permission. Rackflow then:

1. validates the manifest and checks the API range;
2. imports the entry module and checks the registered handlers against the manifest;
3. records the installation (version, manifest snapshot, approved permissions, file hash) as `disabled`, or `enabled` if asked;
4. stores setting defaults;
5. runs the `install` lifecycle handler, if any (failure aborts the install and nothing is kept).

### 4.3 Enable and disable

Enable runs the `enable` lifecycle handler, then starts delivering events and running schedules. Disable runs the `disable` handler (best effort, 10 second limit) and stops everything: no new events are queued for the plugin, queued deliveries stay queued (and expire to dead letters after 24 hours), schedules stop, panels disappear from every surface, actions and MCP tools are refused. Data (settings, state, logs) is kept.

### 4.4 Upgrade

When the package on disk has a higher version than the installation, the admin page shows "Upgrade available" with the permission difference. If the new version declares permissions that were not approved, the upgrade request must accept them; removed permissions are dropped. Rackflow validates and loads the new code, replaces the manifest snapshot and hash, adds defaults for new settings (existing values are kept, settings that no longer exist are kept hidden until uninstall), then runs the `upgrade` handler with `info = {"from_version": ..., "to_version": ...}`. A failing upgrade handler marks the plugin `failed` with the error; the admin can retry or disable it. Downgrades are refused.

### 4.5 Uninstall

Uninstall disables the plugin first, runs the `uninstall` handler, then removes the installation. The admin chooses:

- **Remove and delete data** (default): settings, state, schedules, service links, queued deliveries and logs are deleted.
- **Remove and keep data**: settings and state are kept and come back if the same plugin id is installed again; deliveries, schedules and logs are deleted.

Audit entries are never deleted by uninstall.

### 4.6 Files changed on disk

Rackflow hashes every file of the package (SHA-256 over sorted relative paths and contents, ignoring `__pycache__`). On load, a hash or version that differs from the installation stops the plugin from loading and sets `files_changed`; the admin page offers "Review and upgrade" (same version allowed here, so a hot fix can be re-approved). Code the admin did not approve never runs.

### 4.7 Several processes

Every Rackflow process (API workers, the plugin worker) loads enabled plugins lazily and reloads one when its installation row's hash or version changes, checked at most every 10 seconds per process.

## 5. Settings

The admin settings form is a list of fields:

```json
{"key": "reminder_days", "label": "Remind after (days)", "type": "integer", "default": 30, "min": 1, "max": 365, "required": true, "help": "..."}
```

| `type` | Value | Extra keys |
|---|---|---|
| `string` | string | `max_length` (default 255), `pattern` |
| `text` | multi-line string | `max_length` (default 4000) |
| `integer`, `number` | number | `min`, `max` |
| `boolean` | true or false | |
| `select` | one of `options[].value` | `options: [{"value": "...", "label": "..."}]` |
| `url` | `http` or `https` URL | |
| `secret` | string, write-only | Stored encrypted; never returned |
| `window` | maintenance window | `{"days": ["mon", ...], "start": "02:00", "end": "05:00", "timezone": "Europe/London"}` |

Keys match `^[a-z][a-z0-9_]{0,63}$`. Secrets are sealed with the same Fernet key as other stored secrets (`SERVICE_INSTANCE_ENCRYPTION_KEY`, through `app/services/alerting/secrets.py` `seal` and `unseal`). Without that key, secrets are stored unencrypted with a plain marker, exactly like alert channel secrets today; when `REQUIRE_SERVICE_INSTANCE_ENCRYPTION` is on and no key is configured, saving a secret is refused instead. The settings API returns a set secret as the mask `••••••••`; sending the mask back keeps the stored value, an empty string clears it. Plugins read settings, decrypted, through `ctx.settings`.

## 6. Event bus

### 6.1 Envelope

Every event a handler receives is:

```json
{"id": "6f1c...", "name": "service.suspended", "version": "1.0", "occurred_at": "2026-11-21T10:00:00Z", "service_id": 12, "payload": {...}}
```

`id` is unique per event. Delivery is **at least once**: the same event may reach a handler twice after a crash or timeout, so handlers must be idempotent (use `event["id"]`). Order is by occurrence as far as possible but not guaranteed.

### 6.2 Catalogue (API 1.0)

| Event | Payload fields | Permission to receive | Before hook |
|---|---|---|---|
| `service.created` | `service_id`, `service_type`, `status`, `owner_user_id`, `product_code`, `os_code`, `provisioning_source` | `service.read` | no |
| `service.activated` | `service_id`, `service_type`, `owner_user_id`, `previous_status` (pending to active only) | `service.read` | no |
| `service.suspended` | `service_id`, `service_type`, `owner_user_id`, `previous_status` | `service.read` | no |
| `service.unsuspended` | `service_id`, `service_type`, `owner_user_id`, `status` | `service.read` | no |
| `service.terminated` | `service_id`, `service_type`, `owner_user_id`, `previous_status` | `service.read` | no |
| `service.power_requested` | `service_id`, `service_type`, `action` (`on`, `off`, `shutdown`, `reboot`, `reset`), `source` (`admin`, `client`, `billing`, `reseller`, `mcp`, `client_mcp`, `console`, `plugin`), `actor_user_id` | `service.read` | yes (veto only) |
| `order.paid` | `order_id`, `order_number`, `billing_account_id`, `total_cents`, `currency` | `billing.read` | no |
| `invoice.paid` | `invoice_id`, `billing_account_id`; optional `invoice_number`, `order_id`, `reseller_id`, `number_display`, `total_cents`, `currency`, `document_type` | `billing.read` | no |
| `invoice.created` | `invoice_id`, `billing_account_id`, `purpose`, `total_cents`, `currency`; optional `reseller_id`, `invoice_number`, `number_display`, `order_id`, `service_id`, `due_at`, `document_type` | `billing.read` | no |
| `invoice.overdue` | `invoice_id`, `billing_account_id`, `due_cents`, `currency`; optional `reseller_id`, `invoice_number`, `number_display`, `due_at` | `billing.read` | no |
| `invoice.voided` | `invoice_id`, `billing_account_id`, `reason`; optional `reseller_id`, `invoice_number`, `number_display` | `billing.read` | no |
| `invoice.refunded` | `invoice_id`, `billing_account_id`, `refunded_cents`, `currency`; optional `reseller_id`, `invoice_number`, `number_display` | `billing.read` | no |
| `credit_note.issued` | `credit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome`; optional `reseller_id`, `number_display`, `refund_id` | `billing.read` | no |
| `debit_note.issued` | `debit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome`; optional `reseller_id`, `number_display` | `billing.read` | no |
| `payment.refunded` | `refund_id`, `payment_id`, `invoice_id`, `billing_account_id`, `amount_cents`, `currency`, `destination`, `reason`; optional `reseller_id`, `gateway`, `gateway_refund_ref`, `credit_note_id` | `billing.read` | no |
| `credit.added` | `ledger_entry_id`, `billing_account_id`, `amount_cents`, `balance_cents`, `currency`, `entry_type`; optional `reseller_id`, `invoice_id`, `refund_id` | `billing.read` | no |
| `credit.applied` | `ledger_entry_id`, `billing_account_id`, `invoice_id`, `amount_cents`, `balance_cents`, `currency`; optional `reseller_id`, `payment_id` | `billing.read` | no |
| `ticket.created` | `ticket_id`, `ticket_number`, `billing_account_id`, `department_id`, `subject` | `support.read` | no |
| `tax_id.checked` (API 1.1) | `billing_account_id`, `country`, `status`, `validator`, `checked_at` (never the tax ID itself) | `billing.read` | no |
| `invoice.reminder_sent` (API 1.1) | `invoice_id`, `billing_account_id`, `reseller_id`, `kind` (`upcoming` or `overdue`) | `billing.read` | no |
| `service.termination_scheduled` (API 1.1) | `service_id`, `terminate_at`, `reason` | `billing.read` | no |
| `service.cancellation_requested` (API 1.1) | `service_id`, `request_id`, `type`, `effective_at` | `billing.read` | no |

Service events carry `service_id` in the envelope and are delivered only to plugins active for that service (checked at delivery). The service events come from one place: a SQLAlchemy `before_flush` listener on `Service` (`app/plugin_host/listeners.py`) that sees every insert and every status change from any code path (billing API, admin, reseller, MCP, recurring billing, commerce lifecycle), so no caller can forget to emit. `service.power_requested` is emitted by the power entry points (billing, client portal, admin, reseller, staff and client MCP, VNC console) after their own checks pass and before the power call. Commerce events are emitted where the commerce webhooks were queued before (`checkout_service.py`, `payments/webhooks.py`, `commerce_admin.py`, `ticket_service.py`).

Emitting is part of the caller's transaction: the outbox rows commit or roll back with the change that caused them (a transactional outbox). Emitting never raises into the caller and adds no rows when no enabled plugin subscribes, so with no plugins installed the behaviour of every existing path is unchanged.

`service.power_requested` means a power action was *requested* and passed every check, including before hooks. It is committed on its own session before the hypervisor call, so it is delivered even when the power call then fails. Plugins that need the outcome should read the VM's state rather than treat the event as proof the action happened.

### 6.2.1 Emission call sites and billing (stable names)

There is one bus and one function to publish on it, `app.plugin_host.events.emit(db, name, payload, service_id=None)`. Other subsystems must not build a second publish or subscribe mechanism for these facts.

| Event | Where it is emitted today |
|---|---|
| `service.created`, `service.activated`, `service.suspended`, `service.unsuspended`, `service.terminated` | Only in `app/plugin_host/listeners.py`, from the Service flush listener, whichever code path changed the row |
| `service.power_requested` | `app.plugin_host.events.power_requested` (or `guard_power` for HTTP routes), called by each power entry point |
| `order.paid` | `CheckoutService.mark_paid_and_fulfill` (`app/services/checkout_service.py`), the one fulfilment path for paid and accepted orders |
| `invoice.paid` | Only `publish_invoice_paid` in `app/services/billing/settlement_subscribers.py`, a subscriber of the billing settlement function `settle_invoice` (`app/services/billing/settlement.py`), for invoices with a billing account |
| `invoice.created`, `invoice.overdue`, `invoice.voided`, `invoice.refunded`, `credit_note.issued`, `debit_note.issued`, `payment.refunded`, `credit.added`, `credit.applied` | Only the subscriber `catalogue:<event>` in `app/services/billing/settlement_subscribers.py`, which runs when billing fires the internal event of the same name with `settlement.fire` (`docs/billing-phase1-design.md` section 9). Billing phase 1 fires them from `InvoiceService` (publish and transitions), `InvoiceAdjustmentService`, `RefundService` and `CreditLedgerService`; none is fired before those services land |
| `ticket.created` | `app/services/ticket_service.py` |
| `tax_id.checked`, `invoice.reminder_sent`, `service.termination_scheduled`, `service.cancellation_requested` | Catalogued in API 1.1 for billing phase 2 (`docs/billing-phase2-design.md` 8.8); not emitted until the phase 2 tax-ID checks, reminder job, non-payment job and cancellation service land |

Every way an invoice is paid (card webhook, saved-method charge, reseller credit, USDT, admin mark-paid) ends in `settle_invoice`, which publishes `invoice.paid` exactly once, on the transition to paid. Billing's own steps after settlement (count coupon use, complete a retail renewal cycle, send the email, fulfil the order) are in-process subscribers of that function (`docs/billing-target-spec.md` section 14), not handlers on this bus; the outbound webhook keeps coming from `emit`, which already queues it (section 6.5). Invoices without a billing account (reseller deploy and reseller cycle invoices) publish nothing, as before. The event names in section 6.2 are stable for API 1.x; billing adds new facts to this catalogue rather than invent parallel names. The billing events after `invoice.paid` in section 6.2 are catalogued for billing phase 1 and are not sent to outbound commerce webhooks (`COMMERCE_WEBHOOK_EVENTS` is unchanged). `payment.failed` is still only a suggested name and is not emitted.

### 6.3 Before hooks

A before hook runs synchronously inside the request, before the operation:

- it returns `None` to allow, `ctx.veto("reason")` to refuse, or `ctx.alter({...})` to change fields the catalogue marks mutable (none in API 1.0; the mechanism exists for later events such as order placement);
- each hook has **2 seconds**, all hooks for one operation together **3 seconds**; a hook that times out or raises counts as a failure for the circuit breaker and is treated as **allow** (fail open), so a broken plugin cannot lock clients out of their servers;
- hooks run in order of plugin id; the first veto wins and later hooks do not run;
- a veto is audited (`plugin.veto`) and the caller gets HTTP 409 with `{"detail": "<plugin name>: <reason>"}` (MCP: a tool error with the same text);
- before hooks need the `events.veto` permission as well as the event's own permission.

### 6.4 After handlers: outbox, retries and dead letters

Each emitted event creates one `plugin_deliveries` row per subscribed enabled plugin. The plugin worker (section 9.4) claims due rows with a claim token (the same pattern as `app/services/alerting/sender.py`), runs the handler with a **30 second** limit, and:

- on success marks the row `delivered`;
- on failure marks it `failed` with the error and retries after `min(3600, 30 × 2^(attempts − 1))` seconds;
- after **10** attempts, or 24 hours queued while the plugin is disabled, marks it `dead`;
- while the plugin's circuit is open, leaves rows queued without counting an attempt;
- skips (`skipped`) service events for services the plugin is no longer active for.

Admins see queued, failed and dead deliveries per plugin and can retry a dead one. Delivered and skipped rows are pruned after 7 days, dead ones after 30 days.

### 6.5 Outbound webhooks from the same bus

The bus is the single place events come from. `emit()` also hands events in the commerce webhook catalogue (`order.paid`, `invoice.paid`, `ticket.created`) to `CommerceWebhookService.enqueue`, which is why the old direct calls were replaced by `emit()`. Widening outbound webhooks to service events is then a matter of adding names to that catalogue; it is not done in this change, so webhook behaviour is unchanged.

## 7. Permissions

| Key | Lets the plugin | Risk |
|---|---|---|
| `service.read` | Read basic facts (id, name, type, status, owner id, product, OS) of services it is active for; receive service events; per-service schedules and panels | low |
| `billing.read` | Receive the billing events of section 6.2: `order.paid`, `invoice.paid`, `invoice.created`, `invoice.overdue`, `invoice.voided`, `invoice.refunded`, `credit_note.issued`, `debit_note.issued`, `payment.refunded`, `credit.added` and `credit.applied`; from API 1.1 also `tax_id.checked`, `invoice.reminder_sent`, `service.termination_scheduled` and `service.cancellation_requested` | low |
| `support.read` | Receive `ticket.created` | low |
| `events.veto` | Register before hooks that can refuse operations | medium |
| `notify.client` | Send notices to the owner of a service it is active for (category "Notices from plugins", opt-in) | medium |
| `http.outbound` | Make HTTP requests through `ctx.http` (public addresses only, see section 8.6) | medium |
| `ui.client` | Show panels and actions to clients (portal, WHMCS client area, client MCP). Without it panels are staff-only | medium |
| `mcp.staff` | Offer tools to staff MCP keys | medium |
| `mcp.client` | Offer tools to client MCP keys | medium |
| `vm.power` | Start, shut down, stop, reboot or reset VMs it is active for | high |
| `guest.exec` | Run commands inside VMs it is active for through the QEMU guest agent | high |
| `tax.validate_id` (API 1.1) | Receive customers' tax identification numbers and countries to check them | medium |
| `tax.rule_pack` (API 1.1) | Offer tax rules that an administrator may review and apply | medium |
| `tax.calculate` (API 1.1) | Receive basket lines, amounts and the buyer's address and tax ID, and decide the tax on invoices | high |

The SDK checks the permission on every call and raises `PermissionDenied` otherwise; the manifest validator also refuses a manifest whose events, hooks, panels or tools need a permission it does not declare. Service-scoped calls also check the plugin is active for that service. A plugin's own settings, state, logs and audit entries need no permission.

## 8. SDK context

Every handler receives `ctx`, a fresh `PluginContext` bound to one plugin and one invocation. Its surface (API 1.0):

### 8.1 Identity and time

- `ctx.plugin_id`, `ctx.plugin_version`, `ctx.api_version`
- `ctx.now()`: timezone-aware UTC `datetime`
- `ctx.in_window(setting_key, at=None) -> bool`: whether `at` (default now) falls in a `window` setting

### 8.2 Settings and logging

- `ctx.settings.get(key, default=None)`, `ctx.settings.all() -> dict` (secrets decrypted)
- `ctx.log.debug|info|warning|error(message, service_id=None, **fields)`: written to the plugin log shown to admins (messages up to 2,000 characters, fields up to 4 KB as JSON)

### 8.3 State

- `ctx.store`: plugin-wide key-value store
- `ctx.state(service_id)`: the same interface for one service (requires the plugin to be active for it)

Both offer `get(key, default=None)`, `set(key, value)`, `delete(key)`, `all() -> dict`, `keys(prefix="")`. Values are JSON-serialisable, keys match `^[A-Za-z0-9_.:-]{1,128}$`, one value is at most 64 KB, a plugin has at most 100,000 keys. Writes commit with the invocation (a handler that raises leaves no partial writes). Panels read service state directly, so writing state is how a plugin updates what every surface shows.

### 8.4 Services

- `ctx.services.get(service_id) -> dict`: `{id, name, service_type, status, owner_user_id, product_code, os_code, created_at}` [`service.read`]
- `ctx.services.active(service_type=None) -> list[int]`: ids of services the plugin is active for [`service.read`]

### 8.5 VMs and the guest agent

- `await ctx.guest.ping(service_id) -> bool`: whether the guest agent answers [`guest.exec`]
- `await ctx.guest.exec(service_id, argv, *, timeout=60, input=None, sensitive=False) -> {"exit_code": int, "stdout": str, "stderr": str}` [`guest.exec`]
  - VM services only, the plugin must be active for the service, the service must be active (not suspended or terminated);
  - `argv` is a list of strings (no shell unless the plugin runs one), `timeout` at most 300 seconds, output cut at 64 KB each;
  - rate-limited per plugin and service (`PLUGIN_GUEST_EXEC_PER_MINUTE`, default 6) and per plugin (`PLUGIN_GUEST_EXEC_PER_MINUTE_TOTAL`, default 60); over the limit raises `RateLimited`;
  - audited as `plugin.guest_exec` with the plugin, service, `argv` (replaced by `argv[0]` and a SHA-256 of the rest when `sensitive=True`), exit code and duration;
  - implemented with `resolve_proxmox_plugin_for_service` and the Proxmox adapter's `guest_exec` (`app/plugins/proxmox.py`).
- `await ctx.vm.power(service_id, action) -> bool` with `action` in `on`, `shutdown`, `off`, `reboot`, `reset` [`vm.power`]: VM services only, active for the plugin, not suspended or terminated; emits `service.power_requested` with `source = "plugin"` (before hooks of other plugins apply, its own do not); audited as `plugin.vm_power`.

### 8.6 Notices and HTTP

- `ctx.notify.client(service_id, message, *, key)`: queue a notice to the service owner in the opt-in category `plugin` [`notify.client`]; `key` de-duplicates (same key, one notice); message up to 500 characters; audited as `plugin.notify_client`.
- `await ctx.http.post(url, *, json=None, body=None, headers=None, timeout=10) -> {"status": int, "text": str}` and `await ctx.http.get(url, ...)` [`http.outbound`]: through the address-checked client in `app/services/alerting/safe_http.py` (public addresses only, DNS pinned, HTTPS required unless the URL is `http` and the admin turned on `PLUGIN_HTTP_ALLOW_PLAIN`); response text cut at 64 KB; audited as `plugin.http` with method, host and status (never the body or headers).

### 8.7 Audit, errors and results

- `ctx.audit(action, *, service_id=None, details=None)`: write a plugin audit entry `plugin.<id>.<action>` (no permission needed)
- `ctx.veto(reason)`, `ctx.alter(fields)`: before hook results
- `ctx.ActionError(message)`: raise from an action to show `message` to the user as a failure
- Exceptions: `PermissionDenied`, `RateLimited`, `NotActive` (service not in scope), `GuestAgentError`, all subclasses of `PluginError`, importable from `ctx.errors`

### 8.8 What a handler receives

| Handler | Arguments |
|---|---|
| after handler | `ctx, event` (envelope from 6.1) |
| before hook | `ctx, event`; return `None`, `ctx.veto(...)` or `ctx.alter(...)` |
| schedule | `ctx, service` where `service` is the dict from 8.4, or `None` for a global schedule |
| action | `ctx, request` with `request = {"service": {...}, "action": "<id>", "params": {...}, "actor": {"type": "staff" or "client", "user_id": int or None, "via": "portal", "admin", "billing" or "mcp"}}`; return `{"message": "..."}` |
| MCP tool | `ctx, params` (validated against the tool's `params`); the actor is in `ctx.actor` |
| lifecycle | `ctx, info` |

## 9. Services, add-ons and the scheduler

### 9.1 Per-service enablement

`services.enablement` in the manifest decides which services of `services.types` a plugin is active for:

| Value | Active for |
|---|---|
| `all` | Every service of those types that is not terminated, unless an admin switched it off for that service |
| `opt_in` | Only services an admin (or the plugin's own action) switched on |
| `addon` | Only services that bought a linked add-on, plus any an admin switched on |

Explicit decisions are rows in `plugin_service_links` (`enabled`, `source` = `admin`, `addon` or `plugin`, `addon_ref`). An admin decision overrides an add-on decision. Terminated services are never active.

### 9.2 Add-on linkage

An admin links a plugin to one or more **add-on references** (`PUT /api/admin/plugins/{id}/addon-refs`):

- `whmcs:<addon id>`: a WHMCS product add-on. The WHMCS module's generic hook (section 14.4) reports add-on activation, suspension and termination to `POST /api/billing/services/{service_id}/addons`, and Rackflow switches the linked plugins on or off for that service. No per-plugin PHP.
- `store:<product_addons.id>`: a Rackflow storefront add-on. Rackflow's checkout does not sell add-ons yet; when it does, fulfilment will call the same service function. Until then such references can be set but nothing triggers them.

The add-on endpoint body is `{"addon_ref": "whmcs:12", "status": "active" | "suspended" | "terminated"}`; `active` turns the linked plugins on for the service (source `addon`), the others turn them off. The response lists the plugins affected.

### 9.3 Schedules

```json
{"id": "check", "every": "6h", "scope": "service", "timeout": 120, "jitter": "30m"}
```

- `every`: `<number><s|m|h|d>`, at least `60s`;
- `scope`: `global` (one run per interval) or `service` (one run per service the plugin is active for);
- `timeout`: seconds, at most 600 (default 60);
- `jitter`: spreads per-service runs over that span (a stable offset from the service id), so 500 VMs do not all run at once.

Each (plugin, schedule, service) has a row in `plugin_schedules` with `next_run_at`, `last_run_at`, `last_status`, `last_error` and a lease (`lease_owner`, `lease_expires_at`). A runner claims a row with one conditional `UPDATE` (lease free or expired), the pattern of `recurring_billing_service.acquire_lease`, so several processes never run the same row twice. Missed runs are not caught up: after downtime each row runs once and then keeps its interval. A failed run is retried at the next interval, not sooner. At most 20 runs per plugin start per tick and at most 4 run at once per process.

### 9.4 The plugin worker

`app/workers/plugins.py` runs in the API process when `RUN_PLUGIN_WORKER` is true (default true), like the alert worker, ticking every `PLUGIN_WORKER_INTERVAL_SECONDS` (default 5): deliver due events, run due schedules, create schedule rows for newly active services, and once an hour prune deliveries and logs. It can also run on its own with `python -m app.workers.plugins`.

## 10. Declarative UI

### 10.1 Principle

A plugin never sends markup. Its manifest declares panels made of typed elements that read **service state** (section 8.3); Rackflow evaluates them per request and per audience and returns a render document. Rendering runs no plugin code, so a slow or crashed plugin cannot slow a service page; its panels keep showing the last state it wrote, with a health notice.

### 10.2 Panels and elements (manifest)

```json
{
  "id": "status",
  "title": "Uptime reminder",
  "placement": "service",
  "audience": ["admin", "client"],
  "elements": [
    {"type": "badge", "label": "Status", "state": "status",
     "map": {"ok": {"text": "Confirmed", "tone": "success"}, "due": {"text": "Please confirm", "tone": "warning"}},
     "default": {"text": "Not checked yet", "tone": "neutral"}},
    {"type": "field", "label": "Last confirmed", "state": "confirmed_at", "format": "datetime", "empty": "Never"},
    {"type": "text", "text": "We ask every 30 days so unused services can be cancelled.", "audience": ["client"]},
    {"type": "action", "action": "confirm", "when": {"state": "status", "eq": "due"}}
  ]
}
```

- `placement`: `service` (the service page in admin, portal and WHMCS). Other placements are reserved.
- `audience`: who sees it, `admin` and/or `client`. `client` needs the `ui.client` permission. Elements may narrow the panel's audience with their own `audience`.
- `when` (on panels and elements): one condition or a list that must all hold. A condition names a `state` key and one test: `"truthy": true`, `"falsy": true`, `"eq": value`, `"ne": value`, `"in": [values]`. Missing keys are `null`.

Element types:

| `type` | Keys | Renders as |
|---|---|---|
| `field` | `label`, `state` or `value`, `format` (`text`, `number`, `boolean`, `date`, `datetime`, `duration`, `bytes`), `empty` | Label and value |
| `badge` | `label`, `state`, `map` (value to `{text, tone}`), `default` | Coloured status label |
| `text` | `text` or `state`, `tone` (optional) | A paragraph; `tone` makes it a notice box |
| `action` | `action` (an id from `actions`) | A button, or a small form when the action has params |
| `divider` | | A separator |

Tones are `success`, `warning`, `danger`, `info` and `neutral`. Limits: 20 panels per plugin, 40 elements per panel, strings up to 500 characters (text up to 2,000).

### 10.3 Actions (manifest)

```json
{"id": "confirm", "label": "Confirm I still need this", "audience": ["client", "admin"],
 "confirm": "Confirm that you still use this service?", "style": "primary", "timeout": 15,
 "params": [ParamField, ...], "when": {"state": "status", "eq": "due"}}
```

- `audience`: who may run it; `client` needs `ui.client`. Clients may only run it on their own services; staff on any service.
- `confirm`: text the surface shows before running; when set, the request must carry `confirm: true` or Rackflow refuses it (so an API client cannot skip it).
- `style`: `primary`, `secondary` or `danger`.
- `params`: form fields with the setting field types `string`, `text`, `integer`, `number`, `boolean`, `select` (no secrets, no windows), plus `default_state` to prefill from a state key. Rackflow validates params before the handler runs.
- `timeout`: seconds, at most 30 (default 15). Long work belongs in a schedule: the action records the request in state and returns.
- `when`: hides and refuses the action unless the condition holds.

An action runs synchronously: Rackflow checks audience, ownership, the plugin is active, `when`, `confirm` and params, then calls the handler with the time limit, commits its state writes, audits `plugin.action` with the actor, and returns the handler's message plus the plugin's refreshed panels. Each actor may run each action on one service at most 10 times a minute.

### 10.4 Render document (HTTP)

All surfaces get the same JSON:

```
GET /api/admin/services/{service_id}/plugin-ui                     staff session
GET /api/client/services/{service_id}/plugin-ui                    client session (own services; impersonation headers honoured)
GET /api/billing/services/{service_id}/plugin-ui?audience=client   billing API key (WHMCS), audience client or admin
```

```json
{
  "api_version": "1.0",
  "service_id": 12,
  "audience": "client",
  "panels": [
    {
      "plugin_id": "uptime_reminder",
      "plugin_name": "Uptime reminder",
      "panel_id": "status",
      "title": "Uptime reminder",
      "health": "ok",
      "notice": null,
      "elements": [
        {"type": "badge", "label": "Status", "text": "Please confirm", "tone": "warning"},
        {"type": "field", "label": "Last confirmed", "value": "2026-10-20 09:14 UTC"},
        {"type": "text", "text": "We ask every 30 days so unused services can be cancelled.", "tone": null},
        {"type": "action", "plugin_id": "uptime_reminder", "action_id": "confirm",
         "label": "Confirm I still need this", "style": "primary",
         "confirm": "Confirm that you still use this service?",
         "params": [{"key": "note", "label": "Note", "type": "string", "required": false, "default": null, "options": null, "min": null, "max": null, "max_length": 255, "help": null}]},
        {"type": "divider"}
      ]
    }
  ]
}
```

- Values are formatted by Rackflow (dates as `YYYY-MM-DD HH:MM UTC`, numbers with thousands separators, booleans as "Yes"/"No", durations as "2 h 5 min", bytes as "1.5 GB"), so every surface shows the same text. Surfaces escape all strings and never interpret them as markup.
- `health` is `ok`, `degraded` (recent failures) or `unavailable` (circuit open). `notice` is a sentence to show above the elements when not `ok`, for example "This plugin is not responding; the information shown may be out of date." Actions of an `unavailable` panel are still listed but refused with that notice.
- Panels come in plugin id order, then manifest order.

Running an action:

```
POST /api/admin/services/{service_id}/plugin-actions/{plugin_id}/{action_id}
POST /api/client/services/{service_id}/plugin-actions/{plugin_id}/{action_id}
POST /api/billing/services/{service_id}/plugin-actions/{plugin_id}/{action_id}?audience=client
body {"params": {...}, "confirm": true}
200  {"ok": true, "message": "Thanks, we have recorded that.", "panels": [panels of that plugin only]}
4xx  {"detail": "Message to show"}   400 invalid params or missing confirm, 403 audience, 404 unknown service/plugin/action or not active,
                                     409 refused by when or by the plugin (ActionError), 429 rate limit, 503 plugin unavailable or timed out
```

For billing calls the integration must own the service (`_assert_billing_owned_service`). With `audience=client` the actor is recorded as the service owner via WHMCS; WHMCS has already checked the logged-in client owns the WHMCS service.

### 10.5 Renderer rules (every surface)

1. Draw panels in order; show `title`, then `notice` (if any) as a warning box, then the elements.
2. Skip element types you do not know (forward compatibility). Never fail the page because of the plugin section.
3. `badge`: label plus coloured pill (`success` green, `warning` amber, `danger` red, `info` blue, `neutral` grey).
4. `field`: label and value in a definition list or two-column table.
5. `text`: paragraph, or an alert box in the tone's colour when `tone` is set.
6. `action`: a button with `label` and `style`; when `params` is non-empty, a small inline form with one input per param (`string` text input, `text` textarea, `integer` and `number` number inputs with `min`/`max`, `boolean` checkbox, `select` dropdown), prefilled with `default`. On submit, ask `confirm` (if set) with the surface's confirmation dialog, then POST and show `message` (success) or `detail` (failure), and replace that plugin's panels with the returned `panels`.
7. Escape everything. No HTML from plugins is ever rendered as HTML.
8. If the render request fails, show nothing (admin may show a small "Plugin panels unavailable" note).

## 11. Failure isolation and health

- **Timeouts** on every call into plugin code: before hooks 2 seconds (3 seconds per operation), actions up to 30 seconds, deliveries 30 seconds, schedules up to 600 seconds, lifecycle handlers 30 seconds, MCP tools 30 seconds. Async handlers are cancelled at the limit. A plain function cannot be killed; Rackflow stops waiting, counts the failure, and the thread finishes on its own (the pool is small and separate, so it cannot starve Rackflow).
- **Exceptions** are caught at the boundary, logged to the plugin log with a trimmed traceback and recorded on the installation (`last_error`, `last_error_at`).
- **Circuit breaker** per plugin: after 5 consecutive failures the circuit opens for 60 seconds, doubling on each re-open up to 30 minutes. While open: deliveries wait (no attempt counted), schedules wait, before hooks are skipped (allow), actions and MCP tools are refused with "temporarily unavailable", panels show `health: unavailable`. The first success closes it and resets the count.
- **Auto-disable** after 25 consecutive failures: status `failed`, `disabled_reason` set, a `plugin.auto_disabled` audit entry and a staff alert (alert type `plugin_failed`). Re-enabling resets the counters.
- **Import errors** at load mark the plugin `failed` with the error; other plugins and Rackflow carry on.
- **Health** shown to admins: state, consecutive and total failures, last error and time, last success, circuit open until, queued and dead delivery counts, schedule failures.

## 12. Logging and audit

- **Plugin log** (`plugin_logs`): what plugins write with `ctx.log`, plus every failure Rackflow records for them. Shown per plugin on the admin page with level filter. Kept 14 days and at most 5,000 rows per plugin.
- **Audit log** (`user_audit_events` through `AuditService.log`), never pruned by the plugin system:
  - admin actions: `plugin.install`, `plugin.upgrade`, `plugin.enable`, `plugin.disable`, `plugin.uninstall`, `plugin.settings`, `plugin.service_link`, `plugin.addon_refs`, `plugin.delivery_retry`, `plugin.health_reset` with the admin as actor and the approved permissions where relevant;
  - privileged plugin calls: `plugin.guest_exec`, `plugin.vm_power`, `plugin.notify_client`, `plugin.http`, `plugin.veto`, `plugin.auto_disabled`, with `metadata.plugin_id`;
  - actions and tools: `plugin.action`, `plugin.mcp_tool` with the actor (staff, client or billing integration);
  - plugin-written entries: `plugin.<id>.<action>` from `ctx.audit`.

## 13. MCP contributions

```json
{"id": "status", "description": "Reminder status of one service.", "audience": "staff", "scope": "read",
 "params": [{"key": "service_id", "label": "Service", "type": "integer", "required": true}], "timeout": 15}
```

- `audience`: `staff` (needs `mcp.staff`) or `client` (needs `mcp.client`).
- `scope`: the MCP key scope needed, `read`, `write` or `destructive` (destructive tools need `confirm=true`).
- `params`: as action params. A `service_id` param makes the tool service-scoped: Rackflow checks the plugin is active for it and, for client tools, that the caller owns it.

**Budget rule.** Plugins never add tools to `tools/list`. The staff MCP server has one fixed tool, `plugins`, and the client MCP server reuses its existing tools, so the listing does not grow with the number of plugins:

- staff `plugins(action="list")`: installed plugins with status and health; `action="tools", plugin_id=`: that plugin's staff tools with descriptions and params; `action="call", plugin_id=, tool=, params=, confirm=`: run one; `action="panel", service_id=`: the admin render document.
- client `services(view="plugins", service_id=)`: the client render document plus the client tools of active plugins; `service_action(action="plugin", service_id=, name="<plugin_id>.<action or tool id>", params=, confirm=)`: run a client action or tool.

The staff `tools/list` must stay under its **38,000-byte** budget (`tests/mcp/test_alerts_tool.py::test_staff_tool_count_and_size_budget` and `tests/mcp/test_private_network_dhcp_mcp.py::test_staff_tools_list_stays_within_budget`). It was 36,812 bytes over 66 tools before the `plugins` tool; that tool's schema and description are kept under 700 bytes, and a test checks that installing and enabling the sample plugin does not change the listing. Tool descriptions from plugins are capped at 300 characters and are only ever returned by `action="tools"`.

## 14. WHMCS generic renderer contract

The WHMCS module (`whmcs/modules/servers/rackflow/`) renders plugin panels for every plugin through one generic code path.

### 14.1 Client area

`rackflow_ClientArea` fetches `GET /api/billing/services/{id}/plugin-ui?audience=client` (short timeout, 5 seconds; any failure means no plugin section) and passes the panels to `clientarea.tpl`, which renders them by the rules of section 10.5 using WHMCS's Bootstrap classes. Actions post to `plugin_action.php`; on success the module shows the message and reloads the page, which fetches the refreshed panels.

### 14.2 `plugin_action.php`

Same structure as `slot_action.php`: POST only, `X-Requested-With: XMLHttpRequest` required, the WHMCS session must be the service owner (client) or an admin, the product's server type must be `rackflow`, and the module reads the RackFlow service id from the custom field. It forwards `{"params": ..., "confirm": ...}` to `POST /api/billing/services/{id}/plugin-actions/{plugin_id}/{action_id}?audience=client|admin` (admin when an admin session made the call) and returns `{"ok": true, "message": ..., "panels": [...]}` or `{"ok": false, "error": ...}`. Plugin and action ids are checked against `^[a-z][a-z0-9_]{2,47}$` and `^[a-z][a-z0-9_]{0,63}$` before use in the URL.

### 14.3 Admin service tab

`rackflow_AdminServicesTabFields` adds a "Plugins" row rendering `GET /api/billing/services/{id}/plugin-ui?audience=admin` the same way (actions through `plugin_action.php` with the admin session).

### 14.4 Add-on hook

`hooks.php` registers `AddonActivation`, `AddonActivated`, `AddonUnsuspended` (status `active`), `AddonSuspended` (`suspended`), `AddonTerminated` and `AddonCancelled` (`terminated`). For an add-on on a hosting service whose product uses the `rackflow` server module and has a RackFlow service id, it posts `{"addon_ref": "whmcs:<addonid>", "status": ...}` to `POST /api/billing/services/{id}/addons`. Errors are logged with `logModuleCall` and never block WHMCS. Which plugin an add-on enables is configured in Rackflow (section 9.2), so the module never changes per plugin.

## 15. Data model

All tables are created by the Alembic revision `plugin_system`.

| Table | Purpose | Key columns |
|---|---|---|
| `plugin_installations` | One row per installed plugin | `id` (plugin id, primary key), `name`, `version`, `manifest` JSON, `directory`, `code_hash`, `status`, `granted_permissions` JSON, `addon_refs` JSON, `disabled_reason`, health columns (`consecutive_failures`, `total_failures`, `circuit_open_until`, `circuit_opens`, `last_error`, `last_error_at`, `last_success_at`), `installed_at`, `updated_at` |
| `plugin_settings` | Setting values | `plugin_id`, `key` (unique together), `value` JSON, `sealed_value` text for secrets |
| `plugin_state` | Key-value state | `plugin_id`, `scope` (`global` or `service:<id>`), `key` (unique together), `service_id` (FK, cascade), `value` JSON, `updated_at` |
| `plugin_service_links` | Per-service decisions | `plugin_id`, `service_id` (unique together), `enabled`, `source`, `addon_ref` |
| `plugin_deliveries` | Event outbox | `event_id`, `plugin_id` (unique together), `event`, `service_id`, `envelope` JSON, `status`, `attempts`, `next_attempt_at`, `claim_token`, `claim_expires_at`, `last_error`, `created_at`, `delivered_at` |
| `plugin_schedules` | Schedule rows and leases | `plugin_id`, `schedule`, `scope` (unique together), `service_id`, `next_run_at`, `last_run_at`, `last_status`, `last_error`, `lease_owner`, `lease_expires_at` |
| `plugin_logs` | Plugin log | `plugin_id`, `level`, `message`, `service_id`, `context` JSON, `created_at` |

Data on uninstall: section 4.5. Service rows are never deleted by plugins; state and links of a deleted service go with it (foreign key cascade).

## 16. Worked example: Windows licence check and rearm (not built)

This is the yardstick the design must satisfy. It is **not** shipped. It checks every Windows VM's activation state on a schedule, shows it on the service everywhere, lets the client trigger a rearm, asks the client before rebooting (or reboots in an admin-configured maintenance window), and only runs for services that bought the add-on.

### 16.1 Manifest

```json
{
  "id": "windows_licence",
  "name": "Windows licence check",
  "version": "1.0.0",
  "description": "Checks Windows activation and grace period on VMs, and rearms the evaluation licence with the client's consent.",
  "author": "Example Hosting",
  "rackflow_api": ">=1.0,<2.0",
  "entry": "plugin.py",
  "permissions": ["service.read", "guest.exec", "vm.power", "notify.client", "ui.client"],
  "services": {"types": ["vm"], "enablement": "addon"},
  "settings": [
    {"key": "check_every_hours", "label": "Check every (hours)", "type": "integer", "default": 12, "min": 1, "max": 168},
    {"key": "rearm_below_days", "label": "Rearm when fewer grace days than", "type": "integer", "default": 5, "min": 1, "max": 30},
    {"key": "reboot_policy", "label": "Reboot after rearm", "type": "select", "default": "ask",
     "options": [{"value": "ask", "label": "Ask the client"}, {"value": "window", "label": "In the maintenance window"}]},
    {"key": "window", "label": "Maintenance window", "type": "window",
     "default": {"days": ["sun"], "start": "03:00", "end": "05:00", "timezone": "Europe/London"}}
  ],
  "events": ["service.unsuspended"],
  "schedules": [{"id": "check", "every": "1h", "scope": "service", "timeout": 180, "jitter": "50m"}],
  "actions": [
    {"id": "check_now", "label": "Check now", "audience": ["admin", "client"], "style": "secondary"},
    {"id": "rearm_now", "label": "Rearm licence", "audience": ["admin", "client"], "style": "primary",
     "confirm": "Rearm the Windows licence now? A reboot is needed afterwards to finish.",
     "when": {"state": "rearms_left", "ne": 0}},
    {"id": "approve_reboot", "label": "Reboot now", "audience": ["client", "admin"], "style": "danger",
     "confirm": "Reboot this server now? Unsaved work inside Windows will be lost.",
     "when": {"state": "reboot_pending", "truthy": true}},
    {"id": "defer_reboot", "label": "Reboot in the maintenance window", "audience": ["client"], "style": "secondary",
     "when": {"state": "reboot_pending", "truthy": true}}
  ],
  "ui": {"panels": [{
    "id": "licence", "title": "Windows licence", "placement": "service", "audience": ["admin", "client"],
    "elements": [
      {"type": "badge", "label": "Licence", "state": "status",
       "map": {"licensed": {"text": "Activated", "tone": "success"},
               "grace": {"text": "Grace period", "tone": "warning"},
               "expired": {"text": "Expired", "tone": "danger"},
               "unreachable": {"text": "Cannot check (guest agent not answering)", "tone": "neutral"}},
       "default": {"text": "Not checked yet", "tone": "neutral"}},
      {"type": "field", "label": "Grace days left", "state": "grace_days", "format": "number", "empty": "Unknown"},
      {"type": "field", "label": "Rearms left", "state": "rearms_left", "format": "number", "empty": "Unknown"},
      {"type": "field", "label": "Last checked", "state": "checked_at", "format": "datetime", "empty": "Never"},
      {"type": "text", "tone": "warning", "state": "reboot_message", "when": {"state": "reboot_pending", "truthy": true}},
      {"type": "field", "label": "Last guest error", "state": "last_error", "audience": ["admin"], "when": {"state": "last_error", "truthy": true}},
      {"type": "action", "action": "approve_reboot"},
      {"type": "action", "action": "defer_reboot"},
      {"type": "action", "action": "rearm_now"},
      {"type": "action", "action": "check_now"}
    ]}]}
}
```

### 16.2 Pseudo-code (`plugin.py`)

```python
SLMGR = ["cscript.exe", "//nologo", "C:\\Windows\\System32\\slmgr.vbs"]

def register(plugin):

    async def check(ctx, service_id):
        state = ctx.state(service_id)
        if not await ctx.guest.ping(service_id):
            state.set("status", "unreachable")
            state.set("checked_at", ctx.now().isoformat())
            return
        out = await ctx.guest.exec(service_id, SLMGR + ["/dlv"], timeout=90)
        info = parse_dlv(out["stdout"])                 # licence status, grace minutes, rearm count
        state.set("status", info.status)                # licensed, grace or expired
        state.set("grace_days", info.grace_minutes // 1440)
        state.set("rearms_left", info.rearm_count)
        state.set("checked_at", ctx.now().isoformat())
        state.delete("last_error")
        if (info.status != "licensed" and info.grace_minutes // 1440 < ctx.settings.get("rearm_below_days")
                and info.rearm_count > 0 and not state.get("reboot_pending")):
            await rearm(ctx, service_id, reason="automatic")

    async def rearm(ctx, service_id, reason):
        out = await ctx.guest.exec(service_id, SLMGR + ["/rearm"], timeout=90)
        if out["exit_code"] != 0:
            raise ctx.ActionError("Windows refused the rearm.")
        ctx.audit("rearm", service_id=service_id, details={"reason": reason})
        state = ctx.state(service_id)
        state.set("reboot_pending", True)
        if ctx.settings.get("reboot_policy") == "window":
            state.set("reboot_message", "The licence was rearmed. The server will reboot in the next maintenance window.")
            state.set("reboot_in_window", True)
        else:
            state.set("reboot_message", "The licence was rearmed. Reboot the server to finish; choose when below.")
            ctx.notify.client(service_id, "Your Windows server needs a reboot to finish renewing its licence.",
                              key=f"rearm:{service_id}:{ctx.now().date()}")

    @plugin.schedule("check")
    async def scheduled(ctx, service):
        if service["status"] != "active":
            return
        state = ctx.state(service["id"])
        if state.get("reboot_pending") and state.get("reboot_in_window") and ctx.in_window("window"):
            await ctx.vm.power(service["id"], "reboot")
            clear_reboot(state)
            return
        last = state.get("checked_at")
        if last is None or hours_since(ctx.now(), last) >= ctx.settings.get("check_every_hours"):
            await check(ctx, service["id"])

    @plugin.on("service.unsuspended")
    async def unsuspended(ctx, event):
        await check(ctx, event["service_id"])

    @plugin.action("check_now")
    async def check_now(ctx, request):
        await check(ctx, request["service"]["id"])
        return {"message": "Checked."}

    @plugin.action("rearm_now")
    async def rearm_now(ctx, request):
        await rearm(ctx, request["service"]["id"], reason=f"{request['actor']['type']} request")
        return {"message": "Rearmed. A reboot is needed to finish."}

    @plugin.action("approve_reboot")                    # the client's consent, audited as plugin.action
    async def approve_reboot(ctx, request):
        await ctx.vm.power(request["service"]["id"], "reboot")
        clear_reboot(ctx.state(request["service"]["id"]))
        return {"message": "The server is rebooting."}

    @plugin.action("defer_reboot")
    async def defer_reboot(ctx, request):
        state = ctx.state(request["service"]["id"])
        state.set("reboot_in_window", True)
        state.set("reboot_message", "The server will reboot in the next maintenance window.")
        return {"message": "The reboot will happen in the maintenance window."}
```

### 16.3 How the design covers it

| Need | Mechanism |
|---|---|
| Scheduled job per Windows VM | Schedule with `scope: service` over services active for the plugin (section 9.3) |
| Run commands in the VM | `ctx.guest.exec` with `guest.exec`, rate-limited and audited (section 8.5) |
| Per-service state (grace days, rearms left) | `ctx.state(service_id)` (section 8.3) |
| Status in admin, portal and WHMCS | One panel in the manifest, served as a render document to all three (sections 10 and 14) |
| Client action | `rearm_now` with confirmation text, audience client (section 10.3) |
| Consent before a reboot | `reboot_pending` state shows "Reboot now" and "Reboot in the maintenance window"; nothing reboots without the client's click, unless the admin chose the window policy. Each click is audited with the client as actor |
| Maintenance window | `window` setting plus `ctx.in_window` (sections 5 and 8.1) |
| Sold as an add-on | `enablement: addon` plus add-on references (section 9.2); WHMCS reports the add-on through its generic hook |
| Only Windows VMs | The admin links the add-on only to Windows products; the plugin can also check `service["os_code"]` |

## 17. Out of scope for API 1.0, and next steps

- Plugin-owned tables and migrations (section 3.5).
- Plugin kinds (below).
- More events (invoice created and overdue, ticket replies, backups, installs) and before hooks with mutable fields (order placement).
- Placements other than the service page (client dashboard, server page) and plugin pages of their own.
- Storefront add-on purchase (section 9.2).
- Outbound webhooks for service events (section 6.5).
- Signed plugin packages and a plugin index.

### 17.1 Plugin kinds

API 1.0 has one kind of plugin, the general one described here. A kind is a plugin that also implements a typed provider interface Rackflow calls directly, declared in the manifest as `"provides": [{"kind": "payment_gateway", "id": "stripe"}]` (reserved, refused in 1.0) and registered with `@plugin.provider("payment_gateway", "stripe")`. Each kind gets its own permission, its own typed interface and the same lifecycle, settings, audit and failure isolation as everything else. The kinds expected, with the seam names billing should align to:

| Kind | Interface sketch |
|---|---|
| `payment_gateway` | create a payment, capture, refund, parse and verify a webhook |
| `tax_provider` | calculate tax for a basket and an address |
| `tax_id_validator` | validate a tax or VAT number for a country |
| `tax_rule_pack` | static rules (rates by country and product class) loaded as data |
| `accounting_export` | export invoices, payments and credit notes to an external ledger |
| `currency_rates` | fetch exchange rates |
| `notification_channel` | send a rendered message (email, SMS, chat) |
| `import_source` | import clients, services and invoices from another billing system |
| `fraud_check` | score an order before it is accepted (a before hook with a verdict) |
| `server_adapter`, `switch_adapter`, `pdu_adapter` | the existing hardware adapters in `app/plugins/`, moved later |
