# Rackflow plugin API 1.1 reference

Generated from the code by `python -m app.plugin_host.testkit.reference --write`. Do not edit by hand: `tests/plugin_host/test_reference.py` fails when this file and the code disagree. For how to use these pieces see the [author guide](README.md); for the rules on changing them see the [deprecation policy](deprecation-policy.md).

## Permissions

| Key | Label | Risk | Lets the plugin |
|---|---|---|---|
| `service.read` | Read services | low | Read basic facts of the services it is active for and receive service events. |
| `billing.read` | Billing events | low | Receive billing events: orders and invoices paid, invoices created, overdue, voided and refunded, credit and debit notes issued, payments refunded, account credit added or applied, invoice reminders sent, tax ID check results, service terminations scheduled and cancellations requested. |
| `support.read` | Support events | low | Receive ticket created events. |
| `events.veto` | Refuse operations | medium | Run checks before operations such as power actions and refuse them. |
| `notify.client` | Notify clients | medium | Send notices to the owners of services it is active for. |
| `http.outbound` | Outbound HTTP | medium | Make HTTP requests to public addresses. |
| `ui.client` | Client interface | medium | Show panels and buttons to clients in the portal, WHMCS and client MCP. |
| `mcp.staff` | Staff MCP tools | medium | Offer tools to staff MCP keys. |
| `mcp.client` | Client MCP tools | medium | Offer tools to client MCP keys. |
| `vm.power` | VM power | high | Start, shut down, stop, reboot or reset VMs it is active for. |
| `guest.exec` | Run commands in VMs | high | Run commands inside VMs it is active for through the QEMU guest agent. |
| `tax.validate_id` | Validate tax IDs | medium | Receive customers' tax identification numbers and countries to check them. |
| `tax.rule_pack` | Offer tax rule packs | medium | Offer tax rules that an administrator may review and apply. |
| `tax.calculate` | Calculate tax | high | Receive basket lines, amounts and the buyer's address and tax ID, and decide the tax on invoices. |
| `currency.rates` | Offer exchange rates | medium | Offer published exchange rates that billing stores and uses to show tax in another currency. |

## Events

Each event is delivered inside an envelope: `id`, `name`, `version`, `occurred_at`, `service_id` and `payload`. The fields below are the keys of `payload`. Optional fields may be missing or null.

| Event | Permission to receive | Before hook | Service scoped |
|---|---|---|---|
| [`service.created`](#servicecreated) | `service.read` | no | yes |
| [`service.activated`](#serviceactivated) | `service.read` | no | yes |
| [`service.suspended`](#servicesuspended) | `service.read` | no | yes |
| [`service.unsuspended`](#serviceunsuspended) | `service.read` | no | yes |
| [`service.terminated`](#serviceterminated) | `service.read` | no | yes |
| [`service.power_requested`](#servicepower_requested) | `service.read` | yes | yes |
| [`order.paid`](#orderpaid) | `billing.read` | no | no |
| [`invoice.paid`](#invoicepaid) | `billing.read` | no | no |
| [`invoice.created`](#invoicecreated) | `billing.read` | no | no |
| [`invoice.overdue`](#invoiceoverdue) | `billing.read` | no | no |
| [`invoice.voided`](#invoicevoided) | `billing.read` | no | no |
| [`invoice.refunded`](#invoicerefunded) | `billing.read` | no | no |
| [`credit_note.issued`](#credit_noteissued) | `billing.read` | no | no |
| [`debit_note.issued`](#debit_noteissued) | `billing.read` | no | no |
| [`payment.refunded`](#paymentrefunded) | `billing.read` | no | no |
| [`credit.added`](#creditadded) | `billing.read` | no | no |
| [`credit.applied`](#creditapplied) | `billing.read` | no | no |
| [`tax_id.checked`](#tax_idchecked) | `billing.read` | no | no |
| [`invoice.reminder_sent`](#invoicereminder_sent) | `billing.read` | no | no |
| [`service.termination_scheduled`](#servicetermination_scheduled) | `billing.read` | no | no |
| [`service.cancellation_requested`](#servicecancellation_requested) | `billing.read` | no | no |
| [`ticket.created`](#ticketcreated) | `support.read` | no | no |

### service.created

A service row was inserted, from any code path.

- Required fields: `service_id`, `service_type`, `status`, `owner_user_id`, `product_code`, `os_code`, `provisioning_source`

### service.activated

A service moved from pending to active for the first time.

- Required fields: `service_id`, `service_type`, `owner_user_id`, `previous_status`

### service.suspended

A service became suspended.

- Required fields: `service_id`, `service_type`, `owner_user_id`, `previous_status`

### service.unsuspended

A suspended service became active again.

- Required fields: `service_id`, `service_type`, `owner_user_id`, `status`

### service.terminated

A service was terminated. Delivered to plugins that were active for it.

- Required fields: `service_id`, `service_type`, `owner_user_id`, `previous_status`

### service.power_requested

A power action was requested and passed every check. Also a before hook: a plugin can refuse it.

- Required fields: `service_id`, `service_type`, `action`, `source`, `actor_user_id`
- `action` is one of `on`, `off`, `shutdown`, `reboot`, `reset`
- `source` is one of `admin`, `client`, `billing`, `reseller`, `mcp`, `client_mcp`, `console`, `plugin`

### order.paid

An order was paid and is being fulfilled.

- Required fields: `order_id`, `order_number`, `billing_account_id`, `total_cents`, `currency`

### invoice.paid

An invoice with a billing account was settled, once, on the transition to paid.

- Required fields: `invoice_id`, `billing_account_id`
- Optional fields: `invoice_number`, `order_id`, `reseller_id`, `number_display`, `total_cents`, `currency`, `document_type`

### invoice.created

An invoice was published.

- Required fields: `invoice_id`, `billing_account_id`, `purpose`, `total_cents`, `currency`
- Optional fields: `reseller_id`, `invoice_number`, `number_display`, `order_id`, `service_id`, `due_at`, `document_type`

### invoice.overdue

An invoice passed its due date unpaid.

- Required fields: `invoice_id`, `billing_account_id`, `due_cents`, `currency`
- Optional fields: `reseller_id`, `invoice_number`, `number_display`, `due_at`

### invoice.voided

An invoice was voided.

- Required fields: `invoice_id`, `billing_account_id`, `reason`
- Optional fields: `reseller_id`, `invoice_number`, `number_display`

### invoice.refunded

An invoice was refunded, wholly or in part.

- Required fields: `invoice_id`, `billing_account_id`, `refunded_cents`, `currency`
- Optional fields: `reseller_id`, `invoice_number`, `number_display`

### credit_note.issued

A credit note was issued against an invoice.

- Required fields: `credit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome`
- Optional fields: `reseller_id`, `number_display`, `refund_id`

### debit_note.issued

A debit note was issued against an invoice.

- Required fields: `debit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome`
- Optional fields: `reseller_id`, `number_display`

### payment.refunded

A payment refund was recorded.

- Required fields: `refund_id`, `payment_id`, `invoice_id`, `billing_account_id`, `amount_cents`, `currency`, `destination`, `reason`
- Optional fields: `reseller_id`, `gateway`, `gateway_refund_ref`, `credit_note_id`

### credit.added

Credit was added to an account's balance.

- Required fields: `ledger_entry_id`, `billing_account_id`, `amount_cents`, `balance_cents`, `currency`, `entry_type`
- Optional fields: `reseller_id`, `invoice_id`, `refund_id`

### credit.applied

Account credit was applied to an invoice.

- Required fields: `ledger_entry_id`, `billing_account_id`, `invoice_id`, `amount_cents`, `balance_cents`, `currency`
- Optional fields: `reseller_id`, `payment_id`

### tax_id.checked

A customer's tax ID was checked. The payload carries the outcome, never the number.

- Required fields: `billing_account_id`, `country`, `status`, `validator`, `checked_at`

### invoice.reminder_sent

An invoice reminder email was queued (kind upcoming or overdue).

- Required fields: `invoice_id`, `billing_account_id`, `reseller_id`, `kind`

### service.termination_scheduled

A service suspended for non-payment was given a termination date.

- Required fields: `service_id`, `terminate_at`, `reason`

### service.cancellation_requested

A cancellation of a service was requested.

- Required fields: `service_id`, `request_id`, `type`, `effective_at`

### ticket.created

A support ticket was opened.

- Required fields: `ticket_id`, `ticket_number`, `billing_account_id`, `department_id`, `subject`

## Manifest (`plugin.json`)

Unknown keys are refused everywhere. Setting, parameter, panel, action, schedule and tool ids must be unique within their list.

Setting and parameter field types: `string`, `text`, `integer`, `number`, `boolean`, `select`, `url`, `secret`, `window`. Action and tool parameters use `string`, `text`, `integer`, `number`, `boolean`, `select`.

Service types: `vm`, `bare_metal`, `http_proxy`, `nfs_storage`, `backup_slot`. Value formats: `text`, `number`, `boolean`, `date`, `datetime`, `duration`, `bytes`. Tones: `success`, `warning`, `danger`, `info`, `neutral`. Weekdays: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`.

### Manifest

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `id` | string | required |  | Must equal the plugin's folder name. |
| `name` | string | required | min length 1, max length 80 |  |
| `version` | string | required |  | Semantic version of the plugin itself, MAJOR.MINOR.PATCH. |
| `description` | string | `""` | max length 500 |  |
| `author` | string | none | max length 200 |  |
| `homepage` | string | none | max length 500 |  |
| `licence` | string | none | max length 100 |  |
| `rackflow_api` | string | required |  | Range of plugin API versions the plugin supports, for example `>=1.0,<2.0`. |
| `entry` | string | required |  | Python file inside the package that defines `register(plugin)`. |
| `permissions` | list of string | empty |  | Every permission the plugin uses. The admin accepts them at install. |
| `services` | [ServicesSpec](#servicesspec) | none |  | Required when the plugin has actions, panels or per-service schedules. |
| `settings` | list of [SettingField](#settingfield) | empty | max length 100 |  |
| `events` | list of string | empty |  | Events delivered to after handlers. |
| `hooks` | list of string | empty |  | Events with before hooks (needs `events.veto`). |
| `schedules` | list of [ScheduleSpec](#schedulespec) | empty | max length 20 |  |
| `actions` | list of [ActionSpec](#actionspec) | empty | max length 50 |  |
| `ui` | [UiSpec](#uispec) | empty |  |  |
| `mcp_tools` | list of [McpToolSpec](#mcptoolspec) | empty | max length 20 |  |
| `provides` | list of [ProvideSpec](#providespec) | empty | max length 10 | Typed providers the plugin offers (API 1.1 and later). Each needs the permission of its kind and an `@plugin.provider(kind, id)` in `register`. |
| `tables` | any | none |  | Reserved. Refused in every API version so far. |

### ServicesSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `types` | list of `"vm"` or `"bare_metal"` or `"http_proxy"` or `"nfs_storage"` or `"backup_slot"` | required | min length 1 | Service types the plugin applies to. |
| `enablement` | `"all"` or `"opt_in"` or `"addon"` | `"opt_in"` |  | `all`: active for every service of those types. `opt_in`: only where an admin or the plugin switches it on. `addon`: linked to a billing add-on. |

### SettingField

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `key` | string | required |  |  |
| `label` | string | required | min length 1, max length 500 |  |
| `required` | boolean | `false` |  |  |
| `default` | any | none |  |  |
| `help` | string | none | max length 500 |  |
| `min` | number | none |  |  |
| `max` | number | none |  |  |
| `max_length` | integer | none | at least 1, at most 65535 |  |
| `pattern` | string | none |  |  |
| `options` | list of [Option](#option) | none |  |  |
| `type` | `"string"` or `"text"` or `"integer"` or `"number"` or `"boolean"` or `"select"` or `"url"` or `"secret"` or `"window"` | required |  | `secret` values are encrypted and never shown again. `window` holds days, start, end and a time zone. |

### ParamField

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `key` | string | required |  |  |
| `label` | string | required | min length 1, max length 500 |  |
| `required` | boolean | `false` |  |  |
| `default` | any | none |  |  |
| `help` | string | none | max length 500 |  |
| `min` | number | none |  |  |
| `max` | number | none |  |  |
| `max_length` | integer | none | at least 1, at most 65535 |  |
| `pattern` | string | none |  |  |
| `options` | list of [Option](#option) | none |  |  |
| `type` | `"string"` or `"text"` or `"integer"` or `"number"` or `"boolean"` or `"select"` | required |  |  |
| `default_state` | string | none |  | Prefill the value from this per-service state key. |

### Option

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `value` | string or integer or number or boolean | required |  |  |
| `label` | string | required | max length 500 |  |

### ScheduleSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `id` | string | required |  |  |
| `every` | string or integer | required |  | At least 60 seconds. A number of seconds or a number with s, m, h or d. |
| `scope` | `"global"` or `"service"` | `"global"` |  | `global` runs once; `service` runs once per active service. |
| `timeout` | integer | `60` | at least 1, at most 600 |  |
| `jitter` | string or integer | `0` |  | Spreads per-service runs over this long so they do not all start together. |

### ActionSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `id` | string | required |  |  |
| `label` | string | required | min length 1, max length 500 |  |
| `audience` | list of `"admin"` or `"client"` | `["admin"]` |  |  |
| `confirm` | string | none | max length 500 | Text shown in a confirmation dialog. The caller must confirm. |
| `style` | `"primary"` or `"secondary"` or `"danger"` | `"secondary"` |  |  |
| `timeout` | integer | `15` | at least 1, at most 30 | Seconds the handler may run. |
| `params` | list of [ParamField](#paramfield) | empty | max length 20 |  |
| `when` | [Condition](#condition) or list of [Condition](#condition) | none |  |  |

### UiSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `panels` | list of [Panel](#panel) | empty | max length 20 |  |

### Panel

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `id` | string | required |  |  |
| `title` | string | required | min length 1, max length 500 |  |
| `placement` | `"service"` | `"service"` |  | Only the service page in API 1.0. |
| `audience` | list of `"admin"` or `"client"` | `["admin"]` |  | Who sees the panel. `client` needs the `ui.client` permission. |
| `when` | [Condition](#condition) or list of [Condition](#condition) | none |  |  |
| `elements` | list of [Element](#element) | empty | max length 40 |  |

### Element

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `type` | string | required |  | `field`, `badge`, `text`, `action` or `divider`. |
| `label` | string | none | max length 500 |  |
| `state` | string | none |  | Per-service state key to show. |
| `value` | any | none |  | A fixed value, for a field that does not read state. |
| `format` | `"text"` or `"number"` or `"boolean"` or `"date"` or `"datetime"` or `"duration"` or `"bytes"` | `"text"` |  |  |
| `empty` | string | none | max length 500 |  |
| `map` | object of [BadgeStyle](#badgestyle) | none |  | Badge text and tone per state value. |
| `default` | [BadgeStyle](#badgestyle) | none |  |  |
| `text` | string | none | max length 2000 |  |
| `tone` | `"success"` or `"warning"` or `"danger"` or `"info"` or `"neutral"` | none |  |  |
| `action` | string | none |  | Id of a declared action; the element renders its button. |
| `audience` | list of `"admin"` or `"client"` | none |  |  |
| `when` | [Condition](#condition) or list of [Condition](#condition) | none |  |  |

### Condition

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `state` | string | required |  | Per-service state key to test. |
| `truthy` | boolean | none |  |  |
| `falsy` | boolean | none |  |  |
| `eq` | any | none |  |  |
| `ne` | any | none |  |  |
| `in` | list of any | none |  |  |

### BadgeStyle

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `text` | string | required | max length 500 |  |
| `tone` | `"success"` or `"warning"` or `"danger"` or `"info"` or `"neutral"` | `"neutral"` |  |  |

### McpToolSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `id` | string | required |  |  |
| `description` | string | required | min length 1, max length 300 |  |
| `audience` | `"staff"` or `"client"` | `"staff"` |  | `staff` needs `mcp.staff`; `client` needs `mcp.client`. |
| `scope` | `"read"` or `"write"` or `"destructive"` | `"read"` |  | `read`, `write` or `destructive`, as the MCP key scopes see it. |
| `params` | list of [ParamField](#paramfield) | empty | max length 20 |  |
| `timeout` | integer | `15` | at least 1, at most 30 |  |

### ProvideSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `kind` | `"tax_id_validator"` or `"tax_rule_pack"` or `"tax_provider"` or `"currency_rates"` | required |  | `tax_id_validator`, `tax_rule_pack`, `tax_provider` or `currency_rates`. |
| `id` | string | required |  | Unique with the kind inside the plugin. Billing refers to the provider as `<plugin id>:<provider id>`. |
| `label` | string | required | min length 1, max length 80 | Shown to admins where they choose a provider. |
| `description` | string | `""` | max length 500 | Shown to admins with the label. |
| `countries` | list of string | empty | max length 300 | Upper case ISO alpha-2 codes the provider covers (`EL` for Greece at VIES). Empty means any. |
| `id_types` | list of string | empty | max length 50 | Tax ID types the provider checks, such as `eu_vat`. Required for `tax_id_validator`, refused elsewhere. |
| `currencies` | list of string | empty | max length 200 | Upper case ISO alpha-3 codes. Only for `tax_provider`; empty means any. |
| `timeout_seconds` | integer | `8` | at least 1, at most 30 | Wall-clock limit of one call. Billing may lower it for a call, never raise it. |
| `rate_limit` | [RateLimitSpec](#ratelimitspec) | none |  | Calls allowed per window, shared by every Rackflow process. One counter per provider, used by `check_online`, `calculate`, `load` and `rates`; `supports`, `check_format`, `pack_id` and `versions` are not counted. |
| `pack` | [PackRef](#packref) | none |  | Only for `tax_rule_pack`: the JSON file of the pack inside the package. It must exist and pass the pack schema at install. |

### RateLimitSpec

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `calls` | integer | required | at least 1, at most 10000 | Calls allowed in one window. |
| `per_seconds` | integer | required | at least 1, at most 86400 | Length of the window in seconds. |

### PackRef

| Field | Type | Default | Limits | Notes |
|---|---|---|---|---|
| `file` | string | required | min length 1, max length 255 | Relative path of a `.json` file inside the package. |

## SDK context

Every handler receives `ctx`. A call that needs a permission raises `PermissionDenied` when the plugin was not granted it. Calls on a service raise `NotActive` when the plugin is not active for that service.

| Member | Kind | Permission | Notes |
|---|---|---|---|
| `ctx.actor` | attribute |  | Who caused an action or tool call: type, user_id and via. None elsewhere. |
| `ctx.alter(fields: dict) -> Alter` | method |  | Build the result a before hook returns to change mutable payload fields. |
| `ctx.api_version` | attribute |  | The plugin API version of the running Rackflow. |
| `ctx.audit(action: str, *, service_id: Optional[int] = None, details: Optional[dict] = None) -> None` | method |  | Write an audit entry named plugin.<id>.<action>. |
| `await ctx.guest.exec(service_id: int, argv: list, *, timeout: int = 60, input: Optional[str] = None, sensitive: bool = False) -> dict` | method | `guest.exec` | Run a command in a VM through the guest agent. Rate limited and audited. |
| `await ctx.guest.ping(service_id: int) -> bool` | method | `guest.exec` | Whether the QEMU guest agent of a VM answers. |
| `await ctx.http.get(url: str, *, headers=None, timeout: float = 10) -> dict` | method | `http.outbound` | GET from a public address. Returns status and text. |
| `await ctx.http.post(url: str, *, json=None, body=None, headers=None, timeout: float = 10) -> dict` | method | `http.outbound` | POST to a public address. Returns status and text. |
| `ctx.in_window(setting_key: str, at: Optional[datetime] = None) -> bool` | method |  | Whether a time falls inside a window setting. The time defaults to now. |
| `ctx.kind` | attribute |  | What is running: event, before, schedule, action, mcp_tool, lifecycle or provider. |
| `ctx.log.debug(message, service_id=None, **fields)` | method |  | Write a debug line to the plugin log. |
| `ctx.log.error(message, service_id=None, **fields)` | method |  | Write an error to the plugin log. |
| `ctx.log.info(message, service_id=None, **fields)` | method |  | Write an info line to the plugin log. |
| `ctx.log.warning(message, service_id=None, **fields)` | method |  | Write a warning to the plugin log. |
| `ctx.notify.client(service_id: int, message: str, *, key: str) -> int` | method | `notify.client` | Queue a notice to the owner of a service. The key de-duplicates. |
| `ctx.now() -> datetime` | method |  | The current time as a timezone-aware UTC datetime. |
| `ctx.plugin_id` | attribute |  | The plugin's id. |
| `ctx.plugin_version` | attribute |  | The installed version. |
| `ctx.services.active(service_type: Optional[str] = None) -> list[int]` | method | `service.read` | Ids of the services the plugin is active for, optionally of one type. |
| `ctx.services.get(service_id: int) -> dict` | method | `service.read` | Basic facts of a service the plugin is active for. |
| `ctx.set_active(service_id: int, enabled: bool) -> None` | method | `service.read` | Switch the plugin on or off for one service. An admin's decision always wins. |
| `ctx.settings.all() -> dict` | method |  | Every setting, secrets decrypted. |
| `ctx.settings.get(key: str, default: Any = None) -> Any` | method |  | One setting value, or the default when unset. Secrets are decrypted. |
| `ctx.state(service_id: int) -> StateView` | method |  | Per-service key-value state. The plugin must be active for the service. |
| `ctx.store` | attribute |  | Plugin-wide key-value state. |
| `ctx.veto(reason: str) -> Veto` | method |  | Build the result a before hook returns to refuse the operation. |
| `await ctx.vm.power(service_id: int, action: str) -> bool` | method | `vm.power` | Power a VM on, off, shut it down, reboot or reset it. |
| `state.all() -> dict[str, Any]` | method |  | Every key and value of this scope. |
| `state.delete(key: str) -> None` | method |  | Remove a key. |
| `state.get(key: str, default: Any = None) -> Any` | method |  | A value, or the default when the key is missing. |
| `state.keys(prefix: str = ) -> list[str]` | method |  | Keys of this scope, sorted, optionally those starting with a prefix. |
| `state.set(key: str, value: Any) -> None` | method |  | Store a JSON value. Committed when the handler returns. |

`state.*` is the interface of `ctx.store` (plugin-wide) and of `ctx.state(service_id)` (one service).

## Provider kinds

From API 1.1 a plugin can also offer typed providers that Rackflow calls directly. Declare each in the manifest under `provides` and bind it in `register` with `@plugin.provider(kind, id)`. The decorated class (or a factory function) is instantiated once per load with no arguments. The types below are importable from `rackflow_plugin.kinds`; they are frozen dataclasses, amounts are integer minor units and tax rates are strings with at most four decimals.

A provider method receives only its arguments. To read settings, write the plugin log or make outbound HTTP calls, a provider calls `rackflow_plugin.kinds.current_context()` while a method runs; it returns the usual `ctx` (with `ctx.kind == "provider"`). A method may be a plain function or a coroutine.

| Kind | Permission | Methods | What it is for |
|---|---|---|---|
| `currency_rates` | `currency.rates` | `rates` | Offers the published exchange rates of one month. Billing stores each month once and never changes a stored rate. |
| `tax_id_validator` | `tax.validate_id` | `supports`, `check_format`, `check_online` | Checks a customer's tax or VAT number. Billing asks `supports`, then `check_format`, then `check_online`. |
| `tax_provider` | `tax.calculate` | `supports`, `calculate` | Calculates the tax of a basket. Billing falls back to its own rules when the provider fails. |
| `tax_rule_pack` | `tax.rule_pack` | `pack_id`, `versions`, `load` | Offers a set of tax rules as data. An administrator reviews and applies it; nothing is applied automatically. |

### currency_rates

| Method | Arguments | Notes |
|---|---|---|
| `rates` | `(request: CurrencyRatesRequest) -> CurrencyRatesResult` | Return a `CurrencyRatesResult` for the requested month: units of each currency per one unit of `base`. A month not published yet is an empty `rates`, not an error. |

### tax_id_validator

| Method | Arguments | Notes |
|---|---|---|
| `supports` | `(country: str, id_type: str) -> bool` | Whether the validator handles this country and ID type. Local and fast. |
| `check_format` | `(country: str, tax_id: str) -> bool` | Whether the number is well formed for the country. Local, no network. |
| `check_online` | `(request: TaxIdCheckRequest) -> TaxIdCheckResult` | Ask the registry. Return a `TaxIdCheckResult`; `unavailable` needs an `error_code`. |

### tax_provider

| Method | Arguments | Notes |
|---|---|---|
| `supports` | `(request: TaxProviderRequest) -> bool` | Whether the provider can handle this request (country, currency, lines). |
| `calculate` | `(request: TaxProviderRequest) -> TaxProviderResult` | Return a `TaxProviderResult` with every requested line key. |

### tax_rule_pack

| Method | Arguments | Notes |
|---|---|---|
| `pack_id` | `() -> str` | The pack's id, matching `^[a-z][a-z0-9_]{0,63}$`. |
| `versions` | `() -> list[str]` | Available versions, newest last. |
| `load` | `(version: str) -> dict` | The pack JSON of one version. At most 1 MB. |

### Types

| Type | Field | Type of field | Required |
|---|---|---|---|
| `TaxIdCheckRequest` | `country` | `str` | yes |
| `TaxIdCheckRequest` | `tax_id` | `str` | yes |
| `TaxIdCheckRequest` | `id_type` | `str` | yes |
| `TaxIdCheckRequest` | `requester_country` | `str or None` | no |
| `TaxIdCheckRequest` | `requester_tax_id` | `str or None` | no |
| `TaxIdCheckResult` | `status` | `"valid" or "invalid" or "unavailable"` | yes |
| `TaxIdCheckResult` | `checked_at` | `datetime` | yes |
| `TaxIdCheckResult` | `request_ref` | `str or None` | no |
| `TaxIdCheckResult` | `registered_name` | `str or None` | no |
| `TaxIdCheckResult` | `registered_address` | `str or None` | no |
| `TaxIdCheckResult` | `error_code` | `str or None` | no |
| `TaxProviderLine` | `key` | `str` | yes |
| `TaxProviderLine` | `net_cents` | `int` | yes |
| `TaxProviderLine` | `tax_class` | `str` | yes |
| `TaxProviderLine` | `kind` | `str` | yes |
| `TaxProviderLine` | `product_code` | `str or None` | no |
| `TaxProviderRequest` | `tax_point_at` | `datetime` | yes |
| `TaxProviderRequest` | `currency` | `str` | yes |
| `TaxProviderRequest` | `prices_include_tax` | `bool` | yes |
| `TaxProviderRequest` | `seller` | `dict` | yes |
| `TaxProviderRequest` | `buyer` | `dict` | yes |
| `TaxProviderRequest` | `lines` | `tuple[TaxProviderLine, ...]` | yes |
| `TaxProviderPart` | `name` | `str` | yes |
| `TaxProviderPart` | `rate_percent` | `str` | yes |
| `TaxProviderPart` | `jurisdiction` | `str` | yes |
| `TaxProviderPart` | `base_cents` | `int` | yes |
| `TaxProviderPart` | `tax_cents` | `int` | yes |
| `TaxProviderPart` | `reverse_charge` | `bool` | no |
| `TaxProviderPart` | `legal_note` | `str or None` | no |
| `TaxProviderResult` | `lines` | `dict[str, tuple[TaxProviderPart, ...]]` | yes |
| `TaxProviderResult` | `reference` | `str or None` | no |
| `CurrencyRatesRequest` | `base` | `str` | yes |
| `CurrencyRatesRequest` | `period` | `str` | yes |
| `CurrencyRate` | `currency` | `str` | yes |
| `CurrencyRate` | `units_per_base` | `str` | yes |
| `CurrencyRatesResult` | `source` | `str` | yes |
| `CurrencyRatesResult` | `period_start` | `date` | yes |
| `CurrencyRatesResult` | `period_end` | `date` | yes |
| `CurrencyRatesResult` | `rates` | `tuple[CurrencyRate, ...]` | yes |

Every answer is checked before billing sees it; see [Calls to providers](#calls-to-providers) for what is checked and what happens when a check fails.

## Calls to providers

Billing reaches a provider only through `call_provider(kind: str, provider_ref: str, method: str, *args: Any, timeout_seconds: Optional[float] = None, db: Optional[Session] = None) -> Any`. `provider_ref` is `<plugin id>:<provider id>`. A plugin never sees this function; this section describes what the host does around each call so that a provider's author knows what to expect. The plugin's own code runs inside the call and cannot make `call_provider` raise anything but `ProviderUnavailable`.

### Time limit

- A call may run for the provider's `timeout_seconds` (8 by default, 1 to 30). The caller may pass a lower `timeout_seconds`, never a higher one, and never below 0.1 s.
- The limit starts when the plugin's code starts, so it includes loading the plugin (importing the module, `register` and creating the providers) when this process has not loaded it yet. Time spent asking Redis for the rate limit is taken off it.
- A coroutine is cancelled at the limit. A plain function cannot be stopped: the host stops waiting, counts a `timeout` and rolls back whatever the call wrote to plugin state; the thread runs on until the function returns.

### Slots and `busy`

- Every call runs on a thread of its own. A plugin may have at most 2 calls whose code is still running, counting calls that timed out but have not returned. A further call is refused with `busy` at once, without calling the plugin and without waiting in a queue behind it. A plugin that hangs therefore holds at most 2 threads and database sessions, and never slows calls to other plugins.
- `busy` is also the reason when the call's thread has not started within 2 s.
- A `busy` refusal is not a failure of the plugin, with one exception: when every slot is held by a call of that plugin that has already timed out, each refusal counts as a failure, so that a provider that hangs for good opens its circuit.

### Circuit breaker

- A `timeout`, an exception or an invalid answer is a failure: it is written to the plugin log and counted. 5 failures in a row open the circuit for 60 s, doubling up to 1800 s; 25 in a row disable the plugin.
- Only a valid answer from a method that does real work (`check_online`, `calculate`, `load`, `rates`) counts as a success and resets the count. A valid answer from `supports`, `check_format`, `pack_id` or `versions` does not, so a provider whose real calls keep failing cannot keep its circuit closed with cheap ones.
- When a failure opens the circuit, the audit entry `plugin.provider_failed` records the provider (`<kind>:<provider id>`), the number of failures, the seconds the circuit stays open and the error. The failure that disables the plugin writes `plugin.auto_disabled` instead.
- With a `db` argument the failure count, circuit state, log line and audit entry are written in a savepoint of the caller's transaction on SQLite, and committed on a separate session on other databases and without `db`. The plugin's own state writes during a call always use the plugin's own session.

### Rate limit

- The provider's `rate_limit` counts the methods that may do real work (`check_online`, `calculate`, `load`, `rates`). There is one counter for each provider, shared by all of its counted methods and by every Rackflow process through Redis. While Redis is down or does not answer within 0.25 s each process counts for itself.

### Reasons

`call_provider` raises only `ProviderUnavailable`, whose `reason` is one of these. A call never leaks the plugin's own exception, whatever its message does. Reasons marked failure count against the plugin's health.

| Reason | Failure | Meaning |
|---|---|---|
| `circuit_open` | no | The plugin's circuit is open after repeated failures. Refused without calling the plugin. |
| `rate_limited` | no | The provider's `rate_limit` is used up. Refused without calling the plugin. |
| `busy` | only when hung | The plugin already has the most calls it may run at once, or the call's thread did not start in time. |
| `timeout` | yes | The method did not return within its time limit. |
| `error` | yes | The method raised an exception, or the host failed unexpectedly while making the call. |
| `invalid_answer` | yes | The method returned something that failed the checks below. |
| `disabled` | no | The plugin is not enabled. |
| `not_installed` | no | No plugin with that id is installed. |
| `not_loaded` | no | The plugin's manifest or code could not be loaded. |
| `not_permitted` | no | The plugin was not granted the permission of the provider's kind. |
| `unknown_provider` | no | The plugin does not declare that provider, or the reference is not `<plugin id>:<provider id>`. |
| `unknown_method` | no | The kind has no such method. |
| `bad_arguments` | no | The wrong number of arguments, or arguments that cannot be copied. |

### Answer checks

The plugin receives copies of its arguments, so nothing it does to them reaches billing. Its answer is rebuilt from new objects after checking, and checked against billing's own request, never the plugin's copy. Types are checked exactly: a subclass of a result class, of `str`, `int`, `dict` or `datetime`, and `bool` where a number is asked for, are refused. Any failed check is an `invalid_answer` and counts as a failure.

| Method | What is checked |
|---|---|
| `supports`, `check_format` | A real `bool`. |
| `tax_id_validator.check_online` | A `TaxIdCheckResult` with `status` `valid`, `invalid` or `unavailable`. `checked_at` is a `datetime` from 2000 to one day ahead of now (without a time zone it counts as UTC). `unavailable` needs an `error_code`. `error_code` and `request_ref` are at most 64 characters; `registered_name` and `registered_address` are cut to 512 characters. |
| `tax_rule_pack.pack_id` | Text matching `^[a-z][a-z0-9_]{0,63}$`. |
| `tax_rule_pack.versions` | A list of at most 500 non-empty texts of at most 32 characters. |
| `tax_rule_pack.load` | A `dict` that is JSON (no `NaN` or infinity) and at most 1 MB as JSON text. |
| `tax_provider.calculate` | A `TaxProviderResult` whose `lines` has exactly the keys of the request: none missing, none unknown. Each part is a `TaxProviderPart` with `name` (at most 255 characters) and `jurisdiction` (at most 64) not empty, `legal_note` at most 2,000, `reference` at most 128. `base_cents` and `tax_cents` are `int`, from 0 to 9,223,372,036,854,775,807 (2^63 - 1). `rate_percent` is a string of ASCII digits with at most three before the point and four after. Tax must be within one minor unit of `rate_percent` of `base_cents`, and must be 0 on a `reverse_charge` part. |
| `currency_rates.rates` | A `CurrencyRatesResult` whose `source` matches `^[a-z][a-z0-9_]{0,31}$`, whose `period_start` and `period_end` are exactly the first and last day of the requested month, and with at most 400 rates. Each currency is an upper case ISO alpha-3 code, not the base currency and not listed twice. `units_per_base` is an ASCII decimal above zero with at most twelve digits before the point and six after. |

## Handlers

`register(plugin)` binds handlers with these decorators. Every name must be declared in the manifest, and every declared name must be bound.

| Decorator | Declared in | Handler receives |
|---|---|---|
| `@plugin.on(event)` | `events` | `ctx, event` (the envelope) |
| `@plugin.before(event)` | `hooks` | `ctx, event`; return `None`, `ctx.veto(reason)` or `ctx.alter(fields)` |
| `@plugin.schedule(id)` | `schedules` | `ctx, service` (a service dict, or `None` for a global schedule) |
| `@plugin.action(id)` | `actions` | `ctx, request`; return `{"message": "..."}` |
| `@plugin.mcp_tool(id)` | `mcp_tools` | `ctx, params` |
| `@plugin.provider(kind, id)` | `provides` | a class or factory with the methods of its kind; see Provider kinds |
| `@plugin.lifecycle(step)` | none | `ctx, info`; steps: `install`, `enable`, `disable`, `upgrade`, `uninstall` |

Handlers may be plain functions or coroutines.

## Errors and results

| Name | Meaning |
|---|---|
| `ctx.errors.PluginError` | Base class of the errors below. |
| `ctx.errors.PermissionDenied` | The plugin was not granted the permission the call needs. |
| `ctx.errors.RateLimited` | A rate limit was reached; try again later. |
| `ctx.errors.NotActive` | The plugin is not active for that service, or the call does not suit its type or status. |
| `ctx.errors.GuestAgentError` | The VM or its guest agent could not be reached, or the command failed. |
| `ctx.errors.ActionError` | Raise from an action or tool to show a message to the user as a refusal (HTTP 409). |

## Deprecated items

Nothing is deprecated in this API version.

## Limits and defaults

| What | Value |
|---|---|
| Panels per plugin | 20 |
| Elements per panel | 40 |
| Longest label or help text | 500 characters |
| Longest text element | 2000 characters |
| Plugin id | `^[a-z][a-z0-9_]{2,47}$` |
| Setting, panel, action, schedule and tool ids | `^[a-z][a-z0-9_]{0,63}$` |
| State key | `^[A-Za-z0-9_.:-]{1,128}$` |
| Largest state value | 64 KB |
| State keys per plugin | 100,000 |
| Largest HTTP or guest command output | 64 KB |
| Before hook time limit | 2 s each, 3 s in total |
| After handler time limit | 30 s |
| Delivery attempts before a dead letter | 10 |
| Schedule starts per plugin per pass | 20 |
| Action calls per minute | 10 per plugin, action, service and caller |
| MCP tool calls per minute | 30 per plugin, tool and caller |
| Guest commands per minute | 6 per plugin and service, 60 per plugin |
| Providers per plugin | 10 |
| Provider call time limit | 8 s by default, at most 30 s (`timeout_seconds`); a caller may lower it, to 0.1 s at least |
| Provider calls running at once | 2 per plugin, then `busy` |
| Wait for a provider call's thread to start | 2 s, then `busy` |
| Wait for Redis on a provider rate limit | 0.25 s, then each process counts for itself |
| Largest tax rule pack | 1 MB |
| Versions of a tax rule pack | 500 |
| Rates in a currency rates answer | 400 |
| Largest amount in a tax answer | 9,223,372,036,854,775,807 minor units |
| Provider rate limit | set per provider in `rate_limit`, from 1 call per 86,400 s to 10,000 calls per second |
| Failures that open the circuit | 5 in a row |
| Failures that disable the plugin | 25 in a row |
| Circuit open time | 60 s, doubling to 1800 s |
