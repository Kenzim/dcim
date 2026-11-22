# Billing phase 1 design: invoice model v2, credit and debit notes, credit balance, refunds, cycles

Status: design for implementation, written 2026-11-21 by worker W0d of the billing programme. Authoritative inputs: `docs/billing-target-spec.md` (A1 to A4, D1, sections 1, 11, 12 and 13), `docs/plugin-system.md` (sections 6.2, 6.2.1, 6.5, 7 and 17.1) and the code at commit 3a5b2b5 of the billing integration branch. Line references are to that commit; phase 0 (W0a, W0b, W0c) will move some of them, so workers should search by function name when a line has moved.

Phase 1 workers build directly from this document. Where it says "must", the behaviour is decided and a worker must not choose differently without asking the billing manager. Where a decision belongs to the owner, it is in section 12 with a recommended default, and workers implement the default.

Contents:

1. Schemas
2. Migration sequence
3. Invoice states and `InvoiceService.transition()`
4. `InvoiceService.balance()` and the call sites that switch to it
5. Numbering, series and proforma
6. A3 credit balance for every account, and the shadow plan
7. A4 refunds and chargebacks
8. D1 cycles
9. Settlement, internal follow-ups and the event catalogue
10. Test plan and integrity invariants
11. Phase 1 worker split, file ownership and migration chain
12. Open questions for the owner, with recommended defaults

Appendix A lists the phase 1 settings. Appendix B lists the named rounding rules.

## 0. Ground rules that apply to every section

- **Money** is integer minor units in `BigInteger` columns named `*_cents` (the codebase's existing convention; "cents" means minor units of the row's currency). Every amount that is computed from other amounts uses a named rounding rule from appendix B; no code may use `round()` or float arithmetic on money.
- **Engines and switches.** The new invoice engine (A1 and A2 money decisions) and the new credit engine (A3) each have a mode setting with values `legacy`, `shadow` and `v2` (appendix A). In `legacy` the old code decides and nothing new is computed. In `shadow` the old code still decides and the new engine computes the same answer beside it and records any difference (section 6.4). In `v2` the new engine decides. A mode only becomes `v2` through a switch endpoint that checks the switch criterion (section 6.5); writing the setting directly is not enough.
- **Single writers.** `InvoiceService` is the only code that writes `invoices` and `invoice_lines`; `InvoiceAdjustmentService` the only writer of `invoice_adjustments`; `CreditLedgerService` the only writer of `credit_ledger_entries` and of both balance caches; `RefundService` the only writer of `refunds` and of `payments.refunded_cents`. Other modules call them.
- **Lock order.** Every transaction that locks more than one of these rows takes the locks in this order, and never takes an earlier one after a later one:
  1. `resellers` row (only for a reseller account),
  2. `billing_accounts` row,
  3. `invoices` rows, ascending `id`,
  4. `payments` rows, ascending `id`,
  5. `refunds` rows, then `invoice_adjustments` rows,
  6. `invoice_sequences` rows (number allocation), always last.

  A shared helper, `BillingLocks.for_invoice(db, invoice_id)` in `app/services/billing_locks.py`, reads the invoice without a lock to learn its account, locks the reseller and account rows, then locks the invoice and re-checks that its account did not change (retrying once if it did). Gateway calls never run while any of these locks is held.
- **Idempotency.** Every money-changing entry point takes an idempotency key, stores it in a unique column, and returns the existing row when the same key arrives again with the same parameters (and raises when the parameters differ), as `CreditLedgerService._append_locked` already does (`app/services/credit_ledger_service.py:89-104`).
- **Frozen surfaces.** No change to `app/api/billing.py` or `whmcs/modules/servers/rackflow/` (neither touches invoices: `rackflow.php` only mentions invoices in a client-area link at line 2789). `/api/client/commerce/*`, `/api/reseller/*` and `/api/reseller-panel/*` only gain fields; existing fields keep their meaning in `legacy` and `shadow` modes. `tests/services/test_recurring_billing_service.py` stays green in every mode.

## 1. Schemas

Types below are SQLAlchemy types. "Enum" means the codebase's `_string_enum(...)` helper (`app/models/reseller.py:42-53`): a `VARCHAR(32)` with no database CHECK constraint (`native_enum=False`, and SQLAlchemy 2.0.51 does not create a constraint by default), so adding enum values needs no migration. All new tables have `created_at DateTime(timezone=True) NOT NULL server_default now()` and, where rows change, `updated_at` with `onupdate=func.now()`, as the existing models do; these are not repeated below.

### 1.1 `document_series` (new, `app/models/billing_series.py`)

One row per numbering series.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | no | autoincrement | primary key |
| `code` | String(16) | no | | unique (`uq_document_series_code`); the `{SERIES}` tag, for example `INV`, `PF`, `CN`, `DN` |
| `document_type` | Enum `DocumentType` | no | | `invoice`, `proforma`, `credit_note`, `debit_note` |
| `name` | String(255) | no | | admin label |
| `format` | String(64) | no | `'{NUMBER}'` | tags `{SERIES}`, `{NUMBER}`, `{YEAR}`, `{MONTH}`; must contain `{NUMBER}` |
| `number_padding` | SmallInteger | no | `0` | zero-pad width of `{NUMBER}`, 0 to 12 |
| `number_source` | String(16) | no | `'own'` | `own` (the series' own counter) or `invoice_number` (reuse the invoice's existing `invoice_number`) |
| `gapless` | Boolean | no | `false` | section 5.2 |
| `reset_period` | String(8) | no | `'never'` | `never` or `yearly` (counter restarts each calendar year, UTC) |
| `default_for` | String(16) | yes | NULL | equals `document_type` on the one default series of that type, else NULL; unique (`uq_document_series_default_for`), which gives "at most one default per type" portably on SQLite and MySQL |
| `enabled` | Boolean | no | `true` | |

Check constraints (small new table, no locking concern):

- `ck_document_series_number_source`: `number_source = 'own' OR (number_source = 'invoice_number' AND document_type = 'invoice' AND gapless = false AND reset_period = 'never')`.
- `ck_document_series_default_for`: `default_for IS NULL OR default_for = document_type`.
- `ck_document_series_padding`: `number_padding >= 0 AND number_padding <= 12`.

Seed rows, inserted by the migration (codes are defaults the admin may rename):

| code | document_type | format | number_source | gapless | default_for |
|---|---|---|---|---|---|
| value of `settings.commerce_invoice_prefix` (`INV` today, `app/core/config.py:156`) | `invoice` | `{NUMBER}` | `invoice_number` | false | `invoice` |
| `PF` | `proforma` | `{SERIES}-{NUMBER}` | `own` | false | `proforma` |
| `CN` | `credit_note` | `{SERIES}-{NUMBER}` | `own` | false | `credit_note` |
| `DN` | `debit_note` | `{SERIES}-{NUMBER}` | `own` | false | `debit_note` |

The seeded invoice series keeps today's visible numbers (`Invoice #100123` stays `100123`); see open question 1.

Counters live in the existing `invoice_sequences` table (`app/models/reseller.py:554-563`), one row per series and period, named `series:<series_id>:<period>` where `<period>` is the empty string for `reset_period = never` and the four-digit year for `yearly`. The existing `invoice` row keeps allocating `invoices.invoice_number`. `name` is `String(64)`, which is ample.

### 1.2 `invoices` (existing, `app/models/reseller.py:494-551`): new columns

All new columns are nullable or have a constant server default, so adding them is instant on MariaDB 11 and needs no backfill. Historic invoices keep NULL in every computed column; they are never backfilled with computed values. `model_version` marks a v2 invoice.

| Column | Type | Null | Default | Frozen after `draft` | Notes |
|---|---|---|---|---|---|
| `model_version` | SmallInteger | yes | NULL | yes | NULL for legacy rows, `2` for invoices created by the v2 code path |
| `document_type` | Enum `InvoiceDocumentType` | no | `'invoice'` | yes, except the proforma conversion (5.4) | `invoice` or `proforma` |
| `subtotal_cents` | BigInteger | yes | NULL | yes | sum of non-discount line nets |
| `discount_cents` | BigInteger | yes | NULL | yes | positive; minus the sum of discount line nets |
| `tax_cents` | BigInteger | yes | NULL | yes | sum of line `tax_cents` |
| `prices_include_tax` | Boolean | yes | NULL | yes | |
| `issued_at` | DateTime(tz) | yes | NULL | yes once set | set at publish (or at payment for proforma conversion, 5.4) |
| `seller_snapshot` | JSON | yes | NULL | yes | 5.5 |
| `buyer_snapshot` | JSON | yes | NULL | yes | 5.5 |
| `customer_notes` | Text | yes | NULL | yes | printed |
| `admin_notes` | Text | yes | NULL | no | never printed |
| `locale` | String(16) | yes | NULL | yes | BCP 47 tag, for example `en-GB` |
| `series_id` | Integer FK `document_series.id` ON DELETE RESTRICT | yes | NULL | yes once set | index `ix_invoices_series_id` |
| `number_period` | String(8) | yes | NULL | yes once set | `''` or `'2026'` |
| `number_value` | BigInteger | yes | NULL | yes once set | the `{NUMBER}` value |
| `number_display` | String(64) | yes | NULL | yes once set | the formatted legal number |
| `proforma_series_id` | Integer FK `document_series.id` ON DELETE RESTRICT | yes | NULL | yes once set | |
| `proforma_number_period` | String(8) | yes | NULL | yes once set | |
| `proforma_number_value` | BigInteger | yes | NULL | yes once set | |
| `proforma_number_display` | String(64) | yes | NULL | yes once set | |
| `status_changed_at` | DateTime(tz) | yes | NULL | no | |
| `voided_at` | DateTime(tz) | yes | NULL | no | |
| `void_reason` | String(255) | yes | NULL | no | `admin`, `superseded`, `credited`, `order_cancelled`, `cycle_cancelled`, or free text from an admin |

Constraints and indexes:

- Replace `ck_invoices_positive_amount` (`amount_cents > 0`, `reseller.py:497`) with `ck_invoices_amount_nonnegative`: `amount_cents >= 0`.
- `ck_invoices_v2_totals`: `model_version IS NULL OR (subtotal_cents IS NOT NULL AND discount_cents IS NOT NULL AND tax_cents IS NOT NULL AND prices_include_tax IS NOT NULL)`.
- `ck_invoices_v2_number`: `model_version IS NULL OR status = 'draft' OR number_display IS NOT NULL OR proforma_number_display IS NOT NULL` (a published proforma carries only its proforma number until it is paid, 5.4).
- Unique `uq_invoices_series_number` on (`series_id`, `number_period`, `number_value`) and unique `uq_invoices_series_display` on (`series_id`, `number_display`). Rows with NULLs do not collide on either SQLite or MySQL, so legacy rows are unaffected.
- Unique `uq_invoices_proforma_number` on (`proforma_series_id`, `proforma_number_period`, `proforma_number_value`).
- Index `ix_invoices_account_status_due` on (`billing_account_id`, `status`, `due_at`), the retail twin of `ix_invoices_reseller_status_due`.

`invoice_number` (`reseller.py:502`, unique, allocated at creation by `InvoiceService.allocate_number`, `invoice_service.py:43-80`) is unchanged and stays in every API. It is the internal reference number. The legal number is `number_display`; serialisers add `number_display` and a `display_number` field that is `number_display` when set, else `str(invoice_number)`, so old invoices render as today without any backfill.

`InvoiceStatus` (`reseller.py:90-97`) gains `partially_paid`, `refunded` and `collections` (section 3).

### 1.3 `invoice_lines` (existing, `app/models/commerce_invoice_ext.py:21-45`): new columns

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `kind` | Enum `InvoiceLineKind` | yes | NULL | `product`, `setup`, `option`, `addon`, `discount`, `fee`, `proration`, `adjustment`, `other`; NULL on legacy lines |
| `service_id` | Integer FK `services.id` ON DELETE SET NULL | yes | NULL | index |
| `service_billing_id` | Integer FK `service_billings.id` ON DELETE SET NULL | yes | NULL | index |
| `period_start` | DateTime(tz) | yes | NULL | |
| `period_end` | DateTime(tz) | yes | NULL | |
| `tax_class_id` | Integer | yes | NULL | no foreign key until the tax engine (phase 2, workstream C) creates `tax_classes`; its contract step adds the FK |
| `taxable` | Boolean | yes | NULL | |
| `tax_snapshot` | JSON | yes | NULL | rule, rate and jurisdiction used, written by phase 2; phase 1 writes NULL or `{"source": "checkout_flat"}` (see 1.3.1) |

No CHECK constraints are added to `invoice_lines` (legacy rows would violate some, and the line rules are enforced by `InvoiceService` and the E2 detector).

#### 1.3.1 Line rules for v2 invoices

- `total_cents = quantity * unit_cents` exactly; `quantity >= 1`.
- `discount` lines have `unit_cents <= 0` and `total_cents <= 0`; every other kind has `unit_cents >= 0`.
- `tax_cents >= 0` on every line, and 0 on discount lines (discounts reduce the taxable base of the lines they apply to: the tax engine computes tax after discount; in phase 1 the only tax source is the checkout flat tax below).
- Invoice totals: `subtotal_cents = Σ total_cents of non-discount lines`; `discount_cents = -Σ total_cents of discount lines`; `tax_cents = Σ tax_cents`.
- If `prices_include_tax` is false, `amount_cents = subtotal_cents - discount_cents + tax_cents`. If true, line nets already include tax and `amount_cents = subtotal_cents - discount_cents`; `tax_cents` is the included portion, for reporting only.
- Phase 1 has no tax engine. Today checkout adds tax as a separate line described `"Tax"` (`checkout_service.py:349-358`, with `_tax_cents` at `:203`). For v2 invoices there is no tax line: the order's tax total is spread over the taxable lines by rule `tax_split_largest_remainder` (appendix B), so `Σ line tax_cents` equals the order's tax exactly, and `tax_snapshot` is `{"source": "checkout_flat"}`.
- Reseller invoices gain lines too: `create_topup`, `create_deploy` and `create_cycle` (`invoice_service.py:120-174`) each write one line (`kind` `other`, `setup` or `product` respectively, with `service_id`, `service_billing_id` and the period where known).

### 1.4 `invoice_adjustments` (new, `app/models/invoice_adjustment.py`)

Credit and debit notes. Amounts are positive; `kind` carries the sign.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | no | | primary key |
| `kind` | Enum `AdjustmentKind` | no | | `credit` or `debit` |
| `invoice_id` | Integer FK `invoices.id` ON DELETE RESTRICT | no | | index |
| `billing_account_id` | Integer FK `billing_accounts.id` ON DELETE RESTRICT | yes | | index |
| `reseller_id` | Integer FK `resellers.id` ON DELETE RESTRICT | yes | | index |
| `series_id` | Integer FK `document_series.id` ON DELETE RESTRICT | no | | |
| `number_period` | String(8) | no | `''` | |
| `number_value` | BigInteger | no | | |
| `number_display` | String(64) | no | | |
| `currency` | String(3) | no | | equals the invoice's currency |
| `subtotal_cents` | BigInteger | no | | |
| `tax_cents` | BigInteger | no | `0` | |
| `total_cents` | BigInteger | no | | |
| `reason_code` | Enum `AdjustmentReason` | no | | `cancel`, `refund`, `correction`, `goodwill`, `late_fee`, `overpayment`, `merge`, `write_off` (the spec's list plus `write_off` for "mark paid with remaining balance") |
| `reason_text` | String(512) | yes | | |
| `status` | Enum `AdjustmentStatus` | no | `'issued'` | `issued`; `closed` is reserved for accounting-period close (E3) and is not written in phase 1 |
| `outcome` | Enum `AdjustmentOutcome` | no | | credit notes: `reduced_balance` (reduces what is still owed), `refunded` (paired with a refund to the gateway or a manual refund), `credited_to_balance` (paired with a refund to account credit); debit notes: `increased_balance`, or `credited_to_balance` for overpayment only (4.3) |
| `ledger_entry_id` | Integer FK `credit_ledger_entries.id` ON DELETE RESTRICT | yes | | unique; the account credit entry of an overpayment debit note |
| `issued_at` | DateTime(tz) | no | | |
| `seller_snapshot` | JSON | yes | | copied from the invoice (or built at issue for legacy invoices) |
| `buyer_snapshot` | JSON | yes | | as above |
| `customer_notes` | Text | yes | | |
| `created_by_user_id` | Integer FK `users.id` ON DELETE SET NULL | yes | | |
| `idempotency_key` | String(255) | no | | unique |
| `metadata` (attribute `adjustment_metadata`) | JSON | no | `{}` | |

Constraints and indexes:

- `ck_invoice_adjustments_owner`: `billing_account_id IS NOT NULL OR reseller_id IS NOT NULL`.
- `ck_invoice_adjustments_amounts`: `subtotal_cents >= 0 AND tax_cents >= 0 AND total_cents > 0 AND total_cents = subtotal_cents + tax_cents`.
- `ck_invoice_adjustments_outcome`: `(kind = 'credit' AND outcome IN ('reduced_balance','refunded','credited_to_balance')) OR (kind = 'debit' AND outcome = 'increased_balance') OR (kind = 'debit' AND outcome = 'credited_to_balance' AND reason_code = 'overpayment')`.
- Unique `uq_invoice_adjustments_number` on (`series_id`, `number_period`, `number_value`); unique `uq_invoice_adjustments_display` on (`series_id`, `number_display`).
- Index `ix_invoice_adjustments_invoice_kind` on (`invoice_id`, `kind`).

`InvoicePurpose.CREDIT_NOTE` (`reseller.py:86`) is left alone and never used (spec A2).

### 1.5 `invoice_adjustment_lines` (new, same module)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | no | | primary key |
| `adjustment_id` | Integer FK `invoice_adjustments.id` ON DELETE RESTRICT | no | | index |
| `invoice_line_id` | Integer FK `invoice_lines.id` ON DELETE SET NULL | yes | | the invoice line this credits, if any |
| `kind` | Enum `InvoiceLineKind` | no | | same vocabulary as invoice lines |
| `description` | String(512) | no | | |
| `quantity` | Integer | no | `1` | |
| `unit_cents` | BigInteger | no | | `>= 0` |
| `total_cents` | BigInteger | no | | `= quantity * unit_cents` |
| `tax_cents` | BigInteger | no | `0` | |
| `tax_class_id` | Integer | yes | | |
| `tax_snapshot` | JSON | yes | | |
| `service_id` | Integer FK `services.id` ON DELETE SET NULL | yes | | |
| `period_start`, `period_end` | DateTime(tz) | yes | | |
| `sort_order` | Integer | no | `0` | |

`ck_invoice_adjustment_lines_amounts`: `quantity >= 1 AND unit_cents >= 0 AND total_cents >= 0 AND tax_cents >= 0`. Service invariant: `Σ total_cents = adjustment.subtotal_cents` and `Σ tax_cents = adjustment.tax_cents`.

### 1.6 `payments` (existing, `reseller.py:566-601`): new columns

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `refunded_cents` | BigInteger | no | `0` | cache of Σ `refunds.amount_cents` with status `succeeded` for this payment; written only by `RefundService` (and by the legacy reversal paths, 7.7) |
| `fee_cents` | BigInteger | yes | NULL | gateway fee when the gateway reports it (E3 needs it) |
| `net_cents` | BigInteger | yes | NULL | `amount_cents - fee_cents` when the fee is known |

`PaymentStatus` (`reseller.py:100-104`) gains `partially_refunded`. No CHECK constraints are added to `payments` (adding one rebuilds the table on MySQL); `0 <= refunded_cents <= amount_cents` is enforced by `RefundService` and checked by the E2 detector.

### 1.7 `refunds` (new, `app/models/refund.py`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | no | | primary key |
| `payment_id` | Integer FK `payments.id` ON DELETE RESTRICT | no | | index |
| `invoice_id` | Integer FK `invoices.id` ON DELETE RESTRICT | no | | index |
| `adjustment_id` | Integer FK `invoice_adjustments.id` ON DELETE RESTRICT | yes | | unique; the credit note issued for this refund |
| `billing_account_id` | Integer FK `billing_accounts.id` ON DELETE RESTRICT | yes | | index |
| `reseller_id` | Integer FK `resellers.id` ON DELETE RESTRICT | yes | | index |
| `amount_cents` | BigInteger | no | | |
| `currency` | String(3) | no | | |
| `destination` | Enum `RefundDestination` | no | | `gateway`, `credit`, `manual` (money returned outside Rackflow, attested by an admin) |
| `gateway` | String(64) | no | | copy of `payments.gateway` at request time |
| `reason` | Enum `RefundReason` | no | | `customer_request`, `cancellation`, `duplicate`, `fraud`, `dispute`, `gateway_initiated`, `credit_removed`, `other` |
| `reason_text` | String(512) | yes | | |
| `issue_credit_note` | Boolean | no | `true` | false for disputes and `credit_removed` (section 7) |
| `ledger_entry_id` | Integer FK `credit_ledger_entries.id` ON DELETE RESTRICT | yes | | unique; the credit entry when `destination = 'credit'` |
| `status` | Enum `RefundStatus` | no | `'pending'` | `pending`, `succeeded`, `failed`, `review` |
| `gateway_refund_ref` | String(255) | yes | | |
| `idempotency_key` | String(255) | no | | unique; also sent to the gateway |
| `attempts` | Integer | no | `0` | |
| `next_attempt_at` | DateTime(tz) | yes | | |
| `last_attempt_at` | DateTime(tz) | yes | | |
| `claim_token` | String(128) | yes | | reconciler claim |
| `claim_expires_at` | DateTime(tz) | yes | | |
| `failure_code` | String(128) | yes | | |
| `failure_message` | Text | yes | | |
| `requested_by_user_id` | Integer FK `users.id` ON DELETE SET NULL | yes | | |
| `requested_at` | DateTime(tz) | no | | |
| `completed_at` | DateTime(tz) | yes | | |
| `metadata` (attribute `refund_metadata`) | JSON | no | `{}` | |

Constraints and indexes:

- `ck_refunds_positive_amount`: `amount_cents > 0`.
- `ck_refunds_owner`: `billing_account_id IS NOT NULL OR reseller_id IS NOT NULL`.
- Unique `uq_refunds_gateway_ref` on (`gateway`, `gateway_refund_ref`).
- Index `ix_refunds_status_next_attempt` on (`status`, `next_attempt_at`).

### 1.8 `credit_ledger_entries` (existing, `reseller.py:706-780`): changes

- New `billing_account_id` Integer FK `billing_accounts.id` ON DELETE RESTRICT, nullable (NOT NULL is a later contract step, 2.3).
- `reseller_id` becomes nullable (retail entries have none). Its foreign key and index are unchanged.
- New check `ck_credit_ledger_owner`: `reseller_id IS NOT NULL OR billing_account_id IS NOT NULL`.
- New index `ix_credit_ledger_account_id` on (`billing_account_id`, `id`): the "last entry of an account" lookup is `ORDER BY id DESC LIMIT 1` on it.
- `balance_after_cents` keeps its meaning: the account's balance after this entry. Entries of one account form a chain ordered by `id`: `balance_after(k) = balance_after(k-1) + amount(k)`, with the first entry's predecessor balance 0.
- `reference_type` and `reference_id` (existing) carry links to the new rows: `refund` / refund id for every refund to credit (including credit removal), `debit_note` / adjustment id for overpayment credit, and `invoice` / invoice id for top-ups and applied credit as today. No new foreign keys.

### 1.9 `billing_accounts` and `resellers`

No new columns. `billing_accounts.credit_balance_cents` (`commerce_account.py:89-91`) becomes the account-level cache for every account, retail and reseller. `resellers.cached_balance_cents` (`reseller.py:208`) stays and is written in step until the contract step that retires it (section 6). Note that today the account copy is written only once, when a reseller's billing account is created (`app/dao/commerce_dao.py:85`), so it is stale for every reseller; the A3 backfill resynchronises it (2.2, revision 7).

### 1.10 `billing_shadow_mismatches` (new, `app/models/billing_shadow.py`)

Durable record of every difference the shadow comparisons find (section 6.4).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | no | | primary key |
| `engine` | String(16) | no | | `invoice` or `credit` |
| `check_key` | String(64) | no | | for example `c1_reseller_vs_account` (section 6.4) |
| `subject_type` | String(32) | no | | `invoice`, `billing_account`, `reseller` |
| `subject_id` | Integer | no | | |
| `legacy_value` | JSON | no | | |
| `v2_value` | JSON | no | | |
| `context` | String(128) | yes | | call site or `scan` |
| `occurrences` | Integer | no | `1` | |
| `first_seen_at` | DateTime(tz) | no | | |
| `last_seen_at` | DateTime(tz) | no | | |
| `resolved_at` | DateTime(tz) | yes | | |
| `resolved_by_user_id` | Integer FK `users.id` ON DELETE SET NULL | yes | | |
| `resolution_note` | Text | yes | | |

Unique `uq_billing_shadow_mismatch` on (`engine`, `check_key`, `subject_type`, `subject_id`): a repeated mismatch increments `occurrences` and `last_seen_at` instead of adding rows (a resolved row that recurs is reopened: `resolved_at` set back to NULL). Index `ix_billing_shadow_open` on (`engine`, `resolved_at`).

### 1.11 `price_plan_cycles` (existing, `app/models/storefront.py:195-212`): new columns (D1)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `interval_unit` | String(8) | yes | NULL | `month` or `year` |
| `interval_count` | SmallInteger | yes | NULL | `>= 1` |
| `setup_cents` | BigInteger | yes | NULL | per-cycle setup fee; NULL means use `price_plans.setup_cents` (`storefront.py:178`) |

- `ck_price_plan_cycles_interval`: `(interval_unit IS NULL AND interval_count IS NULL) OR (interval_unit IN ('month','year') AND interval_count >= 1)`.
- `ck_price_plan_cycles_setup_nonnegative`: `setup_cents IS NULL OR setup_cents >= 0`.
- Unique `uq_price_plan_cycles_plan_unit_count` on (`price_plan_id`, `interval_unit`, `interval_count`).

`PricePlanInterval` (`storefront.py:57-61`) gains `biennially` and `triennially`. The existing `interval` column and `uq_price_plan_cycles_plan_interval` stay (section 8).

### 1.12 What phase 1 does not change

`service_billings` gets nothing from phase 1; W0a adds `cycle_interval`, `interval_months` and `cancel_at` in phase 0 (spec A0 item 5). `orders`, `order_items`, coupons and tax tables are untouched.

## 2. Migration sequence

### 2.1 Principles

- Expand, backfill, switch, contract. Phase 1 ships expand and backfill revisions and the switch code; contract revisions are listed (2.3) but belong to a later phase and are not written now.
- One alembic head at every merge. Revision ids are at most 32 characters (the default width of `alembic_version.version_num`).
- Every revision has a working `downgrade()` on SQLite and MySQL, and a test in the style of `tests/services/test_private_network_dhcp_migration.py` (single head, chain position, upgrade adds, downgrade removes).
- Backfills are their own revisions, use plain SQL through `op.get_bind()` (no ORM model imports, so later model changes cannot break them), are idempotent (`WHERE target IS NULL`), and work in primary-key ranges of 5,000 rows inside `with op.get_context().autocommit_block():` so each batch commits on its own and a large table never holds one long transaction. The batch functions live in `app/services/billing_backfill.py` as functions taking a `Connection`, so the same code can be re-run after deploy with `python -m app.services.billing_backfill <name>` (2.2, after the table, explains why that is needed).
- MySQL in this project means MariaDB 11 (`docker-compose.yml:322`, `image: mariadb:11`); statements must also work on MySQL 8.0.19 or later. SQLite is the test and development database.
- Before merging, each worker rehearses its revisions on a MariaDB 11 container loaded with a copy of a production-sized database and records the wall-clock time of each ALTER in the pull request description. Steps marked "locks" below must take under 30 seconds on that copy; if one does not, the manager schedules it in a maintenance window instead of shipping it in a normal deploy.

### 2.2 Phase 1 revisions

The chain starts from the integration branch head after phase 0 merges: `plugin_system` (`alembic/versions/plugin_system.py:16`) followed by any phase 0 revisions (W0a's `service_billings` columns at least). P1-S (section 11) sets `down_revision` of revision 1 to whatever that head is when it starts.

| # | Revision id | Does | MySQL locking | SQLite |
|---|---|---|---|---|
| 1 | `billing_p1_series_shadow` | Create `document_series` and `billing_shadow_mismatches`; insert the four seed series; insert `invoice_sequences` rows `series:<id>:` for the three `own` series, starting at 1 | None (new tables, small inserts) | Plain |
| 2 | `billing_p1_invoice_columns` | `invoices`: add every column of 1.2; FKs `fk_invoices_series_id` and `fk_invoices_proforma_series_id`; unique keys `uq_invoices_series_number`, `uq_invoices_series_display` and `uq_invoices_proforma_number`; index `ix_invoices_account_status_due`. `invoice_lines`: add every column of 1.3, FKs to `services` and `service_billings`, indexes | ADD COLUMN is instant (MariaDB 10.4+, MySQL 8.0.29+; earlier MySQL 8 is instant only when appending, which alembic does). Index and unique-key builds are online (INPLACE, LOCK=NONE). Add FKs with `SET foreign_key_checks = 0` for the statement so InnoDB uses INPLACE; the columns are all NULL, so no violation is possible | `batch_alter_table` is not needed for ADD COLUMN; FKs on new columns are declared inline with the column for SQLite (precedent: `private_network_dhcp.py:24`) |
| 3 | `billing_p1_adjustments` | Create `invoice_adjustments` and `invoice_adjustment_lines` | None | Plain |
| 4 | `billing_p1_refunds` | `payments`: add `refunded_cents` (NOT NULL, server default `0`), `fee_cents`, `net_cents`. Create `refunds` | ADD COLUMN instant (a NOT NULL column with a constant default is instant on MariaDB 10.3+ and MySQL 8) | Plain |
| 5 | `billing_p1_ledger_accounts` | `credit_ledger_entries`: add `billing_account_id`; make `reseller_id` nullable; add `ix_credit_ledger_account_id`; add `fk_credit_ledger_billing_account_id` | ADD COLUMN instant. `MODIFY reseller_id INT NULL` (alembic `alter_column(..., existing_type=sa.Integer(), nullable=True)`, restating the type) is INPLACE with a table rebuild but allows concurrent reads and writes (LOCK=NONE). Index online. FK as in revision 2 | `batch_alter_table("credit_ledger_entries", recreate="always")` for the nullability change (SQLite cannot alter a column), see 2.4 |
| 6 | `billing_p1_cycles` | `price_plan_cycles`: add the three columns of 1.11, the two checks and the unique key; backfill `interval_unit`/`interval_count` from `interval` in the same revision (the table holds a few rows per plan) using the mapping in section 8.1 | **Locks** briefly: COPY rebuild for the checks, on a tiny table | Batch recreate |
| 7 | `billing_p1_backfill_accounts` | Batched, idempotent: (a) insert a `billing_accounts` row (`account_type='reseller'`, `reseller_id`, `user_id` from `resellers.user_id`, `currency='USD'`, `status='active'`) for every reseller that has none, mirroring `BillingAccountDAO.ensure_reseller_account` (`commerce_dao.py:75-89`); (b) set `billing_accounts.credit_balance_cents = resellers.cached_balance_cents` for every reseller account; (c) set `credit_ledger_entries.billing_account_id` from `billing_accounts.reseller_id` where it is NULL; (d) for every retail account with `credit_balance_cents <> 0` and no ledger entry, insert one `adjustment` entry for that amount with `idempotency_key = 'a3-opening:<account_id>'` and description "Opening balance (A3 migration)", so the chain explains the cache | Row updates only, batched | Same SQL |
| 8 | `billing_p1_backfill_payments` | `UPDATE payments SET refunded_cents = amount_cents WHERE status = 'refunded' AND refunded_cents = 0`, batched. This records a fact the status already states; it is not a computed value on an invoice | Row updates only | Same SQL |
| 9 | `billing_p1_invoice_checks` | Drop `ck_invoices_positive_amount`; add `ck_invoices_amount_nonnegative`, `ck_invoices_v2_totals`, `ck_invoices_v2_number` | **Locks**: adding a CHECK validates every row and rebuilds `invoices` (ALGORITHM=COPY, writes blocked). Done as one `ALTER TABLE` so the table is rebuilt once. See 2.4 | Table recreate in batch mode, see 2.4 |
| 10 | `billing_p1_ledger_owner_check` | Add `ck_credit_ledger_owner` | **Locks**: COPY rebuild of `credit_ledger_entries` (the largest money table on a busy reseller install) | Batch recreate, see 2.4 |

The two locking revisions are last on purpose: if the rehearsal shows them over budget, the operator runs `alembic upgrade billing_p1_backfill_payments` in the normal deploy and `alembic upgrade head` later in a window. Phase 1 code must work with the database at revision 8:

- until revision 9 runs, the old `amount_cents > 0` check is still present, so `InvoiceService` keeps refusing zero-amount invoices (as checkout does today, `checkout_service.py:319-320`). It decides once per process by reading the constraint list (2.4 step 1 on MySQL, `sqlite_master` on SQLite) and caching the answer; after revision 9 a restart enables zero-amount invoices;
- until revision 10 runs, `CreditLedgerService` enforces the owner rule in code (it always does) and the E2 detector checks it.

The old code keeps running while revisions 7 and 8 execute during a rolling deploy, and it writes ledger entries without `billing_account_id` and without touching the account cache. Therefore:

- the deploy runbook re-runs `python -m app.services.billing_backfill accounts` (all four steps, idempotent) once no old process remains, because while the deploy rolls old processes write unlinked reseller entries and move only the reseller cache;
- the credit engine stays in `shadow` until its switch criterion holds (6.5), and the first criterion is "no ledger entry with `reseller_id` set and `billing_account_id` NULL";
- while the credit engine is not `v2`, an admin action "Resynchronise account caches from reseller balances" (P1-C) repeats step (b) for named accounts, audited. It is refused in `v2`.

### 2.3 Contract steps (later phase; listed so phase 1 code does not block them)

| Revision id (suggested) | When | Does |
|---|---|---|
| `billing_c1_ledger_account_required` | Credit engine in `v2` for one release with zero mismatches | `credit_ledger_entries.billing_account_id` NOT NULL (locks: rebuild) |
| `billing_c2_retire_reseller_cache` | After c1, and after every reader of `resellers.cached_balance_cents` reads the account cache (the readers are listed in 6.6) | Stop writing `resellers.cached_balance_cents`; a release later drop the column. `/api/reseller` `balance_cents` keeps its name and is served from the account cache |
| `billing_c3_line_tax_class_fk` | Phase 2 creates `tax_classes` | FKs from `invoice_lines.tax_class_id` and `invoice_adjustment_lines.tax_class_id` |
| `billing_c4_cycle_interval_label` | When cycles other than the six named ones are allowed | Make `price_plan_cycles.interval` nullable and drop `uq_price_plan_cycles_plan_interval` |

Phase 1 code must therefore (a) always write `billing_account_id` on new ledger entries, invoices, adjustments and refunds, and (b) read balances only through `CreditLedgerService.available_cents(db, account)` (6.3), never from a cache column directly, in new code.

### 2.4 The constraint revisions in detail

**Revision 9 on MySQL/MariaDB.** The constraint was created by name in `alembic/versions/reseller_platform_phase1.py:456`. MariaDB and MySQL 8.0.16+ enforce CHECK constraints; MySQL before 8.0.16 parsed and ignored them, so a very old database may not have it. The revision:

1. Looks the constraint up portably: `SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'invoices' AND CONSTRAINT_TYPE = 'CHECK'` (both servers expose CHECK rows in `TABLE_CONSTRAINTS`; MySQL's `CHECK_CONSTRAINTS` view has no `TABLE_NAME` column, so it is not used).
2. Runs one statement: `ALTER TABLE invoices DROP CONSTRAINT ck_invoices_positive_amount, ADD CONSTRAINT ck_invoices_amount_nonnegative CHECK (amount_cents >= 0), ADD CONSTRAINT ck_invoices_v2_totals CHECK (...), ADD CONSTRAINT ck_invoices_v2_number CHECK (...)`, leaving out the DROP clause if step 1 did not find the name. `DROP CONSTRAINT` works for CHECK constraints on MariaDB 10.2+ and MySQL 8.0.19+. (Alembic's `op.drop_constraint(name, table, type_="check")` emits `DROP CHECK` on MySQL and `DROP CONSTRAINT` on MariaDB; raw SQL is used so the drop and the adds happen in one rebuild.)
3. Downgrade: one statement dropping the three new constraints and adding `ck_invoices_positive_amount CHECK (amount_cents > 0)`. Downgrade fails if zero-amount invoices exist; that is intended and the error message says so.

**Revision 9 on SQLite.** SQLite cannot drop a constraint, so the table is recreated with `op.batch_alter_table("invoices", recreate="always", copy_from=_invoices_before())`, where `_invoices_before()` builds an explicit `sa.Table` of `invoices` exactly as it stands before this revision (including the named old check), rather than relying on reflection of CHECK constraints. Inside the batch: `drop_constraint("ck_invoices_positive_amount", type_="check")` and three `create_check_constraint(...)`. Recreating a table that other tables reference needs `PRAGMA foreign_keys=OFF` around the batch, issued inside an `autocommit_block()` because the pragma is ignored inside a transaction, and `PRAGMA foreign_keys=ON` afterwards. The migration test asserts that the `sql` of `invoices` in `sqlite_master` contains `amount_cents >= 0` and not `amount_cents > 0`, and that inserting an invoice with `amount_cents = 0` succeeds after upgrade and fails after downgrade.

**Revision 5 on SQLite.** Same technique with an explicit `copy_from` table for `credit_ledger_entries`, keeping `ck_credit_ledger_nonzero_amount`, the unique keys on `reversal_of_id` and `idempotency_key`, and the self-referencing FK.

**Revision 10.** MySQL: `ALTER TABLE credit_ledger_entries ADD CONSTRAINT ck_credit_ledger_owner CHECK (reseller_id IS NOT NULL OR billing_account_id IS NOT NULL)`. Every existing row has `reseller_id`, so validation cannot fail. SQLite: batch recreate as in revision 5.

**Which steps lock, in summary:** revisions 9, 10 and (trivially) 6 rebuild a table with writes blocked. Everything else is instant or online. If the rehearsal shows revision 9 or 10 over the 30-second budget, the options in order of preference are: run them in a maintenance window (they are last in the chain for this reason); or run the statement with an online schema change tool (`pt-online-schema-change` or `gh-ost`) outside alembic and then `alembic stamp` the revision.

## 3. Invoice states and `InvoiceService.transition()`

### 3.1 The two entry points

- `InvoiceService.transition(db, invoice, to, *, cause, actor_user_id=None, reason=None) -> Invoice` is the only code that assigns `invoices.status`. The caller must hold the invoice row lock (taken through `BillingLocks`, section 0). It checks the edge against the table for the current invoice engine mode, raises `InvoiceTransitionError` (a subclass of the existing `InvoiceError`) for an edge that is not in the table, applies the side effects listed for the edge, sets `status_changed_at`, and writes one audit row (`AuditService`, action `invoice.status`, with from, to, cause and actor).
- `InvoiceService.recompute(db, invoice, *, cause) -> Invoice` derives the status the invoice should have from `balance()` (section 4) and calls `transition()` if it differs. Every money event calls `recompute()` instead of choosing a status itself: payment succeeded or failed, credit applied or removed, credit or debit note issued, refund succeeded, dispute received.
- A transition **to `paid`** is only made by the settlement function (W0a, section 9). `recompute()` that finds the target is `paid` calls the settlement function instead of `transition()`, so `invoice.paid`, order fulfilment and cycle completion happen however the invoice came to be paid (a gateway payment, applied credit, a zero total, or a credit note covering the rest of a part-paid invoice). `transition(..., to=PAID)` raises unless `cause == "settlement"`.

### 3.2 The status rule (engine `v2`)

With `B = balance(invoice)` (section 4) and `pending` meaning a `payments` row with status `pending` exists for the invoice:

| Condition, checked in this order | Status |
|---|---|
| status is `draft`, `void`, `refunded` or `failed` | unchanged (`recompute` does not move these; `draft` only leaves through publish, the other three are terminal) |
| `B.due_cents < 0` | handle the overpayment first (4.3), then evaluate again |
| `B.due_cents == 0` and `B.paid_net_cents > 0` | `paid` |
| `B.due_cents == 0` and `B.paid_net_cents == 0` and `B.refunded_cents > 0` | `refunded` |
| `B.due_cents == 0` and `B.paid_net_cents == 0` and `B.credit_cents > 0` | `void`, with `void_reason = 'credited'` |
| `B.due_cents == 0` and nothing paid, refunded or credited (a zero-total invoice) | `paid` |
| `B.due_cents > 0` and `pending` | `pending_action` |
| `B.due_cents > 0` and current status is `collections` | `collections` (sticky until the balance reaches 0) |
| `B.due_cents > 0` and the cause is a dispute and the track's dispute policy is `reopen_invoice` (7.6) | `collections` |
| `B.due_cents > 0` and current status is `overdue` | `overdue` (sticky) |
| `B.due_cents > 0` and `B.paid_net_cents > 0` | `partially_paid` |
| otherwise | `open` |

`overdue` is only entered by the recurring billing worker (and later by the B-workstream automation) through `transition(..., to=OVERDUE)`, never by `recompute()`.

### 3.3 Transition table (engine `v2`)

Every edge not listed is refused. "Recompute" in the trigger column means the edge is chosen by `recompute()` after the named money event.

| From | To | Trigger | Side effects (besides audit and `status_changed_at`) |
|---|---|---|---|
| (none) | `draft` | `InvoiceService.create()` | allocate `invoice_number` (unchanged, `invoice_service.py:43-80`); lines written; `model_version = 2` only when the invoice engine is `v2` (3.4) |
| `draft` | `open` | `InvoiceService.publish()`; `create(..., publish=True)` (the default for every automatic path) calls it in the same transaction | validate lines (1.3.1); compute and write `subtotal_cents`, `discount_cents`, `tax_cents`, `amount_cents` (in invoice engine `shadow`, `amount_cents` stays the legacy amount and a difference is recorded as check `i3_totals`, 6.4); `issued_at = now`; snapshots (5.5); allocate the number last (5.2); emit `invoice.created`; then, if auto-apply credit is on for the track, apply credit (6.7); then `recompute()` (a zero total settles at once) |
| `draft` | (deleted) | admin deletes a draft | hard delete of the row and its lines; allowed only while `number_display` and `proforma_number_display` are NULL (always true for drafts) |
| `open` | `pending_action` | recompute after a gateway payment returns "requires action" (`orchestrator.py:604-613`) | none |
| `open`, `partially_paid`, `overdue`, `collections` | `pending_action` | recompute after a pending payment is created | none |
| `pending_action` | `open`, `partially_paid`, `overdue`, `collections` | recompute after the pending payment fails or expires (`webhooks.py:219-241`) | none |
| `open`, `pending_action`, `partially_paid`, `overdue`, `collections` | `paid` | settlement only | `paid_at = now` (first time only); proforma conversion if `document_type = 'proforma'` (5.4); settlement subscribers (section 9) |
| `open`, `pending_action` | `partially_paid` | recompute after a payment or applied credit leaves `due > 0` | none |
| `open`, `partially_paid`, `pending_action` | `overdue` | recurring worker when the due date passes unpaid (`recurring_billing_service.py:557-558`) | emit `invoice.overdue` |
| `overdue`, `partially_paid`, `paid` | `collections` | admin action "send to collections"; or recompute after a dispute with policy `reopen_invoice` | none in phase 1 (automation in phase 2 decides suspension) |
| `paid` | `partially_paid` | recompute after a debit note (correction) or a refund with no credit note (credit removal) leaves `due > 0` with `paid_net > 0` | `paid_at` is kept (it records the first time the invoice was paid) |
| `paid`, `partially_paid` | `open` | recompute after credit removal or a refund with no credit note leaves `paid_net == 0` and `due > 0` | `paid_at` is kept |
| `paid` | `refunded` | recompute after a full refund with credit note | emit `invoice.refunded` |
| `partially_paid`, `overdue`, `collections` | `refunded` | recompute after refunds bring `paid_net` to 0 and credit notes bring `due` to 0 | emit `invoice.refunded` |
| `open`, `overdue`, `collections`, `partially_paid` | `void` (`credited`) | recompute after a credit note brings `due` to 0 with nothing paid | `voided_at`, `void_reason = 'credited'`; emit `invoice.voided` |
| `open`, `pending_action` | `refunded` | recompute when a refund's credit note arrives after a full refund with no credit note reopened the invoice | emit `invoice.refunded` |
| `open` | `collections` | recompute after a dispute with policy `reopen_invoice` on an open invoice | none in phase 1 |
| `pending_action` | `void` (`credited`) | recompute after a credit note brings `due` to 0 while a payment is still pending | `voided_at`, `void_reason = 'credited'`; emit `invoice.voided` |
| `paid` | `pending_action`, `void` | recompute after a payment reversal leaves only a pending payment, or only a credit note | as the `pending_action` and `void` (`credited`) rows |
| `open`, `overdue` | `void` | `InvoiceService.void(db, invoice, reason, actor)` by an admin, by order cancellation, or by billing cancellation (`recurring_billing_service.py:883`, `reseller_billing_service.py:795`) | allowed only if `paid_net_cents == 0`, `pending_cents == 0`, no adjustment exists and no refund is pending; `voided_at`, `void_reason`; the number is kept and never reused; emit `invoice.voided` |

The four rows above `void` by admin were added during P1-I: an exhaustive test of the status rule (3.2) found it can reach them, so without them `transition()` would raise. The test pins this table to the rule, so the two cannot drift apart again.

Statuses `failed` (legacy) and `void`, `refunded` are terminal in `v2`. Nothing in `v2` writes `failed`; existing `failed` rows stay as they are (they are reversed payments under the legacy model).

### 3.4 Transition table (engine `legacy` and `shadow`)

These modes keep today's behaviour byte for byte, so the table is today's edges, now made through `transition()` so there is one writer:

| From | To | Today's writer |
|---|---|---|
| (none) | `open` | `InvoiceService.create` (`invoice_service.py:109`), checkout (`checkout_service.py:335`), upgrade quote (`commerce_lifecycle_service.py:185`) |
| `open`, `pending_action`, `overdue` | `paid` | `mark_paid_if_fully_allocated` (`invoice_service.py:211-213`), admin mark-paid (`commerce_admin.py:439`; W0a moves it onto the settlement function) |
| `paid` | `open` | `mark_paid_if_fully_allocated` when allocation drops (`invoice_service.py:214-216`) |
| `open` | `pending_action` | `orchestrator.py:607` |
| `pending_action` | `open` | `webhooks.py` `_payment_failed` (cause `payment_failed`) |
| `open`, `pending_action` | `overdue` | `recurring_billing_service.py:557-558` |
| `open`, `pending_action`, `overdue` | `void` | `commerce_admin.py:479`, `reseller_billing_service.py:795`, `recurring_billing_service.py:883`; `order_refunds.cancel_order` (cause `order_cancelled`, also from `failed`) |
| any except `void`, `draft` | `failed` | `RefundService` completion (causes `admin_refund`, `refund`, `dispute`) |

As built (P1-R): before phase 1, a refund or dispute of a late payment on a `void` invoice wrote `failed` anyway. There is no `void` to `failed` edge, so such an invoice now stays `void`; the payment, refund and ledger effects still apply. This is a deliberate change from the pre-phase-1 behaviour in `legacy` and `shadow`, listed in the phase 1 report.

In these modes `create()` still writes `draft` first and publishes in the same transaction (so drafts become real from phase 1 for admin-built invoices, A5 later), and the publish side effects of 3.3 apply with two differences: `amount_cents` is the legacy amount, and `model_version`, `subtotal_cents`, `discount_cents` and `tax_cents` stay NULL (the v2 totals are computed only for the `i3_totals` comparison). Lines with their new columns, snapshots, `issued_at`, the number columns and `invoice.created` are written in every mode, since they are additive and harmless. So `model_version = 2` means exactly "its totals were decided by the v2 engine", and the PDF and the E2 totals detector rely on that. New statuses (`partially_paid`, `refunded`, `collections`) are never written in these modes, so `/api/reseller-panel` and `/api/client/commerce` see no new status values until the switch (open question 4).

### 3.5 Frozen columns and the ORM guard

Once an invoice's status is not `draft`, these columns are frozen (writes raise):

- `invoices`: `invoice_number`, `reseller_id`, `billing_account_id`, `order_id`, `purpose`, `amount_cents`, `subtotal_cents`, `discount_cents`, `tax_cents`, `prices_include_tax`, `currency`, `description`, `customer_notes`, `locale`, `seller_snapshot`, `buyer_snapshot`, `model_version`, `created_at`.
- Frozen once set (NULL to a value is allowed once, a value never changes): `service_id`, `issued_at`, `series_id`, `number_period`, `number_value`, `number_display`, `proforma_series_id`, `proforma_number_period`, `proforma_number_value`, `proforma_number_display`.
- `document_type`: frozen except `proforma` to `invoice` inside the proforma conversion (5.4).
- Deliberate deviation (P1-I): `service_id` may be set once, from NULL to a value, on a published invoice, because a reseller deploy charge is invoiced before the service it pays for exists; any later change or clearing through the ORM is refused (the database's ON DELETE SET NULL still clears it when the service is deleted).
- Exception for foreign keys set to NULL: a change of `billing_account_id` to NULL is allowed (the database's ON DELETE SET NULL, `reseller.py:511-522`, may be mirrored by the ORM when the parent is deleted).
- Always writable: `status` (through `transition()` only, enforced by review and a test that greps for `.status = InvoiceStatus` outside `invoice_service.py`), `paid_at`, `due_at` (through `InvoiceService.change_due_date`, audited), `admin_notes`, `status_changed_at`, `voided_at`, `void_reason`, `updated_at`.
- `invoice_lines` of a non-draft invoice: no insert, update or delete at all. This applies to legacy invoices too, so every creator that adds lines after inserting the invoice must instead create it as `draft`, add the lines and then publish: checkout (`checkout_service.py:311-362`, which adds the tax line after the invoice row) and the upgrade quote (`commerce_lifecycle_service.py:185`) are the two known cases, both owned by P1-I.

Guard design, in `app/services/invoice_guard.py`, registered on import from `app/models/__init__.py` (or wherever models are imported for the app and for tests):

- `event.listen(Invoice, "before_update", _guard_invoice)`: for each frozen column, `inspect(target).attrs[name].history`; if it has changes, take the old status from `inspect(target).attrs.status.history.deleted` when the status is also changing, else the current value; if the old status is not `draft`, the change is a violation unless the column is in the "once set" list and the old value was NULL, or the NULL-foreign-key exception applies, or the current context allows it (below).
- `event.listen(Invoice, "before_delete", ...)`: violation unless the status is `draft`.
- `event.listen(InvoiceLine, "before_insert" | "before_update" | "before_delete", ...)`: read the parent's committed status with `connection.execute(select(invoices.c.status).where(invoices.c.id == target.invoice_id))` (mapper events must use the passed connection, not the session); violation if it is not `draft`. Inserting lines for a brand-new invoice in the same flush is fine because the parent row is inserted as `draft` first.
- An escape hatch for the proforma conversion only: a `contextvars.ContextVar` set by `with invoice_guard.allow("proforma_conversion", {"document_type", "issued_at", "series_id", ...}):` inside `InvoiceService`.
- Core `update(Invoice)` statements bypass ORM events, so a test asserts no `update(Invoice)` or `update(InvoiceLine)` appears outside `invoice_service.py` and the backfill module.
- What a violation does depends on the setting `billing_invoice_guard_mode` (appendix A): `enforce` raises `FrozenInvoiceError`; `log` logs an error with the invoice id, column and stack, and records shadow mismatch check `i4_frozen_write` (6.4), and lets the write proceed. If the guard cannot read its mode, it refuses the write. In `log` mode, snapshots, descriptions and notes are recorded only by size and other values are cut to 120 characters, and records are written only when the outermost transaction commits. The test suite always runs with `enforce` (a fixture sets it; the few tests that must write a published invoice in their setup are listed in `tests/conftest.py`). The default in production is `log` while the invoice engine is not `v2`, and the guard is always `enforce` once the invoice engine is `v2`, so no stray writer that tests missed can break a production renewal before the switch, but none survives the switch.

## 4. `InvoiceService.balance()`

### 4.1 Definition

```python
@dataclass(frozen=True)
class InvoiceBalance:
    total_cents: int        # invoices.amount_cents
    debit_cents: int        # Σ total_cents of debit notes on the invoice
    credit_cents: int       # Σ total_cents of credit notes on the invoice
    worth_cents: int        # total + debit - credit (never negative, 7.3 and 4.4)
    paid_gross_cents: int   # Σ payments.amount_cents, status in (succeeded, partially_refunded, refunded)
    refunded_cents: int     # Σ payments.refunded_cents over the same rows
    paid_net_cents: int     # paid_gross - refunded
    pending_cents: int      # Σ payments.amount_cents, status pending (not deducted)
    due_cents: int          # worth - paid_net; negative means overpaid
    remaining_cents: int    # max(0, due_cents), the value old API fields show
```

`balance(db, invoice)` runs two aggregate queries (payments grouped by status, adjustments grouped by kind) and is pure: it never writes. Payments by applied credit (`gateway = 'credit'`, `orchestrator.py:487-498`) are payments like any other, which is what makes "apply credit" and "remove credit" symmetrical with card payments. Adjustments count whatever their `status` (`issued` or later `closed`).

On legacy-shaped data (no adjustments, no partial refunds, and `refunded_cents = amount_cents` on refunded payments after revision 8) `paid_net_cents` equals today's `allocated_cents` (`invoice_service.py:192-199`, which sums only `succeeded` payments), and `remaining_cents` equals today's `max(0, amount - allocated)`. That equality is what the invoice shadow mode checks.

### 4.2 Call sites of the old comparison that must switch

All of these go through `balance()` or `recompute()`. In engines `legacy` and `shadow` they keep their old result; in `shadow` they also compute `balance()` and record check `i1_decision` on any difference (6.4); in `v2` they use `balance()`.

| Site | Today | Becomes |
|---|---|---|
| `invoice_service.py:192-199` `allocated_cents` | sum of succeeded payments | kept as a thin wrapper returning `balance().paid_net_cents` in `v2` (old meaning otherwise), deprecated for new code |
| `invoice_service.py:202-218` `mark_paid_if_fully_allocated` | raises on over-allocation (`:209-210`), sets `paid` or reverts to `open` | replaced by `recompute()`; kept as an alias that calls `recompute()` so the reseller path's call sites need not move in one step |
| `invoice_service.py:237-248` `serialize` | `allocated_cents`, `remaining_cents` | from `balance()` in `v2`; adds `balance` (all fields of 4.1), `number_display`, `display_number`, `document_type`, `issued_at`; detail views add `adjustments` and `refunds` |
| `invoice_pdf_service.py:59-60` | allocated and remaining | `balance()`; v2 invoices print total, debit notes, credit notes, paid, refunded, due |
| `payments/orchestrator.py:358-362` `_result` | `allocated_cents`, `remaining_cents` | `balance()` |
| `payments/orchestrator.py:381`, `:463`, `:522`, `:534`, `:633` | `mark_paid_if_fully_allocated` | `recompute()` (via the alias at first) |
| `payments/orchestrator.py:393`, `:400`, `:457-470` | `initial.remaining_cents`, `first.remaining_cents`, `after_credit.remaining_cents` | `remaining_cents` from `balance()` |
| `payments/orchestrator.py:480` | `reseller.cached_balance_cents` for available credit | `CreditLedgerService.available_cents(db, account)` (A3, 6.6) |
| `recurring_billing_service.py:479` | `mark_paid_if_fully_allocated` | `recompute()` |
| `reseller_billing_service.py:694` | available credit from the reseller cache | `available_cents` (A3) |
| `reseller_billing_service.py:718` | `mark_paid_if_fully_allocated` | `recompute()` |
| `api/reseller_admin.py:684-696` | `allocated_cents`, `remaining_cents` | `balance()`, plus the new fields |
| `api/reseller_panel.py:1115-1116` | from the orchestrator result | unchanged code; values come from `balance()` through `_result` |
| `api/commerce_admin.py:427-465` mark paid | sets `paid` directly (W0a replaces it with a manual `Payment` and the settlement function) | `remaining_cents` from `balance()` is the default manual payment amount; an admin may enter less and choose "write off the rest", which issues a credit note with reason `write_off` |

Status filters that mean "unpaid" must use one shared set, `InvoiceService.UNPAID_STATUSES = {open, pending_action, partially_paid, overdue, collections}`: `api/reseller_panel.py:478-486`, `api/reseller_admin.py:320`, and the client invoice lists in `api/commerce_client.py`.

### 4.3 Overpayment

Engine `v2` only. When `recompute()` finds `due_cents < 0` (two payments raced, or a pending gateway payment settled after credit paid the invoice), with `excess = -due_cents`:

1. Issue a debit note on the invoice through `InvoiceAdjustmentService.issue_overpayment(db, invoice, excess, payment)`: reason `overpayment`, outcome `credited_to_balance`, one line (`kind` `adjustment`, "Overpayment credited to account balance", `unit_cents = excess`, tax 0), idempotency key `overpayment:<invoice_id>:<payment_id>` where the payment is the one whose success caused the overpayment.
2. In the same call, credit the invoice's account with `excess` through `CreditLedgerService.credit_account` (entry type `adjustment`, reference `debit_note` / adjustment id, idempotency key `overpayment-credit:<adjustment_id>`) and set the debit note's `ledger_entry_id`.
3. Evaluate the status again: the debit note raised `worth` by `excess`, so `due` is 0 and the invoice is `paid`.

This is the spec's rule (A2: "the excess becomes account credit (A3) with a debit note"). It cannot loop: a debit note raises what the invoice is worth, so it never produces a new negative `due`. Both steps happen in the transaction that recorded the overpaying payment, after the account and invoice locks are held. This replaces the `InvoiceError` raised today (`invoice_service.py:209-210`), which inside a webhook rolls back and makes the gateway retry forever. In `legacy` and `shadow` the old raise stays, and the shadow recorder writes check `i1_decision` on its own session first (6.4), so the case is visible before the switch.

### 4.4 Guards on adjustments that use the balance

- A credit note with outcome `reduced_balance` must have `total_cents <= B.remaining_cents`, and is refused while `pending_cents > 0` (a pending payment could otherwise settle into an overpayment created by the note).
- Credit notes with outcome `refunded` or `credited_to_balance` are only created by `RefundService`, together with their refund row, and their total equals the refund amount; the refund raises `refunded_cents`, so `paid_net` and `worth` fall by the same amount and `due` is unchanged. A refund that has no credit note (dispute, credit removal, refunds on a proforma) lowers `paid_net` only, so `due` rises.
- In all cases `credit_cents` after the note must not exceed `total_cents + debit_cents` (worth never negative), and a credit note never makes `due` negative: `reduced_balance` notes are limited to `remaining_cents`, and refund-paired notes are limited by the refund rules in 7.1.

## 5. Numbering, series and proforma

### 5.1 Which documents get which numbers

| Document | Series used | Allocated when |
|---|---|---|
| Invoice (`document_type = 'invoice'`) | default `invoice` series, or a series chosen by an admin on a manual invoice | at publish (`draft` to `open`) |
| Proforma (`document_type = 'proforma'`, setting `billing_proforma_enabled`) | default `proforma` series, into the `proforma_*` columns | at publish |
| Proforma converted to invoice | default `invoice` series, into `series_id`/`number_*` | at the transition to `paid`, inside the settlement transaction |
| Credit note | default `credit_note` series | at issue (notes are never drafts) |
| Debit note | default `debit_note` series | at issue |

`invoice_number` is still allocated at creation for every invoice and remains the internal reference.

### 5.2 Allocation modes

`InvoiceNumbering.allocate(db, series, at) -> (period, value, display)` in `app/services/invoice_numbering.py`:

- **`number_source = 'invoice_number'`** (the seeded invoice series): `value = invoice.invoice_number`, no counter. Numbers are unique and ordered by creation; drafts that are deleted leave gaps. Only allowed when `gapless` is false.
- **`own` counter, `gapless = false`** (the default for the other series): the counter row `series:<id>:<period>` is incremented on a **separate short-lived session** (`app.core.database` session factory, committed at once), using the dialect-specific pattern of today's `allocate_number` (`invoice_service.py:46-80`: `INSERT ... ON CONFLICT DO NOTHING` then `UPDATE ... RETURNING` on SQLite; `SELECT ... FOR UPDATE` then update on MySQL). The counter lock is held for milliseconds. If the caller's transaction later rolls back, that number is never used: numbers are unique and increasing, with possible gaps.
- **`own` counter, `gapless = true`**: the counter row is locked and incremented **inside the caller's transaction**, as the last lock it takes (section 0), and held until commit. A rollback returns the number with everything else, so every committed number is used exactly once and in order. The cost is that publishing (or paying, for proforma conversion) documents of that series is serialised; the publish and settlement transactions must therefore commit promptly: no gateway calls and no synchronous email inside them (both are already queued in this codebase).
- In every mode a void keeps its number, and published documents can never be deleted (3.5), so a number is never freed or reused.
- Period: `''` when `reset_period = 'never'`, else the four-digit UTC year of `at` (the publish time, or the payment time for conversions).

Admin rules (series API, P1-A): `format` must contain `{NUMBER}`, and also `{YEAR}` when `reset_period = 'yearly'`; the expanded number must fit 64 characters; a format change applies only to numbers allocated afterwards; switching a series from `invoice_number` to `own` sets the counter to one more than the largest `number_value` already in the series (or a higher start the admin gives); a series cannot be deleted once used, only disabled; there is always exactly one enabled default per document type. Switching to `own` also starts the counter above the next internal invoice number, under the publish locks, so an invoice being published cannot collide. Series changes take the series row, the global invoice number row and the series counter, in that order, as publish does.

Counter creation is race-safe: on MySQL and MariaDB it is `INSERT ... ON DUPLICATE KEY UPDATE` with no read before it (`INSERT IGNORE` deadlocked on MariaDB 11.8). A gapless series records its first number per period in `invoice_sequences` as `series-start:<id>:<period>`. The start moves with `start_value` only while nothing is issued in that period, after which raising the counter is refused (409). `billing_numbering_gaps` requires the issued numbers to run unbroken from that start to the largest, and reports issued numbers without a start row as a separate finding.

### 5.3 Format tags

| Tag | Expands to |
|---|---|
| `{SERIES}` | the series `code` |
| `{NUMBER}` | the value, left-padded with zeros to `number_padding` |
| `{YEAR}` | four-digit year of the allocation time, UTC |
| `{MONTH}` | two-digit month of the allocation time, UTC |

Anything else in `format` is literal text; braces other than these four tags are refused when the admin saves.

### 5.4 Proforma flow

Setting `billing_proforma_enabled` (default false). When it is on, every new invoice (all purposes, both tracks) is created with `document_type = 'proforma'`:

1. Publish allocates a proforma number into `proforma_series_id`, `proforma_number_period`, `proforma_number_value`, `proforma_number_display`; `series_id` and `number_*` stay NULL. `issued_at` is set (the proforma's date). Clients see the proforma number.
2. Payments, applied credit and pending payments work as for invoices. A part-paid proforma stays a proforma with status `partially_paid`.
3. When settlement moves it to `paid`, inside the settlement transaction and under the guard's `proforma_conversion` allowance: allocate a number from the default invoice series into `series_id` and `number_*`, set `document_type = 'invoice'`, and set `issued_at` to the payment time (the tax point). The snapshots taken at publish are kept. The proforma number stays on the row for reference.
4. A proforma is not a tax document, so credit notes and debit notes against it are refused. Refunds of payments on a proforma never issue credit notes (`issue_credit_note` is forced false), and the status is recomputed (it returns to `open` or `partially_paid`). An unpaid proforma is voided, never credited; it keeps its proforma number.
5. If `billing_proforma_enabled` is turned off, existing proformas finish their life as proformas (they still convert when paid); only new invoices change.

### 5.5 Snapshots

At publish, `InvoiceService` writes:

- `seller_snapshot`: `{"company_name", "company_address", "company_tax_id", "captured_at"}` from the settings the PDF reads today (`invoice_pdf_service.py:76-85`). Phase 2 (tax, invoice profile) adds keys; keys are only ever added.
- `buyer_snapshot`: from the account's `BillingProfile` (`commerce_account.py:108-132`): `legal_name`, `company`, `address_line1`, `address_line2`, `city`, `region`, `postal_code`, `country`, `tax_id`, `invoice_email`, plus `account_type`, `billing_account_id`, `reseller_id` and `captured_at`. If no profile exists, the user's name and email.

The PDF for a v2 invoice reads only the snapshots. Legacy invoices (`model_version IS NULL`) render from live data exactly as today; nothing is backfilled. Credit and debit notes copy the invoice's snapshots, or build fresh ones at issue for a legacy invoice that has none.

## 6. A3: credit balance for every account

### 6.1 Model

- Every account has one ledger chain: the entries with its `billing_account_id`, ordered by `id`. The reseller track uses the reseller's `billing_accounts` row (`account_type = 'reseller'`); the retail track uses the client's row. A reseller always has such a row after revision 7, and `BillingAccountDAO.ensure_reseller_account` (`commerce_dao.py:75-89`) keeps creating it for new resellers, now with `credit_balance_cents = 0` explicitly for a reseller that has no ledger entries (copying the reseller cache there was the source of the stale value).
- Reseller entries carry both `reseller_id` and `billing_account_id`; retail entries only `billing_account_id`.
- `billing_accounts.credit_balance_cents` is the cache for every account. For reseller accounts, `resellers.cached_balance_cents` is a second cache of the same chain until contract step c2.
- `balance_after_cents` on each entry is the authoritative balance after that entry (see 6.2 for which cache is authoritative per mode).

### 6.2 `CreditLedgerService` changes (P1-C)

- Keep every existing method and signature (`credit`, `debit`, `adjust`, `reverse`, all taking `reseller_id`; `credit_ledger_service.py:139-280`), because the reseller path is the reference and its tests call them. Internally they resolve the reseller's account and call the account-level code.
- Add `credit_account`, `debit_account`, `adjust_account` taking `billing_account_id` with the same keyword arguments, `available_cents(db, account) -> int`, `apply_to_invoice(...)` and `remove_from_invoice(...)` (6.7).
- One private `_append(db, account, reseller_or_none, entry_type, signed_amount_cents, context, allow_negative_balance)`:
  1. lock the reseller row (reseller accounts only), then the account row (section 0; re-locking a row the transaction already holds is a no-op);
  2. idempotency check as today, now also comparing `billing_account_id`;
  3. compute per credit engine mode:
     - `legacy`: exactly today's code (`credit_ledger_service.py:106-136`); `billing_account_id` is not written and the account cache is not touched. This mode exists only as an emergency rollback.
     - `shadow`: for a reseller account, `authoritative = reseller.cached_balance_cents + amount` (the insufficient-credit check uses it, as today), `mirror = account.credit_balance_cents + amount`; for a retail account `authoritative = account.credit_balance_cents + amount` and there is no mirror. Write both caches, set `balance_after_cents = authoritative` and `billing_account_id`.
     - `v2`: as `shadow` with the roles swapped for reseller accounts: the account cache is authoritative and the reseller cache is the mirror.
  4. if a mirror exists and differs from `authoritative`, record check `c1_cache_pair` (6.4). The mirror is written as computed, not overwritten with the authoritative value, so drift stays visible to the daily scan until someone resolves it;
  5. insert the entry, then fire the internal credit event (`credit.added` for a positive entry, section 9.2).
- `available_cents(db, account)` returns `max(0, authoritative cache)` by the same mode rule. Every reader listed in 6.6 uses it.
- `apply_to_invoice`, `adjust_account` and `debit_account` take `allow_negative_balance` (default `False`). With `True`, `apply_to_invoice` is no longer capped by the available credit, only by the invoice's remaining amount, and the balance carries the shortfall (the reseller `debit_balance` dispute policy, 7.6). Every other caller keeps the default.

### 6.3 Lock order in practice

- `PaymentOrchestrator._lock_funding_rows` (`orchestrator.py:404-415`) locks reseller, then invoice. P1-C changes it to reseller, then account, then invoice, so the later ledger call inside the same transaction does not take the account lock after the invoice lock.
- `PaymentRefundService.refund` (`refunds.py:72-75`) and `PaymentWebhookService._payment_for_update` (`webhooks.py:121-144`) lock the payment first and the reseller later (through the ledger), the reverse of the orchestrator's order, which can deadlock today. P1-D replaces both with `BillingLocks` (account, invoice, payment) in section 7.
- Retail settlement (W0a) locks the account, then the invoice, then the payment.
- Number allocation in gapless mode comes last in every transaction.

### 6.4 Shadow comparisons

Two engines run in shadow during phase 1: the invoice engine (`billing_invoice_engine = shadow`) and the credit engine (`billing_credit_engine = shadow`). Both are the default after deploy (appendix A).

**Where differences are recorded.** `BillingShadowService.record(engine, check_key, subject_type, subject_id, legacy_value, v2_value, context)` in `app/services/billing_shadow.py`:

- writes on its **own short session** and commits at once, so a mismatch found in a transaction that later rolls back (the overpayment raise in 4.3, for example) is still kept;
- upserts on `uq_billing_shadow_mismatch` (increment `occurrences`, set `last_seen_at`, clear `resolved_at`);
- logs one line at WARNING: `billing shadow mismatch engine=<e> check=<k> <subject_type>=<id> legacy=<..> v2=<..> context=<..>`;
- never raises into the caller (it logs its own failures).

**What is compared.**

| Check key | Engine | When | Compares |
|---|---|---|---|
| `c1_cache_pair` | credit | every ledger write to a reseller account | `resellers.cached_balance_cents` and `billing_accounts.credit_balance_cents` after the write |
| `c2_cache_vs_chain` | credit | daily scan, every account with entries | the authoritative cache equals `balance_after_cents` of the account's last entry (max `id`) |
| `c2_cache_vs_sum` | credit | daily scan | the authoritative cache equals Σ `amount_cents` of the account's entries |
| `c2_chain_continuity` | credit | daily scan | for each entry, `balance_after_cents = previous balance_after_cents + amount_cents` (window function `LAG() OVER (PARTITION BY billing_account_id ORDER BY id)`, available on MariaDB 10.2+ and SQLite 3.25+); one mismatch row per account at its first broken entry |
| `c2_mirror` | credit | daily scan | for reseller accounts, the mirror cache equals the authoritative cache |
| `c2_unlinked` | credit | daily scan | count of entries with `reseller_id` set and `billing_account_id` NULL is 0 (subject is the reseller) |
| `i1_decision` | invoice | every call site of 4.2 in shadow mode | legacy `remaining_cents` and paid/not-paid decision against `balance().remaining_cents` and the `v2` status rule's paid/not-paid answer |
| `i2_scan` | invoice | daily scan, invoices with `updated_at` in the last 48 hours plus every invoice with `id % 7` equal to the weekday number (a full sweep each week) | same as `i1` |
| `i2_expected_partial_refund` | invoice | daily scan, as `i2_scan` | an `i2` difference that equals exactly the sum, over the invoice's partially refunded payments, of the amount minus `refunded_cents`; listed separately and not counted by the switch criterion (6.5), the quiet period or the mismatch alert. Any other difference stays `i2_scan` |
| `i3_totals` | invoice | at publish in shadow mode | the legacy `amount_cents` against the total computed from v2 lines (1.3.1) |
| `i4_frozen_write` | invoice | guard in `log` mode | a frozen column was written (3.5) |

**The daily scan.** `BillingShadowService.scan(db, now)` runs in the recurring billing worker process (`app/workers/recurring_billing.py`) once every 24 hours under its own lease row `billing_shadow_scan` in `recurring_billing_leases` (the `acquire_lease` pattern of `recurring_billing_service.py:86-121`). It reads in batches of 1,000 accounts or invoices, records mismatches through `record()`, and finally writes `system_settings` key `billing_shadow_scan` (outside W0c's `setting:` prefix) with `{"engine": {"credit": {...}, "invoice": {...}}}`, each holding `started_at`, `finished_at`, `subjects_checked` and `mismatches_found`. The scan also runs on demand from the admin engines page.

**Alerts.** P1-C adds two detectors to `app/services/alerting/billing_integrity.py` (the E2 module W0b is writing), registered the way W0b registers its detectors in the alerting catalogue and `DETECTORS`:

- `billing_shadow_mismatch`: staff audience, severity warning, one finding per engine with open (unresolved) rows, listing the check keys and counts. Read-only, as detectors must be (`app/services/alerting/detectors.py:1-5`).
- `billing_shadow_scan_stale`: a shadow engine whose last completed scan is older than 26 hours.

The admin engines page (P1-C) lists open mismatches with their values and lets an admin resolve one with a note; it never edits money.

### 6.5 The switch criterion ("zero mismatches")

An engine moves to `v2` only through `POST /api/admin/billing/engines/{engine}/switch` with `{"to": "v2"}` (P1-C, admin only, audited). The endpoint checks, and refuses with the failing item named:

1. the engine is currently `shadow`;
2. preconditions: for `credit`, check `c2_unlinked` is 0 right now and every reseller has an account; for `invoice`, revision 9 is applied (zero totals possible) and the credit engine is already `v2` or the operator confirms that overpayments will be credited to the reseller cache's chain (open question 6 recommends switching credit first);
3. quiet period: `now - max(shadow_since, the latest last_seen_at of any mismatch row of this engine) >= billing_shadow_min_days` (default 14 days). Resolving a row does not shorten the period: the clock restarts at the last time any mismatch was seen;
4. at least `billing_shadow_min_days` daily scans completed in the quiet period, the latest finished less than 26 hours ago, and the latest found 0 mismatches for the engine;
5. no open mismatch rows for the engine.

"Zero mismatches" therefore means: no mismatch of that engine, inline or scanned, for the whole quiet period, with a complete scan every day of it. `shadow_since` is recorded when the engine enters `shadow` (on first boot after the migration, or when an admin switches back to it).

On success the endpoint writes the setting and a `system_settings` row `billing_engine_switch:<engine>` with the mode, time, admin and a copy of the criteria values. The engine treats the setting as `v2` only if that record agrees; a setting edited to `v2` by other means is treated as `shadow` and logged at ERROR.

Rollback (as built by P1-C; every switch is audited as `billing.engine.switch` with the admin and the from and to modes):

- The credit engine may go from `v2` back to `shadow` or to `legacy` at any time. The reseller cache becomes authoritative again, and the response and audit record list any reseller accounts whose two caches differ at that moment, for staff to resynchronise.
- The invoice engine may go from `v2` back to `shadow` or `legacy` only while no credit note, debit note or partial refund exists, because the legacy rule ignores them; the only way forward then is a fix in `v2`.
- Going back to `shadow` restarts the quiet period; going to `legacy` clears `shadow_since`; `legacy` to `shadow` starts it. Deviation: the design first allowed `legacy` only from `shadow`; the direct `v2` to `legacy` path was added so both rollbacks exist.

The engine settings change only through this endpoint: `PUT` and `DELETE` on `billing_credit_engine` and `billing_invoice_engine` in `/api/admin/settings` return 409 `setting_managed_elsewhere`, and its listing marks them `read_only` with `managed_by: "billing_engines"`.

### 6.6 Readers of the reseller cache

P1-C moves every reader to `available_cents(db, account)` (or, for display, to a `balance_cents` computed by the same rule), so that contract step c2 only removes writes: `api/reseller.py:149` and `:158`, `api/reseller_panel.py:504` and `:752`, `api/reseller_admin.py:394` and `:1991`, `reseller_billing_service.py:237` and `:694`, `recurring_billing_service.py:803`, `payments/orchestrator.py:480`, and `credit_ledger_service.py:109-117` itself. `commerce_dao.py:85` stops copying the cache (6.1). `api/commerce_admin.py:208` already shows the account cache, which is correct after the backfill. Response fields keep their names (`balance_cents`, `new_balance_cents`, `credit_balance_cents`).

The retirement (contract step c2) is a later phase: once the credit engine has been `v2` for one release with no `c1_cache_pair` or `c2_mirror` mismatch, stop writing `resellers.cached_balance_cents`, then drop the column a release later.

### 6.7 Behaviour for both tracks

Settings are in appendix A; defaults follow spec A3.

- **Add funds (retail).** `POST /api/client/commerce/credit/top-up-invoices` creates a `credit_topup` invoice for the client's account through `InvoiceService.create_topup(billing_account_id=...)`, exactly as `POST /top-up-invoices` does for resellers (`reseller_panel.py:875-899`), within the minimum, maximum and maximum-balance settings, and only if the account has at least one order in status `fulfilled` when `billing_credit_require_approved_order` is on (spec A3 default: on). When it is paid, the settlement function credits the account: today's top-up credit in `finalize_success` (`orchestrator.py:633-650`) uses `invoice.reseller_id` and becomes `credit_account(invoice.billing_account_id, ...)` with the same idempotency key `topup-invoice:<id>`.
- **Apply credit to an invoice.** `CreditLedgerService.apply_to_invoice(db, invoice, amount_cents=None, actor_user_id, idempotency_key)`: amount defaults to `min(available, remaining)`; creates `Payment(gateway="credit", status=succeeded)` and a debit entry exactly as `_apply_credit` does today (`orchestrator.py:474-523`, which P1-C moves here and calls from the orchestrator), then `recompute()`. Available to admins on both tracks and to clients (retail) and resellers in their panels.
- **Automatic application.** After publish, if the track's setting is on (`billing_credit_auto_apply_retail`, default off; resellers keep their `charge_preference`, `reseller.py:211`, read by the orchestrator as today).
- **Remove credit from an invoice.** `remove_from_invoice(db, payment, amount_cents, actor)` calls `RefundService.refund(destination="credit", reason="credit_removed", issue_credit_note=False)` on the credit payment: the money returns to the balance and the invoice's due amount reopens.
- **Overpayment** goes to credit (4.3).
- **Refund to credit** (A4, 7.2) issues a credit note with outcome `credited_to_balance`.
- **Credit on downgrade** is A6 (phase 3); phase 1 only provides `credit_account` for it.
- **Negative balances.** Resellers can go negative only through the paths that allow it today (`allow_negative_balance=True` in chargebacks, `webhooks.py:271-290`), which become the reseller dispute policy in 7.6. A retail balance goes negative only when a credit top-up is charged back after the credit was spent (7.6); the E2 detector lists every negative retail balance for staff follow-up. Everything else refuses to take a balance below zero.

## 7. A4: refunds and chargebacks

### 7.1 Rules

- `RefundService` (`app/services/refund_service.py`, new) is the only writer of `refunds` and of `payments.refunded_cents` and payment refund statuses. `PaymentRefundService().refund` (`app/services/payments/refunds.py:65-142`), used by the reseller admin endpoint (`app/api/reseller_admin.py:1171`), becomes a thin wrapper that calls `RefundService.request(...)` for the full refundable amount, so its tests (`tests/services/test_payment_refunds.py`) keep passing.
- A refund is against one payment. `refundable = amount_cents - refunded_cents - Σ amount of this payment's pending and review refunds`; a request above it is refused. A payment must be `succeeded` or `partially_refunded`.
- Currency is the payment's currency; no conversion.
- Partial refunds, refunds with credit notes, `destination = 'credit'` on gateway payments, and `destination = 'manual'` need the invoice engine in `v2`. In `legacy` and `shadow` the service accepts only full refunds and applies today's side effects (payment `refunded`, invoice `failed` through `transition()`, ledger reversal of credit and top-up entries, `refunds.py:144-198` and `webhooks.py:245-301`), but it still uses the refund row, the idempotency key and the reconciler, which fixes the money-safety gaps immediately.
- Destinations per gateway: `gateway` for Stripe and PayPal; `credit` for every gateway; `manual` for every gateway (admin attests the money was returned outside Rackflow); USDT is credit-only, so `gateway` is refused for `usdt_crypto` (today `refunds.py:61-62`); a payment with `gateway = 'credit'` (applied credit) can only be refunded to `credit`.
- Refunds on proformas (5.4) never issue credit notes, and refunds on `credit_topup` invoices never issue credit notes either (a top-up invoice is a deposit, not a supply); both set `issue_credit_note = false`.

### 7.2 Flow

`RefundService.request(db, *, payment_id, amount_cents, destination, reason, reason_text=None, issue_credit_note=None, credit_note_lines=None, actor_user_id, idempotency_key) -> Refund`. The API layer passes the `Idempotency-Key` request header (the admin UI generates one per dialog), or `admin-refund:<payment_id>:<uuid4>` if none.

**Destination `credit` or `manual`** (no gateway): one transaction. Locks per section 0, validate, insert the refund already `succeeded`, then run the completion step below.

**Destination `gateway`**: three steps.

1. **Reserve** (transaction 1, short). Lock account, invoice, payment (section 0). If a refund with this `idempotency_key` exists, return it (or raise if its parameters differ). Validate the rules of 7.1. Insert the refund `pending` with `attempts = 1`, `claim_token` (uuid4), `claim_expires_at = now + 10 minutes`. For a `credit_topup` invoice, also place a **credit hold**: `debit_account` of the refund amount (reference `refund` / refund id, idempotency key `refund-hold:<refund_id>`), refusing the request if the balance is too low (resellers: `allow_negative_balance=False` here too), so the deposit cannot be spent while the gateway works. Commit.
2. **Gateway call** with no database locks held: `gateway.refund_payment(payment, amount_cents, idempotency_key=refund.idempotency_key, reason=...)`. The gateway protocol (`app/services/payments/base.py:127`) gains the keyword-only `idempotency_key` argument: Stripe sends it as the `Idempotency-Key` request option (`stripe_gateway.py:229-250`), PayPal as the `PayPal-Request-Id` header (`paypal_gateway.py:433-470`). Outcomes:
   - `SUCCEEDED` (or Stripe `pending`, which Stripe completes itself): go to step 3 with the gateway refund id.
   - A definitive decline (the gateway returns `FAILED` with an error code): transaction 2b sets the refund `failed` with `failure_code` and `failure_message`, releases the credit hold (`credit_account`, idempotency key `refund-hold-release:<refund_id>`) and emits nothing else. No payment or invoice change.
   - A timeout, a network error, a 5xx or anything not understood: leave the refund `pending`, set `next_attempt_at = now + 5 minutes`, clear the claim, and return the refund to the caller with HTTP 202 and status `pending`. Never mark success or failure without a gateway answer.
3. **Complete** (transaction 2). Lock account, invoice, payment, refund. If the refund is no longer `pending` (the reconciler or a webhook completed it first), return it. Otherwise run the completion step.

**Completion step** (shared by every path, idempotent on the refund's status):

1. Refund: `status = succeeded`, `gateway_refund_ref`, `completed_at`.
2. Payment: `refunded_cents += amount`; status `refunded` if `refunded_cents == amount_cents`, else `partially_refunded`; `refunded_at = now` (last refund); `payment_metadata` keeps the existing `reversal_type` key for compatibility.
3. Credit: for `destination = 'credit'`, `credit_account` of the amount (entry type `refund`, reference `refund` / refund id, idempotency key `refund:<refund_id>`), set `refunds.ledger_entry_id`. For a `credit_topup` invoice, the hold from step 1 is the reversal (nothing more to do); for a top-up refunded to credit (an odd but possible admin choice) no hold is placed and nothing is credited, since the money is already balance.
4. Credit note: if `issue_credit_note`, `InvoiceAdjustmentService.issue_for_refund(db, refund, lines=credit_note_lines)`: kind `credit`, reason `refund`, outcome `refunded` (destinations `gateway`, `manual`) or `credited_to_balance` (destination `credit`), total equal to the refund amount, idempotency key `refund-cn:<refund_id>`, and set `refunds.adjustment_id`. Default lines: one line "Refund of payment <id>" of kind `adjustment`, with the tax part split from the amount by rule `refund_tax_proportional` (appendix B) using the invoice's tax and total; the admin may instead pass explicit lines that reference invoice lines.
5. `InvoiceService.recompute(db, invoice, cause="refund")` (engine `v2`), or `transition(..., to=FAILED)` for a full refund in `legacy`/`shadow`.
6. Fire the internal event `payment.refunded` (section 9); `credit_note.issued` and `invoice.refunded` fire from their own code paths in the same transaction.

As built (P1-R): a partial refund's tax is `refund_tax_proportional(previous + this) - tax already refunded`, stored in `refund_metadata.tax_cents` and passed to the credit note as explicit lines, so rounding never refunds more tax than the invoice carries. A legacy or shadow refund of a payment on a `void` invoice leaves the invoice `void` (3.4); the payment and ledger effects still apply. Outside `v2`, a partial refund reported by the gateway is recorded as a full refund, as before phase 1 (7.1 allows only full refunds there).

### 7.3 Idempotency keys

| Key | Where | Format |
|---|---|---|
| Refund request | `refunds.idempotency_key`, also sent to the gateway | header value, `admin-refund:<payment_id>:<uuid4>`, `<gateway>-refund:<event_id>`, `<gateway>-dispute:<event_id>`, `order-cancel:<order_id>:<payment_id>` |
| Credit hold | ledger | `refund-hold:<refund_id>` |
| Hold release | ledger | `refund-hold-release:<refund_id>` |
| Refund to credit | ledger | `refund:<refund_id>` |
| Credit note | `invoice_adjustments.idempotency_key` | `refund-cn:<refund_id>` |

Stripe keeps idempotency keys for at least 24 hours and PayPal request ids for a limited period, so the reconciler stops retrying the gateway call after 24 hours (7.4) instead of relying on them indefinitely.

As built (P1-R): staff `Idempotency-Key` values are stored as `admin:<value>`, so they never replay or collide with system keys.

### 7.4 Reconciler

`RefundReconciler.run(db, now)` in `app/services/payments/refund_reconciler.py`, run every 5 minutes by the recurring billing worker through the periodic job list (section 11, P1-S) under lease `billing_refund_reconciler`:

1. Select up to 50 refunds with `status = 'pending'`, `next_attempt_at <= now`, and (`claim_token IS NULL` or `claim_expires_at < now`).
2. Claim each with a conditional `UPDATE refunds SET claim_token = :t, claim_expires_at = :exp, attempts = attempts + 1, last_attempt_at = :now WHERE id = :id AND status = 'pending' AND (claim_token IS NULL OR claim_expires_at < :now)`; skip it if no row was updated.
3. Ask the gateway: `gateway.lookup_refund(payment, idempotency_key, gateway_refund_ref)` when the gateway implements it (new optional protocol method; Stripe lists refunds of the charge and matches `metadata.rackflow_refund_id`, which `refund_payment` now sets; PayPal looks up by refund id); otherwise call `refund_payment` again with the same idempotency key, which returns the original result instead of refunding twice.
4. Succeeded: run step 3 of 7.2. Definitively failed: run 2b. Still unknown: back off (`next_attempt_at` after 5 minutes, 15 minutes, 1 hour, 6 hours, then 24 hours from the request), clear the claim.
5. After 24 hours or 6 attempts with no definitive answer, set `status = 'review'`. Staff resolve a `review` refund from the admin refunds page by confirming "succeeded" (with the gateway refund reference, then the completion step runs) or "failed" (the hold is released). Both are audited.

The gateway webhook for a refund (`charge.refunded`, PayPal `PAYMENT.CAPTURE.REFUNDED`) also completes a pending refund: it looks the refund up by `gateway_refund_ref` or `metadata.rackflow_refund_id` and runs step 3. A refund event that matches no refund row was made in the gateway's dashboard: the webhook creates a refund row (`reason = 'gateway_initiated'`, `destination = 'gateway'`, `status = 'succeeded'`, idempotency key `<gateway>-refund:<event_id>`) for the amount the event reports, or for the whole refundable amount when the event has none (as today's handler assumes, `webhooks.py:245-301`), and runs the completion step.

E2 detectors added by P1-R: `billing_refund_stuck` (any refund in `review`, or `pending` for more than 1 hour) and `billing_refund_mismatch` (a payment whose `refunded_cents` differs from the sum of its succeeded refunds, or exceeds its amount).

As built (P1-R): a refund event without a gateway reference completes an open gateway refund only when exactly one open refund has the event's amount. A Stripe `charge.refunded` without a refunds list reports `amount_refunded` as a running total; only the part beyond the refunds already succeeded is recorded.

### 7.5 Chargebacks (disputes)

A chargeback event (`charge.dispute.created`, PayPal `CUSTOMER.DISPUTE.CREATED` resolved against the merchant; today `webhooks.py:195-209` and `:245-301`) goes through the same completion step:

- refund row: `reason = 'dispute'`, `destination = 'gateway'`, `status = 'succeeded'` (the money has already left), `issue_credit_note = false`, idempotency key `<gateway>-dispute:<event_id>`, amount from the event (default: the whole refundable amount);
- no credit note: the invoice was correct, the customer just took the money back, so `paid_net` falls and `due` rises;
- then the track's dispute policy (7.6).

As built (P1-R): in `legacy` and `shadow`, and for reseller invoices in `v2`, the services linked to a disputed invoice are suspended after the dispute's transaction commits, each in its own transaction with no billing lock held. A failed suspension is rolled back, logged and audited as `billing.dispute_suspend_failed`; it does not undo the dispute or stop the other suspensions. The webhook routes run `process` in a worker thread. Before phase 1 this suspension ran inside the async webhook routes through `asyncio.run`, which raised, so a dispute on an invoice with linked services returned 500 and was never recorded (to-do 13e91a6c, an existing production bug fixed by phase 1).

### 7.6 Dispute policies

| Setting | Values | Default | Behaviour |
|---|---|---|---|
| `billing_dispute_policy_reseller` | `debit_balance`, `reopen_invoice` | `debit_balance` | `debit_balance` is today's behaviour (`webhooks.py:271-290`): apply credit to the reopened invoice with `allow_negative_balance=True`, so the invoice returns to `paid` and the reseller's balance carries the loss. For a `credit_topup` invoice, debit the top-up amount from the balance, also allowing a negative balance |
| `billing_dispute_policy_retail` | `reopen_invoice`, `debit_balance` | `reopen_invoice` | `reopen_invoice`: `recompute()` with the dispute cause moves the invoice to `collections` (3.2); the services are not suspended by phase 1 (automation, workstream B, decides later). For a `credit_topup` invoice, debit the top-up from the balance, allowing a negative retail balance (the one exception in 6.7) |

A dispute later won by the merchant is recorded by staff as a manual payment (W0a's `record_manual_payment`) in phase 1; automatic handling of `charge.dispute.closed` is open question 9.

### 7.7 Order cancellation

W0a's `billing_order_refund_policy` (`manual_review`, `original_method`, `credit`; W0a's `app/services/billing/order_refunds.py` holds `original_method` and `credit` for manual review "until the refund engine exists") becomes real in P1-R when the invoice engine is `v2`: `original_method` calls `RefundService.request(destination='gateway', reason='cancellation')` for each succeeded payment of the order's invoice (credit payments go to `credit`), `credit` uses `destination='credit'`. Both issue credit notes. In `legacy` and `shadow` the manual review behaviour stays.

As built (P1-R): gateway refunds from order cancellation are only reserved in the cancel transaction and sent afterwards by dispatch or the reconciler. Staff resolve a refund review by refunding to card, crediting the account, or dismissing with a note, through `/api/admin/billing/refund-reviews`; each resolution is idempotent and audited.

## 8. D1: cycles

### 8.1 The interval vocabulary

One table, in W0a's `app/services/billing/intervals.py` (`INTERVAL_MONTHS`, `normalise_interval`, `interval_months`, `advance_months`), extended by P1-D:

| Label (`PricePlanInterval`, `order_items.cycle_interval`, `service_billings.cycle_interval`) | `interval_unit` | `interval_count` | months |
|---|---|---|---|
| `monthly` | `month` | 1 | 1 |
| `quarterly` | `month` | 3 | 3 |
| `semiannually` | `month` | 6 | 6 |
| `annually` | `year` | 1 | 12 |
| `biennially` | `year` | 2 | 24 |
| `triennially` | `year` | 3 | 36 |

- `price_plan_cycles.interval_unit` and `interval_count` are the canonical description of a cycle from revision 6 on; the existing `interval` label column stays and is kept consistent by the store admin API (`app/api/commerce_store_admin.py`), which accepts either form and writes both. Contract step c4 can make the label optional later for cycles outside the six names.
- `months = interval_count * (12 if interval_unit == 'year' else 1)`.
- `normalise_interval` today maps any unknown label to `monthly` (W0a's draft). P1-D changes it to raise `ValueError` for an unknown non-empty label, and keeps `None` or empty as `monthly`, so a triennial service can never be billed monthly by a typo or an old process.
- Rollout order matters: biennial and triennial cycles are refused by the store admin API until the setting `billing_long_cycles_enabled` is on, and the admin may only turn it on once every process runs the new `intervals.py` (deploy finished), so no old renewal worker meets an unknown label.

### 8.2 Meeting `service_billings` (A0 item 5)

W0a adds `service_billings.cycle_interval` (String(32), nullable), `interval_months` (Integer, nullable) and `cancel_at`, with `monthly_price_cents` meaning the price of one period of `interval_months` (W0a's draft of `app/models/reseller.py`). D1 builds on that with no new columns:

- At fulfilment (`commerce_recurring_service.py:51-90`, W0a's code) the service billing copies `cycle_interval` from the order item and sets `interval_months` from the table above. `interval_months` is the number every engine uses for date arithmetic (`advance_months(value, interval_months, anchor_day)`); `cycle_interval` is the display label and the key back to the price plan cycle.
- Renewal price: the service keeps the price it was sold at (`monthly_price_cents`, per period) unless an admin changes it; D1 does not re-read the plan at renewal. Phase 3 price overrides (D2 and later) build on this.
- Reseller services stay monthly (`interval_months` NULL means 1), so `tests/services/test_recurring_billing_service.py` is unaffected.
- Invoice lines of a renewal carry `kind = 'product'`, `period_start = cycle.period_start`, `period_end = cycle.period_end` and the `service_billing_id`, so a 36-month invoice shows its period.

### 8.3 Per-cycle setup fees

- `CheckoutService._resolve_cycle_price` (`checkout_service.py:90-115`) moves its body to `app/services/billing/cycles.py` as `resolve_cycle(plan, cycle_interval) -> ResolvedCycle(label, interval_unit, interval_count, months, recurring_cents, setup_cents)`; `_resolve_cycle_price` stays as a wrapper (P1-S does the move in wave 1 so P1-D and P1-I never edit the same file).
- `setup_cents = cycle.setup_cents if cycle.setup_cents is not None else plan.setup_cents`. The setup fee is charged once, on the order invoice, as a line of `kind = 'setup'`; renewals never include it.
- Cycles are looked up by (`interval_unit`, `interval_count`) when the request gives them, else by label.
- Storefront and client APIs gain `interval_unit`, `interval_count`, `months` and `setup_cents` on each cycle (fields only added).

## 9. Settlement, internal follow-ups and the event catalogue

### 9.1 One registry, not two

W0a's draft `app/services/billing/settlement.py` has `settle_invoice(db, invoice_id, payment=None)`, the only code that moves an invoice to `paid`, and an in-process subscriber list for `invoice.paid` (`subscribe_invoice_paid(name, handler, applies=..., isolated=True)`, `fire_invoice_paid(db, event)`, isolated subscribers in savepoints, defaults loaded from `settlement_subscribers.py`). W0a's names may still change before it merges; phase 1 follows whatever W0a merges.

Phase 1 generalises that list instead of adding a second mechanism (P1-S, wave 1):

- `subscribe(event, name, handler, *, applies=_always, isolated=True)` and `fire(db, event, payload)` keyed by event name, using the same list structure, the same savepoint isolation and the same default loading. `subscribe_invoice_paid` and `fire_invoice_paid` stay as wrappers for `event = "invoice.paid"`.
- Internal event names are the catalogue names (9.2), so there is one vocabulary. Each internal payload is a frozen dataclass carrying ORM objects (as `InvoicePaid(invoice, payment)` does), not a dict.
- For each catalogue event, exactly one default subscriber named `catalogue:<event>` (registered in `settlement_subscribers.py`) builds the catalogue payload and calls `app.plugin_host.events.emit(db, name, payload, service_id=...)`. That subscriber is the only emission site for the event. `emit()` also queues the outbound commerce webhook for names in `COMMERCE_WEBHOOK_EVENTS` (`app/services/commerce_webhook_service.py:23-25`, `app/plugin_host/events.py:90-116`), so billing code never calls `CommerceWebhookService.enqueue` itself. The catalogue subscriber is `isolated=True`, and `emit()` never raises, so a plugin problem can never undo money.
- Internal follow-ups subscribe to the same events: for example the invoice email (`invoice.created`), a future accounting export (`credit_note.issued`), and reseller notifications.
- Every `fire()` happens inside the transaction that made the change, after the change is flushed; plugin deliveries and webhooks are queued rows in that transaction, so they are sent only if it commits.

### 9.2 Catalogue additions

All new events need permission `billing.read`, are not service-scoped, and are not "before" events. The existing `invoice.paid` (`app/plugin_host/catalogue.py:77`) is unchanged: phase 1 only adds optional fields to it. The plugin doc's list of suggested names (`docs/plugin-system.md`, section 6.2.1, after the emission table) is followed where it covers the case (`invoice.created`, `invoice.overdue`, `payment.refunded`, `credit.applied`).

| Event | Required fields | Optional fields | The one emission site (internal event fired by) |
|---|---|---|---|
| `invoice.paid` (existing) | `invoice_id`, `billing_account_id` | `invoice_number`, `order_id`; added: `reseller_id`, `number_display`, `total_cents`, `currency`, `document_type` | `settle_invoice` (W0a) |
| `invoice.created` | `invoice_id`, `billing_account_id`, `purpose`, `total_cents`, `currency` | `reseller_id`, `invoice_number`, `number_display`, `order_id`, `service_id`, `due_at`, `document_type` | `InvoiceService.publish` (`draft` to `open`) |
| `invoice.overdue` | `invoice_id`, `billing_account_id`, `due_cents`, `currency` | `reseller_id`, `invoice_number`, `number_display`, `due_at` | `InvoiceService.transition` on the edge to `overdue` |
| `invoice.voided` | `invoice_id`, `billing_account_id`, `reason` | `reseller_id`, `invoice_number`, `number_display` | `InvoiceService.transition` on edges to `void` |
| `invoice.refunded` | `invoice_id`, `billing_account_id`, `refunded_cents`, `currency` | `reseller_id`, `invoice_number`, `number_display` | `InvoiceService.transition` on edges to `refunded` |
| `credit_note.issued` | `credit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome` | `reseller_id`, `number_display`, `refund_id` | `InvoiceAdjustmentService` after inserting a credit note |
| `debit_note.issued` | `debit_note_id`, `invoice_id`, `billing_account_id`, `total_cents`, `currency`, `reason`, `outcome` | `reseller_id`, `number_display` | `InvoiceAdjustmentService` after inserting a debit note |
| `payment.refunded` | `refund_id`, `payment_id`, `invoice_id`, `billing_account_id`, `amount_cents`, `currency`, `destination`, `reason` | `reseller_id`, `gateway`, `gateway_refund_ref`, `credit_note_id` | `RefundService` completion step (7.2), including disputes (`reason = 'dispute'`) |
| `credit.added` | `ledger_entry_id`, `billing_account_id`, `amount_cents`, `balance_cents`, `currency`, `entry_type` | `reseller_id`, `invoice_id`, `refund_id` | `CreditLedgerService._append` for a positive entry |
| `credit.applied` | `ledger_entry_id`, `billing_account_id`, `invoice_id`, `amount_cents`, `balance_cents`, `currency` | `reseller_id`, `payment_id` | `CreditLedgerService.apply_to_invoice` |

Notes:

- `billing_account_id` is required everywhere because every invoice, note, refund and ledger entry written by phase 1 has one (2.3). Legacy invoices without one (old reseller rows before the backfill) carry the value resolved through the reseller's account.
- `credit.added` and `credit.applied` carry `balance_cents` only from the authoritative cache of 6.2.
- `COMMERCE_WEBHOOK_EVENTS` is not changed in phase 1, so outbound commerce webhooks behave exactly as today (open question 8).
- Catalogue edits (`app/plugin_host/catalogue.py`), the matching rows in `docs/plugin-system.md` sections 6.2 and 6.2.1 and the `billing.read` description in section 7 are owned by P1-S and land in wave 1, so no wave 2 worker touches `catalogue.py`. Wave 2 workers only call `fire()` with the names above.

### 9.3 Ordering inside a transaction

Within one transaction, events fire in the order the changes happen. For a full gateway refund in `v2` that is `credit_note.issued`, `payment.refunded`, then `invoice.refunded`. For an overpaid card payment it is `debit_note.issued`, `credit.added`, then `invoice.paid`. Subscribers must not depend on any other order.

## 10. Test plan

### 10.1 Migrations (P1-S)

- One test module per revision, in the style of `tests/services/test_private_network_dhcp_migration.py`: a single head; each revision's `down_revision` is the one before it in 2.2; upgrade then downgrade then upgrade again on a SQLite file database built by `alembic upgrade` from the phase 0 head.
- Revision 9: zero-amount insert refused before, accepted after, refused again after downgrade; the table keeps every index, unique key and foreign key (compare `PRAGMA index_list` and `PRAGMA foreign_key_list` before and after).
- Revision 5: `reseller_id` NULL insert works with `billing_account_id` set; revision 10: both NULL refused.
- Backfills: idempotent (run twice, same result); batched (a test with a batch size of 2 over 5 rows); account creation matches `ensure_reseller_account`.
- MySQL: the same tests run against MariaDB 11 in a CI job with a `mariadb:11` service container, marked so that they are skipped when the service is absent (this project has no MySQL tests today; P1-S adds the job).

### 10.2 Unit and service tests per worker

- P1-I: every edge in 3.3 allowed and every other edge refused (table-driven over all 10 statuses squared); `recompute()` against the rule in 3.2; guard tests: each frozen column write raises in `enforce` and logs in `log`, line insert on an open invoice raises, the proforma allowance works and nothing else uses it; numbering: format tags, padding, yearly reset, gapless rollback (allocate, roll back, allocate again gives the same number), non-gapless rollback leaves a gap, a concurrent publish test on two sessions for gapless series (SQLite serialises; the MariaDB job proves the row lock); snapshots written at publish and the PDF reads only them; legacy invoices render unchanged (golden PDF text for one legacy reseller invoice and one legacy retail invoice).
- P1-N: credit note limits (4.4); debit note raises `worth`; overpayment flow; numbering per series; idempotency (same key twice returns one note; same key with a different amount raises).
- P1-C: the shadow and `v2` modes of 6.2; lock order (a test asserting the order of `with_for_update` calls through a recording session wrapper); every check of 6.4 detects an injected drift; the switch endpoint refuses each failing criterion and accepts when all hold; retail top-up end to end; apply and remove credit symmetrical.
- P1-R: each outcome of the gateway call (success, decline, timeout) with a fake gateway; reconciler claim race (two reconcilers, one wins); webhook completes a pending refund; dashboard refund creates a row; dispute policies; USDT gateway refund refused; legacy-mode full refund still makes the invoice `failed` and passes `tests/services/test_payment_refunds.py` unchanged.
- P1-D: the interval table; unknown label raises; triennial `advance_months` across leap years with anchor 29, 30 and 31; per-cycle setup fee used once; reseller services still monthly.

### 10.3 Property tests

With Hypothesis (P1-S adds `hypothesis` to the test requirements). The generators produce sequences of operations on one account and a few invoices: create and publish invoice (random lines including discounts, `prices_include_tax` either way), pay by card (succeed, fail, pend then succeed), apply credit, remove credit, top up, refund (partial, full, to credit, to gateway, timeout then reconcile), dispute, credit note, debit note, void. After every step, assert:

- **P1 line sums**: for v2 invoices, `amount_cents` equals the formula in 1.3.1 and is never negative.
- **P2 balance identity**: `due = total + debits - credits - (paid_gross - refunded)` and `worth >= 0`; `due >= 0` after every committed step (overpayment is always converted).
- **P3 status agreement**: the stored status equals the rule in 3.2 for the current balance.
- **P4 ledger chain**: per account, `balance_after(k) = balance_after(k-1) + amount(k)`; the authoritative cache equals the last `balance_after` and the sum of amounts; the mirror cache equals it too.
- **P5 refund bounds**: per payment `0 <= refunded_cents <= amount_cents` and `refunded_cents = Σ succeeded refunds`.
- **P6 money conservation**: Σ card and gateway money in, minus gateway refunds, equals Σ v2 invoice `paid_net` over gateway payments plus the change in account balances that came from top-ups and refunds to credit.
- **P7 numbering**: numbers in each series and period are unique and strictly increasing in allocation order; in gapless series they are also contiguous from 1.
- **P8 immutability**: no published invoice's frozen columns or lines changed between steps.
- **P9 idempotency**: replaying any step with the same idempotency key changes nothing.

The same generators run in legacy mode with the operations legacy supports and assert that the legacy decision and `balance()` agree (that is, the shadow comparison never fires on legacy-shaped data).

### 10.4 E2 integrity detectors

W0b writes the first detectors in `app/services/alerting/billing_integrity.py` now. Phase 1 adds to the same module (each detector read-only, each with a test that injects the bad state and sees one finding):

| Detector | Owner | Finds |
|---|---|---|
| `billing_shadow_mismatch` | P1-C | open rows in `billing_shadow_mismatches` (6.4) |
| `billing_shadow_scan_stale` | P1-C | an engine in `shadow` whose last scan is over 26 hours old |
| `billing_ledger_chain_broken` | P1-C | P4 failures (also recorded by the daily scan as `c2_*`) |
| `billing_negative_retail_balance` | P1-C | retail accounts below zero (6.7) |
| `billing_invoice_totals` | P1-I | v2 invoices failing P1 |
| `billing_invoice_status` | P1-I | v2 invoices failing P3 |
| `billing_numbering_gaps` | P1-I | gapless series with a gap or a duplicate (P7) |
| `billing_refund_stuck` | P1-R | 7.4 |
| `billing_refund_mismatch` | P1-R | P5 failures |
| `billing_adjustment_bounds` | P1-N | invoices with `worth < 0` or adjustments whose lines do not sum to their totals |

To avoid collisions in that one file, P1-S adds an empty section per worker (a function stub registered in the detector list that returns no findings) in wave 1; each worker fills only its own functions.

### 10.5 Reference behaviour

`tests/services/test_recurring_billing_service.py`, `tests/services/test_payment_refunds.py` and W0b's WHMCS contract tests must pass unchanged in every engine mode. A parametrised fixture runs these suites with the engines in `legacy`, `shadow` and `v2`.

## 11. Phase 1 worker split

### 11.1 Waves

Migrations must form one chain, and parallel workers branching from one base would each create a new head. So the schema lands first, in one worker, and the behaviour lands in parallel on top of it.

- **Wave 1: P1-S, foundations** (alone; about a day of work). Every model, every migration of 2.2 with its tests, every setting of appendix A in `settings_store.py`, every catalogue addition of 9.2, the generalised event registry, and the shared helpers with working minimal bodies, so that wave 2 workers only fill in behaviour.
- **Wave 2: P1-I, P1-N, P1-C, P1-R, P1-D in parallel**, each in an isolated branch from the integration branch after P1-S merged. None of them adds a migration. If one finds it needs a schema change, it asks the manager, who adds a follow-up revision to the chain after `billing_p1_ledger_owner_check` (the head), so the chain stays single.
- **Wave 3: P1-UI**, the admin and client screens, once the wave 2 APIs are merged.

### 11.2 File ownership

| Worker | Owns (creates or edits) | Model and effort |
|---|---|---|
| **P1-S** foundations | `app/models/billing_series.py`, `app/models/invoice_adjustment.py`, `app/models/refund.py`, `app/models/billing_shadow.py`, new columns and enum values in `app/models/reseller.py`, `app/models/commerce_invoice_ext.py`, `app/models/storefront.py`; `alembic/versions/billing_p1_*.py` (all ten) and their tests; `app/services/billing_backfill.py`; `app/services/billing_locks.py`; `app/services/billing_shadow.py` (record and store; scan stub); `app/services/settings_store.py` (phase 1 definitions only); `app/plugin_host/catalogue.py` and the matching rows of `docs/plugin-system.md`; `app/services/billing/settlement.py` (registry generalisation only, 9.1); `app/services/billing/settlement_subscribers.py` (the catalogue subscribers); `app/services/billing/cycles.py` (moved `resolve_cycle`, 8.3) and the one-line delegation in `checkout_service.py`; `app/services/billing/periodic.py` (periodic job list in the recurring worker, with stubs for the shadow scan and refund reconciler); stubs in `app/services/alerting/billing_integrity.py` (10.4); the public interfaces of 11.3 as stubs raising `NotImplementedError` (or working, where small: `balance()`, `InvoiceNumbering.allocate`); the MariaDB CI job; `hypothesis` in test requirements | Opus, high: the migrations are the riskiest part of the phase |
| **P1-I** invoice engine | `app/services/invoice_service.py` (`transition`, `recompute`, `publish`, `void`, `balance` hardening, serialisers), `app/services/invoice_guard.py`, `app/services/invoice_numbering.py`, `app/services/invoice_pdf_service.py`, `app/services/checkout_service.py` (v2 lines and tax split; not the cycle code), `app/services/commerce_lifecycle_service.py` (invoice creation only), `app/services/billing/settlement.py` (`settle_invoice` on `balance()`/`recompute()` and overpayment, after P1-S), `app/api/commerce_admin.py` (invoice endpoints: void, due date, mark paid default amount), new `app/api/billing_series_admin.py`, its section of `billing_integrity.py`, tests | Opus, high: state machine, guard and numbering are where the money rules live |
| **P1-N** credit and debit notes | new `app/services/invoice_adjustment_service.py`, new `app/services/adjustment_pdf_service.py`, new `app/api/billing_adjustments.py` (admin: issue credit note by amount or by lines, issue debit note; client and reseller: list and download their notes), its section of `billing_integrity.py`, tests | Sonnet, high |
| **P1-C** credit balance (A3) | `app/services/credit_ledger_service.py`, `app/services/payments/orchestrator.py`, `app/services/reseller_billing_service.py`, `app/services/recurring_billing_service.py` (only the cache reads of 6.6 and the `mark_paid_if_fully_allocated` call), `app/dao/commerce_dao.py`, `app/api/reseller.py`, `app/api/reseller_panel.py`, `app/api/reseller_admin.py` (balance reads, invoice serialiser at `:684-696`, unpaid filters), `app/api/commerce_client.py` (retail top-up, apply credit, unpaid filters), new `app/api/billing_engines_admin.py` (engines page API, switch endpoint, cache resync), `app/services/billing_shadow.py` (scan body, after P1-S), its section of `billing_integrity.py`, tests | Opus, high: live reseller balances |
| **P1-R** refunds (A4) | new `app/services/refund_service.py`, `app/services/payments/refunds.py` (wrapper), new `app/services/payments/refund_reconciler.py`, `app/services/payments/webhooks.py` (refund and dispute branches), `app/services/payments/base.py` (protocol: `idempotency_key`, `lookup_refund`), `stripe_gateway.py`, `paypal_gateway.py`, `usdt_crypto.py`, `app/services/billing/order_refunds.py`, new `app/api/billing_refunds_admin.py` (retail and reseller refunds, review resolution), its section of `billing_integrity.py`, tests | Opus, high: gateway money and races |
| **P1-D** cycles (D1) | `app/services/billing/intervals.py`, `app/services/billing/cycles.py` (body, after P1-S), `app/services/commerce_recurring_service.py`, `app/api/commerce_store_admin.py` (cycle fields), storefront and client cycle serialisers, tests | Sonnet, medium: small and well bounded |
| **P1-UI** screens (wave 3) | `frontend/` pages: admin invoice detail (balance, notes, refunds, transitions), series settings, engines and mismatches, refunds and review queue, retail credit (admin and client), credit and debit notes lists, cycle editor | Sonnet, medium |

Shared files where two workers meet, and how: `app/services/billing/settlement.py` (P1-S then P1-I, sequential by wave); `app/services/billing_shadow.py` (P1-S then P1-C); `app/services/billing/cycles.py` (P1-S then P1-D); `billing_integrity.py` (each worker edits only its own stub functions). `app/services/recurring_billing_service.py` is P1-C's only; the `transition()` change at its status writes (`:557-558`, `:883`) is done by P1-C, calling the interface P1-I implements.

### 11.3 Interfaces between workers (stubbed by P1-S)

```python
# P1-I
InvoiceService.balance(db, invoice) -> InvoiceBalance
InvoiceService.recompute(db, invoice, *, cause: str) -> Invoice
InvoiceService.transition(db, invoice, to: InvoiceStatus, *, cause: str, actor_user_id=None, reason=None) -> Invoice
InvoiceService.publish(db, invoice) -> Invoice
InvoiceService.void(db, invoice, *, reason: str, actor_user_id=None) -> Invoice
InvoiceNumbering.allocate(db, series: DocumentSeries, *, at: datetime) -> tuple[str, int, str]
InvoiceNumbering.default_series(db, document_type: DocumentType) -> DocumentSeries

# P1-N
InvoiceAdjustmentService.issue_credit_note(db, invoice, *, lines | amount_cents, reason, outcome, actor_user_id, idempotency_key, reason_text=None) -> InvoiceAdjustment
InvoiceAdjustmentService.issue_debit_note(db, invoice, *, lines, reason, actor_user_id, idempotency_key, reason_text=None) -> InvoiceAdjustment
InvoiceAdjustmentService.issue_for_refund(db, refund, *, lines=None) -> InvoiceAdjustment
InvoiceAdjustmentService.issue_overpayment(db, invoice, excess_cents: int, payment) -> InvoiceAdjustment

# P1-C
CreditLedgerService.credit_account / debit_account / adjust_account(db, billing_account_id, amount_cents, **same keywords as today)
CreditLedgerService.available_cents(db, account) -> int
CreditLedgerService.apply_to_invoice(db, invoice, *, amount_cents=None, actor_user_id=None, idempotency_key) -> Payment | None
CreditLedgerService.remove_from_invoice(db, payment, *, amount_cents, actor_user_id, idempotency_key) -> Refund
BillingShadowService.record(engine, check_key, subject_type, subject_id, legacy_value, v2_value, context=None) -> None
billing_engine_mode(db, engine: str) -> str   # "legacy" | "shadow" | "v2", honouring the switch record (6.5)

# P1-R
RefundService.request(db, *, payment_id, amount_cents, destination, reason, actor_user_id, idempotency_key, reason_text=None, issue_credit_note=None, credit_note_lines=None) -> Refund
RefundService.complete(db, refund_id, *, gateway_refund_ref=None) -> Refund

# P1-D
resolve_cycle(plan, cycle_interval=None, *, interval_unit=None, interval_count=None) -> ResolvedCycle
interval_months(label) -> int

# P1-S
BillingLocks.for_invoice(db, invoice_id) -> LockedInvoice   # reseller, account, invoice locked in order
BillingLocks.for_payment(db, payment_id) -> LockedPayment   # adds the payment
settlement.subscribe(event, name, handler, *, applies=..., isolated=True); settlement.fire(db, event, payload)
```

Wave 2 workers write their tests against fakes of the other workers' interfaces where the real body is not merged yet, and the manager runs the whole suite after each merge. Suggested merge order within wave 2: P1-D (independent), P1-I, P1-C, P1-N, P1-R.

### 11.4 Migration chain

```
<phase 0 head: plugin_system, then W0a's service_billings revision and any other phase 0 revisions>
  -> billing_p1_series_shadow
  -> billing_p1_invoice_columns
  -> billing_p1_adjustments
  -> billing_p1_refunds
  -> billing_p1_ledger_accounts
  -> billing_p1_cycles
  -> billing_p1_backfill_accounts
  -> billing_p1_backfill_payments
  -> billing_p1_invoice_checks        (locks invoices)
  -> billing_p1_ledger_owner_check    (locks credit_ledger_entries; head)
```

All ten are written by P1-S. Contract revisions (2.3) chain after them in a later phase.

### 11.5 Rollout after phase 1 merges

1. Deploy with both engines in `shadow` (the default), guard in `log`. Run `billing_backfill accounts` once the old processes are gone.
2. Watch `billing_shadow_mismatches` and the E2 alerts. Fix and redeploy until clean.
3. After the quiet period, switch the credit engine to `v2` (6.5). Retail credit features (top-up, apply, remove) are available from this point; before it they are refused for retail with a clear error.
4. After its own quiet period, switch the invoice engine to `v2`. Credit and debit notes, partial refunds, refunds to credit, overpayment to credit, `partially_paid`, `refunded` and `collections` become live, and the guard becomes `enforce`.
5. Later phase: contract steps of 2.3.

## 12. Open questions for the owner

Each has a recommended default that phase 1 implements unless the owner decides otherwise. **Decided (project chat, 2026-11-21):** build the recommended default for all fourteen, except question 4, where `refunded` maps to `paid` instead of `failed` (see below).

1. **Legal invoice number format.** Keep today's visible invoice number (`invoice_number`, for example 100123) as the legal number of the default invoice series, or start a new formatted series (for example `INV-2026-00001`) at the switch? *Recommended: keep today's numbers* (series `number_source = invoice_number`); an admin can switch the series to its own counter and format later without a migration.
2. **Gapless series.** The decided default is unique and ordered, with gapless per series as an option. Should the invoice and credit note series be seeded gapless for any installation? *Recommended: no, all seeded series non-gapless*; the admin settings page explains when to turn it on and that it serialises publishing.
3. **Retail negative balance after a top-up chargeback.** *Recommended: allow it* (debit the balance below zero and alert staff), rather than writing off the loss silently.
4. **New status values on frozen APIs.** `/api/client/commerce/*` and `/api/reseller/*` may only gain fields; a new value in the existing `status` field could break integrations that switch on it. **Decided (project chat, 2026-11-21): keep `status` within today's values on those APIs and add a new field `status_detail` with the real value**; the admin API and Rackflow's own frontend use `status_detail`. The mapping is `partially_paid` shown as `open`, `collections` as `overdue`, and `refunded` shown as **`paid`** with `status_detail = "refunded"`, not as `failed`.
   - *Why not `failed`:* an API client reads `failed` as "the payment never went through" and could retry the charge or chase the customer for money that was in fact paid and then returned.
   - *Why `paid` rather than `void`:* `paid` is true (the invoice was paid and nothing is owed), it never invites a retry or a chase, and the visible status does not move at all when a paid invoice is refunded, so consumer state machines that do not allow `paid` to `void` are not broken. It also matches partial refunds, which leave a v2 invoice `paid`. Consumers that care about refunds read `status_detail` or the `invoice.refunded` event.
   - *Legacy and shadow modes:* today's full-refund paths write `failed` to the invoice (`app/services/payments/refunds.py:196`, `app/services/payments/webhooks.py:301`), and 3.4 keeps that in `legacy` and `shadow` because the reseller path is the reference behaviour. So the frozen APIs show `failed` for refunded invoices until the invoice engine switches to `v2`. **Known limitation, decided (project chat, 2026-11-21): leave this as it is.** The legacy write is not changed outside the engine switch, because the reseller path is the reference behaviour and the WHMCS reseller module may read that status; the switch to `v2` fixes it.
   - *Implementation and test:* P1-S adds a pure function `public_invoice_status(status) -> str` in `app/services/invoice_service.py`'s serialiser area (owned by P1-I after wave 1) and uses it for `status` on every `/api/client/commerce/*` and `/api/reseller/*` invoice serialiser, with `status_detail` carrying the real value. P1-S adds a contract test in `tests/contract/test_public_invoice_status.py` that pins the whole mapping (each of the ten statuses), asserts that no internal status maps to a value outside today's seven, and asserts that `refunded` maps to `paid` and never to `failed`.
5. **Overpayment document.** The spec says a debit note (4.3). The alternative is no document, moving the excess to credit as a refund to credit. *Recommended: the spec's debit note*, as WHMCS-style accounting expects a document for every change of what an invoice is worth.
6. **Switch order.** *Recommended: credit engine first, then invoice engine*, because v2 overpayment and refund-to-credit write to account balances.
7. **Shadow quiet period.** *Recommended: 14 days* (`billing_shadow_min_days`), long enough to cover two weekly invoice sweeps and the monthly reseller billing peak on the 1st if the period includes it.
8. **Outbound commerce webhooks for the new events.** *Recommended: not in phase 1*; plugins receive them, `COMMERCE_WEBHOOK_EVENTS` stays unchanged, and adding names later is a one-line change.
9. **Dispute won.** *Recommended: staff record it as a manual payment in phase 1*; automatic handling of the gateway's "dispute closed, won" event comes with the gateway plugin kind work (plugin doc section 17.1).
10. **Dispute policy defaults.** *Recommended: resellers `debit_balance` (today's behaviour), retail `reopen_invoice` into `collections`* (7.6).
11. **Checkout tax before the tax engine.** v2 checkout invoices spread the flat checkout tax over lines instead of a separate "Tax" line (1.3.1). *Recommended: yes*, so invoices have their final shape before phase 2; PDFs show the tax total as today.
12. **Proforma mode.** *Recommended: off by default* (`billing_proforma_enabled = false`); operators who need it (some EU jurisdictions) turn it on.
13. **Retail credit requires a fulfilled order.** Spec default on. *Recommended: on*, as specified.
14. **Guard mode before the switch.** *Recommended: `log` in production until the invoice engine is `v2`, `enforce` in tests always* (3.5).

## Appendix A. Phase 1 settings

All declared by P1-S as `SettingDef` entries in `app/services/settings_store.py` (W0c's store, stored under `setting:<key>` in `system_settings`), with a `fallback` because none has a field in `app/core/config.py`. New group `billing_engines` for the first five; group `retail_billing` and `reseller_payments` as appropriate for the rest.

| Key | Type | Choices or range | Default |
|---|---|---|---|
| `billing_invoice_engine` | str | `legacy`, `shadow`, `v2` | `shadow` (only reaches `v2` through the switch endpoint, 6.5) |
| `billing_credit_engine` | str | `legacy`, `shadow`, `v2` | `shadow` (same) |
| `billing_invoice_guard_mode` | str | `log`, `enforce` | `log` (ignored, always `enforce`, once the invoice engine is `v2`) |
| `billing_shadow_min_days` | int | 1 to 90 | 14 |
| `billing_shadow_scan_hour_utc` | int | 0 to 23 | 3 |
| `billing_proforma_enabled` | bool | | false |
| `billing_credit_enabled_retail` | bool | | true (effective only once the credit engine is `v2`) |
| `billing_credit_topup_min_cents` | int | 1 to 1,000,000,000 | 500 |
| `billing_credit_topup_max_cents` | int | 1 to 1,000,000,000 | 100,000 |
| `billing_credit_max_balance_cents` | int | 0 to 10,000,000,000 (0 means no limit) | 0 |
| `billing_credit_require_approved_order` | bool | | true |
| `billing_credit_auto_apply_retail` | bool | | false |
| `billing_dispute_policy_reseller` | str | `debit_balance`, `reopen_invoice` | `debit_balance` |
| `billing_dispute_policy_retail` | str | `reopen_invoice`, `debit_balance` | `reopen_invoice` |
| `billing_refund_reconcile_minutes` | int | 1 to 60 | 5 |
| `billing_long_cycles_enabled` | bool | | false |

Consistency rule (W0c's `ConsistencyRule`): `billing_credit_topup_min_cents <= billing_credit_topup_max_cents`. Series configuration is data in `document_series`, not settings.

## Appendix B. Named rounding rules

In `app/services/billing/rounding.py` (P1-S), each a pure function on integers, with property tests.

| Rule | Used for | Definition |
|---|---|---|
| `tax_split_largest_remainder(total_tax, weights)` | spreading the checkout flat tax over lines (1.3.1) | each share `floor(total_tax * w / Σw)`; the remaining units go one each to the lines with the largest fractional remainders, ties to the earlier line; shares sum exactly to `total_tax` |
| `refund_tax_proportional(amount, invoice_tax, invoice_total)` | tax part of a default refund credit note (7.2) | `round_half_even(amount * invoice_tax / invoice_total)` computed with integers (`divmod`), 0 when `invoice_total` is 0; the net part is `amount - tax` |
| `round_half_even(numerator, denominator)` | the primitive behind the rule above | integer division with ties to the even quotient |
