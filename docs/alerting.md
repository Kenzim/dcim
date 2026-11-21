# Alerting and notifications

Rackflow notices when something is wrong and tells staff, instead of staff
hearing about it from customers. Phase 1 is admin alerts. Phase 2 reuses the
same pieces so a client can opt in to being told about their own audit events
and their own services.

## Principles

- **Detect by checking recorded state.** A worker loop reads columns that
  other code already records (last seen times, error columns, job statuses).
  Event hooks exist only where a check cannot see the event (the client
  events in phase 2, and VM backup outcomes, which are not recorded today).
- **One lifecycle for every alert.** An alert opens when a condition starts,
  notifies once, stays quiet while it stays open, and sends a "resolved"
  notice when the condition clears.
- **Durable delivery.** Notices go through an outbox table with the same
  claim, lease and retry pattern as `NotificationOutbox`. The sender never
  runs inside the detection transaction.
- **Fail safe.** A detector that raises never resolves or opens anything. A
  channel that fails never blocks the others.

## Moving parts

| Piece | Where |
|---|---|
| Tables | `app/models/alerting.py` (Alembic revision `alerting_tables`, after `drop_proxy_access_requires_product`) |
| Alert type catalogue (defaults, parameters) | `app/services/alerting/catalogue.py` |
| Detectors | `app/services/alerting/detectors.py` |
| Lifecycle engine | `app/services/alerting/engine.py` |
| Fan-out to channels and recipients | `app/services/alerting/dispatch.py` |
| Rendering (email, webhook, Discord, Slack) | `app/services/alerting/render.py` |
| Delivery sender (claim, lease, retry) | `app/services/alerting/sender.py` |
| Outbound HTTP with address checks | `app/services/alerting/safe_http.py` |
| Retention | `app/services/alerting/retention.py` |
| VM backup outcome recording | `app/services/alerting/backup_watch.py` |
| Worker loop | `app/workers/alerts.py` |
| Staff API | `app/api/alerts.py` (`/api/admin/alerts`) |
| Client API | `app/api/client_notifications.py` (`/api/client/notifications`) |
| Client event hooks | `app/services/client_notifications.py` |
| Staff MCP | one new tool `alerts` in `app/mcp/tools/alerts.py` |
| Client MCP | `view="notifications"` on the existing `account` tool |
| Admin UI | `frontend/src/components/Alerts.svelte`, sidebar count |
| Client UI | `frontend/src/components/client/ClientNotifications.svelte` (Account tab) |

## Data model

All tables are new. Nothing existing changes shape.

- `alerts`: one row per occurrence of a condition.
  `alert_type`, `resource_type`, `resource_id`, `resource_label`, `severity`
  (`info`, `warning`, `critical`), `title`, `message`, `details` (JSON),
  `state` (`open` or `resolved`), `active_key` (unique, set only while open, so
  one resource has at most one open alert of a type), `opened_at`,
  `last_seen_at`, `resolved_at`, `clear_since` (hold-down start),
  `notified_at`, `last_notified_at`, `notify_count`, `resolve_notice_due_at`,
  `resolve_notified`, `flap_count`, `acknowledged_at`, `acknowledged_by`,
  `acknowledge_note`.
  `audience` is `staff` or `client`; `owner_user_id` is set for client alerts.
- `alert_silences`: `alert_type` (null means any), `resource_type` and
  `resource_id` (null means any), `starts_at`, `ends_at`, `reason`,
  `created_by`. A silence with a type and a resource is "silence this alert". A
  silence with only a resource is "this resource is in maintenance". A silence
  with only a type mutes a whole alert type for a period.
- `alert_type_settings`: one row per alert type that has been changed from the
  defaults: `alert_type`, `enabled`, `severity` (override), `params` (JSON),
  `reminder_interval_seconds` and `hold_down_seconds` (overrides).
- `alert_channels`: `name`, `kind` (`email`, `webhook`, `discord`, `slack`),
  `enabled`, `min_severity`, `alert_types` (JSON list, empty means all),
  `send_resolved`, `config` (JSON; secrets sealed with the existing Fernet
  helper when `SERVICE_INSTANCE_ENCRYPTION_KEY` is set).
- `alert_recipients`: staff users who receive email: `user_id` (unique),
  `enabled`, `min_severity`.
- `notification_deliveries`: the outbox for alerts and client notifications.
  `audience`, `alert_id`, `user_id` (client recipient), `channel_id`,
  `channel_kind`, `target` (address or channel name), `event`, `payload`
  (JSON, rendered at enqueue time so the sender is simple), `status`
  (`queued`, `sending`, `sent`, `failed`, `dead`, `skipped`), `attempts`,
  `next_attempt_at`, `error`, `claim_token`, `claim_expires_at`, `sent_at`,
  `idempotency_key` (unique), `created_at`.
- `client_notification_prefs`: per client user: `email_enabled`, `categories`
  (JSON map of category to boolean), `webhook_url` and `webhook_secret`
  (sealed), `webhook_error`, `updated_at`.
- `vm_backup_runs`: `service_id`, `upid`, `status` (`running`, `succeeded`,
  `failed`), `started_at`, `finished_at`, `error` (internal only). This is the one signal recorded for the first time, because a
  Proxmox backup outcome is not stored anywhere today.

Global tuning lives in `system_settings` under the key `alerting`
(`SystemSetting`, already used for platform configuration).

## Lifecycle

The worker evaluates every enabled alert type each cycle (default 60 s). A
detector returns the conditions currently true, each as a finding (type,
resource, severity, title, message, details). The engine then reconciles
findings with the open alerts of that type.

1. **Open.** A finding with no open alert creates one, and queues an
   `opened` notice unless the alert is silenced. Silenced alerts are still
   created and visible; when the silence ends, an alert that is still open,
   not acknowledged and never notified gets its `opened` notice.
2. **Stay open.** While the finding keeps coming, the alert is refreshed
   (`last_seen_at`, message, details) and nothing is sent. A reminder
   (`reminder`) is sent every `reminder_interval_seconds` while the alert is
   open, not acknowledged and not silenced. The default is off.
   If the severity rises, the alert is updated and an `escalated` notice is
   sent. A fall in severity updates the alert quietly.
3. **Hold-down.** When the finding stops, the alert is not resolved at once.
   `clear_since` is set, and the alert resolves only after the condition has
   stayed clear for `hold_down_seconds` (default 300). A condition that comes
   back inside the hold-down just clears `clear_since`; nobody is told
   anything. This is what stops a flapping condition from spamming.
4. **Resolve.** After the hold-down the alert becomes `resolved` and a
   `resolved` notice is queued for channels with `send_resolved`. Resolved
   alerts keep their row for history.
5. **Reopen window.** A condition that returns within
   `reopen_window_seconds` (default 1800) of a resolve reopens the same row
   (`flap_count` goes up) instead of making a new one. Notices for a row are
   sent while `flap_count` is below `flap_limit` (default 3). At the limit one
   `flapping` notice says notices are paused, and the `resolved` notice for a
   flapping alert is held until the row has stayed resolved for the whole
   reopen window (`resolve_notice_due_at`).
6. **Acknowledge.** Staff acknowledge an open alert (with an optional note).
   It stays open, stops reminding, and drops out of the navigation count.
   Acknowledgement ends when the alert resolves.
7. **Silence.** See `alert_silences`. A silenced alert sends nothing (open,
   reminder, resolved) and is excluded from the navigation count.
8. **Storm control.** When one cycle produces more than `storm_threshold`
   (default 5) notices for the same channel, that channel gets one `summary`
   delivery listing them rather than one delivery each.

If a detector raises, that type is skipped for the cycle: alerts of that type
are neither opened nor resolved.

Only one worker evaluates at a time: the worker takes a lease row (name
`alerts`) in `recurring_billing_leases`, the existing generic lease table, and
renews it each cycle.

## Detectors and default thresholds

Each type can be switched off, given another severity, and tuned in the admin
UI (Alerts, Settings). Defaults:

| Type | Default severity | Condition | Default parameters |
|---|---|---|---|
| `storage_node_offline` | critical | enabled storage node, no runner contact | `offline_after_seconds` 300 |
| `storage_node_ceph` | critical | online node reports `ceph_ok` false | none |
| `storage_node_error` | warning | online node with `last_error`, or an enabled SFTP gateway that is not listening or reports an error | `offline_after_seconds` 300 (a node past this is left to `storage_node_offline`) |
| `storage_tenant_error` | warning | tenant or share runner state `error`, or an active linked tenant that no node has served for longer than `unserved_after_seconds` while failover is active in its location | `min_age_seconds` 300, `unserved_after_seconds` 120 |
| `storage_tenant_failover` | warning | a tenant is served by a node other than its preferred node because of a failover (not a planned move or drain); resolves when it is back on its preferred node or staff make the current node preferred | none |
| `storage_location_degraded` | critical | a location with at least two enabled storage nodes has fewer than two healthy ones (online, Ceph healthy, lease client healthy, not fenced, not draining) | none |
| `storage_sftp_no_master` | critical | a location whose SFTP is behind a virtual IP (with failover active) has had no online node reporting to be the SFTP master for the threshold, counted from the last settings change, SFTP master event or report of the last master | `no_master_after_seconds` (120) |
| `proxy_runner_offline` | warning | enabled proxy runner with no phone-home | `offline_after_seconds` 300 |
| `runner_offline` | critical | enabled DHCP, TFTP or media runner offline (Redis online key and `last_seen_at`) | `offline_after_seconds` 300 |
| `pdu_poll_failed` | warning | enabled metered PDU with a poll error, or no successful poll | `stale_after_seconds` 600 |
| `power_feed_near_limit` | warning | feed current at or above this share of the usable limit | `warn_percent` 90 |
| `power_feed_over_limit` | critical | feed current above the usable limit (`max_utilisation_percent`) | none |
| `switch_unreachable` | critical | enabled switch whose newest port status is stale | `stale_after_seconds` 900 |
| `switch_vpc_inconsistent` | warning | vPC pair data that does not agree between peers, or a vPC detection error | none |
| `deployment_job_failed` | warning | latest job of a service failed recently | `lookback_hours` 24 |
| `deployment_job_stuck` | warning | job queued, retrying, waiting or running too long (queued means no worker) | `queued_after_minutes` 15, `retrying_after_minutes` 30, `waiting_after_hours` 6, `running_after_minutes` 30 |
| `boot_task_stuck` | warning | boot task pending too long | `pending_after_minutes` 30, `max_age_hours` 72 |
| `install_failed` | warning | latest install task of a server failed recently | `lookback_hours` 24 |
| `install_stuck` | warning | install task in progress too long | `stuck_after_minutes` 120 |
| `private_network_error` | warning | attachment in `error`, a detach that keeps failing, or an attach still pending after `min_age_minutes` | `min_age_minutes` 10 |
| `vm_backup_failed` | warning | a recorded VM backup run failed recently | `lookback_hours` 24 |
| `notification_delivery_failing` | warning | billing outbox, account email or alert deliveries failing, or queued and unsent (queue checks only while SMTP is on) | `failed_threshold` 3, `window_minutes` 60, `queued_after_minutes` 30 |
| `client_storage_quota` | info, client audience | a share of a client's storage at or above `quota_percent` of its quota; never shown in the staff list, it only feeds the client's `storage_quota` notice | `quota_percent` 90 |
| `billing_invoice_lines_mismatch` | warning, ships disabled | an open, overdue, paid, pending-action or failed invoice with lines whose totals do not add up to the invoice total; invoices without lines are skipped | `min_age_minutes` 60, `lookback_days` 90, `max_rows` 25 |
| `billing_paid_without_payment` | warning, ships disabled | a paid invoice whose succeeded payments do not cover its total (refunded payments do not count) | `min_age_minutes` 60, `lookback_days` 90, `max_rows` 25 |
| `billing_overallocated` | critical, ships disabled | an invoice whose succeeded payments add up to more than its total | `min_age_minutes` 60, `lookback_days` 90, `max_rows` 25 |
| `billing_credit_drift` | critical, ships disabled | a reseller with ledger entries whose cached balance differs from the sum of its entries or from the newest entry's balance after | `max_rows` 25 |
| `billing_orphaned_payment` | warning, ships disabled | a pending payment with a gateway reference that has stayed pending, or a pending payment whose invoice is already paid or void | `pending_after_minutes` 1440, `settled_grace_minutes` 60, `max_rows` 25 |
| `billing_refund_stuck` | warning | a refund that has stayed pending (reads a `refunds` table; reports nothing until refunds are recorded in one) | `pending_after_minutes` 60, `max_rows` 25 |

Resource keys: `storage_node`, `storage_tenant`, `proxy_runner`, `runner`,
`pdu`, `power_feed`, `switch`, `deployment_job`, `boot_task`,
`install_task`, `private_network_attachment`, `service` (backups), `system`,
`invoice`, `payment`, `reseller`, `refund` (billing integrity).
A silence uses the same pair, so a maintenance silence on `switch` 4 mutes
every alert about that switch.

### Billing integrity types

The six `billing_*` types implement the integrity checks of the billing spec
(E2). They live in `app/services/alerting/billing_integrity.py` and only read
recorded state: they never change a record, call a gateway or emit an event.

- Each detector aggregates in SQL (`GROUP BY` and `HAVING` over the invoice,
  payment, invoice line and credit ledger tables, using their existing
  indexes) and runs on SQLite and MySQL alike.
- Money is compared in integer minor units. A payment counts only when its
  status is `succeeded`; a payment recorded as `refunded` no longer counts, so
  a fully refunded paid invoice is reported by `billing_paid_without_payment`.
- Each offending record is one finding (resource `invoice`, `payment`,
  `reseller` or `refund`), with the ids and amounts in the alert details. At
  most `max_rows` records are listed per cycle. When there are more, one extra
  finding with resource id `overflow` carries the number not listed
  (`remaining`) and the number listed (`shown`); fixing the listed records
  brings the next ones into view.
- `min_age_minutes` keeps a just-created invoice from being flagged in the
  middle of its own transaction, and `lookback_days` bounds the scan to recent
  invoices.
- Paid invoices without a payment row are legitimate today (admin mark-paid and
  credit-funded settlement record no payment), so `billing_paid_without_payment`
  would page on existing data. It, `billing_invoice_lines_mismatch`,
  `billing_overallocated`, `billing_credit_drift` and `billing_orphaned_payment`
  therefore ship disabled: enable each one in Alerts, Settings after checking
  what it reports on your data. A type with no settings row is enabled unless
  its definition says it ships disabled (`enabled_by_default`).
- Credit drift covers resellers. Retail `billing_accounts.credit_balance_cents`
  has no ledger to compare with yet.
- `billing_refund_stuck` has nothing to read until refunds are stored in their
  own `refunds` table (columns `id`, `status`, `created_at`); until then it
  reports nothing.

Not detectable from recorded state today (follow-ups, not built): the IPMI
proxy runner (it keeps no heartbeat), BMC reachability per server, Proxmox
cluster reachability, Redis or database health, disk space on the media
runner, and a dead-man switch for the worker itself (the admin UI shows the
last evaluation time and warns when it is old).

## Channels and recipients

- **Email** uses the existing SMTP settings. When SMTP is disabled the
  delivery is marked `skipped`, as `NotificationOutbox` does, and the admin UI
  says so.
- **Webhook** posts JSON: `{"version":1,"event":...,"sent_at":...,"alerts":[...]}`.
  Headers `X-Rackflow-Event`, `X-Rackflow-Delivery` and, when a secret is
  set, `X-Rackflow-Signature: sha256=<hex HMAC of the body>`.
  The commerce webhook service signs with a stored hash and has no retry or
  worker, so it is not reused; the signing format is the same idea.
- **Discord** and **Slack** use the incoming webhook formats (an embed and an
  attachment) so the URL can be pasted straight in.
- Each channel has a minimum severity and an optional type filter. Staff users
  can also be recipients in their own right (email to their account address,
  with their own minimum severity).
- Details are truncated and secret-masked (`app/services/secret_masking.py`)
  before they leave the system.
- Retries back off 60 s doubling to one hour. After `max_attempts` (8) a
  delivery is `dead`. A dead delivery to one channel never stops the others.
- Staff webhook URLs may point at internal hosts. Client webhook URLs may not
  (see below).

## Phase 2: client notifications

A client opts in per category, under Account, Notifications. Nothing is sent
until they opt in. Channels are the account email and optionally one webhook.

Categories and their sources:

| Category | Source |
|---|---|
| `sign_in` | audit `login_success` |
| `failed_sign_in` | audit `login_failed` (known account), `login_2fa_failed` |
| `two_factor` | audit `2fa_enabled`, `2fa_disabled`, `recovery_codes_regenerated`, `recovery_code_used`, `require_2fa_changed` |
| `password` | audit `password_reset_requested`, `password_reset_completed`, `password_set_by_admin` |
| `mcp_keys` | audit `client_mcp_key.*` |
| `sftp_login` | the storage runner's login report for the client's own storage |
| `service_suspended` | a service of theirs moves to suspended |
| `service_terminated` | a service of theirs moves to terminated |
| `storage_quota` | a share of theirs passes `storage_quota_percent` (default 90) of its quota; a client alert, so it opens once and closes when usage falls |
| `backup` | a backup run of theirs finishes or fails |
| `reinstall` | a reinstall of theirs finishes or fails |
| `network_sharing` | an invitation to a shared private network, an answer to or a removal from one of theirs, or a shared network ending; fixed sentences only (`docs/private-network-sharing.md`) |

Hooks: `AuditService.log` calls the client hook after it writes the audit row
(fail-open, inside a savepoint). Service status changes are caught in one
place with a SQLAlchemy `before_flush` listener, so every code path that
suspends or terminates a service is covered, including future ones. Reinstall
and backup outcomes come from the deployment job result and `vm_backup_runs`.

Scope rules, enforced in `app/services/client_notifications.py`:

- A notification is addressed to exactly one user, found from the event's
  subject (audit) or the service owner. An event with no known client is
  dropped.
- Payloads are built from an allow-list: category, a fixed sentence, time, and
  for audit events the client's own IP address and user agent; for service
  events the service id and the service name the client already sees in the
  portal. They never carry server, node, host, cluster, runner or switch
  names, other clients' data, staff user names, error text or internal ids.
  Failures say "failed" and point to the portal or support.
- Staff-side alerts and client notifications share the outbox table but never
  share a code path for building content.
- Rate limit: at most `client_rate_limit_per_hour` (default 20) deliveries per
  client per channel kind per hour; extra events are dropped and logged.
- Client webhook URLs must be `https`, have no credentials, and resolve to
  public addresses. The check runs when the URL is saved and again on every
  send, and the connection is pinned to the checked address so a DNS change
  cannot redirect it. Redirects are not followed. Each client webhook has its
  own signing secret, generated by Rackflow and shown once.
- Preferences cannot be changed in an impersonation session.

## Retention

The worker prunes about once an hour, in batches of 1,000 rows so locks stay
short:

| Data | Kept |
|---|---|
| Resolved alerts | 90 days after `resolved_at` (`alert_retention_days`) |
| Expired silences | 30 days after they end |
| Sent and skipped deliveries | 30 days (`delivery_retention_days`) |
| Dead and failed deliveries | 90 days |
| Finished `vm_backup_runs` | 90 days |

Open alerts are never pruned. No new table is left to grow without bound.
`user_audit_events` has no retention today; that is outside this change and
noted as a follow-up.

## Interfaces

**Staff API** (`require_admin`): `GET /api/admin/alerts` (filters, paging),
`GET /summary` (counts for the navigation badge, last evaluation time, SMTP
state), `GET /{id}`, `POST /{id}/acknowledge`, `POST /{id}/unacknowledge`,
`POST /{id}/silence`, `GET|POST|DELETE /silences`, `GET /types`,
`PUT /types/{type}`, `GET|PUT /settings`, `GET|POST|PUT|DELETE /channels`,
`POST /channels/{id}/test`, `GET|PUT /recipients`, `GET /deliveries`,
`POST /evaluate`.

**Client API** (client session): `GET /api/client/notifications`,
`PUT /api/client/notifications` (categories, email switch, `webhook_url`,
`regenerate_secret`; a new signing secret is in that response only) and
`POST /api/client/notifications/test`. Changes are written to the client's
audit log as `notification_prefs_changed`.

**Staff MCP.** One new tool, `alerts`, with `action` of `list`, `get`,
`acknowledge`, `unacknowledge`, `silence`, `unsilence` and `silences`.
**Client MCP.** `account` gains `view="notifications"` to read the settings and
an optional `notifications` argument to change categories and the email
switch (webhooks are portal-only). Tool-list sizes before and after are in the
change report and the ledger.

**Worker.** `python -m app.workers.alerts`, or in the app process with
`RUN_ALERT_WORKER=true` (the default for the app, because runner connection
state lives in the web process and Redis). Settings: `ALERT_WORKER_INTERVAL_SECONDS`
(60), `ALERT_WORKER_LEASE_SECONDS` (180), `ALERT_DELIVERY_MAX_ATTEMPTS` (8),
`ALERT_DELIVERY_BATCH_SIZE` (50), `ALERT_DELIVERY_TIMEOUT_SECONDS` (10). Delivery
runs every 15 seconds; evaluation, backup polling and pruning run on the
lease holder only.

## Not built

See "Not detectable from recorded state today" above. WHMCS gets no
preferences screen: client preferences live in the Rackflow portal.
