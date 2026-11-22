# Billing phase 2: design

Status: design for the phase 2 workers, written 2026-11-22 against main at `62a0296` and reviewed by the billing manager the same day (place of supply and registration choice, mirrored `tax_rates` behaviour, the per-row tax base for notes and refunds, cancellation through phase 1's helpers, and smaller points). Nothing here is built yet.

Phase 2 of `docs/billing-target-spec.md` (section 12) delivers:

- **C, the tax engine** (spec section 4): tax classes, rules with validity dates, seller registrations, exemptions, per-line tax records, legacy, shadow and v2 modes, an admin UI, the **UK VAT** and **EU VAT** rule packs and a **VIES tax-ID validator plugin** (spec section 13, decided default 3).
- **Plugin API 1.1**: the typed plugin kind mechanism (`provides`, `@plugin.provider`) of `docs/plugin-system.md` section 17.1, with three kinds only: `tax_id_validator`, `tax_rule_pack`, `tax_provider` (section 8 of this document lists every addition for the plugin developer guide).
- **B2 to B5, B7, B8, automation**: a scheduler with per-job off, dry run and live modes, invoices generated ahead of the due date, reminders, late fees, suspension and termination policy, cancellation requests.
- **F4, the email template editor**, with the senders that B4 and B7 need.

Phase 2 is done when it is merged to main, every new automation job ships `off`, each has a dry run and its own switch, and no job ever acts on a past trigger it would have handled before it was switched on (section 11.2 defines "retroactive" per job and how the code prevents it).

The document follows `docs/billing-phase1-design.md` (cited below as "P1" with its section number). Appendix A lists the phase 2 settings, appendix B the new named rounding rules, appendix C the legal matrix the packs implement.

## 0. Ground rules that apply to every section

Every rule of P1 section 0 carries over unchanged: integer minor units in `*_cents` columns with named rounding rules (no `round()`, no floats; tax rates are exact decimals converted to integers before any arithmetic, section 4.4); single writers (`InvoiceService` for `invoices` and `invoice_lines`, and now also for `invoice_line_taxes`; `InvoiceAdjustmentService` for notes and their new tax rows); idempotency keys on every money-changing entry point; frozen surfaces. In addition:

- **Lock order.** `LOCK_ORDER` in `app/services/billing_locks.py:45` is unchanged: service billing, reseller, billing account, order, invoice, payment, billing cycle, coupon, refund, number counter. Phase 2 adds no lockable money row kind; the new tables are either written in the same transaction as an already locked owner row (tax rows under the invoice lock) or are claim rows protected by a unique key (section 9.3). Every status, due date or balance check is made **after** the lock, through `BillingLocks` (which flushes and re-reads with `populate_existing`), never from a plain read taken earlier in the transaction: MariaDB REPEATABLE READ returns the transaction's snapshot for a plain read, and phase 1 found two production races from exactly that. The MariaDB 11.6+ snapshot isolation issue (ledger to-do 2ec3548c, error 1020 on `FOR UPDATE` after a plain read of the same row) makes the same rule doubly necessary: a job reads candidate ids without a lock, then locks each subject by id as its first statement on that row in the item transaction.
- **No external call under a lock.** VIES, tax providers and any plugin call run before any billing row lock is taken, as gateway calls already do (P1 section 0). Code that holds a lock and needs a tax answer uses the pure engine (section 4.3) on data it already has.
- **Engines and jobs.** The tax engine has `legacy`, `shadow` and `v2` modes and reaches `v2` only through the existing switch endpoint and its criterion (section 5). Automation jobs have `off`, `dry_run` and `live` per job, changed only through their own endpoint (section 9.2).
- **Published means frozen.** Tax on a published invoice never changes. `invoice_line_taxes` rows of a published invoice are frozen by the invoice guard (`app/services/invoice_guard.py`, P1 3.5) exactly as published lines are; a later rate change, exemption, tax-ID result or pack update affects only invoices published afterwards. Corrections are credit notes and new invoices.
- **Frozen surfaces.** `app/api/billing.py` and `whmcs/modules/servers/rackflow/` gain no route, field or meaning. `tests/contract/` does not change; the only WHMCS-visible behaviour change is listed in section 13. `/api/client/commerce/*`, `/api/reseller/*` and `/api/reseller-panel/*` only gain fields and routes. `tests/services/test_recurring_billing_service.py` stays green in every mode with every job `off`.
- **Reference path.** The reseller path is the reference implementation: today's reseller retry, grace, `block_new` and `suspend_all` behaviour (`app/models/reseller.py:67-70`, worker in `app/services/recurring_billing_service.py`) is not changed by any phase 2 default. Retail follows the reseller pattern.
- **Documentation.** Each worker updates the billing rows of `CODEBASE.md`; British spelling; no em dashes.

## 1. Schemas

All new tables are created by P2-S (section 14). Columns are `BigInteger` for `*_cents`, `DateTime(timezone=True)` for times, `String` enums stored as their value (the `_string_enum` pattern of `app/models/storefront.py`). Every table has `created_at` and `updated_at` unless noted.

### 1.1 `tax_classes` (new, `app/models/tax.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `code` | String(32), unique | `standard`, `reduced`, `zero`, `exempt`, `outside_scope` seeded; operators add more (for example `digital_service`) |
| `name` | String(128) | |
| `description` | String(512) null | |
| `system` | Boolean | seeded rows; cannot be deleted |

`outside_scope` is new against the spec list: credit top-ups and late fees use it (section 4.6), and it differs from `zero` and `exempt` on the invoice and in reports.

Lines get their class from `products.tax_class_id` (new nullable column, `app/models/storefront.py`, NULL means `standard`). Phase 1 already added `invoice_lines.tax_class_id` and `taxable` (`NewLine`, `app/services/invoice_service.py:160-175`); phase 2 starts filling them.

### 1.2 `tax_rules` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `name` | String(255) | shown on invoices, for example `UK VAT 20%` |
| `country` | String(2) null | ISO 3166-1 alpha-2; NULL matches any country; `XI` is refused by the admin API (Northern Ireland is `GB` for services, 4.2) |
| `region` | String(128) null | as `tax_rates.region` today |
| `postcode_pattern` | String(128) null | anchored regular expression on the normalised postcode; unused by the UK and EU packs |
| `tax_class_id` | FK `tax_classes` | |
| `customer_type` | String(16) | `any`, `business`, `consumer` |
| `rate_percent` | Numeric(9,4) | exact decimal percentage, 0 to 100 (14.975 is valid) |
| `priority` | Integer | higher wins among equally specific rules |
| `compound` | Boolean | applied to base plus the earlier taxes of the line |
| `compound_order` | SmallInteger | order among the taxes of one line |
| `jurisdiction` | String(16) | which seller registration it needs: `GB`, `EU_OSS`, an EU country code, or `ANY` (section 4.2) |
| `reverse_charge` | Boolean | the rule is a reverse charge rule: zero tax, legal note, evidence required |
| `reverse_charge_zone` | String(16) null | `EU` for intra-EU B2B; the buyer must be in the zone and outside the registration's country |
| `legal_note_key` | String(64) null | key into `tax_legal_notes` (1.8) |
| `rounding_point` | String(16) | `line` or `invoice_rate` (section 4.4) |
| `rounding_rule` | String(32) | a rule name of appendix B |
| `valid_from` | DateTime | inclusive |
| `valid_to` | DateTime null | exclusive |
| `enabled` | Boolean | |
| `source` | String(16) | `admin`, `pack`, `tax_rates` |
| `pack_id` | String(64) null | pack the row came from (`uk_vat`, `eu_vat`, or `<plugin_id>:<provider_id>`) |
| `pack_version` | String(32) null | |
| `pack_rule_key` | String(128) null | stable key of the rule inside the pack, for diffs |
| `legacy_tax_rate_id` | FK `tax_rates` null, unique | set on rows mirrored from `tax_rates` (section 3) |
| `in_effect_since` | DateTime null | informational: the first publish that used the rule (written by the daily tax scan, never on the publish path, so publishing takes no lock on rule rows) |
| `supersedes_id` | FK `tax_rules` null | the row this one replaced |

Indexes: `(country, tax_class_id, enabled)`, `(pack_id, pack_rule_key)`.

**Versioning.** A rule whose `valid_from` has passed is never edited in place: it may have been used by a publish, and checking usage would need a lock on rule rows from every publish. The admin API refuses (409 `tax_rule_in_use`) and offers supersede: close the old row with `valid_to = T` and insert a new row with `valid_from = T` and `supersedes_id`. `T` may not be in the past (`T >= now`), so a supersede never changes the rate a past tax point used.

### 1.3 `seller_tax_registrations` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `jurisdiction` | String(16) | `GB`, an EU country code (domestic registration), `EU_OSS` |
| `scheme` | String(16) | `domestic`, `oss_union`, `oss_non_union` |
| `tax_number` | String(64) | printed on invoices issued under it |
| `country` | String(2) | country of establishment of the registration (the OSS member state of identification for OSS) |
| `valid_from`, `valid_to` | DateTime, DateTime null | |
| `invoice_series_id` | FK `document_series` null | default series for invoices issued under this registration (P1 1.1); NULL uses the default series |
| `invoice_profile_id` | FK `invoice_profiles` null | |
| `enabled` | Boolean | |

The seller block of the invoice (`seller_snapshot`, `app/services/invoice_service.py:299-308`) gains the registration's `tax_number` and `jurisdiction`; `company_tax_id` from settings stays as the fallback when no registration applies, so a legacy invoice prints exactly as today.

### 1.4 `tax_exemptions` (new)

`id`, `scope` (`account`; `client_group` is reserved for D5 and refused in phase 2), `billing_account_id` FK, `reason` (String 255), `certificate_ref` (String 128 null), `valid_from`, `valid_to` null, `tax_class_ids` (JSON list, empty means every class), `created_by_user_id`, `revoked_at` null. Written only by the admin API (section 6.2), audited `billing.tax_exemption.created` and `.revoked`.

The existing `billing_accounts.tax_exempt` flag (`app/models/commerce_account.py:92`) keeps its meaning: an account with it set is exempt from every class, with `billing_accounts.tax_exempt_reason` (new, String 255 null) as its reason. Phase 2 gives it its first admin write path.

### 1.5 `invoice_line_taxes` (new)

One row per tax applied to one invoice line.

| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger PK | |
| `invoice_id` | FK `invoices` | denormalised for the summary query and the guard |
| `invoice_line_id` | FK `invoice_lines` | |
| `tax_rule_id` | FK `tax_rules` null | NULL for a provider result |
| `position` | SmallInteger | compound order |
| `name` | String(255) | snapshot |
| `rate_percent` | Numeric(9,4) | snapshot |
| `jurisdiction` | String(16) | snapshot |
| `base_cents` | BigInteger | net amount taxed (after line discounts; for compound taxes includes the earlier taxes) |
| `tax_cents` | BigInteger | |
| `reverse_charge` | Boolean | |
| `legal_note_key` | String(64) null | |
| `source` | String(32) | `rules`, `tax_rates`, `provider:<plugin_id>:<id>` |

Unique `(invoice_line_id, position)`. Invariant checked at publish and by the E2 detector `tax_line_sum` (section 15.4): for each line, Σ `tax_cents` of its rows equals `invoice_lines.tax_cents`.

The grouped summary the PDF and API show is not stored separately: `invoices.tax_summary` (JSON, written at publish, frozen) holds one entry per `(name, rate_percent, jurisdiction, reverse_charge)` with Σ base and Σ tax, so the PDF of a published invoice never re-derives it.

### 1.6 `invoice_adjustment_line_taxes` (new)

The same columns as 1.5 with `invoice_adjustment_id` and `invoice_adjustment_line_id`, plus `source_line_tax_id` (FK `invoice_line_taxes`, the row it reverses). Written only by `InvoiceAdjustmentService`.

### 1.7 `invoices`, `orders`, `billing_profiles`, `billing_accounts`: new columns

| Table | Column | Type | Notes |
|---|---|---|---|
| `invoices` | `tax_engine` | String(16) null | `legacy`, `rules`, `provider:<id>`; NULL on rows before phase 2 |
| `invoices` | `tax_point_at` | DateTime null | the date whose rates applied (section 4.5); set at publish |
| `invoices` | `tax_summary` | JSON null | 1.5 |
| `invoices` | `tax_evidence` | JSON null | buyer country, region, business flag, tax ID and its check reference, IP country, sources and conflicts (section 7.6) |
| `invoices` | `legal_notes` | JSON null | list of `{key, text}` rendered at publish in the invoice locale |
| `invoices` | `seller_registration_id` | FK null | |
| `orders` (`app/models/commerce_order.py`) | `tax_evidence` | JSON null | captured at checkout (IP, country sources) |
| `billing_profiles` (`app/models/commerce_account.py:108-138`) | `tax_id_type` | String(16) null | `eu_vat`, `gb_vat`, `other` |
| `billing_profiles` | `tax_id_status` | String(16) | `unchecked` (default), `valid`, `invalid`, `unavailable`, `format_invalid` |
| `billing_profiles` | `tax_id_checked_at` | DateTime null | |
| `billing_profiles` | `tax_id_check_id` | FK `tax_id_checks` null | the check that set the status |
| `billing_profiles` | `is_business` | Boolean | default false; true when the client says it buys as a business |
| `billing_accounts` | `tax_exempt_reason` | String(255) null | 1.4 |
| `products` | `tax_class_id` | FK null | 1.1 |
| `price_plan_cycles` | `invoice_lead_days` | SmallInteger null | per-cycle override of B3 (section 10.1) |

All new columns are nullable or have server defaults; none is backfilled on existing invoices (P1 2.1).

### 1.8 `tax_legal_notes` and `invoice_profiles` (new)

`tax_legal_notes`: `key` (String 64), `locale` (String 16), `text` (String 1000), unique `(key, locale)`. Seeded by the packs (appendix C lists the keys and the English text). Editable by admins; the text is copied into `invoices.legal_notes` at publish, so editing never changes a published invoice.

`invoice_profiles`: `id`, `name`, `document_title` (default `Invoice`; for example `VAT invoice`), `proforma_title`, `credit_note_title`, `footer_text`, `locale`, `required_buyer_fields` (JSON list, for example `["legal_name","address_line1","country"]` checked at publish when the buyer is a business), `enabled`. One seeded profile `default` reproducing today's titles. The PDF reads the title from the snapshot written at publish; phase 2 only adds the title, footer and legal notes to the PDF, the rest of F6 stays in phase 4.

### 1.9 `tax_id_checks` (new)

Cache and evidence of tax-ID checks.

| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger PK | |
| `country` | String(2) | VIES member state code (`EL` is stored as `GR` with `vies_country = EL`) |
| `normalised_id` | String(64) | upper case, no spaces, dots or dashes, country prefix removed |
| `id_type` | String(16) | |
| `validator` | String(96) | `core_format` or `<plugin_id>:<provider_id>` |
| `status` | String(16) | `valid`, `invalid`, `unavailable`, `format_invalid` |
| `checked_at` | DateTime | |
| `expires_at` | DateTime | cache horizon (section 7.4) |
| `request_ref` | String(64) null | VIES consultation number when the requester's VAT number was sent |
| `registered_name`, `registered_address` | String(512) null | as returned; personal data, purged with the account by the GDPR service (`app/services/gdpr_service.py:209` already clears `tax_id`) |
| `error_code` | String(64) null | for example `MS_UNAVAILABLE`, `TIMEOUT`, `PLUGIN_ERROR` |
| `billing_account_id` | FK null | who asked |

Index `(country, normalised_id, validator, checked_at)`. Rows are evidence: they are never updated, only added.

### 1.10 Automation tables (new, `app/models/billing_jobs.py`)

`billing_job_states`: one row per job.

| Column | Type | Notes |
|---|---|---|
| `job` | String(64) PK | section 9.1 |
| `mode` | String(16) | `off` (default), `dry_run`, `live` |
| `dry_run_since` | DateTime null | dry-run watermark: set to now each time the mode becomes `dry_run`; cleared when it leaves `dry_run` (section 9.2) |
| `live_since` | DateTime null | watermark: set to now each time the mode becomes `live`; cleared when it leaves `live` |
| `config_hash` | String(64) null | SHA-256 of the job's settings as last seen by a live run |
| `config_since` | DateTime null | when the live run last saw `config_hash` change (section 11.2) |
| `last_run_id` | FK `billing_job_runs` null | |
| `changed_by_user_id`, `changed_at` | | |

`billing_job_runs`: `id`, `job`, `mode` (`dry_run` or `live`), `trigger` (`schedule` or `admin`), `owner` (process id string as the lease owner), `started_at`, `finished_at` null, `status` (`running`, `complete`, `failed`, `aborted`), `now_at` (the instant the run evaluated triggers against), `watermark_at` (the effective watermark used, section 11.2), `counts` (JSON: per action, planned or done, skipped, failed), `error` (String 1000 null).

`billing_job_run_items`: `id`, `run_id`, `subject_key` (String 128), `action` (String 64), `outcome` (`planned`, `done`, `skipped`, `failed`, `already_done`), `detail` (JSON: amounts, invoice and service ids, the reason for a skip). Written for dry runs and live runs alike, so a dry run and the following live run can be compared item by item. Kept 90 days (`billing_job_run_retention_days`).

`billing_job_actions`: the claim and idempotency record of live actions.

| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger PK | |
| `job` | String(64) | |
| `subject_key` | String(128) | for example `invoice:123:reminder:overdue_1` |
| `run_id` | FK | |
| `status` | String(16) | `claimed`, `done`, `failed`, `needs_review` |
| `claimed_at`, `finished_at` | | |
| `detail` | JSON null | |

Unique `(job, subject_key)`: the claim mechanism of section 9.3.

`billing_nonpayment_policies` (B7): `id`, `scope` (`retail_default`, `reseller_default`, `product`), `product_id` FK null, `suspend_after_days` SmallInteger null (NULL: never suspend), `terminate_after_days` SmallInteger null (NULL: never terminate), `termination_notice_days` SmallInteger (default 7), `unpaid_invoice_action_on_terminate` (`keep` or `cancel`), `enabled`. Unique `(scope, product_id)` with `product_id` stored as 0 for the two defaults (MariaDB unique indexes do not treat NULLs as equal). Seeded: `retail_default` and `reseller_default` with every day column NULL, which is today's behaviour (no retail suspension, reseller suspension left to the existing worker, no termination).

`cancellation_requests` (B8): `id`, `service_id` FK, `service_billing_id` FK null, `billing_account_id` FK null, `reseller_id` FK null, `type` (`end_of_period`, `immediate`), `reason` (String 1000 null), `requested_by_user_id`, `source` (`client`, `admin`, `reseller`), `status` (`pending`, `scheduled`, `completed`, `withdrawn`, `failed`), `effective_at`, `completed_at` null, `invoice_action` (`cancel_unpaid` or `keep`), `idempotency_key` unique, `failure` (String 1000 null).

### 1.11 `email_templates` (new, `app/models/commerce_email.py`) and `email_messages` columns

`email_templates`: `id`, `event_key` (String 64), `locale` (String 16, `en` seeded), `product_scope` (Integer, 0 for every product, else a product id; only the welcome event accepts a product), `subject` (String 255), `body_text` (Text), `body_html` (Text null), `enabled` (Boolean; a disabled template means "do not send this event"), `version` (Integer, increments on each save), `updated_by_user_id`, `updated_at`. Unique `(event_key, locale, product_scope)`.

`email_template_versions`: `id`, `template_id`, `version`, `subject`, `body_text`, `body_html`, `saved_by_user_id`, `saved_at`. Every save appends one; "reset to default" saves the built-in text as a new version.

`email_messages` gains `template_id` (null), `template_version` (null) and `render_fallback` (Boolean, default false: set when the stored template failed and the built-in default was used, section 12.3).

### 1.12 What phase 2 does not change

`tax_rates` stays as a read-through and compatibility table (section 3). `invoices.tax_cents`, `invoice_lines.tax_cents` keep their meaning. `service_billings` and `billing_cycles` gain no columns: generation ahead (B3) is computed from `next_charge_at` (`app/models/reseller.py:939`) and the lead days. Payment gateways do not move onto plugin kinds. B6 (saved retail methods, capture before due), B9 (grouping, continuous generation), A6 (proration and pro-rata refunds), D5 (client groups) and F6 (full PDF templates) stay in their phases.

## 2. Migration sequence

### 2.1 Principles

As P1 2.1: additive and nullable; one revision per concern; each revision up, down and up again on SQLite and on MariaDB 11.8 (`tests/migrations/`, one file per revision, and `tests/migrations/test_billing_p1_mariadb.py` extended or a sibling `test_billing_p2_mariadb.py`), and the full chain from an empty database (`tests/core/test_alembic_fresh_mariadb.py`). The current head is `billing_p1_ledger_owner_check` (P1 11.4); P2-S confirms with `alembic heads` before writing and chains from whatever single head main has then. Downgrades drop only what the revision added; data revisions (2.2, number 3) downgrade by deleting the rows they inserted, identified by `source` and `system`.

Ledger to-do e01f7dbc (fresh MariaDB `alembic upgrade head` stopped at `add_rack_units_servers`) is already fixed on main (`4343069`), and `tests/core/test_alembic_fresh_mariadb.py` already runs the whole chain from an empty MariaDB 11.8 database as the first step of the billing MariaDB CI job. P2-S changes no old revision; it only makes sure the phase 2 revisions pass that test.

### 2.2 Phase 2 revisions

1. `billing_p2_tax_schema`: `tax_classes`, `tax_rules`, `seller_tax_registrations`, `tax_exemptions`, `tax_legal_notes`, `invoice_profiles`, `tax_id_checks`, `invoice_line_taxes`, `invoice_adjustment_line_taxes`.
2. `billing_p2_tax_columns`: the columns of 1.7 on `invoices`, `orders`, `billing_profiles`, `billing_accounts`, `products`, `price_plan_cycles`.
3. `billing_p2_tax_seed`: seeds the five system classes, the `default` invoice profile, the English legal notes of appendix C, and mirrors every `tax_rates` row into `tax_rules` (section 3). No pack rules are inserted: packs are applied by an admin (section 7.2), so an installation with tax off stays with no rules.
4. `billing_p2_automation`: `billing_job_states` (one row per job of 9.1, mode `off`), `billing_job_runs`, `billing_job_run_items`, `billing_job_actions`, `billing_nonpayment_policies` (two seeded rows), `cancellation_requests`.
5. `billing_p2_email_templates`: `email_templates`, `email_template_versions`, the `email_messages` columns, and seeds one `en` template per event from the hard-coded texts (`app/services/email_message_service.py:32-`) plus the new events of 12.2.

No revision locks a money table for long: all are new tables or nullable column adds. The `tax_rates` mirror runs in revision 3 in one statement per row (installations have a handful).

## 3. Migration from `tax_rates` (read-through)

Today tax is a flat rate on the taxable subtotal of a checkout: `CheckoutService._tax_cents` (`app/services/checkout_service.py:206-229`) selects the enabled `TaxRate` (`app/models/commerce_coupon_tax.py:145-156`) for the profile's country, the region-specific row first, and returns `(taxable_cents * rate_bps) // 10_000`; `quote` (`:232`) and `_create_commerce_invoice` (`:334`) share it, and v2 checkout invoices spread it over lines with `tax_split_largest_remainder` (`:397-418`, P1 appendix B). Cycle, deploy and top-up invoices carry no tax today (`InvoiceService.create_cycle`, `create_deploy`, `create_topup`, `app/services/invoice_service.py:767-845`).

- **Mirror.** Each `tax_rates` row becomes a `tax_rules` row: class `standard`, `customer_type = any`, `rate_percent = rate_bps / 100`, `priority = 0` (`10` for a row with a region, so the region row wins as today), `jurisdiction = ANY`, `rounding_point = invoice_rate`, `rounding_rule = tax_floor` (appendix B; it reproduces `// 10_000` exactly), `valid_from = 1970-01-01`, `enabled` copied, `source = tax_rates`, `legacy_tax_rate_id` set.
- **Read-through.** In `legacy` and `shadow`, `tax_rates` stays authoritative. The `/tax-rates` admin routes (`app/api/commerce_store_admin.py:761-785`) keep their shapes and write `tax_rates` as today, and in the same transaction upsert the mirrored rule (`TaxRuleMirror.sync(db, tax_rate)` in `app/services/tax/mirror.py`): a change to a mirrored rule that is already used supersedes it from now (1.2), so the shadow comparison stays like for like.
- **After the switch.** In `v2`, the `/tax-rates` routes still work for one release as a façade over the mirrored rules and answer with a `Deprecation` header; the admin UI edits `tax_rules`. Removing them is a later contract step (P1 2.3 style).
- **Existing invoices** are never touched; their single tax total remains valid and their PDF prints as today (`app/services/invoice_pdf_service.py:112-114`).

## 4. Tax engine (C)

### 4.1 Modules

`app/services/tax/` (new package):

- `context.py`: `TaxContext`, `TaxLineInput`, `BuyerTaxFacts` (frozen dataclasses).
- `result.py`: `TaxResult`, `LineTax`, `LineTaxPart`, `TaxSummaryEntry`, `TaxEvidence`.
- `engine.py`: `TaxEngine.calculate(db, context) -> TaxResult` (pure apart from reads of rules, registrations and exemptions; no I/O beyond the database, no locks) and `TaxEngine.prepare(db, context) -> TaxResult` (may call a `tax_provider` plugin; never called under a billing lock).
- `rules_repo.py`: rule selection (4.3).
- `mode.py`: `tax_engine_mode(db) -> str`, through `billing_engine_mode(db, "tax")` (P1 11.3), so the switch record is honoured.
- `shadow.py`: the comparisons of section 5.
- `mirror.py`: section 3.
- `validators.py`: `TaxIdService` (section 7.4).
- `packs/` and `pack_import.py`: section 7 (owned by P2-P).
- `legal_notes.py`: rendering of note keys in a locale.

### 4.2 The context

```python
@dataclass(frozen=True)
class BuyerTaxFacts:
    country: str | None            # billing profile country, upper case
    region: str | None
    postcode: str | None
    is_business: bool              # billing_profiles.is_business
    tax_id: str | None
    tax_id_type: str | None
    tax_id_status: str             # as stored; "valid" counts only if checked within billing_tax_id_max_age_days
    tax_id_check_id: int | None
    exempt_all: bool               # billing_accounts.tax_exempt
    exemption_ids: tuple[int, ...] # active tax_exemptions at the tax point

@dataclass(frozen=True)
class TaxLineInput:
    key: str                       # caller's stable id for the line ("line:0", "item:42")
    net_cents: int                 # quantity * unit, after line discounts; gross when prices_include_tax
    tax_class_code: str            # "standard" when the product has none
    kind: str                      # InvoiceLineKind value

@dataclass(frozen=True)
class TaxContext:
    tax_point_at: datetime
    currency: str
    prices_include_tax: bool
    buyer: BuyerTaxFacts
    lines: tuple[TaxLineInput, ...]
    purpose: str                   # InvoicePurpose value or "quote"
    owner: str                     # "retail" or "reseller"
    evidence: Mapping[str, Any]    # ip_country and other location evidence (7.6)
```

`TaxContextBuilder.for_checkout(db, account, profile, lines, now, evidence)`, `.for_invoice(db, invoice, lines, now)` and `.for_adjustment(db, invoice, adjustment_lines)` build it; they read the profile and account **without** locks (the tax facts are not money rows; the invoice row lock at publish is what freezes the result).

**Northern Ireland.** For services, Northern Ireland is the UK. When `billing_tax_enabled` is true, `normalise_country` maps `XI` to `GB` (and `EL` to `GR`) for the buyer, IP and seller countries and for rule countries. A valid `XI` VAT number counts as a UK business. With tax disabled, codes stay literal so that mirrored-only matches `tax_rates`. The context and its hash keep the raw code. The evidence records the mapped code as `buyer_country` and the original as `buyer_country_declared`, which the stored-context rebuild uses. The tax admin API refuses `XI` as a rule country and tells the admin to use `GB`.

### 4.3 Calculation

`TaxEngine.calculate` does, in order:

1. **Scope.** If the purpose is out of scope (section 4.6), every line gets no tax rows and the result says `engine = "rules"`, `scope = "out_of_scope"`. If `billing_tax_enabled` is false, only the mirrored rules (`source = tax_rates`, `jurisdiction = ANY`) are considered, and steps 2 and 5 are skipped: this reproduces today's checkout tax, which applies whenever an enabled `tax_rates` row matches and has no global switch (`checkout_service.py:206-229`). `billing_tax_enabled` therefore means "pack and admin rules, registrations and reverse charge apply", not "tax is charged". Requirement (P2-T test): on an installation with only mirrored rules and `billing_tax_enabled` false, shadow records no `t1_checkout` difference for a table of checkouts covering an exempt account, a region row, a profile with no country, a coupon by amount and by percent, and a zero subtotal.
2. **Place of supply and registration.** Setting `billing_tax_seller_country` (appendix A) is the seller's country of establishment, and its registration (the enabled `domestic` registration of that country valid at `tax_point_at`) is the **establishment registration**. The engine decides, in order:
   1. Buyer in the seller's country: the establishment registration; that country's rules.
   2. Buyer is a business whose tax ID is `valid` and fresh, in another country: the place of supply is the buyer's country (general B2B rule), and the seller does not charge that country's VAT. The invoice is issued under the establishment registration (its number is printed), every line gets a zero-tax row with `reverse_charge = true`, and the legal note comes from the establishment's pack: `eu_reverse_charge` when seller and buyer are both in the EU, `gb_outside_scope_b2b` from a UK seller to an EU business, `outside_scope` for any other buyer country. When no reverse-charge rule matches, the engine still gives each taxed line a zero-tax row with `reverse_charge = true`, named after its legal note, under the establishment registration's jurisdiction (or `ANY` with no registration) and with that note. The row counts as explained in shadow (`t1_rule_divergence`). `ANY` rules never apply to a business buyer abroad with a valid fresh tax ID, because that supply is taxed where the buyer is and the seller does not charge it.
   3. Otherwise (a consumer, or a business without a valid fresh tax ID, in another country), place of supply for electronically supplied services is the buyer's country: a `domestic` registration of the buyer's country if the seller holds one (its rules apply, for example GB 20% for an EU seller registered in the UK); else `EU_OSS` when the buyer is in the EU and the seller holds an OSS registration (the buyer country's rules under OSS); else, for an EU-established seller and an EU buyer, the establishment registration and the seller's own country's rules (the under-threshold case of appendix C; the threshold itself is not tracked); else no tax, the legal note `outside_scope` when the buyer is outside the EU and the UK, and for an EU or UK buyer the evidence note `not_registered` plus the alert detector `tax_not_registered` (no legal note, because the supply is taxable there and the seller is not registered for it).
   Rules are then matched only among those whose `jurisdiction` is the chosen registration's (or `ANY`). Appendix C is the acceptance table for this step: P2-P's golden tests run every row of it through `TaxEngine.calculate`. With no establishment country set, only `ANY` rules apply (today's behaviour), and the switch criterion (section 5) refuses `v2` when non-`ANY` rules are enabled.
3. **Exemption.** `exempt_all`, or an active exemption covering the line's class, makes the line exempt: no tax row, and the legal note `exempt_customer` with the exemption reason.
4. **Rule match.** For each line, enabled rules valid at `tax_point_at` (`valid_from <= t < valid_to`) whose country, region and postcode match the buyer (NULL matches anything), whose class is the line's class, and whose `customer_type` is `any` or matches `is_business`. Most specific wins (postcode, then region, then country, then none), then highest `priority`, then lowest `id`. Compound rules of the same specificity are all applied in `compound_order`. No matching rule: no tax.
5. **Reverse charge.** Case 2.2 above, implemented as pack rules with `reverse_charge = true` under the establishment registration's jurisdiction. Such a rule applies only if the buyer is a business, `tax_id_status` is `valid` and fresh, the buyer's country is in `reverse_charge_zone` (when set), and the buyer's country differs from the establishment registration's country. Then the line gets a tax row with `tax_cents = 0`, `reverse_charge = true` and the rule's legal note. If any condition fails, the buyer is handled as case 2.3, and the evidence records why (`tax_id_unchecked`, `tax_id_stale`, `tax_id_invalid`, `tax_id_unavailable`).
6. **Amounts.** Per line in compound order, with the rule's `rounding_point` and `rounding_rule` (4.4). Exclusive prices add the tax; inclusive prices derive it from the gross.
7. **Result.** `TaxResult` holds, per line key, the parts (rule id, name, rate, jurisdiction, base, tax, reverse charge, note key), the line tax total, the summary, the legal note keys of the invoice, the registration used, the evidence, the engine name and a `context_hash` (SHA-256 of the canonical context and the ids and versions of every rule, registration and exemption read).

**Line kinds.** Discount lines carry no tax (P1 1.3.1 forbids it). A coupon discount reduces the taxable base: the engine taxes each product line's net after its share of the discount, the share computed with `tax_split_largest_remainder` over the product lines (the rule P1 already uses for spreading). This changes nothing in legacy, where the flat tax is on the discounted taxable subtotal already.

### 4.4 Rates and rounding

`rate_percent` is converted once to an integer `rate_ppm = rate_percent * 10_000` (parts per million of the base; 20% is 200,000 and 14.975% is 149,750) with `Decimal` and an exactness check (a rate with more than four decimals is refused by the API). All arithmetic after that is on integers.

New named rules (appendix B), in `app/services/billing/rounding.py`:

- `tax_exclusive(base_cents, rate_ppm, rule)`: `rule(base_cents * rate_ppm, 1_000_000)`.
- `tax_inclusive(gross_cents, rate_ppm, rule)`: `gross_cents - rule(gross_cents * 1_000_000, 1_000_000 + rate_ppm)`, so net plus tax equals the gross exactly.
- `round_half_up(n, d)` and `round_down(n, d)` beside the existing `round_half_even`; `tax_floor` is `round_down` named for the legacy mirror.

**Rounding point**, per rule:

- `line`: each line's tax is rounded on its own.
- `invoice_rate`: the bases of all lines that share a rule are summed, the tax on the sum is rounded once, and the result is spread back over those lines with `tax_split_largest_remainder` weighted by base, so the line taxes add up to the rounded total exactly.

**Pack choices** (owner question 1 asks to confirm both): UK VAT uses `invoice_rate` with `round_half_up` (HMRC accepts rounding the invoice total of VAT to the nearest penny or down; nearest is the common choice); EU VAT uses `invoice_rate` with `round_half_up` (commercial rounding, which the member states that set a rule mostly require at invoice level per rate). The mirrored legacy rules use `invoice_rate` with `tax_floor`. A mixed invoice (rules with different points) applies each rule's point to its own lines.

### 4.5 Tax point and effective dating

The rate is the one valid at the invoice's **tax point**, `invoices.tax_point_at`:

- An invoice (not a proforma) published by `InvoiceService.publish` (`app/services/invoice_service.py:584-661`): `tax_point_at = issued_at`, the publish time. Publish always recalculates under the invoice lock with the pure path at that tax point, whatever was calculated for the draft, because a rule may have been superseded between create and publish; a result prepared by a provider before the lock is used only if its `context_hash` (which includes the tax point's date and the rule rows valid then) still matches. For a continuing supply of services such as hosting, the UK and EU tax point is the earlier of the invoice and the payment, and the invoice comes first on every Rackflow path (checkout invoices before payment, renewals before the due date with B3). The rate is therefore fixed at publish and never moves.
- **Stale rules at publish.** Publish writes tax rows only after `invoice_tax.lock_rules` has share-locked every rule the result uses, in ascending id. It does this with one read by primary key (`WHERE id IN (...) ORDER BY id LOCK IN SHARE MODE`, with `FORCE INDEX (PRIMARY)` on MariaDB), the same order the pack import locks rules in. A locking read sees the latest committed row whatever the transaction's snapshot, and every supersede closes the old row's `valid_to` under its own lock, so a rule closed after the transaction first read the rules is seen. If a rule is not in force at the tax point, or has an enabled successor in force by then, publish raises `TaxRuleChanged` and writes nothing. `TaxRuleChanged` is a `RetryableTaxPublishError` (`app/services/tax/errors.py`), the base class every caller catches. The caller rolls back and runs its whole transaction again, because recalculating in the same transaction would read the same old snapshot under REPEATABLE READ. Checkout answers 400 with the error's message asking the customer to try again. The reseller top-up, client credit top-up and client upgrade routes answer 409 with the error's code and message (`tax_rules_changed`). The recurring worker rolls back only that billing, logs it and carries on; the billing is invoiced on the next pass. Other publish errors that are safe to retry subclass `RetryableTaxPublishError` and get the same handling.
- A proforma (P1 5.4) is not a tax invoice. Tax is calculated at its publish for the amount the client is asked to pay, with `tax_point_at` set provisionally to `issued_at`. At conversion on payment (`convert_proforma`, `:720-748`) the tax is **not** recalculated (the invoice is frozen); if a rule covering one of its lines has a different rate at the payment time, the conversion records shadow-style mismatch `t3_proforma_rate_changed` and staff issue a credit or debit note. Owner question 6.
- A quote uses the rules valid at the quote time; the checkout invoice is created in the same request, so the quote and the invoice agree, which today's shared `_tax_cents` guarantees and the engine keeps (`quote` and `_create_commerce_invoice` both call one `CheckoutService._tax_result(...)`).

### 4.6 Which invoices are taxed

| Purpose | Phase 2 in `v2` | Setting |
|---|---|---|
| `order_charge` (checkout) | taxed, as today | |
| `cycle_charge`, `renewal` retail | taxed (new: today renewals carry no tax) | `billing_tax_retail_renewals` (default true; owner question 2) |
| reseller `cycle_charge`, `deploy_charge` | not taxed unless enabled | `billing_tax_resellers_enabled` (default false) |
| `credit_topup` | never taxed: class `outside_scope`; the tax is on the invoices the credit pays | |
| `adjustment` (manual), `addon`, `upgrade` | taxed by their lines' classes | |
| late fee debit notes (B5) | class `outside_scope` (compensation, not a supply; owner question 3) | |

The reseller default keeps today's reseller invoices unchanged, as the reference rule requires; an operator who wants VAT on reseller invoices turns the setting on after the switch.

### 4.7 Where the engine is called

| Path | Mode `legacy` | Mode `shadow` | Mode `v2` |
|---|---|---|---|
| `CheckoutService.quote` and `_create_commerce_invoice` | `_tax_cents` as today | `_tax_cents` decides; engine runs beside it, `t1_checkout` | engine decides; `quote` returns the parts and summary as new fields |
| `InvoiceService.create` and `publish` for other purposes | no tax | engine runs at publish, `t2_uncovered` recorded, nothing written | engine at create (draft lines get `invoice_line_taxes`), always re-run at publish at the tax point (4.5; the publish run is final) |
| `InvoiceAdjustmentService` credit notes by lines | phase 1 behaviour | as legacy, plus `t4_note_tax` | reverses `invoice_line_taxes` proportionally (4.8) |
| B3 generation ahead | n/a (job off) | engine runs beside, nothing written | engine at create |

`InvoiceService.create` gains a keyword `tax_result: TaxResult | None`. Callers that may involve a provider compute it with `TaxEngine.prepare` before any lock and pass it; `create` recomputes the `context_hash` under the lock with the pure path and, if it differs (a rule, exemption or profile changed meanwhile), discards the given result and uses `TaxEngine.calculate` with built-in rules, recording `t5_context_changed`. With no `tax_result`, `create` calls `calculate`. `NewLine.tax_cents` passed by callers is ignored in `v2` for lines whose purpose is taxed by the engine; P2-T asserts this with a test.

Every checkout invoice line, the discount line included, carries the product's tax class, so the quote's context and the invoice's context agree. At place order, if the invoice's tax differs from the quote's, checkout raises a `CheckoutError` asking the customer to review the new quote. That happens when `create` discarded the prepared result and the total changed (`TaxQuoteChanged`), or when the frozen context hash differs from the quote's.

### 4.8 Credit notes and debit notes

Phase 1 settled one rule for the tax of a credit note without lines and of a v2 refund (P1 4.4 and 7.2, `InvoiceAdjustmentService.tax_base` and `tax_of`, `app/services/invoice_adjustment_service.py:719-732`, used by `RefundService._refund_tax`, `app/services/refund_service.py:860`): the base is the invoice plus debit notes, leaving out overpayment debit notes, less credit notes issued before this note (`before_key`), and earlier refunds that issued no note are taken off by the refund service. The base shrinks by what each note takes, so partial notes that add up to the base take exactly its tax, with no drift. Phase 2 generalises that rule per tax row instead of replacing it:

- **The per-row base.** For a v2 tax invoice, `tax_base` gains a per-row form, `tax_base_rows(db, invoice, *, before_key) -> list[RowBase]`: one entry per `(name, rate_percent, jurisdiction, reverse_charge)` group of the invoice's `invoice_line_taxes` and of its debit notes' `invoice_adjustment_line_taxes` (overpayment notes left out, as in P1), each with its remaining worth and remaining tax after the credit notes issued before `before_key`, plus one untaxed entry for lines with no tax row. The single-pair `tax_base` stays and equals the sum of the rows, so phase 1 callers and tests are unchanged; on an invoice with no tax rows (legacy, shadow, or before phase 2) both behave exactly as in phase 1.
- **A credit note by amount and a v2 refund** split their gross over `tax_base_rows` with `split_by_rows`, one share per rate group (name, rate, jurisdiction, reverse charge). Their tax rows record the group, not a source `invoice_line_taxes` row. There are three reasons: a debit note's share of a group has no source row; a refund without a note keeps only its per-group shares in its metadata; and linking each share to source rows would need a second rounded split. A refund uses the same function, so the refund's `tax_cents` and its credit note's tax rows always agree.
- **A credit note by lines** reverses the tax rows of the lines it names. Each reversal row carries `source_line_tax_id`, the same name, rate, jurisdiction and reverse charge flag, so a reverse-charged line stays reverse charged on its note. Each row is bounded by what its rate group has left in `tax_base_rows`, which is the invoice's tax plus debit notes, less every earlier credit note, whether by amount or by lines. A note that asks for more net than a group has left is refused with 409. A note that takes exactly what is left takes exactly the group's remaining tax, so the note that completes a rate leaves no cent behind or over.
- **Refunds that issued no note.** `tax_base_rows` counts only tax documents: the invoice, debit notes and credit notes. A refund that issued no credit note is money only. It does not reduce the VAT charged, so a credit note issued after it still takes its tax from the invoice. Only a v2 refund's own split takes such earlier refunds off each rate first, so that refund money and its tax stay in proportion. Credit notes alone stay within the tax charged per rate in that case, and an admin credit note is capped at what the client still owes, so a full refund followed by a full admin credit note pays nothing out twice (P2-T test `test_admin_credit_note_after_a_full_refund_with_no_note_pays_nothing_out_twice`).
- **A debit note** is taxed as a new supply at its own tax point (its issue time) through the engine with `purpose = adjustment`; late fees are `outside_scope` (a zero-rate untaxed entry in later bases, as P1's untaxed late fee already is).
- **Tests (P2-T):** P1's refund and credit-note tax tests pass unchanged; on a two-rate invoice (20% and 5%) with a taxed debit note: a full credit, a full refund, refunds in several parts, and a mix of notes by amount and by lines each take exactly the total tax, per rate; a retry returns the same note and rows.
- Notes print the legal notes of their invoice plus the `credit_note_reference` note (appendix C).

### 4.9 Invoice and PDF

At publish in `v2` the invoice stores `tax_summary`, `legal_notes`, `seller_registration_id`, `tax_point_at`, `tax_evidence` and `tax_engine`, and the seller and buyer snapshots gain `registration_tax_number`, `buyer_tax_id`, `buyer_tax_id_status`, `buyer_is_business`. `app/services/invoice_pdf_service.py` (`_totals_lines`, `:95-124`, and the header) prints, for a v2 tax invoice:

- the document title from the invoice profile (for example `VAT invoice`) and the invoice number and date (already printed);
- the seller's registration number for the jurisdiction (`VAT registration number: GB...`);
- the buyer's VAT number when the buyer is a business and has one (already printed as `Tax ID`, kept);
- per line: net amount, rate and tax (a new column), with "Reverse charge" in the rate column for reverse-charged lines;
- the summary per rate: net, rate, tax, then the totals as today;
- totals in the invoice currency; if the currency is not GBP, the registration is `GB` and the VAT is above zero, the total VAT in sterling with the HMRC rate, its month and its source (4.10);
- the legal notes.

Invoices before phase 2 and legacy and shadow invoices print exactly as today.

### 4.10 Sterling VAT on invoices in another currency (owner question 4, decided 2026-11-22)

Rackflow runs in one operator currency for the whole system (`settings.commerce_default_currency`, USD today; making it configurable is later work, not phase 2). A UK VAT invoice in that currency is published, never refused for its currency, and shows its VAT total in sterling at HMRC's published monthly exchange rate for the month of the tax point.

- **Rates.** A core table `tax_exchange_rates` (`source`, `base` `GBP`, `currency`, `period` `YYYY-MM`, `units_per_gbp` `Numeric(18,6)`, `period_start`, `period_end`, `fetched_at`; unique on source, base, currency and period). Rows are written by the job `tax_exchange_rates_fetch` (9.1; daily, fetches the current and next month when missing, never overwrites a stored month: a different value for a stored month raises an alert and keeps the stored one) or by an audited admin entry (`/api/admin/billing/tax/exchange-rates`, `billing.tax.exchange_rate_*`), the fallback when the source is down. Only the stored table is read at publish; no network call happens under a lock (section 0).
- **Source.** A `currency_rates` provider (8.5), shipped as the bundled plugin `plugin_packages/hmrc_exchange_rates/`, which reads HMRC's monthly file from the UK Trade Tariff (`/api/v2/exchange_rates/files/monthly_xml_<YYYY>-<M>.xml`, currency units per £1). The source is recorded as `hmrc_monthly`.
- **Month.** The tax point's month in UK time (`Europe/London`), so a tax point at 00:30 BST on the 1st uses that month.
- **At publish** (`invoice_tax.stamp_published`, replacing the old currency refusal): when the registration is `GB`, the invoice VAT is above zero and the currency is not GBP, the stored rate for that month and currency is required. The invoice freezes `fx_rate` (the stored `units_per_gbp`), `fx_period`, `fx_source` and `tax_total_gbp_cents = round_half_up(tax_cents / fx_rate)`, all guarded after publish by `invoice_guard`. Only the total VAT is converted; per-rate figures stay in the invoice currency.
- **No rate yet.** The publish is refused with the retryable publish error of 4.7 (message: the HMRC rate for the month is not stored yet; try again later), so checkout, top-ups and upgrades answer 409 and the recurring worker retries that billing on its next pass. Nothing is guessed and no earlier month is used. The detector `billing_tax_fx_rate_missing` is enabled by default but fires only when all of these hold: tax is in `v2` or `shadow` (shadow gives early warning before the switch), an active `GB` registration exists, and the operator currency is not GBP. When they hold, it raises if the current month's rate is missing, and from the 25th if next month's is. A system with no UK tax set up never sees it.
- **Notes.** Credit and debit notes on such an invoice convert their VAT at the invoice's frozen rate and print it the same way.
- **Switch.** The tax switch to `v2` (5) lists, as a blocking item, a missing rate for the current month when a `GB` registration exists and the operator currency is not GBP.
- **No new hard-coded currency.** Phase 2 code reads the operator currency from one helper instead of the literal `"USD"`.

## 5. Modes, shadow comparison, switch and rollback

The tax engine is a third engine of the P1 engine framework: setting `billing_tax_engine` (`legacy`, `shadow`, `v2`; default `shadow`), managed by `POST /api/admin/billing/engines/tax/switch` in `app/api/billing_engines_admin.py`, with the same `setting_managed_elsewhere` protection on the settings API and the same `billing_engine_switch:tax` record (P1 6.5).

**Checks** recorded through `BillingShadowService.record("tax", ...)` (`app/services/billing_shadow.py`):

| Check key | When | Compares | Counts for the switch |
|---|---|---|---|
| `t1_checkout` | every quote and checkout invoice in `shadow` | `_tax_cents` total against the engine's total tax | yes, unless reclassified as `t1_rule_divergence` |
| `t1_rule_divergence` | same | a `t1_checkout` difference that the engine explains by a rule with `source != tax_rates` (a pack or admin rule), an exemption, or a reverse charge; the context lists which | no; the switch request must list them (below) |
| `t2_uncovered` | publish of a taxed purpose legacy never taxed (4.6) | legacy 0 against the engine's tax | no; summarised on the engines page as "invoices that will carry tax after the switch", per purpose |
| `t3_proforma_rate_changed` | proforma conversion in `v2` | 4.5 | no; alert only |
| `t4_note_tax` | credit note by lines in `shadow` | phase 1 tax part against 4.8 | yes |
| `t5_context_changed` | `create` in `v2` | 4.7 | no; alert if more than ten a day |
| `t6_scan` | daily scan of the last 48 hours of v2 invoices | for each, Σ `invoice_line_taxes` per line equals the line tax, summary equals rows, and the engine re-run with the stored context and the rules valid at `tax_point_at` gives the same tax | yes |

**Switch criterion.** As P1 6.5 items 1, 3, 4 and 5 for engine `tax`, and in place of item 2:

1. the invoice engine is `v2` (v2 tax lives on v2 lines and their tax rows; a legacy invoice has no lines to tax);
2. `billing_tax_enabled` is true, or the operator confirms switching with only the mirrored `tax_rates` rules in force (4.3 step 1);
3. every enabled rule with `jurisdiction != ANY` has an enabled registration for its jurisdiction valid now, and `billing_tax_seller_country` is set with an establishment registration (4.3 step 2) when any such rule is enabled;
4. the request body carries `"confirm_divergence": [<t1_rule_divergence row ids>]` listing every open divergence row, and `"confirm_uncovered": true` when `t2_uncovered` rows exist, so the operator has seen what changes.

**Rollback.** `v2` to `shadow` or `legacy` is allowed at any time: published invoices keep their tax rows (frozen), and later invoices go back to `_tax_cents` and untaxed renewals. The response lists the number of v2-taxed invoices still open, because a client may then receive a renewal without the VAT its previous invoice had; the audit row `billing.engine.switch` records it. A draft created while the tax engine was `v2` and published after a rollback to `shadow` or `legacy` becomes a plain legacy invoice at publish (`invoice_tax.reset_to_legacy`). Its `invoice_line_taxes` rows are deleted and `prices_include_tax` becomes false. Each line then gets the tax legacy gives the invoice's purpose: `CheckoutService._tax_cents` on the untaxed total for a purpose legacy taxes, spread over the charged lines by the largest remainder as checkout spreads it, and nothing otherwise. The invoice stores no tax summary, tax point or evidence.

## 6. Tax admin

### 6.1 Admin UI (P2-UI)

`/admin/billing/tax` with tabs: Overview (mode, enabled, registrations, mismatches link), Classes, Rules (filter by country, class, pack; supersede; history), Registrations, Exemptions, Packs (available packs with version, preview diff, apply), Legal notes, Tax ID checks (search, recheck), Calculator (a basket and a buyer to `POST /preview`). Account detail gains a Tax panel (exempt flag and reason, exemptions, profile tax ID with status and last check). Product editor gains the tax class.

### 6.2 Admin API (`app/api/billing_tax_admin.py`, new; admin only; every write audited `billing.tax.*`)

| Method and path | Purpose |
|---|---|
| `GET/POST /api/admin/billing/tax/classes`, `PUT/DELETE .../classes/{id}` | system classes cannot be deleted |
| `GET/POST /api/admin/billing/tax/rules`, `GET/PUT .../rules/{id}` | `PUT` refused with 409 `tax_rule_in_use` once `valid_from` has passed |
| `POST /api/admin/billing/tax/rules/{id}/supersede` | body: the changed fields and `valid_from` (not in the past) |
| `GET/POST /api/admin/billing/tax/registrations`, `PUT .../{id}` | |
| `GET/POST /api/admin/billing/tax/exemptions`, `POST .../{id}/revoke` | |
| `PUT /api/admin/billing/accounts/{id}/tax` | `tax_exempt`, `tax_exempt_reason` |
| `GET /api/admin/billing/tax/packs` | packs from the pack registry (built-in and plugin), versions, applied version |
| `POST /api/admin/billing/tax/packs/{pack_id}/preview` | diff against applied rules (7.3) |
| `POST /api/admin/billing/tax/packs/{pack_id}/apply` | body: `version`, `effective_from`, the diff hash the admin saw |
| `GET/PUT /api/admin/billing/tax/legal-notes` | |
| `GET /api/admin/billing/tax/id-checks`, `POST .../id-checks/recheck` | recheck one profile now |
| `POST /api/admin/billing/tax/preview` | a `TaxContext` in JSON, answers the `TaxResult` (never writes) |

Client API (`app/api/commerce_client.py:837-885`, `GET/PUT /profile`): gains `is_business`, `tax_id_type` (writable) and `tax_id_status`, `tax_id_checked_at` (read only). A `PUT` that changes `tax_id` or `country` resets the status to `unchecked` and runs the check (7.4) before answering, with the plugin timeout; the answer includes the resulting status. The quote route (`app/api/commerce_client.py:311`) gains `tax_lines`, `tax_summary`, `legal_notes`, `reverse_charge` (additive). Reseller panel profile routes gain the same fields if they expose the profile.

**WHMCS visibility:** none. No `/api/billing` route reads profiles, tax or invoices.

## 7. Rule packs and the VIES validator

### 7.1 Decision: the UK and EU packs are core data in the pack registry, not plugins

The packs ship as versioned JSON in `app/services/tax/packs/uk_vat.json` and `eu_vat.json`, registered in `TaxRulePackRegistry` with source `builtin`. Third-party packs arrive through the `tax_rule_pack` plugin kind and register into the same registry, so preview, diff, apply and update are one code path. Reasons:

- they are legal data that must be reviewed in the repository with tests (golden cases of appendix C), and change with the code that interprets them (the `jurisdiction`, `reverse_charge_zone` and rounding fields);
- every UK operator needs them; a plugin install, enable and permission grant would be a step between the operator and correct tax, and a plugin can be disabled or circuit-broken, while the registry entry cannot;
- applied rules are copied into `tax_rules`, so the source of a pack never matters after apply, and the kind is still exercised by a fixture plugin in the tests and by the example plugin the developer guide ships.

A built-in pack can be updated only by a Rackflow release; a plugin pack by a plugin update. Both are applied by an admin, never automatically (WHMCS's automatic EU rate update is deferred; owner question 7).

### 7.2 Pack format

```json
{
  "pack_id": "eu_vat",
  "version": "2026.11.1",
  "jurisdictions": ["EU_OSS", "AT", "BE", "..."],
  "classes": ["standard", "reduced", "zero", "exempt", "digital_service"],
  "legal_notes": {"eu_reverse_charge": {"en": "..."}},
  "rules": [
    {"key": "eu_oss_b2c_digital_DE", "name": "DE VAT 19%", "country": "DE", "class": "digital_service",
     "customer_type": "consumer", "rate_percent": "19.0000", "jurisdiction": "EU_OSS",
     "rounding_point": "invoice_rate", "rounding_rule": "round_half_up",
     "valid_from": "2007-01-01", "valid_to": null, "legal_note_key": null},
    {"key": "eu_b2b_reverse_charge", "name": "EU reverse charge", "country": null, "class": "digital_service",
     "customer_type": "business", "rate_percent": "0.0000", "jurisdiction": "ANY_EU",
     "reverse_charge": true, "reverse_charge_zone": "EU", "legal_note_key": "eu_reverse_charge"}
  ]
}
```

Validated by `PackSchema` (pydantic, strict). `rate_percent` is a string to keep it exact. `ANY_EU` matches the buyer's EU country at apply time (one rule row per member state is generated so each is versioned on its own).

### 7.3 Preview, apply and update

`PackImporter.preview(db, pack_id, version, effective_from) -> PackDiff` lists for each pack rule key: `add`, `unchanged`, `change` (the old and the new values) and `remove` (a key applied before and absent now). `apply` takes the diff hash the admin saw (409 `pack_changed` if it differs), and in one transaction: inserts added rules with `valid_from = effective_from`; for a changed rule, closes the old row with `valid_to = effective_from` and inserts the new one (`supersedes_id`); for a removed rule, closes it; upserts legal notes not edited by an admin (an edited note is listed and kept). `effective_from` may not be in the past. Audited `billing.tax.pack_applied` with the diff. The class `digital_service` is created if missing, and products are not reassigned automatically: the Packs tab prompts the admin to set the class on products (owner question 8 asks whether everything sold is a digital service).

### 7.4 Tax ID checks (`TaxIdService`, core)

`TaxIdService.check(db, profile, *, force=False) -> TaxIdCheck`:

1. Normalise; core format check (`core_format`: a per-country regular expression table in `app/services/tax/id_formats.py` for the EU 27 and GB). A format failure stores `format_invalid` and stops.
2. Cache: the latest `tax_id_checks` row for `(country, normalised_id)` from a validator that supports it, with `expires_at > now`, is reused unless `force`.
3. Pick the validator: enabled `tax_id_validator` providers whose `supports(country, id_type)` is true, in the order of setting `billing_tax_id_validators` (a list of `<plugin_id>:<provider_id>`). None: the status stays `unchecked` (format-only is not evidence of a valid VAT number).
4. Call `check_online` through the host (section 8.6) with `billing_tax_id_check_timeout_seconds` (default 8). Map the answer to `valid`, `invalid` or `unavailable`; a timeout, plugin error or open circuit is `unavailable` with `error_code`.
5. Store a `tax_id_checks` row; update the profile's status, `tax_id_checked_at`, `tax_id_check_id` (the profile row is not a money row; a plain update is fine); emit `tax_id.checked`.

Cache horizons (`expires_at`): `valid` 30 days (`billing_tax_id_valid_days`), `invalid` 1 day, `unavailable` 15 minutes. "Fresh" for reverse charge means checked within `billing_tax_id_max_age_days` (default 30) of the tax point. The job `tax_id_recheck` (section 9.1) re-checks profiles whose `valid` result is older than 25 days and every `unavailable` profile, so active reverse-charge clients stay fresh.

**When VIES is down**, setting `billing_tax_id_unavailable_policy`:

- `charge_tax` (default, fail open for the sale): the invoice is taxed as for a consumer or a business without a valid number, the checkout proceeds, the client sees "We could not verify your VAT number; VAT has been charged. We will retry." A later `valid` result applies to later invoices only; a client who wants the earlier VAT back gets a credit note and a reverse-charge invoice from staff (manual in phase 2).
- `block_checkout` (fail closed): a checkout by a business buyer with an EU VAT number and status `unavailable` is refused with 409 `tax_id_unverifiable`, retry later. Renewals are never blocked: they are taxed as `charge_tax`.

### 7.5 The VIES validator plugin (`plugin_packages/vies_validator/`)

A first-party plugin package in the repository at `plugin_packages/vies_validator/`, API `>=1.1,<2`, kind `tax_id_validator`, id `vies`. `plugin_packages/` holds plugins Rackflow ships and supports; `examples/plugins/` (the developer kit's folder) holds teaching examples only. Plugins install from `settings.plugins_dir` today (`app/plugin_host/loader.py:33`, `discover` at `:98`); P2-K adds a second, read-only discovery root, `settings.bundled_plugins_dir` (default the repository's `plugin_packages/`), so the admin plugin page lists first-party plugins as installable without copying files, and an upgrade of Rackflow upgrades them. Install, permission grant and enable are the ordinary lifecycle; nothing is installed or enabled automatically (owner question 12 is closed by this).

- **Manifest:** `permissions: ["tax.validate_id", "http.outbound"]`; `provides: [{"kind": "tax_id_validator", "id": "vies", "countries": [the EU 27 VIES codes, with "EL" for Greece], "id_types": ["eu_vat"], "rate_limit": {"calls": 60, "per_seconds": 60}}]`; settings `requester_country` and `requester_vat_number` (optional; when set, VIES returns a consultation number, `requestIdentifier`, the evidence a tax authority accepts), `endpoint` (default the Commission's REST endpoint `https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number`, overridable for the test service).
- **Behaviour:** `check_online` posts the country code and number, maps `valid: true` to `valid`, `valid: false` to `invalid`, and the error codes `MS_UNAVAILABLE`, `SERVICE_UNAVAILABLE`, `TIMEOUT`, `MS_MAX_CONCURRENT_REQ`, `GLOBAL_MAX_CONCURRENT_REQ` (and their `_TIME` variants) and any HTTP or network error to `unavailable` with the code. `INVALID_INPUT` is `invalid` with the code. `INVALID_REQUESTER_INFO`, `VAT_BLOCKED` and `IP_BLOCKED` say nothing about the customer's number, so they are `unavailable` with the code, and `INVALID_REQUESTER_INFO` also writes a plugin log line telling the operator to fix the requester settings. It never maps an unavailable answer to `invalid`. P2-K checks the full code list and the test-service numbers against the Commission's current VIES REST documentation and records any difference in the plugin's README.
- **Rate limiting:** the host enforces the manifest's `rate_limit` per provider (8.6; excess calls answer `unavailable`, `error_code = RATE_LIMITED`, without calling the plugin); the plugin also keeps a per-member-state back-off: after `MS_UNAVAILABLE` for a country, it answers `unavailable` for that country for 5 minutes without calling VIES.
- **Caching** is the host's (7.4); the plugin is stateless apart from the back-off.
- **Network:** only the configured endpoint host, through the outbound HTTP facility `http.outbound` grants (plugin doc section 7); no other egress.
- **Tests:** against the Commission's test service numbers (`100` valid, `200` invalid, `300` service unavailable, `301` member state unavailable, `302` timeout) recorded as fixtures, never live in CI.

UK VAT number validation (HMRC's API, which needs an application registration) is deferred; GB numbers get the format check only, which is enough in phase 2 because no UK rule depends on a valid GB number (appendix C).

### 7.6 Customer location evidence

For B2C electronically supplied services the place of supply is where the customer is, and the EU rules ask for location evidence (two non-contradictory items, or one for small sellers). Phase 2 records, on the order at checkout and on each invoice at publish, `tax_evidence`: billing country (profile), the checkout request's IP address and IP country (from a configured header such as `CF-IPCountry`, setting `billing_tax_ip_country_header`, default empty; no GeoIP database in core), and, when a gateway reports it later, the payment country (stored on the order's evidence by the payment path when available; Stripe card country is a phase 3 addition with B6). The engine uses the billing country; when the IP country is present and differs, the evidence carries `conflict: true` and an alert detector `tax_evidence_conflict` lists such invoices for staff. Choosing a country by majority of evidence is deferred (owner question 5).

## 8. Plugin API 1.1: typed plugin kinds

This section is the complete list of additions for the plugin developer guide. Everything in API 1.0 keeps working unchanged; a 1.0 plugin never sees a difference.

### 8.1 Version and compatibility

- `app/plugin_host/catalogue.py:7`: `API_VERSION = "1.1"`, plus `SUPPORTED_API_VERSIONS = ("1.0", "1.1")`.
- Compatibility (`Manifest.compatible`, `app/plugin_host/manifest.py:459`, and the loader check at `app/plugin_host/loader.py:86`) becomes: the manifest's `rackflow_api` range allows **at least one** supported version. Without this, a plugin that declared `"==1.0"` would stop loading when the host moved to 1.1. The plugin's **effective API** is the highest supported version its range allows, stored on the installation (`plugin_installations.effective_api`, new nullable column, P2-S) and shown in the admin plugin page.
- A manifest using any 1.1 field (`provides`) must have an effective API of at least 1.1, else it is refused with `"provides needs plugin API 1.1; this manifest allows only 1.0"`.
- Within 1.x only additions: new kinds, new optional manifest fields, new optional protocol methods (detected with `hasattr`), new optional result fields. Removing or changing anything is 2.0.

### 8.2 Manifest additions

`Manifest` (`app/plugin_host/manifest.py:327-346`, `extra="forbid"` at `:87-88`, which is why `provides` is refused in 1.0 today) gains:

```python
class ProvideSpec(_Strict):
    kind: Literal["tax_id_validator", "tax_rule_pack", "tax_provider", "currency_rates"]
    id: str                                   # ITEM_ID_RE, unique per (kind, id) within the plugin
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    countries: list[str] = Field(default_factory=list)   # ISO alpha-2 (VIES "EL" allowed); empty = any
    id_types: list[str] = Field(default_factory=list)    # tax_id_validator only
    currencies: list[str] = Field(default_factory=list)  # tax_provider only; empty = any
    timeout_seconds: int = Field(default=8, ge=1, le=30)
    rate_limit: Optional[RateLimitSpec] = None           # {"calls": int 1..10000, "per_seconds": int 1..86400}
    pack: Optional[PackRef] = None                       # tax_rule_pack only: {"file": "packs/x.json"} inside the package

provides: list[ProvideSpec] = Field(default_factory=list, max_length=10)
```

Validation (`_check_permissions`-style, as at `manifest.py:424-440`): each kind requires its permission in `permissions` (8.3); `tax_id_validator` requires non-empty `id_types`; `tax_rule_pack` requires `pack.file` to exist in the package and to pass `PackSchema` at install; `(kind, id)` unique; `id_types` and `currencies` only on their kind.

### 8.3 Permissions (new entries in `PERMISSION_LIST`, `catalogue.py:21-39`)

| Key | Label | Description | Risk |
|---|---|---|---|
| `tax.validate_id` | Validate tax IDs | Receive customers' tax identification numbers and countries to check them. | medium (personal data) |
| `tax.rule_pack` | Offer tax rule packs | Offer tax rules that an administrator may review and apply. | medium |
| `tax.calculate` | Calculate tax | Receive basket lines, amounts and the buyer's address and tax ID, and decide the tax on invoices. | high |

A validator that calls an external service also needs `http.outbound` (existing). The admin's permission grant at enable covers both, as for any plugin.

### 8.4 Registration

`Registrar` (`app/plugin_host/loader.py:119-163`) gains `providers: dict[tuple[str, str], Any]` and a binder; `_PublicRegistrar` (`:166-174`) gains `provider`:

```python
def register(plugin):
    @plugin.provider("tax_id_validator", "vies")
    class Vies:
        def supports(self, country: str, id_type: str) -> bool: ...
        def check_format(self, country: str, tax_id: str) -> bool: ...
        def check_online(self, request: TaxIdCheckRequest) -> TaxIdCheckResult: ...
```

`@plugin.provider(kind, id)` accepts a class (instantiated once per load with no arguments) or a factory function returning the object. At load the host checks, as it does for actions and schedules: every `provides` entry has exactly one bound provider and every bound provider is declared; the object satisfies the kind's protocol (8.5, `typing.runtime_checkable` plus a signature arity check of the required methods). A mismatch is a load error (`PluginLoadError`), the plugin goes to `failed` with the reason, as today.

### 8.5 Host-side interfaces (`app/plugin_host/kinds.py`, new)

All types are frozen dataclasses importable by plugins from `rackflow_plugin.kinds` (the SDK module the guide documents; P2-K adds it beside the existing SDK surface). Amounts are integer minor units; rates are strings with up to four decimals.

```python
@dataclass(frozen=True)
class TaxIdCheckRequest:
    country: str            # ISO alpha-2 of the number's country (VIES code for EU: "EL" for Greece)
    tax_id: str             # normalised, without the country prefix
    id_type: str
    requester_country: str | None
    requester_tax_id: str | None

@dataclass(frozen=True)
class TaxIdCheckResult:
    status: Literal["valid", "invalid", "unavailable"]
    checked_at: datetime
    request_ref: str | None = None        # consultation number or equivalent
    registered_name: str | None = None
    registered_address: str | None = None
    error_code: str | None = None         # required when status is "unavailable"

@runtime_checkable
class TaxIdValidator(Protocol):
    def supports(self, country: str, id_type: str) -> bool: ...
    def check_format(self, country: str, tax_id: str) -> bool: ...
    def check_online(self, request: TaxIdCheckRequest) -> TaxIdCheckResult: ...

@runtime_checkable
class TaxRulePack(Protocol):
    def pack_id(self) -> str: ...
    def versions(self) -> list[str]: ...                 # newest last
    def load(self, version: str) -> dict: ...            # the JSON of 7.2; validated by PackSchema

@dataclass(frozen=True)
class TaxProviderLine:
    key: str
    net_cents: int
    tax_class: str
    kind: str
    product_code: str | None

@dataclass(frozen=True)
class TaxProviderRequest:
    tax_point_at: datetime
    currency: str
    prices_include_tax: bool
    seller: dict             # registrations: [{"jurisdiction", "tax_number", "country"}]
    buyer: dict              # country, region, postcode, is_business, tax_id, tax_id_status
    lines: tuple[TaxProviderLine, ...]

@dataclass(frozen=True)
class TaxProviderPart:
    name: str
    rate_percent: str
    jurisdiction: str
    base_cents: int
    tax_cents: int
    reverse_charge: bool = False
    legal_note: str | None = None

@dataclass(frozen=True)
class TaxProviderResult:
    lines: dict[str, tuple[TaxProviderPart, ...]]  # by TaxProviderLine.key; every key present
    reference: str | None = None                   # the provider's transaction id

@runtime_checkable
class TaxProvider(Protocol):
    def supports(self, request: TaxProviderRequest) -> bool: ...
    def calculate(self, request: TaxProviderRequest) -> TaxProviderResult: ...
```

Billing validates every answer: a validator result with an unknown status, or a provider result that misses a line key, has a negative amount, a rate that does not reproduce `tax_cents` from `base_cents` within one minor unit per part, or a part with `reverse_charge` and non-zero tax, is treated as a failure (8.6).

**`currency_rates`** (added for 4.10 by P2-X; permission `currency.rates`; metered method `rates`): `rates(request: CurrencyRatesRequest) -> CurrencyRatesResult`. The request carries `base` (`GBP`) and `period` (`YYYY-MM`); the result carries `source`, `period_start`, `period_end` and a list of `(currency, units_per_base)` with `units_per_base` an ASCII decimal string above zero. The host checks the answer by exact type and rebuilds it, as for the tax kinds. A missing month is an empty list, not an error.

### 8.6 Calls, timeouts, failure isolation

`app/plugin_host/providers.py` (new): `call_provider(kind, provider_ref, method, *args) -> Any`.

- Runs the method in the plugin host's worker thread pool (the mechanism `app/plugin_host/invoke.py` uses for actions) with a wall-clock timeout of the provide spec's `timeout_seconds` (8 by default, at most 30; billing may pass a lower one).
- Not called when the installation is not `enabled` or its circuit is open (`app/plugin_host/health.py:22-36`); the call then fails at once with `ProviderUnavailable("circuit_open")`.
- Rate limit: a per-provider token bucket from `rate_limit`, kept in Redis (`app/core/redis.py`) so all processes share it, falling back to a per-process bucket when Redis is down; an exhausted bucket fails at once with `ProviderUnavailable("rate_limited")`.
- Every timeout, exception or invalid answer counts as a failure for the circuit breaker (`record_failure`, `FAILURES_TO_OPEN = 5`, `FAILURES_TO_DISABLE = 25`, `health.py:16-17`) and writes a plugin log line; a valid answer calls `record_success`. Exceptions never propagate into billing: `call_provider` raises only `ProviderUnavailable(reason)`.
- **What billing does on failure:** never blocks an invoice. A validator failure is the status `unavailable` (7.4). A tax provider failure falls back to the built-in rules for that calculation and records `t5_provider_fallback` with the reason, and the invoice records `tax_engine = "rules"`. A pack provider failure makes the pack unavailable in the Packs tab; applied rules are unaffected.
- Never called while a billing lock is held (section 0); `TaxEngine.prepare` and `TaxIdService.check` are the only callers.

### 8.7 Selection, lifecycle, audit

- Validators: ordered by `billing_tax_id_validators`; the first whose `supports` is true is used.
- Tax provider: at most one active, chosen by `billing_tax_provider` (`""` means built-in rules; default). No tax provider plugin ships in phase 2; the kind is defined and tested with a fixture plugin, and its route into billing is `TaxEngine.prepare`.
- Packs: every enabled `tax_rule_pack` provider is listed in the pack registry as `<plugin_id>:<provider_id>`.
- Lifecycle, settings (sealed secrets), audit (`plugin.enabled`, `plugin.disabled`, and the new `plugin.provider_failed` audit when a circuit opens on a provider) and uninstall are the existing ones. Disabling or uninstalling a provider never changes stored results: tax ID checks, applied rules and invoice tax rows stay.

### 8.7.1 Discovery of first-party plugins

`discover` (`app/plugin_host/loader.py:98`) also scans a second, read-only root, `settings.bundled_plugins_dir` (default the repository's `plugin_packages/`), after `settings.plugins_dir`. Packages found there are marked `bundled: true` in the admin plugin list; a plugin id present in both roots is an error shown on the page, and neither copy loads until it is resolved. Install, permission grant, enable, upgrade and uninstall are the ordinary lifecycle. This is a host addition of API 1.1 that applies to any plugin, not only to kinds.

### 8.8 Events

New catalogue entries (`app/plugin_host/catalogue.py`, permission `billing.read`, payload fields only ever added):

| Event | Payload | Emitted by |
|---|---|---|
| `tax_id.checked` | `billing_account_id`, `country`, `status`, `validator`, `checked_at` (never the number itself) | `TaxIdService.check` |
| `invoice.reminder_sent` | `invoice_id`, `billing_account_id`, `reseller_id`, `kind` | job `invoice_reminders` |
| `service.termination_scheduled` | `service_id`, `terminate_at`, `reason` | job `nonpayment_termination` (notice) |
| `service.cancellation_requested` | `service_id`, `request_id`, `type`, `effective_at` | `CancellationService.request` |

`invoice.overdue` (already in the catalogue, `catalogue.py:96`, with no emitter yet, spec 14.3) gains its emitter: job `invoice_reminders`, once per invoice at its first overdue notice (or at the first overdue trigger when overdue notices are disabled). `service.suspended` and `service.terminated` (`catalogue.py:72`, `:74`) are already emitted by the service flush listener for every suspension and termination, including the new ones. `COMMERCE_WEBHOOK_EVENTS` is unchanged (as P1 open question 8).

### 8.9 Not in 1.1

Payment gateways, fraud checks, accounting export and notification channels stay off the kind mechanism. `tables` stays refused (`manifest.py:386-387`). `currency_rates` is in 1.1 only for the HMRC monthly source of 4.10; D4 currencies builds on it.

## 9. Scheduler and dry run (B2)

### 9.1 Jobs

Each job is a class in `app/services/billing/jobs/` implementing:

```python
class BillingJob(Protocol):
    name: str
    setting_keys: tuple[str, ...]                    # hashed into config_hash
    def candidates(self, db, run: JobRun) -> Iterable[int]: ...          # ids, plain read, batched
    def plan(self, db, run: JobRun, subject_id: int) -> PlannedAction | None: ...   # read only
    def execute(self, db, run: JobRun, action: PlannedAction) -> str: ...           # live only; returns outcome
```

`PlannedAction` has `subject_key`, `action`, `trigger_at` and `detail`. Registered in `app/services/billing/jobs/registry.py` (`JOBS`).

| Job | Spec | Cadence | Subjects |
|---|---|---|---|
| `generate_invoices_ahead` | B3 | hourly | service billings with `next_charge_at` within the largest lead time |
| `invoice_reminders` | B4 | daily at `billing_automation_time_utc` | open, overdue and partially paid invoices with a balance |
| `late_fees` | B5 | daily | overdue invoices with a balance |
| `nonpayment_suspension` | B7 | hourly | retail service billings with an unpaid cycle invoice |
| `nonpayment_termination` | B7 | hourly | services suspended for non-payment |
| `cancellations` | B8 | hourly | cancellation requests due |
| `tax_id_recheck` | C | daily | profiles with a stale or unavailable check |

The existing recurring pass (reseller charge, retry, grace, suspension, end-of-period cancel, `app/services/recurring_billing_service.py`) is not one of these jobs: it keeps running exactly as today under its own lease (`LEASE_NAME = "recurring-billing"`, `:73`) and has no mode. The phase 1 shadow scan stays in `app/services/billing/periodic.py`.

### 9.2 Modes and the job endpoint

Mode lives in `billing_job_states.mode`, never in the settings store, and changes only through `POST /api/admin/billing/jobs/{job}/mode` with `{"to": "off" | "dry_run" | "live", "confirm": true}` (admin only; audited `billing.job.mode` with from, to and the watermark). Rules:

- any mode to `off` at any time;
- `off` to `dry_run`: sets `dry_run_since = now` (new column on `billing_job_states`);
- to `live` only from `dry_run` that has completed at least one scheduled run (`billing_job_min_dry_runs`, default 1) and with every precondition of the job (section 10) met; sets `live_since = now` and `config_hash` and `config_since` from the current settings;
- leaving `live` clears `live_since`; coming back sets a new one, so an off period is never caught up.

Other routes (`app/api/billing_jobs_admin.py`, new): `GET /api/admin/billing/jobs` (state, last run, next run, counts); `POST /api/admin/billing/jobs/{job}/run` (runs now in the job's current mode, or `{"dry_run": true}` from any mode; admin runs are marked `trigger = admin`); `GET /api/admin/billing/jobs/{job}/runs`; `GET /api/admin/billing/jobs/runs/{run_id}` with items, filterable by outcome; `GET /api/admin/billing/jobs/{job}/forecast?days=7` (triggers in the next days, read only).

### 9.3 Running and claiming on several processes

The jobs run in the recurring billing worker process (`app/workers/recurring_billing.py`) through the periodic job list of `app/services/billing/periodic.py`, in a thread (`asyncio.to_thread`), each job on its own cadence. Several worker processes may run; three layers keep a job from acting twice:

1. **Run lease.** `acquire_lease(db, f"billing_job:{name}", owner=..., seconds=billing_job_lease_seconds, now=...)` (`app/services/billing/periodic.py:38`) on `recurring_billing_leases`; one process runs a job at a time. The run renews the lease after every batch of 100 items and stops (status `aborted`) if renewal fails.
2. **Subject lock.** Each item runs in its own transaction: lock the subject rows through `BillingLocks` in `LOCK_ORDER` (`app/services/billing_locks.py:45`), which re-reads them; re-evaluate the trigger and every condition on the locked rows (never on the candidate read).
3. **Claim row.** Immediately after the locks and before any effect, insert `billing_job_actions(job, subject_key, status="claimed")`. The unique key makes a second insert fail (on MariaDB the second transaction waits for the first's uncommitted key and then gets a duplicate-key error); the item is then rolled back with outcome `already_done`. The claim commits in the same transaction as the effect, so a rolled-back effect leaves no claim and the next run retries it.

Effects outside the database (termination's power-off) use two transactions: claim with status `claimed` and commit; perform the effect; then `done` or `failed` in a second transaction. A `claimed` row older than `billing_job_stuck_minutes` (60) becomes `needs_review` on the next run and is never retried automatically; it shows on the jobs page and in the alert `billing_job_needs_review`.

### 9.4 Dry run

A dry run calls `candidates` and `plan` only, inside a transaction that is rolled back, and writes only `billing_job_runs` and `billing_job_run_items` (on the job tables' own short session, as `BillingShadowService.record` does). It never enqueues email, emits events, calls plugins with side effects or locks rows. Its watermark is `dry_run_since` (11.1); an admin dry run of a job that is `off` (no `dry_run_since`) or `live` uses the job's live watermark if it has one, else the run's own `now_at`, so it shows exactly what going live now would do (normally nothing at once; the forecast route shows what follows). After a week in dry run the operator sees, item by item, what the job would have done in that week; the forecast route shows what is coming. Amounts in a dry run (invoice totals, tax, late fees) are computed by the same pure functions the live path uses. A test (15.2) snapshots the row counts of every non-job table before and after a dry run of each job.

### 9.5 Time of day and catch-up

Daily jobs run once per UTC day at or after `billing_automation_time_utc` (default `09:00`); hourly jobs at minute 5. A missed run is caught up at the next run: items whose trigger passed since the last run are handled late. Items whose trigger is older than `billing_job_max_catch_up_days` (default 3) are skipped with outcome `skipped` and reason `stale`, so a worker outage of a week never produces a burst of a week's notices, fees or suspensions.

## 10. The automation jobs

Every job: mode `off` by default; acts only on triggers at or after its watermark (section 11); one action per subject key; locks in lock order and re-checks under the lock; refuses `live` until its preconditions hold.

### 10.1 `generate_invoices_ahead` (B3)

**What.** For a service billing due on `next_charge_at` (`app/models/reseller.py:939`) with lead time `L` days, create the cycle and its invoice at `next_charge_at - L` instead of at `next_charge_at`. `L` is `price_plan_cycles.invoice_lead_days` of the billing's cycle, else `billing_invoice_lead_days` (default 0, so nothing is generated ahead until an operator sets it; spec B3). `issued_at` (publish time) and `due_at = next_charge_at` become distinct.

**Trigger.** `T = next_charge_at - L`. Act when `W <= T <= now < next_charge_at`, the billing is active, not cancelled, its `cancel_at` is NULL or after `next_charge_at`, and no cycle exists yet for the period starting at `next_charge_at`.

**Action** (one transaction): lock the service billing (first in the lock order), re-check, compute the price as the at-due path would (`_process_billing` and the retail `renewals.py` cycle steps), create the `billing_cycles` row for the period with status `DUE` and the invoice through `InvoiceService.create_cycle(..., due_at=next_charge_at, period_start, period_end, tax_result=...)` (`app/services/invoice_service.py:811-845`), link `billing_cycles.invoice_id`, and for retail queue the `invoice_created` email (idempotency key `invoice:{id}:created`, the key the checkout path uses). Subject key `billing:{id}:cycle:{next_charge_at ISO}`.

**At due.** The recurring worker's at-due path (`app/services/recurring_billing_service.py`, the cycle creation at `:159-166` and `:379` per spec B3) must reuse an existing cycle and invoice for the same period instead of creating one: reseller billings are then charged from credit at due exactly as today (no earlier capture; capture before due is B6); retail invoices simply wait to be paid (decided default 4). P2-J changes the at-due path for this and adds a MariaDB test where the job and the worker race on one billing and produce exactly one cycle and one invoice.

**Preconditions for live:** the invoice engine is `v2` (the invoice's `due_at` and `issued_at` must differ; the legacy engine treats creation as due).

**Retroactive guard:** a billing whose `T` was before `W` (for example due in three days when the job goes live with `L = 7`) is left to the at-due path, as today.

### 10.2 `invoice_reminders` (B4)

**Kinds.** `upcoming` at `due_at - billing_reminder_upcoming_days` (default 3; 0 disables); `overdue_1`, `overdue_2`, `overdue_3` at `due_at + billing_reminder_overdue_days` (default `[1, 7, 14]`; an empty list disables; at most three, ascending).

**Trigger and anchor.** For kind `k`, `T_k` as above. An invoice is in scope for the overdue sequence only if `T_overdue_1 >= W` (its first notice falls after the watermark); a later notice is sent only after the earlier one was sent. `upcoming` needs `T_upcoming >= W`. So turning the job on never sends a third notice to an invoice that never had the first.

**Action.** Lock the invoice (`BillingLocks.for_invoice`), re-check that it is published, not paid, void or refunded, and `InvoiceService.balance(...).remaining_cents > 0`; claim `invoice:{id}:reminder:{kind}`; enqueue the email: retail through `EmailMessageService.enqueue` (`app/services/email_message_service.py:160`), resellers through `enqueue_for_reseller` (`:212`), with idempotency key `invoice:{id}:reminder:{kind}` (spec B4); emit `invoice.reminder_sent`, and on the first overdue kind also `invoice.overdue`. Reminders never change invoice status.

**Precondition for live:** the email templates for `invoice_reminder` and `invoice_overdue` exist and are enabled (F4).

### 10.3 `late_fees` (B5)

**What.** One late fee per invoice, as a debit note (A2), so the original invoice is untouched. Amount: `billing_late_fee_type` `fixed` (`billing_late_fee_fixed_cents`, only for invoices in `billing_late_fee_currency`; other currencies are skipped with reason `currency`) or `percent` (`billing_late_fee_percent_bp` of the remaining balance, rule `late_fee_percentage`, appendix B), then clamped to `billing_late_fee_min_cents` and `billing_late_fee_max_cents` (0 means no maximum). Tax class `outside_scope` (owner question 3).

**Trigger.** `T = due_at + billing_late_fee_days` (default 14). Act when `W <= T <= now` and the remaining balance is at least `billing_late_fee_min_balance_cents` (default 1). Excluded: invoices in `collections`, `credit_topup` invoices, invoices with an open dispute (P1 7.5), accounts or resellers with `billing_late_fee_exempt` (new Boolean on `billing_accounts`, P2-S; group exemption waits for D5).

**Action.** `BillingLocks.for_invoice(db, id, reseller=True, billing_account=True)`, `balance()` under the lock, claim `invoice:{id}:late_fee`, `InvoiceAdjustmentService.issue_debit_note(db, invoice, lines=[...], reason="late_fee", actor_user_id=None, idempotency_key=f"late_fee:{invoice.id}")` (P1 11.3; it re-locks rows already held), queue the `late_fee_added` email.

**Preconditions for live:** invoice engine `v2` (debit notes exist only there); the `late_fee_added` template enabled; `billing_late_fee_type` set and its amount non-zero.

### 10.4 `nonpayment_suspension` (B7, retail)

**Policy.** `billing_nonpayment_policies`: the service's product row if any, else `retail_default`. `suspend_after_days` NULL means never (the seeded default, today's behaviour). Reseller suspension stays entirely with the existing recurring pass in phase 2: the API refuses a non-NULL `suspend_after_days` on `reseller_default` and on product rows for reseller-sold products, so there is one suspension authority per track.

**Trigger.** Per unpaid cycle invoice of the billing, `T = due_at + suspend_after_days`. The job acts on the earliest unpaid cycle invoice whose `T` satisfies `W <= T <= now`, when that invoice still has a balance and the service is active. An unpaid invoice whose `T` is before `W` never triggers a suspension; the next unpaid cycle invoice can.

**Action.** Lock service billing, then the invoice (lock order); re-check; claim `service:{id}:suspend:invoice:{invoice_id}`; set the billing's status to `suspended_nonpayment` (`ServiceBillingStatus`, `app/models/reseller.py:131`) and `last_failure_code = "nonpayment"`; commit; then suspend the service through `ServiceLifecycle.suspend` (`app/services/service_lifecycle.py:92`, async, own transactions, outside the billing locks) as the second step of the two-transaction pattern (9.3); queue the `service_suspended` email (with `reason = "nonpayment"`).

**Unsuspend on payment (retail).** New settlement subscriber `unsuspend_after_payment` in `app/services/billing/settlement_subscribers.py`, after `complete_billing_cycle`: for a retail cycle invoice whose billing is `suspended_nonpayment` and which has no other unpaid cycle invoice, it queues the service for unsuspension, but only when the service's current suspension is the one this job made (a `done` claim `service:{id}:suspend:...` and no later suspension by staff or abuse handling in the service's audit trail); a service suspended for another reason is left suspended and listed for staff; the unsuspension runs after the settlement commits, beside `run_deferred_fulfilment` (`app/services/billing/settlement.py:673-706`), because the lifecycle call does I/O and must not run inside the settlement transaction. The reseller unsuspend path in the worker (`recurring_billing_service.py:796-799`, `:840-855`) is unchanged.

### 10.5 `nonpayment_termination` (B7, both tracks)

**Policy.** As 10.4, with `terminate_after_days` (NULL: never, the seeded default) and `termination_notice_days` (default 7). Applies to retail and reseller services; nothing terminates for non-payment today, so every termination is new behaviour and needs a policy the operator set.

**Two actions per service.**

- *Notice:* `T_n = due_at + terminate_after_days - termination_notice_days`. Act when `W <= T_n <= now` and the service is suspended for non-payment (retail: billing `suspended_nonpayment`; reseller: suspended by the recurring pass for that cycle). Claim `service:{id}:termination_notice:invoice:{invoice_id}`; queue `service_termination_warning`; emit `service.termination_scheduled`.
- *Terminate:* `T_t = due_at + terminate_after_days`. Act only if the notice claim for the same invoice is `done` and was made at least `termination_notice_days` before now, the invoice still has a balance and the service is still suspended for non-payment. Claim `service:{id}:terminate`, commit, call `terminate_service` (`app/services/commerce_lifecycle_service.py:33`, which audits and raises if the power-off fails, A0 spec 14.2.3), then mark `done` or `failed` (a failed termination becomes `needs_review`, never retried automatically). Then the policy's `unpaid_invoice_action_on_terminate`: `keep` (default; the debt stays open) or `cancel` (each unpaid invoice of the service through `cancel_unpaid_invoices`, 10.6). Queue `service_terminated`.

**Retroactive guard:** the notice anchor `T_n >= W` and the rule that termination needs a sent notice mean that turning the job on cannot terminate anything for at least `termination_notice_days`, and never a service whose notice time had already passed.

**Preconditions for live:** at least one policy with `terminate_after_days` set; templates `service_termination_warning` and `service_terminated` enabled; the operator confirms with the count of services the forecast shows for the next 30 days.

### 10.6 Cancellation requests (B8)

**Service.** `CancellationService` in `app/services/billing/cancellations.py` (new):

- `request(db, *, service_id, type, reason, actor_user_id, source, idempotency_key, invoice_action="cancel_unpaid") -> CancellationRequest`: locks the service billing first (as `request_cancellation` does since A0, spec 14.2.3), writes the request. `end_of_period` sets `effective_at` from `next_charge_at` and delegates to the existing `CommerceLifecycleService.request_cancellation` (`app/services/commerce_lifecycle_service.py:100`) for `cancel_at`; status `scheduled`. `immediate` delegates to the existing immediate path; status `completed`, or `scheduled` with `termination_pending` when the power-off failed (A0 behaviour).
- In both cases, in the same transaction, every unpaid invoice of the service whose period starts at or after `effective_at` (an invoice generated ahead for a period the client will not have) is cancelled through `cancel_unpaid_invoices`. This is a client or admin action, not automation, so it is not subject to the job watermark.
- **`cancel_unpaid_invoices` reuses phase 1's cancellation helpers, never a direct `transition(VOID)`.** The phase 1 core status review found that direct `transition(VOID)` calls skip `void()`'s checks (fixes merged at `8e855ac`). In `v2` each invoice goes through `void_cancelled_invoice_v2` (`app/services/billing/order_refunds.py:337`): `void()` with its checks under the reseller, account and invoice locks; when `void()` refuses and no money is held, a `cancel` credit note (`close_kept_invoice_v2`, `:358`); when money is held, a refund review, never a silent void. In `legacy` and `shadow` it makes the same decision the cycle cancellation path makes since those fixes (`_void_cancelled_cycle_invoice`, `app/services/recurring_billing_service.py`), deciding from the locked row.
- `withdraw(db, request_id, *, actor_user_id)`: only while `scheduled` and before `effective_at`; clears `cancel_at` under the service billing lock; status `withdrawn`.

**Job `cancellations`.** Trigger `T = effective_at`. Act when `W <= T <= now`, the request is `scheduled` and the service is terminated: claim `cancellation:{id}:complete`; if `invoice_action = cancel_unpaid`, cancel the service's remaining unpaid invoices as above; status `completed`; queue `cancellation_completed`. A request whose service is not yet terminated is left to the recurring pass, which terminates at `cancel_at` today.

**Refunds.** Pro-rata refund on cancellation needs A6 and stays off (decided default 2, `billing_refund_on_cancel` false); phase 2 adds no refund path.

**Routes.** The existing client route `POST /api/client/commerce/services/{service_id}/cancel` (`app/api/commerce_client.py:728`) keeps its request and response shape and now goes through `CancellationService.request`, adding `request_id` to the response. New: `GET /api/client/commerce/cancellation-requests`, `POST /api/client/commerce/cancellation-requests/{id}/withdraw`; admin `GET /api/admin/billing/cancellation-requests` (queue with filters), `POST` (on behalf), `POST .../{id}/withdraw`; reseller panel the same for its own services. **WHMCS visibility:** none; the WHMCS terminate route (`DELETE /api/billing/services/{service_id}`) is unchanged and writes no request.

### 10.7 `tax_id_recheck`

Re-checks profiles with an EU VAT number whose `valid` check is older than 25 days or whose status is `unavailable` (at most every 15 minutes per profile), through `TaxIdService.check(force=True)` (7.4), at most `billing_tax_id_recheck_batch` (200) per run. It writes only checks and profile status, never invoices. Its watermark applies like any job's but has no practical effect (re-checking is not an action on a past trigger).

## 11. No retroactive schedules

### 11.1 Definition

A job acts **retroactively** if, when it is switched on (or its schedule settings are changed), it acts on a subject whose trigger time under the new schedule is earlier than that moment. "Trigger time" is defined per job in section 10 (`T`). A job's effective watermark is

`W = max(live_since, config_since)` for a live run, and `W = dry_run_since` (also moved forward by `config_since` when the settings change) for a dry run,

and a job acts on a subject only when `W <= T <= now` (and `T >= now - billing_job_max_catch_up_days` for catch-up, 9.5). Sequenced jobs additionally anchor the sequence: the first step's trigger must be at or after `W`, and later steps require the earlier step's claim (`invoice_reminders`, `nonpayment_termination`).

### 11.2 How the code enforces it

- `live_since` is written only by the mode endpoint (9.2), to the server's `now`, and cleared when the job leaves `live`.
- At the start of every live run the runner computes `config_hash` over the job's `setting_keys` values and, for B7 jobs, the rows of `billing_nonpayment_policies`; if it differs from the stored hash, `config_since = now` and the new hash are stored before any item is processed. So shortening `billing_late_fee_days` from 14 to 3 does not charge every invoice that is now more than 3 days late; only invoices whose new trigger falls after the change are charged.
- The watermark is applied in `plan()` through one shared helper, `JobRun.in_window(trigger_at) -> bool`, and asserted again in `execute()` on the locked rows; a unit test per job sets `live_since` after a trigger and asserts no action, and a property test generates schedules, watermarks and clocks and checks that no action ever has `trigger_at < W`.
- Off periods are never caught up: leaving `live` clears the watermark and coming back sets a new one.
- Per job, the result (with the trigger definitions of section 10): generation ahead never creates an invoice for a billing that was already inside its lead window; reminders never send a notice whose time had passed, nor a later notice without the earlier one; late fees never charge an invoice that was already past its fee day; suspension never suspends a service whose suspension day had passed (it waits for the next unpaid cycle); termination never terminates without a notice sent under the live job at least `termination_notice_days` earlier; cancellation follow-up never acts on requests effective before the watermark.

## 12. Email templates (F4)

### 12.1 Rendering

`app/services/email_templates/` (new):

- `catalogue.py`: `EVENTS: dict[str, EventTemplateSpec]`, each with its documented variables (name, type, description, sample value) and the built-in default subject and bodies (moved from `app/services/email_message_service.py:32-`, which then reads them from here). Variables are plain data (strings, integers, ISO dates, lists of dicts); never ORM objects.
- `render.py`: `render(db, event_key, *, locale, variables, product_id=None) -> RenderedEmail(subject, text, html, template_id, template_version, fallback)`. Two `jinja2.sandbox.ImmutableSandboxedEnvironment`s (Jinja2 3.1.2 is installed) with `undefined=StrictUndefined`: text with `autoescape=False`, HTML with `autoescape=True`; no loader (no `include`, `import` or `extends`); filters limited to the built-ins plus `money(cents, currency)` (formats with the integer helpers, no floats) and `date(value, format)`; template source at most 64 KB and output at most 256 KB.
- Template choice: `(event_key, locale, product_scope=product_id)`, then `(event_key, locale, 0)`, then `(event_key, "en", 0)`, then the built-in default. A disabled row means "do not send": the message is stored with status `skipped` (`EmailMessageStatus.SKIPPED`, `app/models/commerce_email.py:43`).
- Rendering happens at enqueue, and the rendered subject and bodies are stored on the message with `template_id` and `template_version`, so editing a template never changes mail already queued.

### 12.2 Events

Existing (`email_message_service.py:33-44`): `welcome`, `verify_email`, `password_reset`, `user_invite`, `order_confirmation`, `invoice_created`, `invoice_paid`, `invoice_overdue`, `payment_failed`, `service_provisioned`, `service_suspended`, `ticket_reply`. New: `invoice_reminder`, `late_fee_added`, `service_suspended_nonpayment` (seeded disabled; the non-payment suspension's own text, so `service_suspended` keeps its current wording), `service_termination_warning`, `service_terminated`, `cancellation_requested`, `cancellation_completed`, `tax_id_unverified` (sent once per profile change when the check ends `invalid` or `format_invalid`).

Senders added in phase 2 (spec B4, "the events with no sender"): `invoice_overdue` and `invoice_reminder` (10.2), `service_suspended_nonpayment` (10.4), `late_fee_added` (10.3), the termination and cancellation events (10.5, 10.6), `payment_failed` at the card webhook's failure branch (`app/services/payments/webhooks.py`, idempotency key `payment:{id}:failed`), `service_provisioned` at successful fulfilment (`CheckoutService.mark_paid_and_fulfill`, key `service:{id}:provisioned`), `welcome` after email verification (key `user:{id}:welcome`, per-product variant chosen by the first order's product), `ticket_reply` at a staff reply (key `ticket_reply:{id}`). Each sender passes only the catalogue's variables.

### 12.3 Failure behaviour

A template that fails to render at enqueue (an undefined variable the validation missed, a sandbox security error) falls back to the built-in default for that event, sets `render_fallback = true`, audits `billing.email_template_render_failed` with the template id and error, and raises the staff alert `email_template_render_failed`. A money email (invoice, reminder, late fee, suspension, termination) is never dropped because of a template.

### 12.4 Admin API and UI

`app/api/email_templates_admin.py` (new, admin only, audited): `GET /api/admin/commerce/email-templates`; `GET /api/admin/commerce/email-templates/events` (each event with its variables and samples); `POST /api/admin/commerce/email-templates` (a locale or product variant); `GET/PUT /api/admin/commerce/email-templates/{id}` (`PUT` validates by rendering both bodies against the samples with `StrictUndefined`; 422 with the line and message on error); `POST .../{id}/preview` (optional variable overrides; never sends); `POST .../{id}/test-send` (to the calling admin's address, or a given address; at most 10 per admin per hour; the message is marked `test`); `POST .../{id}/reset` (saves the built-in text as a new version); `GET .../{id}/versions`; `POST .../{id}/restore/{version}`. UI at `/admin/commerce/email-templates`: event list, editor with variable sidebar, side-by-side text and HTML preview, test send, version history.

### 12.5 No new email at deploy

Templates for events that had no sender before phase 2 (`welcome`, `payment_failed`, `service_provisioned`, `ticket_reply`, `invoice_overdue`, and every new event of 12.2) are seeded with `enabled = false`, so deploying phase 2 sends nothing new; the operator enables each. The seeded text of the events that already send is identical to today's hard-coded text, so their mail does not change. P2-E establishes from the code (every `enqueue` caller on main) exactly which events send today, seeds those enabled and every other one disabled, and lists both sets in its report; the list above is the design's reading and the code decides.

## 13. Changes visible to WHMCS, frozen APIs and legacy behaviour

- **WHMCS (`/api/billing`, `tests/contract/`):** no route, field or shape changes; the contract tests do not change. One behaviour an operator can cause: when `nonpayment_termination` is live with a policy, a Rackflow-billed or reseller service terminated for non-payment shows `service_status: terminated` on `GET /api/billing/services/{service_id}/status`, and a retail service suspended by `nonpayment_suspension` shows suspended. Both values exist today (WHMCS terminate and suspend produce them), so the module needs no change. Services billed by WHMCS have no `service_billings` row and are never selected by any job.
- **Legacy `/tax-rates` admin routes:** same shapes; each write also syncs the mirrored rule (section 3). After the tax switch they are a façade with a `Deprecation` header for one release.
- **Client and reseller APIs (additive only):** profile `is_business`, `tax_id_type`, `tax_id_status`, `tax_id_checked_at`; quote `tax_lines`, `tax_summary`, `legal_notes`, `reverse_charge`; invoice serialisers `tax_summary`, `legal_notes`, `tax_point_at`; cancel route response `request_id`; new cancellation request routes. `status` values unchanged (P1 open question 4).
- **With the tax engine `v2` (operator action):** retail renewal invoices carry tax (`billing_tax_retail_renewals`); quotes and invoices show per-rate tax; reverse charge applies to EU businesses with a valid VAT number; PDFs gain the tax column, summary, title and legal notes. Reseller invoices are unchanged unless `billing_tax_resellers_enabled`.
- **With jobs live (operator action, each on its own):** invoices before the due date, reminders, late fee debit notes, retail suspension, termination. With every job `off` (the default) nothing changes, including the recurring worker: its at-due path reuses a pre-generated cycle only when one exists.
- **Cancellation:** a cancellation now also cancels invoices already generated for periods after the effective date (only possible once generation ahead is live).

## 14. Phase 2 worker split

### 14.1 Waves

- **Wave 1: P2-S, foundations** (alone). Every model and column of section 1; the migrations of 2.2 (chain in 14.4) with their tests (from empty included, 2.1); every setting of appendix A in `app/services/settings_store.py`; catalogue permissions and events of 8.3 and 8.8 in `app/plugin_host/catalogue.py`, together with the version bump of 8.1 (`API_VERSION = "1.1"`, `SUPPORTED_API_VERSIONS`, compatibility when a range allows any supported version, `effective_api` recorded), the regenerated `docs/plugins/api-reference.md` and a new `docs/plugins/api-surface-1.1.json` (`api-surface-1.0.json` stays as published). The developer kit's reference test pins the 1.0 surface, and its deprecation policy puts new permissions and events in a new minor version, so the additions and the bump land together; the rounding rules of appendix B with property tests (working, they are small); `billing_engine_mode(db, "tax")` and the managed-setting protection for `billing_tax_engine`; the public interfaces of 14.3 as stubs raising `NotImplementedError`; the new MariaDB tests registered in `.forgejo/workflows/billing-mariadb.yml`.
- **Wave 2, in parallel, isolated branches after P2-S merges:** P2-T, P2-K, P2-P, P2-J, P2-L, P2-E. None adds a migration; a needed schema change goes to the manager, who chains a follow-up revision after the head.
- **Wave 2 follow-up: P2-X, sterling VAT (4.10)**, after P2-T merges: the `tax_exchange_rates` table and its migration after the head, the `currency_rates` kind, the bundled `hmrc_exchange_rates` plugin, the fetch job, the admin rate routes, the publish and note conversion, the PDF line, the detector and the switch item, and the operator currency helper. Opus, high.
- **Wave 3: P2-UI**, all screens, after the wave 2 APIs merge.
- **Reviews:** an Opus 5.5 high reviewer on each area of section 17 before merge to main.

Suggested wave 2 merge order: P2-K, P2-P, P2-T, P2-E, P2-J, P2-L (P2-L last because it edits one function in a P2-T file and calls P2-J's runner).

### 14.2 File ownership

| Worker | Owns (creates or edits) | Model and effort |
|---|---|---|
| **P2-S** foundations | `app/models/tax.py`, `app/models/billing_jobs.py`; new columns in `app/models/reseller.py`, `commerce_account.py`, `commerce_order.py`, `storefront.py`, `commerce_email.py`, `plugin_host.py`; `alembic/versions/billing_p2_*.py` and `plugin_p2_effective_api.py` and their tests; `app/services/settings_store.py` (phase 2 definitions); `app/plugin_host/catalogue.py` (8.3, 8.8 entries, `API_VERSION`, `SUPPORTED_API_VERSIONS`); the version check in `manifest.py:459` and `loader.py:86`; `docs/plugins/api-reference.md` and `api-surface-1.1.json` (generated); `app/services/billing/rounding.py` (appendix B); stubs of 14.3; `.forgejo/workflows/billing-mariadb.yml`; stub functions in `app/services/alerting/billing_integrity.py` | Opus, high: migrations and money rounding |
| **P2-T** tax engine | `app/services/tax/` except `packs/`, `pack_import.py`, `validators.py`, `id_formats.py`, `legal_notes.py`; `app/services/invoice_service.py` (tax at create and publish, `tax_result`, snapshots), `app/services/invoice_guard.py` (tax rows frozen), `app/services/invoice_pdf_service.py`, `app/services/adjustment_pdf_service.py`, `app/services/invoice_adjustment_service.py` (tax reversal 4.8), `app/services/checkout_service.py` (`_tax_result`, evidence capture), `app/api/commerce_client.py` (profile and quote fields), `app/api/commerce_store_admin.py` (`/tax-rates` façade, product tax class), new `app/api/billing_tax_admin.py`, `app/api/billing_engines_admin.py` (tax switch), `app/services/billing_shadow.py` (tax checks and scan), its detectors in `billing_integrity.py`, tests | Opus, high |
| **P2-K** plugin API 1.1 | `app/plugin_host/manifest.py` (`provides`), `loader.py` (provider registration, bundled plugin folder), new `kinds.py`, `providers.py` (stubbed by P2-S), `health.py` (provider failure audit), the regenerated API reference and 1.1 surface after its additions, the SDK module `rackflow_plugin.kinds`, `docs/plugin-system.md` section 17.1 and a new 1.1 section, `plugin_packages/vies_validator/` (manifest, code, recorded fixtures, tests), a fixture plugin per kind under `tests/plugin_host/fixtures/` | Sonnet, high |
| **P2-P** packs and tax IDs | `app/services/tax/packs/uk_vat.json`, `eu_vat.json`, `app/services/tax/packs/registry.py`, `pack_import.py`, `validators.py` (`TaxIdService`), `id_formats.py`, `legal_notes.py`, the appendix C golden tests, `docs/billing-tax-packs.md` (operator guide: packs are starting points, not advice) | Opus, high: legal data |
| **P2-J** scheduler, B3, B4 | `app/services/billing/jobs/__init__.py`, `base.py`, `registry.py`, `runner.py` (lease, claim, watermark, dry run, catch-up), `generate_ahead.py`, `reminders.py`, `tax_id_recheck.py`; `app/services/billing/periodic.py` (job list); `app/workers/recurring_billing.py`; `app/services/recurring_billing_service.py` (at-due reuse of a pre-generated cycle, 10.1); new `app/api/billing_jobs_admin.py`; its detectors (`billing_job_needs_review`, `billing_job_stale`); tests including the MariaDB claim and race tests | Opus, high: claim mechanism and the recurring worker |
| **P2-L** B5, B7, B8 | `app/services/billing/jobs/late_fees.py`, `suspension.py`, `termination.py`, `cancellations.py` (job); new `app/services/billing/cancellations.py` (`CancellationService`); `app/services/commerce_lifecycle_service.py`; `app/services/billing/settlement_subscribers.py` (`unsuspend_after_payment`); `app/services/billing/settlement.py` (deferred unsuspend beside `run_deferred_fulfilment`); new `app/api/billing_cancellations.py` (client, reseller and admin routes); the one cancel route function in `app/api/commerce_client.py:728` (after P2-T merged); policy admin routes in the same new file; tests | Opus, high: suspensions, terminations and debit notes |
| **P2-E** email templates | `app/services/email_templates/` (new), `app/services/email_message_service.py`, new `app/api/email_templates_admin.py`, `app/services/payments/webhooks.py` (the `payment_failed` sender only), the support ticket staff-reply sender, `app/services/billing/email_events.py` (one small function per new sender that other workers call: `send_service_provisioned`, `send_welcome`), tests including sandbox escape attempts | Sonnet, high |
| **P2-UI** screens (wave 3) | `frontend/`: tax admin (6.1), account tax panel, product tax class, jobs page with runs, items and forecast, nonpayment policies, cancellation queue (admin, client, reseller), email template editor, client profile VAT fields and quote tax display | Sonnet, high (the manager may split it into admin and client screens, as phase 1 did) |

Shared files and how: `catalogue.py` (P2-S only); `manifest.py` and `loader.py` (P2-S for the version check, then P2-K); `docs/plugins/api-reference.md` and `api-surface-1.1.json` (generated; P2-S, then P2-K regenerates); `commerce_client.py` (P2-T, then P2-L's one function); `checkout_service.py` (P2-T; the `send_service_provisioned` call is inserted by P2-T calling P2-E's stubbed function); `settlement.py` (P2-L only in phase 2); `billing_integrity.py` (each worker its own stub functions). `recurring_billing_service.py` is P2-J's only.

### 14.3 Interfaces between workers (stubbed by P2-S)

```python
# P2-T
TaxEngine.calculate(db, context: TaxContext) -> TaxResult
TaxEngine.prepare(db, context: TaxContext) -> TaxResult          # may call a tax_provider; never under a lock
TaxContextBuilder.for_checkout(db, account, profile, lines, now, evidence) -> TaxContext
TaxContextBuilder.for_invoice(db, invoice, lines, now) -> TaxContext
TaxContextBuilder.for_adjustment(db, invoice, adjustment_lines) -> TaxContext
tax_engine_mode(db) -> str                                        # "legacy" | "shadow" | "v2"
InvoiceService.create(..., tax_result: TaxResult | None = None)   # existing signature plus one keyword
InvoiceService.create_cycle(..., tax_result: TaxResult | None = None)

# P2-K
call_provider(kind: str, provider_ref: str, method: str, *args) -> Any   # raises only ProviderUnavailable(reason)
providers_of(kind: str) -> list[ProviderRef]                             # enabled, circuit closed

# P2-P
TaxIdService.check(db, profile, *, force: bool = False) -> TaxIdCheck
PackRegistry.list(db) -> list[PackInfo]; PackRegistry.load(db, pack_id, version) -> PackData
PackImporter.preview(db, pack_id, version, effective_from) -> PackDiff
PackImporter.apply(db, pack_id, version, effective_from, *, diff_hash, actor_user_id) -> PackDiff
render_legal_note(db, key, locale, **values) -> str

# P2-J
run_job(db_factory, name: str, *, dry_run: bool, trigger: str, now: datetime) -> JobRunResult
JobRun.in_window(trigger_at: datetime) -> bool
JobRun.claim(db, subject_key: str) -> bool                       # False: already done (unique key)
BillingJob protocol (9.1); JOBS registry

# P2-L
CancellationService.request(db, *, service_id, type, reason, actor_user_id, source, idempotency_key, invoice_action="cancel_unpaid") -> CancellationRequest
CancellationService.withdraw(db, request_id, *, actor_user_id) -> CancellationRequest
cancel_unpaid_invoices(db, service_id, *, from_period_start: datetime | None, cause: str) -> list[int]

# P2-E
render(db, event_key, *, locale, variables, product_id=None) -> RenderedEmail
EmailMessageService.enqueue(..., variables: dict | None = None)  # renders through templates when given
email_events.send_service_provisioned(db, service) / send_welcome(db, user)
```

### 14.4 Migration chain

```
billing_p1_ledger_owner_check   (current head; confirm with `alembic heads`)
  -> plugin_p2_effective_api     (plugin_installations.effective_api)
  -> billing_p2_tax_schema
  -> billing_p2_tax_columns      (also billing_accounts.billing_late_fee_exempt)
  -> billing_p2_tax_seed
  -> billing_p2_automation
  -> billing_p2_email_templates  (head)
```

All six are written by P2-S.

## 15. Test plan

### 15.1 Migrations (P2-S)

Each revision: up, down, up on SQLite and on MariaDB 11.8 (`RACKFLOW_TEST_MARIADB_URL`), with data present (a `tax_rates` row with and without a region, a billing profile, an email message); the mirror produces the rows of section 3; the chain from an empty MariaDB database (`tests/core/test_alembic_fresh_mariadb.py`, already in CI); `alembic heads` returns one head.

### 15.2 Unit and service tests per worker

- P2-T: rule selection order; reverse charge conditions one by one; exemptions; inclusive and exclusive; compound; both rounding points; quote equals invoice; `tax_result` hash mismatch fallback; tax rows frozen after publish (guard in `enforce`); credit note by lines and by amount reverse tax exactly; legacy mode writes nothing new; every shadow check key; the switch criterion items and rollback; `tests/services/test_recurring_billing_service.py` green in all three tax modes.
- P2-K: a 1.0 manifest with `"==1.0"`, `">=1.0"` and `">=1.0,<2"` loads unchanged; `provides` refused for a 1.0-only range; each kind's protocol check; timeout, exception and invalid answers become `ProviderUnavailable` and count for the circuit; rate limit shared across two processes (Redis) and per process when Redis is down; VIES mapping of every test-service code.
- P2-P: appendix C golden cases as a table-driven test through `TaxEngine.calculate`; pack schema; preview and apply diffs; supersede from `effective_from`; refusal of past dates; tax ID normalisation and formats for the 27 member states and GB; cache horizons; fail-open and fail-closed.
- P2-J: watermark per job (trigger before `live_since` never acts; trigger after acts once); config change moves the watermark; catch-up limit; dry run changes no non-job table (row-count snapshot of every table); forecast; mode endpoint rules; generation ahead then at-due charge for a reseller (one cycle, one invoice, charged at due); reminder sequence anchoring.
- P2-L: late fee amounts and clamps (rule `late_fee_percentage`), currency skip, exemptions, once per invoice; suspension and unsuspension after payment (deferred, after commit); termination needs the notice and its age; failed termination becomes `needs_review`; cancellation cancels invoices generated ahead; withdraw; reseller suspension untouched.
- P2-E: sandbox refuses attribute access to internals (`__class__`, `mro`), `include`, oversized templates; `StrictUndefined` errors at save; fallback at enqueue; disabled template skips; rendered text stored at enqueue; test-send rate limit; seeded texts equal today's.

### 15.3 Property tests (hypothesis)

`tax_exclusive` and `tax_inclusive` (net plus tax equals gross for inclusive; monotonic in base); `invoice_rate` spreading sums exactly; credit note tax of a full credit equals the line tax and partial credits never exceed it in total; late fee within bounds; job windows never act on `trigger_at < W`.

### 15.4 E2 integrity detectors

`tax_line_sum` (line tax equals its rows), `tax_summary_consistent` (summary equals rows), `tax_evidence_conflict`, `tax_not_registered` (4.3 step 2), `billing_job_needs_review`, `billing_job_stale` (a live job whose last scheduled run is older than twice its cadence), `email_template_render_failed`.

### 15.5 MariaDB concurrency tests (each added to `.forgejo/workflows/billing-mariadb.yml`)

- `tests/services/test_billing_p2_job_claim_mariadb.py`: two sessions claim the same subject key concurrently, exactly one effect; two processes start the same job, the lease admits one; a run that loses its lease stops.
- `tests/services/test_billing_p2_generate_ahead_mariadb.py`: the generation job and the recurring worker's at-due path race on one billing: one cycle, one invoice, one charge.
- `tests/services/test_billing_p2_late_fee_mariadb.py`: two runs and a concurrent payment on one invoice: at most one debit note, none when the payment commits first.
- `tests/services/test_billing_p2_cancel_mariadb.py`: a cancellation request and the generation job on one billing: either no invoice is generated or it is cancelled in the same transaction as the request.
- `tests/services/test_billing_p2_tax_publish_mariadb.py`: a rule supersede committed during a publish: the invoice uses exactly one consistent set of rules (those valid at its tax point), and the rule rows are never locked by the publish.
- `tests/migrations/test_billing_p2_mariadb.py`: the chain of 14.4.

## 16. Rollout after phase 2 merges

1. Deploy. Tax engine `shadow` (default) with tax disabled and only mirrored rules, so behaviour is today's; every job `off`; new email events disabled (12.5).
2. Invoice engine to `v2` through the phase 1 switch if not done (a precondition of the tax switch, of generation ahead and of late fees).
3. Configure seller registrations, apply the UK (and if needed EU) pack with an `effective_from` in the future, set product classes, enable the VIES plugin with the requester VAT number. Watch `t1_checkout`, `t1_rule_divergence` and `t2_uncovered` until the quiet period passes, then switch the tax engine to `v2`.
4. Enable templates, then put jobs into `dry_run` one by one and read a week of run items before `live`, in this order: `tax_id_recheck`, `invoice_reminders`, `generate_invoices_ahead`, `cancellations`, `late_fees`, `nonpayment_suspension`, `nonpayment_termination` last.
5. Later phase: remove the `/tax-rates` façade.

## 17. Areas that need an Opus 5.5 high review

1. Tax amounts: rounding rules, rounding points, inclusive pricing, compound order, the discount base, credit note and debit note tax (P2-T, appendix B).
2. Legal logic and data: place of supply, reverse charge conditions, registrations, legal notes, the UK and EU packs and the appendix C cases (P2-P). A qualified adviser should also read appendix C; the review cannot replace that.
3. Tax shadow checks, switch criterion and rollback (P2-T, section 5).
4. The scheduler: lease, claim, watermark, catch-up and dry-run isolation (P2-J, sections 9 and 11).
5. Generation ahead and the recurring worker's at-due reuse (P2-J, 10.1): the one change to the reseller reference path.
6. Late fees, suspension and the deferred unsuspend after settlement, termination, cancellation of unpaid invoices (P2-L).
7. The migrations (P2-S).

Security review (not a money review): the email template sandbox (P2-E) and provider calls, rate limits and data passed to plugins (P2-K).

## 18. Open questions for the owner

Each has a recommended default that phase 2 builds unless the owner decides otherwise.

1. **Tax rounding per pack.** *Recommended: UK and EU both per invoice per rate, half up.* HMRC also allows rounding the invoice's VAT down; some EU states have their own rules. Confirm with the adviser.
2. **Tax on retail renewals.** Renewal invoices carry no tax today. *Recommended: taxed in `v2`* (`billing_tax_retail_renewals = true`), since VAT is due on every supply.
3. **Late fees and VAT.** *Recommended: outside the scope of VAT* (compensation for late payment, not a supply), class `outside_scope`. Confirm.
4. **Invoices in other currencies with UK VAT.** A UK VAT invoice in another currency must show the VAT total in sterling. *Decided by the owner (2026-11-22), replacing the recommendation to refuse until D4: never refused; the VAT total is shown in sterling at HMRC's monthly rate for the tax point's month, frozen at publish (4.10).*
5. **Location evidence.** *Recommended: phase 2 records billing and IP country and flags conflicts; no automatic choice between them.*
6. **Proforma and rate changes.** *Recommended: tax fixed at proforma publish; a rate change before payment is flagged for a manual note.*
7. **Automatic pack updates.** *Recommended: no; an admin previews and applies every update.*
8. **What Rackflow sells, for VAT.** *Recommended: treat VMs, bare metal, proxies, storage and backup slots as electronically supplied services (class `digital_service`).* Dedicated bare-metal rental and anything like colocation could instead be hire of equipment or a service connected to property, with a different place of supply. Adviser question.
9. **UK seller, consumers outside the UK.** *Recommended: no UK VAT on B2C digital services to consumers outside the UK; EU consumers need the seller's non-Union OSS registration, and without one the engine charges nothing and alerts.* Confirm the operator's registrations.
10. **Reverse charge wording.** *Recommended: "Reverse charge" (EU businesses, Article 196 of Directive 2006/112/EC) and, from a UK seller, "Outside the scope of UK VAT: the customer accounts for any VAT due".* Adviser to confirm.
11. **Northern Ireland.** *Recommended: for services, Northern Ireland is the UK (GB VAT); `XI` numbers are not used for services.* Confirm.
12. **Plugin package folder.** *Decided by the billing manager:* first-party plugins in `plugin_packages/`, listed through a read-only bundled discovery root (7.5); the developer kit's teaching examples stay in `examples/plugins/`. No owner decision needed.
13. **Unpaid invoices at termination.** *Recommended: keep them open as debt (`keep`)*; `cancel` is available per policy.
14. **VAT on reseller invoices.** *Recommended: off by default (`billing_tax_resellers_enabled = false`)*, so reseller invoices stay as they are; turn on deliberately.
15. **Tax ID check when VIES is down.** *Recommended: fail open (`charge_tax`)*; renewals never blocked.
16. **UK domestic reverse charge.** It covers construction, specified goods such as mobile phones and computer chips, wholesale telecoms and energy, not hosting, so the UK pack has no domestic reverse charge rule. Confirm.

## Appendix A. Phase 2 settings

All `SettingDef` entries in `app/services/settings_store.py` with a `fallback`; groups `billing_tax`, `billing_automation`, `billing_email`. Job modes are not settings (section 9.2).

| Key | Type | Choices or range | Default |
|---|---|---|---|
| `billing_tax_engine` | str | `legacy`, `shadow`, `v2` | `shadow` (managed by the switch endpoint) |
| `billing_tax_enabled` | bool | | false (pack and admin rules, registrations and reverse charge; mirrored `tax_rates` rules apply either way, 4.3 step 1) |
| `billing_tax_seller_country` | str | ISO 3166-1 alpha-2 or empty | `""` (4.3 step 2) |
| `billing_tax_prices_include_tax` | bool | | false |
| `billing_tax_retail_renewals` | bool | | true |
| `billing_tax_resellers_enabled` | bool | | false |
| `billing_tax_provider` | str | `""` or `<plugin_id>:<provider_id>` | `""` |
| `billing_tax_id_validators` | str | comma-separated provider refs | `""` |
| `billing_tax_id_unavailable_policy` | str | `charge_tax`, `block_checkout` | `charge_tax` |
| `billing_tax_id_check_timeout_seconds` | int | 1 to 30 | 8 |
| `billing_tax_id_valid_days` | int | 1 to 365 | 30 |
| `billing_tax_id_max_age_days` | int | 1 to 365 | 30 |
| `billing_tax_id_recheck_batch` | int | 1 to 5,000 | 200 |
| `billing_tax_ip_country_header` | str | header name or empty | `""` |
| `billing_automation_time_utc` | str | `HH:MM` | `09:00` |
| `billing_job_lease_seconds` | int | 60 to 3,600 | 600 |
| `billing_job_max_catch_up_days` | int | 1 to 30 | 3 |
| `billing_job_min_dry_runs` | int | 0 to 30 | 1 |
| `billing_job_stuck_minutes` | int | 5 to 1,440 | 60 |
| `billing_job_run_retention_days` | int | 7 to 365 | 90 |
| `billing_job_tracks_<job>` (for `generate_invoices_ahead`, `invoice_reminders`, `late_fees`, `nonpayment_termination`, `cancellations`) | str | `both`, `reseller`, `retail` | `both` |
| `billing_invoice_lead_days` | int | 0 to 60 | 0 |
| `billing_reminder_upcoming_days` | int | 0 to 30 | 3 |
| `billing_reminder_overdue_days` | str | up to three ascending integers, 1 to 365 | `1,7,14` |
| `billing_late_fee_type` | str | `none`, `fixed`, `percent` | `none` |
| `billing_late_fee_fixed_cents` | int | 0 to 1,000,000,000 | 0 |
| `billing_late_fee_currency` | str | ISO 4217 | `USD` (the `InvoiceService.create` default) |
| `billing_late_fee_percent_bp` | int | 0 to 10,000 | 0 |
| `billing_late_fee_min_cents` | int | 0 to 1,000,000,000 | 0 |
| `billing_late_fee_max_cents` | int | 0 to 1,000,000,000 (0: no maximum) | 0 |
| `billing_late_fee_days` | int | 1 to 365 | 14 |
| `billing_late_fee_min_balance_cents` | int | 1 to 1,000,000,000 | 1 |

Consistency rules: `billing_late_fee_min_cents <= billing_late_fee_max_cents` when the maximum is non-zero; `billing_reminder_overdue_days` ascending; `billing_tax_id_max_age_days <= billing_tax_id_valid_days` is not required (a shorter cache than freshness only causes more checks). Nonpayment policies and tax rules are data, not settings.

## Appendix B. New named rounding rules

In `app/services/billing/rounding.py` beside P1 appendix B; pure integer functions with property tests.

| Rule | Used for | Definition |
|---|---|---|
| `round_half_up(n, d)` | primitive | integer division, ties away from zero (`d > 0`) |
| `round_down(n, d)` | primitive | floor division for `n >= 0` |
| `tax_floor` | the mirrored `tax_rates` rules | `round_down`; reproduces `(taxable * rate_bps) // 10_000` (`checkout_service.py:229`) |
| `tax_exclusive(base, rate_ppm, rule)` | tax added to a net amount | `rule(base * rate_ppm, 1_000_000)` |
| `tax_inclusive(gross, rate_ppm, rule)` | tax inside a gross amount | for `tax_floor` and `round_down` the tax is floored in both modes: tax `rule(gross * rate_ppm, 1_000_000 + rate_ppm)` (the floor of the exact tax) and net `gross` minus that tax; for the other rules the rule rounds the net: tax `gross - rule(gross * 1_000_000, 1_000_000 + rate_ppm)`. Exclusive mode is unchanged |
| `tax_invoice_rate(bases, rate_ppm, rule)` | rounding point `invoice_rate` | tax on Σ bases by the rule above, spread over the lines with `tax_split_largest_remainder` weighted by base |
| `late_fee_percentage(remaining, bp)` | B5 | `round_half_up(remaining * bp, 10_000)`, then clamped to the minimum and maximum |

## Appendix C. UK and EU VAT: the cases the packs implement

The engine produces documents; compliance advice is the operator's (spec 4). These are starting points to be confirmed by an adviser (section 18). "Valid" means a fresh `valid` tax ID check.

**Seller established in the UK** (registration `GB`, optionally `EU_OSS` non-Union):

| Buyer | Tax | Legal note key | Notes |
|---|---|---|---|
| UK consumer | GB VAT standard 20% | none | |
| UK business | GB VAT standard 20% | none | no domestic reverse charge for hosting (question 16) |
| EU business, valid VAT number | none | `gb_outside_scope_b2b` | place of supply is the customer's country; customer VAT number printed |
| EU business, number not valid or unchecked | as an EU consumer | none | evidence records why |
| EU consumer | member state rate under `EU_OSS` (or a domestic registration of that state) if registered; else none and the `tax_not_registered` alert | none | question 9 |
| Rest of world, any | none | `outside_scope` | question 9 |

**Seller established in an EU member state** (registration of that state, optionally `EU_OSS` union scheme):

| Buyer | Tax | Legal note key |
|---|---|---|
| same member state, any | that state's rate | none |
| other member state business, valid VAT number | none (reverse charge) | `eu_reverse_charge` |
| other member state consumer | customer state's rate under `EU_OSS` (union scheme), else the seller's state rate (the operator declares which by its registrations; the 10,000 euro threshold is not tracked in phase 2) | none |
| UK or rest of world, business with a valid number | none | `outside_scope` |
| UK consumer (or UK business without a valid number) | GB VAT if the seller holds a GB registration; else none, `not_registered` alert | none |
| rest of world, consumer | none (place of supply is the customer's country) | `outside_scope` |

**Legal note keys seeded in English:** `gb_outside_scope_b2b`: "Outside the scope of UK VAT: reverse charge. The customer accounts for any VAT due."; `eu_reverse_charge`: "Reverse charge: VAT to be accounted for by the customer (Article 196, Directive 2006/112/EC)."; `outside_scope`: "Outside the scope of VAT."; `exempt_customer`: "VAT exempt: {reason}."; `credit_note_reference`: "Credit note for invoice {number} dated {date}."

**What a phase 2 VAT invoice shows** (UK full VAT invoice and EU Article 226 content): a unique sequential number (P1 series); issue date and the tax point when it differs; seller name, address and the registration's VAT number; buyer name and address, and the buyer's VAT number when reverse charged or a business; description, quantity, unit price excluding VAT, VAT rate and amount per line; totals excluding VAT, VAT per rate and total; discounts; the legal notes. Credit notes add the reference to the original invoice. The sterling VAT total for an invoice in another currency is printed as in 4.10. Deferred: simplified invoices, self-billing, languages other than English, threshold tracking for the OSS union scheme, HMRC VAT number checks, US sales tax (a `tax_provider` plugin later), and Making Tax Digital submission.
