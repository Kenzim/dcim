# Rackflow billing: target specification

Status: proposal, written 2026-11-21 on branch `task/billing-feature-comparison-wit-cedb9f` (base 5e60719). Documentation only; nothing here is implemented, and no code was changed or run to write it.

Companion document: `docs/billing-whmcs-comparison.md` (the feature-by-feature comparison and evidence, with `path:line` references). Finding numbers such as "finding 3" refer to section 2 of that document.

## 0. Target and principles

**Target.** An operator can run a hosting business on Rackflow's billing alone: sell products with cycles, options and add-ons; invoice in advance; take payment by card, wallet or crypto; chase late payers and suspend or terminate; refund and credit correctly; charge the right tax in any region; report and export to accounting; and move over from WHMCS with their history.

**Owner decisions this spec follows** (Kenzi, 2026-11-21):

- Kenzi keeps WHMCS for his own business; Rackflow is open source and must be able to replace WHMCS for others. A WHMCS import is in scope at lower priority.
- Tax must work in any region through a general engine. Region specifics (EU VIES and similar) are plugins and rule packs, never special cases in core.
- Where a WHMCS feature is better as a plugin (gateways, fraud checks, accounting exports, tax-ID validators, registrars, notification channels), the spec says so.

**Principles that apply to every workstream.**

1. **Close the money path before adding features.** The retail track has defects that make it unsafe to build on (comparison document, findings 1 to 4, 6, 7). Workstream A0 comes first.
2. **Integer minor units, explicit rounding.** All amounts are integers in the currency's minor unit; rates are decimals; every division names its rounding rule; the minor-unit count comes from a currency table, not an assumed 100.
3. **Published documents are immutable.** After an invoice leaves draft, only status, payment allocation and linked adjustments change. Corrections are credit or debit notes.
4. **One settlement point.** Every payment, whatever the gateway, ends in one function that records the allocation, marks the invoice paid, and fires the internal "invoice paid" event that fulfils orders, completes cycles, unsuspends, queues emails and enqueues outbound webhooks.
5. **Idempotency everywhere money moves.** Idempotency keys, row locks and unique constraints, in the style already used by `CreditLedgerEntry` (`app/models/reseller.py:706-780`) and `BillingCycle` (`:880-943`).
6. **Snapshots, not live joins.** Invoices keep their own copy of seller details, buyer details, tax rule, rate and legal wording. Editing a client or a tax rate never alters an issued invoice.
7. **Additive migrations on live data.** Production has real money in it. Expand, backfill in batches, switch behind a setting, contract later. The test database is SQLite and production is MySQL (`app/services/invoice_service.py:43-80` has a branch for each), so every migration and query must work on both.
8. **Stable external contracts.** `/api/billing/*` and the WHMCS module (`whmcs/modules/servers/rackflow/`) do not change. Existing `/api/client/commerce/*` and `/api/reseller/*` response shapes only gain fields.
9. **Shadow before switch.** New calculation engines (tax, proration, invoice totals) run beside the old path and log differences before they become authoritative.
10. **Settings in the database with environment defaults**, so an operator can change behaviour without a deploy (today recurring billing is environment-only, `app/core/config.py:112-117`).

## 1. Plugin seams

Aligned on 2026-11-21 with `docs/plugin-system.md` (plugin API 1.0, core in `app/plugin_host/`, merged into main at ce345cc). Its vocabulary is used here:

- A **plugin** is an admin-installed package with a `plugin.json` manifest declaring `permissions`, `settings`, `events`, `hooks`, `schedules`, `actions`, `ui.panels` and `mcp_tools` (plugin doc section 3.2).
- **Events** are delivered to plugin **after handlers** through the plugin outbox (at least once, idempotent by event id); **before hooks** may veto (plugin doc section 6). Events are published with `app.plugin_host.events.emit(db, name, payload, service_id=None)`, which also enqueues the outbound commerce webhook for the same event (plugin doc section 6.5), so billing never calls `CommerceWebhookService.enqueue` directly for catalogue events. Billing receives no second event mechanism: billing's single settlement function fires its internal subscriber list (section 14 of this spec), and one subscriber of that list is the only caller of `emit("invoice.paid", ...)`. The event catalogue (`app/plugin_host/catalogue.py`, plugin doc sections 6.2.1 and 17.1) is the one list of event names: new billing events such as `invoice.created` or `invoice.refunded` are added there when they gain an emitter, and section 14 of this spec refers to it rather than keeping a parallel list. Within plugin API 1.x payload fields are only ever added.
- Receiving billing events needs the `billing.read` permission (plugin doc section 7).
- Each seam below becomes a **plugin kind**: a kind-specific registrar on top of the plugin core, as plugin doc section 17 plans ("Plugin kinds for payment gateways, tax providers, accounting export, fraud checks"). Kinds are not in plugin API 1.0; until a kind exists, its seam is an in-core registry with the interface named here, so the later plugin registrar only wraps it.
- Plugin settings (secrets sealed) and schedules (leased rows in `plugin_schedules`) come from the plugin core; billing kinds use them rather than their own.
- The hardware adapters in `app/plugins/` are a separate, older mechanism for server management only and are not billing seams.

Already present: `PaymentGatewayRegistry` with a `PaymentGateway` protocol (`app/services/payments/registry.py:14`, `base.py:97`), which is the contract the payment gateway kind will keep.

| Seam (plugin kind) | Core provides | Plugin provides | Built-in or first plugins |
|---|---|---|---|
| Payment gateway | Registry, settlement point, refund and webhook plumbing, `Payment` and `Refund` records | Charge, refund, vault, webhook normalisation, hosted-page flows | Stripe, PayPal, USDT as plugins (they already fit the protocol); manual and credit stay in core |
| Fraud check | Hook after order placement, `fraud_score` column (`app/models/commerce_order.py:115`), hold-for-review order state | Scoring against FraudLabs Pro, MaxMind, others | None in core |
| Tax-ID validator | Tax-ID fields and status, caching, fail-open or fail-closed setting | `supports(country, id_type)`, format check, online check | Generic regex check in core; EU VIES, UK HMRC, India GSTIN, Australian ABN as plugins |
| Tax provider | Built-in rule engine as default provider; snapshot of any provider's answer | External calculation (Stripe Tax, TaxJar, Avalara) | Needed for US sales tax; none in core |
| Tax rule pack | Rule import and update mechanism | Data sets of rules per region | EU VAT, UK VAT, India GST, Australia GST, Canada GST and PST as data packs |
| Accounting export | Journal-entry generator, payment fee and payout fields | Format and delivery (CSV, Xero, QuickBooks, SFTP) | Generic CSV in core; others as plugins |
| Currency rates | Currency table, conversion helper, manual rates | Scheduled rate fetch | ECB or Open Exchange Rates plugin |
| Notification channel | Email queue, event catalogue, templates | Delivery to SMS, Slack, Discord, webhooks | Email in core |
| Usage metrics | Usage-to-invoice-line mechanism | Metric collection per service type | None initially |
| Import source | Idempotent importer framework, external-id mapping | Source-specific reader | WHMCS first |
| Domain registrar | None | Out of scope (see section 9) | None |

What stays in core regardless: the ledger, invoice and adjustment model and numbering, the tax engine and its data model, the automation scheduler, the settings store, the email queue, and the settlement point.

## 2. Workstream A: ledger correctness

**Purpose.** Make every amount, state change and correction on both tracks correct and auditable. This is the foundation; later workstreams assume it.

### A0. Close the retail money path (prerequisite, do first)

Scope, each with a regression test:

1. **Card payment settles.** `POST /api/client/commerce/invoices/{id}/pay` (`app/api/commerce_client.py:444-493`) must create a pending `Payment` with `external_ref` set to the PaymentIntent id before returning the client secret, so the existing webhook handler (`app/services/payments/webhooks.py:145-193`) can find it. Route the call through the gateway registry instead of importing Stripe inside the handler.
2. **Settlement fires fulfilment.** Make `PaymentOrchestrator.finalize_success` (`app/services/payments/orchestrator.py:627`) the one settlement function and add an internal event subscriber list. Subscribers: fulfil an order if `invoice.order_id` is set (`CheckoutService.mark_paid_and_fulfill`, `checkout_service.py:668`), complete a billing cycle, enqueue `invoice.paid` webhook and the `invoice_paid` email. Remove the duplicate enqueue code at `webhooks.py:173-193` and `commerce_admin.py:449-463`.
3. **Admin mark-paid records a payment.** `commerce_admin.py:427-465` creates a `Payment(gateway="manual")` for the outstanding amount with reason and actor, then goes through the settlement function.
4. **Invoice lines sum to the total.** In `CheckoutService.quote` and `_create_commerce_invoice` (`checkout_service.py:229-362`), emit real lines for the setup fee and the first period, and stop adding the option delta to both setup and recurring (`:249-250`). Add an invariant test: sum of lines equals `amount_cents` for every purpose.
5. **Renewals respect the cycle.** Add `cycle_interval` and `interval_months` to `service_billings`; fill them in `CommerceRecurringService.ensure_for_fulfilled_item` (`commerce_recurring_service.py:47-`); replace `next_calendar_month` use at `recurring_billing_service.py:793` with an interval-aware advance. Backfill existing rows as monthly (the only behaviour they have today).
6. **Retail rows are processed by the worker.** Introduce an `AccountFunder` interface (reseller implementation wraps the current orchestrator path; retail implementation uses saved methods, credit, or leaves the invoice open). Create cycle invoices with `billing_account_id`. Until saved retail methods exist (B6), retail renewal invoices stay open and are paid through the client area.
7. **End-of-period cancellation terminates.** Replace the immediate `CANCELLED` status at `commerce_lifecycle_service.py:72-75` with a `cancel_at` date on `service_billings`; the worker terminates the service at that date and stops invoicing.
8. **Order cancel and provisioning failure clean up.** Cancelling an order voids its unpaid invoice, or, if paid, issues a credit note and refund (A2 and A4). Replace the "refund path stub" at `checkout_service.py:702` with a policy setting: refund to original method, credit to balance, or hold for manual review (default).
9. **Enforce stored coupon rules.** Apply `max_uses_per_account`, `duration` and `duration_months` (`app/models/commerce_coupon_tax.py:84-93`); count usage at payment, not at order placement (`checkout_service.py:365-385`). Recurring discounts depend on D6 and may be deferred.

Data model: `service_billings` gains `cycle_interval`, `interval_months`, `cancel_at`; nothing else new.
Size: **M**.
Risks: item 6 touches the worker that production resellers use. Gate retail processing behind a setting and leave the reseller code path byte-for-byte equivalent in behaviour, proven by the existing `tests/services/test_recurring_billing_service.py`.

### A1. Invoice model v2

Scope: line-based totals with tax columns, snapshots, states, numbering.

Data model changes (all additive):

- `invoices`: `subtotal_cents`, `discount_cents`, `tax_cents` (nullable, backfilled only for new invoices; `amount_cents` stays the total); `issued_at`; `prices_include_tax` (bool); `seller_snapshot` and `buyer_snapshot` (JSON); `customer_notes`, `admin_notes`; `locale`; `document_type` (`invoice`, `proforma`); `number_display` (string, unique per series) and `series`. Replace the `amount_cents > 0` check (`reseller.py:497`) with `>= 0` so fully discounted and zero invoices can exist.
- `invoice_lines` (`app/models/commerce_invoice_ext.py:21-45`): `kind` (product, setup, option, addon, discount, fee, proration, adjustment, other), `service_id`, `service_billing_id`, `period_start`, `period_end`, `tax_class_id`, `taxable`.
- Reseller invoices gain lines too, so both tracks share one shape.

States: keep every current value (`draft`, `open`, `paid`, `void`, `failed`, `pending_action`, `overdue`; `reseller.py:90-97`) and add `partially_paid`, `refunded`, `collections`. Put the allowed transitions in one `InvoiceService.transition()` table. Start using `draft`: `create()` currently writes `open` directly (`invoice_service.py:109`).

Immutability: once status leaves `draft`, lines and totals are frozen. Enforce in `InvoiceService` (the only writer) and with an ORM `before_update` guard that raises on frozen columns, so a stray direct write fails in tests. Void remains available for unpaid, unadjusted invoices; anything else needs a credit note.

Numbering: `invoice_sequences` is already a named counter table (`reseller.py:554-563`). Add sequences per series for invoice, proforma, credit note and debit note. Format string with tags `{NUMBER}`, `{YEAR}`, `{MONTH}`, `{SERIES}`, using the unused `commerce_invoice_prefix` (`config.py:148`) as the default. Proforma mode: unpaid documents carry a proforma number; the final invoice number is allocated on payment (as WHMCS does). Allocate inside the publish transaction and never reuse a number, so numbering is gapless where law requires it.

Snapshots: copy seller details (settings today: `invoice_pdf_service.py:73-80`) and buyer profile into the invoice at publish; the PDF reads only the snapshot (finding 9).

Plugins: none.
Size: **L**.
Risks: the largest schema change on the busiest table; existing reseller invoices have no lines, so rendering must handle both shapes. Never backfill old invoices with computed values; leave the new columns null and render from `amount_cents`.

### A2. Credit and debit notes

Scope: all corrections to published invoices.

Data model: one table `invoice_adjustments` with `kind` (`credit` or `debit`), number, `invoice_id` (RESTRICT), `billing_account_id`, `reseller_id`, currency, subtotal, tax, total (positive amounts; the kind carries the sign), `reason_code` (cancel, refund, correction, goodwill, late_fee, overpayment, merge), `status` (issued, closed), `outcome` (reduced_balance, refunded, credited_to_balance), actor, `idempotency_key`. Child `invoice_adjustment_lines` mirrors invoice lines with tax snapshot. Creating one is the only way to change what a published invoice is worth. The unused `InvoicePurpose.CREDIT_NOTE` (`reseller.py:86`) is left alone for compatibility and not used.

Balance rule, implemented once as `InvoiceService.balance()` and used everywhere instead of the current `allocated_cents` comparison (`invoice_service.py:192-218`): amount due equals invoice total, plus debit notes, minus credit notes, minus succeeded payments net of refunds. Overpayment no longer raises (`:209-210`); the excess becomes account credit (A3) with a debit note, as WHMCS does.

Triggers: cancel invoice, cancel line item, refund (A4), mark paid with remaining balance, apply and remove credit, merge, order cancellation, automatic cancellation of old unpaid invoices.
Plugins: none.
Depends on: A1.
Size: **M**.

### A3. Credit balance for every account

Scope: give retail accounts the credit facility resellers already have, with one implementation.

Data model: add nullable `billing_account_id` to `credit_ledger_entries` and make `reseller_id` nullable with a check that at least one is set; backfill from `billing_accounts.reseller_id` in batches; `CreditLedgerService` locks the billing-account row and keeps `balance_after_cents`. `billing_accounts.credit_balance_cents` (`commerce_account.py:89`) becomes the authoritative cache for retail; `resellers.cached_balance_cents` stays in step until a later contract step. Add a nightly job that checks each cache against the last `balance_after_cents` and raises an alert on drift.

Behaviour (settings, defaults in brackets): enable credit [on], minimum and maximum deposit, maximum balance, require an approved order before adding funds [on, as WHMCS recommends], apply credit automatically when an invoice is generated [off for retail, current preference for resellers], credit on downgrade, apply or remove credit on a specific invoice by admin or client, refund to credit, overpayment to credit. Retail "add funds" creates a credit top-up invoice exactly as `POST /top-up-invoices` does for resellers (`reseller_panel.py:875`).

Plugins: none.
Depends on: A1, A2 (for apply and remove credit documents).
Size: **M**.
Risks: live reseller balances. The reseller path must not change behaviour; run old and new balance calculations side by side for a release and alert on any mismatch before switching reads.

### A4. Refunds

Scope: full and partial, to gateway or to credit, on both tracks.

Data model: `refunds` (payment, invoice, adjustment, amount, currency, `destination` of gateway or credit, `status` of pending, succeeded or failed, gateway reference, reason, actor, `idempotency_key`). `payments` gains `refunded_cents` and `fee_cents` and `net_cents`; `PaymentStatus` gains `partially_refunded`. The gateway protocol already takes an amount (`base.py:127`); `PaymentRefundService` always passes the full amount (`refunds.py:105`), which is the only restriction.

Flow: create a pending refund row, call the gateway with its idempotency key, then in one transaction mark the refund succeeded, update the payment, issue the credit note, reverse ledger entries for credit-paid amounts, and enqueue emails and webhooks. A gateway timeout leaves a pending row that a reconciler resolves, never a silent mismatch. Chargebacks (`webhooks.py:195-209`) use the same path with reason `dispute`.

Surface: retail admin endpoint and UI (the only refund UI today is for reseller payments in `AdminBilling.svelte`), partial amount field, refund-to-credit option, optional "refund unused time" action using A6.
Plugins: gateway-specific refund behaviour lives in the gateway plugin; USDT stays "credit only" (`refunds.py:61-62`).
Depends on: A2, A3.
Size: **M**.

### A5. Manual invoices and billable items

Scope: admin creates a draft invoice with free-form lines, edits it, publishes (with or without email), duplicates it, and reissues a published invoice (void with credit note, new draft copy with the same lines). Billable items are one-off or recurring charges (description, amount, cycle, "invoice on next run") that the automation adds to the next invoice for that account.
Data model: `billable_items` (account, description, amount, tax class, `recurrence`, `next_invoice_date`, `service_id`, `invoiced_invoice_id`). Admin endpoints and UI for invoice CRUD (none exist today).
Plugins: none.
Depends on: A1, A2.
Size: **M**.

### A6. Proration and prorata billing

Scope: mid-cycle upgrade and downgrade, mid-cycle add-on, optional refund of unused time, and prorata alignment of due dates to a day of the month.

Design: a pure, heavily tested module `app/services/billing/proration.py` taking old price, new price, period start and end, change time, day-count rule (UTC calendar days) and rounding (half-up per line), returning invoice lines of kind `proration` (charge for the remaining period at the new price, credit for the unused part of the old price). Settings: policy per direction (`none`, `daily`), downgrade credit goes to balance or is dropped, refund-on-cancel off by default. Prorata date: a per-product day of month with a "charge next month if after day N" threshold, as in WHMCS prorata billing; the first invoice covers the stub period and later cycles align to the anchor (the existing `billing_anchor_day`, `reseller.py:849`, becomes meaningful).
Replace the setup-fee-difference upgrade at `commerce_lifecycle_service.py:133-205` with the proration module and apply the change automatically after payment, with admin approval kept as an optional setting.
Plugins: none.
Depends on: A1, A2, D1 (cycles stored on the service).
Size: **M**. Property tests: the slices of a period sum to the full price within one minor unit.

## 3. Workstream B: automation

**Purpose.** Turn the failure-handling loop in `recurring_billing_service.py` into a schedule that matches how operators expect to run billing, on both tracks.

### B1. Settings store
A typed settings schema stored through the existing `system_settings` table (`commerce_account.py:141`, endpoints `commerce_admin.py:651-666`), with environment variables as defaults (`config.py:112-117`). Per-cycle overrides now; per-client-group overrides after D5. An "Automation settings" admin page. Size **S**.

### B2. Scheduler and dry run
Split the worker (`app/workers/recurring_billing.py`) into named jobs that are each idempotent and each claim work using the existing lease and claim-token pattern (`reseller.py:880-956`): generate invoices, send reminders, capture payments, retry, add late fees, suspend, terminate, cancel at period end, auto-cancel stale invoices. Add time-of-day for the daily jobs. Add a dry-run mode that reports what each job would do without writing; it is required for safe cutover and import. Size **M**.

### B3. Generate invoices ahead of the due date
Add `next_invoice_at` and `next_due_at` semantics: invoices are generated N days before due (global default, per-cycle override, as in WHMCS Automation Settings). Today the invoice is created when the cycle is already due (`recurring_billing_service.py:159-166`, `:379`). Existing rows behave as N equals zero, so nothing changes until an operator sets it. `invoices.issued_at` and `due_at` become distinct. Size **M**. Depends on A1, A0.5.

### B4. Reminders
Unpaid reminder N days before due; up to three overdue notices at configurable days after due; idempotent per invoice and kind (`invoice:{id}:reminder:{kind}`); sent through the email queue for clients (F4 templates) and `NotificationOutbox` for resellers. Wire the six email events that have no sender today (comparison section 13). Size **S** after F4, **M** with it.

### B5. Late fees
Fixed or percentage, with a minimum, added after N days overdue, once per invoice, as a debit note (A2) so the original invoice is untouched; group exemption after D5. Size **S**. Depends on A2.

### B6. Unified auto-charge and saved methods
Generalise saved payment methods from `reseller_payment_methods` (`reseller.py:604`) to a table keyed by `billing_account_id`, reusing the Stripe SetupIntent and PayPal vault flows (`orchestrator.py:59-330`) for retail, and replace the stub at `commerce_client.py:499-507`. Add "process N days before due" capture. The `AccountFunder` from A0 uses saved methods then credit (order governed by a per-account preference like `ResellerChargePreference`, `reseller.py:56-58`). Gateway code moves behind the plugin seam. Size **L**. Risks: card data stays at the gateway, we hold tokens only; migrating reseller methods must keep fallback ordering intact.

### B7. Suspension and termination policy
Days after due to suspend and to terminate, as policy objects per account type (the reseller default reproduces today's retry days, grace and `block_new` or `suspend_all`, `reseller.py:67-70`), per product override, per client-group exemption. Termination for non-payment calls the existing lifecycle (`ServiceLifecycle`) and is new: nothing terminates for non-payment today. Final invoice and notices before termination. Unsuspend on payment already exists (`recurring_billing_service.py:796-799`, `:840-855`). Size **M**.

### B8. Cancellation requests
End of period (uses `cancel_at` from A0) or immediate; unpaid invoices for the cancelled service are cancelled with a credit note automatically (WHMCS "cancellation request handling"); optional pro-rata refund via A6 and A4. Size **S** after A.

### B9. Optional: continuous generation and invoice grouping
Continuous generation (invoice each cycle even if the last is unpaid) and grouping several services due on the same day into one invoice require relaxing the one-invoice-per-cycle rule (`billing_cycles.invoice_id` is unique, `reseller.py:909-915`) and a line-to-cycle link. Build last; many operators do not need it. Size **L**.

Plugins for B: notification channels. Everything else is core.
Risks for B: automation sends real emails and suspends real services. Ship each job disabled by default with dry-run first. Never apply new schedules retroactively to existing cycles.
Workstream size: **L**.

## 4. Workstream C: tax engine

**Purpose.** A general engine that can express VAT, GST, sales tax and exemptions for any country, with region specifics supplied as data and plugins.

### Data model (all new tables; existing `tax_rates` stays as a read-through during migration)

- `tax_classes`: standard, reduced, zero, exempt, plus operator-defined (for example `digital_service`). Products, add-ons and fee lines carry a class (default standard).
- `tax_rules`: name, country (null for any), region, optional postcode pattern, `tax_class_id`, `customer_type` (any, business, consumer), rate as a decimal percentage (not basis points; 14.975 per cent exists), `priority`, `compound` flag and order, `inclusive` override, `reverse_charge_zone` and `reverse_charge` flags, `legal_note_key`, validity dates, enabled.
- `seller_tax_registrations`: country or region, tax number, effective dates, default invoice series. The engine charges tax only where the seller is registered, and picks the series and legal wording from the registration.
- `tax_exemptions`: scope (account, client group), reason, certificate reference, valid from and to, and the rules or classes exempted.
- `invoice_line_taxes`: line, rule snapshot (name, rate, base, amount, compound position). The invoice stores a grouped summary derived from these.
- `billing_profiles` additions (`commerce_account.py:108-138`): `tax_id_type`, `tax_id_status` (unchecked, valid, invalid, unavailable), `tax_id_checked_at`, `tax_id_check_ref`, `is_business`.
- `billing_accounts` additions: `tax_exempt_reason`; the existing `tax_exempt` flag (`:92`) is kept and finally given an admin write path.
- A per-seller `invoice_profile`: document names (Invoice, Tax Invoice, Proforma), required fields checklist, numbering series, footer text, language. This carries local invoice requirements as configuration.

### Calculation
`TaxEngine.calculate(context) -> TaxResult`. The context holds the seller registration candidates, buyer (country, region, postcode, business flag, tax-ID status), currency, `prices_include_tax`, and lines with class and amount. Steps: choose registration; apply exemptions; match rules by specificity then priority; apply reverse charge only when the matching rule allows it and the buyer's tax ID status is `valid` and the buyer is in a different jurisdiction from the registration; compute per line in compound order; apply the configured rounding point (per line or per invoice, because jurisdictions differ); return tax lines, invoice-level legal notes and the evidence used. Inclusive pricing derives tax from the gross amount; exclusive adds it.

The same call serves the quote, the order, the invoice, proration lines and adjustments, so a quote can never disagree with its invoice (today `quote` and `_create_commerce_invoice` share `_tax_cents`, `checkout_service.py:203-226`; that single entry point is kept). Reseller invoices pass through the same engine via their billing account.

### Plugins and packs
- Tax-ID validators (VIES and others) implement `supports`, `check_format` and `check_online`; results are cached and a setting chooses fail-open (charge tax, retry later) or fail-closed (block checkout) when the authority is down. Core ships only a configurable regex format check.
- Tax providers (Stripe Tax, TaxJar, Avalara) return a `TaxResult`; core stores the snapshot, so invoices never depend on the provider afterwards. This is the intended route for US sales tax, which needs nexus and jurisdiction data that a rules table cannot honestly cover.
- Rule packs (EU, UK, India, Australia, Canada) are data shipped as plugins, with optional automatic updates as WHMCS does for EU VAT.

### Legal correctness
Place-of-supply evidence (billing country, IP country, payment country) is stored on the order for regimes that require it. The engine produces documents; compliance advice is the operator's. Ship with tax disabled and no rules by default, and document that rule packs are starting points.

### Migration
`tax_rates` rows (`commerce_coupon_tax.py:145-156`) become `tax_rules` one-to-one (standard class, any customer, basis points to percentage). The `/tax-rates` admin routes (`commerce_store_admin.py:703-739`) stay as a compatibility façade for one release. No existing invoice is touched; their single "Tax" line remains valid. Add the admin UI that does not exist today.

Depends on: A1 (line taxes, snapshots, series). B and D use the engine but do not block it.
Risks: legal exposure, rounding differences between line and invoice mode, retroactive rate edits (rules are versioned by validity dates, never edited in place once used), outage of external validators.
Size: **L**.

## 5. Workstream D: catalogue

**Purpose.** Pricing expressive enough for real hosting products.

- **D1 Cycles.** Replace the four-value interval enum (`app/models/storefront.py:57-61`) with `interval_unit` and `interval_count`, keeping the four existing values and adding biennially and triennially. Per-cycle setup fee (`price_plan_cycles.setup_cents`; today one `price_plans.setup_cents`, `:178`). Store interval on the service (A0). Size **M**.
  - *Built in phase 1 (P1-D).* `app/services/billing/intervals.py` knows all six labels in every process, whatever the settings say, and an unknown non-empty label is an error, never monthly (`None` or empty still means monthly). Cycles longer than 12 months are sold and created only while `billing_long_cycles_enabled` is on; with it off, admins get 422 and checkout "Invalid billing cycle", as before. `resolve_cycle` (`app/services/billing/cycles.py`) selects by label, by unit and count, or both, and cycle rows are read in id order, so "first enabled cycle" is the lowest id. A duplicate cycle length returns 409. Known gaps: checkout requests cannot yet pass unit and count (the label path covers every cycle); a one-time plan with no enabled cycle is still priced at 0 plus its setup fee, unchanged, because a setup-fee-only one-time plan is a valid product.
- **D2 Configurable options.** Add types radio, yes/no and quantity (min, max, step, optional tiers) beside select and text (`storefront.py:64-66`). Move pricing to `product_option_value_prices` by cycle and currency, with recurring and setup parts, replacing the single flat `price_delta_cents` (`:256`). Emit one invoice line per option. Store the priced components of each service (`service_billing_items`) so renewals rebuild from a snapshot: catalogue price changes do not silently reprice existing services, and a separate bulk "update prices" tool does it deliberately. Size **L**.
- **D3 Add-ons.** Wire the unused `ProductAddon` model (`storefront.py:262`) into checkout and fulfilment, with its own cycle optionally aligned to the parent service's due date (prorata, A6). Size **M**.
- **D4 Currencies.** A `currencies` table: code, symbol, prefix and suffix, minor-unit count, format, rate to base, rate timestamp, enabled, base flag. Per-currency price rows. An account's currency is fixed after its first invoice. Store the exchange rate on each invoice at issue for reporting. Replace every "divide by 100" with minor-unit-aware helpers. Rate fetch is a plugin. Size **M**.
- **D5 Client groups.** Group with colour, invoice-stage percentage discount, exemption from suspension, separate-invoices flag, tax-exempt flag, group prices (`price_plan_group_prices`, modelled on `ResellerGroupPrice`, `reseller.py:307`). `billing_accounts.client_group_id`. Size **M**.
- **D6 Promotions v2.** Types: percentage, fixed, price override, free setup; recur for N invoices tracked per service (`service_billings.promo_id`, `promo_cycles_used`); applies-to lists across products, plans, cycles and add-ons; requires lists; new-clients-only; per-account limit; admin-created orders with a custom code; usage report. Fixes the discounts that vanish on renewal (finding 5). Size **M**.
- **D7 Trials.** Use `price_plans.trial_days` (`storefront.py:179`): no charge until the trial ends, then the first invoice. Size **S**.
- **D8 Usage billing (optional).** Metric providers feed usage lines at renewal; Rackflow already collects bandwidth data. Plugin-driven. Size **L**, last.
- **Not planned:** bundles and cross-sell recommendations.

Plugins: currency rates (D4), usage metrics (D8).
Depends on: A1 for line kinds; C for tax class on products.
Risks: D2's component snapshot changes how renewals are priced. Existing services keep their single `monthly_price_cents` (`reseller.py:846`) until re-snapshotted; the renewal code reads components if present, else the old field.
Workstream size: **L**.

## 6. Workstream E: reporting and accounting

**Purpose.** Operators can see what they are owed, what they collected, what they owe the tax authority, and hand it to an accountant.

- **E1 Reports.** Admin endpoints and pages, with CSV export: income by period (invoiced and collected), by product and by currency; unpaid and aged receivables; tax collected by jurisdiction and rule; refunds and credit notes; credit liabilities (sum of account balances); promotion usage; MRR, churn and failed-payment rates from `service_billings`; daily activity (orders, invoices, tickets). Read-only queries over invoices, payments, adjustments and ledger. Size **M**.
- **E2 Integrity checks.** A job and alert detectors (the alerting framework exists in `app/services/alerting/`) for: invoice lines sum to total; paid invoices have matching payments; payments never exceed invoice balance; ledger entries sum to the cached balance; orphaned PaymentIntents and pending refunds older than a threshold. Size **S**; start in Phase 1.
- **E3 Accounting export.** A journal generator per day: sales net by tax class, tax liability by jurisdiction, receivables, cash by gateway with fees, refunds, credit liability changes. Needs `fee_cents`, `net_cents` and payout reference on payments (A4). Core ships generic CSV; Xero, QuickBooks and others are plugins. Size **M**.
- **E4 Statements and exports.** Account statement (invoices, payments, credits) per client, and a full invoice and payment export for operators who leave. Size **S**.

Plugins: accounting export formats.
Depends on: A (data), C (tax reports), D4 (currencies).
Workstream size: **M** to **L**.

## 7. Workstream F: client area

- **F1 Quotes.** `quotes` and `quote_lines`: draft, delivered, on hold, accepted, declined, expired, converted; valid-until date; admin builder with predefined products; client view, accept and decline; conversion to invoice or order; PDF; tax via the engine; own number series. Size **M**. Depends on A1, C.
- **F2 Mass pay and invoice merge.** One payment covering several invoices needs a `payment_allocations` table (`payment_id`, `invoice_id`, `amount`), because `payments.invoice_id` is a single required foreign key (`reseller.py:574-579`). Keep that column for single-invoice payments. Merge becomes: cancel the originals with credit notes, issue one new invoice. Size **M**. Depends on A2.
- **F3 Saved methods and gateway choice.** Retail saved methods come from B6; "choose gateway on the invoice" lists the enabled gateway plugins for the invoice's currency and country. Size **S** after B6.
- **F4 Email template editor.** `email_templates` (event key, locale, subject, text and HTML bodies, enabled, editor, updated at) with a documented variable list per event, rendered in a sandboxed Jinja environment with strict undefined variables and autoescaping; preview and test send; reset to default; per-product welcome email. Seed from the text now hard-coded in `app/services/email_message_service.py:32-`. Add the missing senders (B4). Size **M**.
- **F5 Account features.** Add funds and credit view for retail (A3), on-demand renewal (generate the next cycle's invoice early), billing contacts with per-contact invoice routing (today only `invoice_email`, `commerce_account.py:132`), downloadable statements. Size **M**.
- **F6 Invoice templates.** PDF with logo, paper size (A4 and Letter; fixed to Letter today, `invoice_pdf_service.py:64`), language, tax breakdown, seller and buyer snapshots, legal notes from the invoice profile (C). Size **M**.

Plugins: none beyond gateways and channels.
Workstream size: **L** in total, deliverable piecemeal.

## 8. Workstream G: WHMCS import

**Purpose.** Let an operator move from WHMCS with clients, products, services, invoices, payments, credit and tickets intact. Lower priority than A to D, but its design must be settled early because it dictates what the target model has to hold.

**Mechanism.** An importer framework with a WHMCS source plugin, run as a CLI and an admin job, never inside a request. Two readers: a read-only database reader (complete, version-sensitive) and an API reader (portable, rate-limited, lacks some tables). Idempotent: every imported row stores `external_source` and `external_id`, with a unique constraint, so re-runs update rather than duplicate. A dry-run report lists counts, warnings and total mismatches before anything is written.

**Mapping.**

| WHMCS | Rackflow |
|---|---|
| Clients, contacts | `users`, `billing_accounts`, `billing_profiles`, account contacts; WHMCS client group to D5 |
| Product groups, products, pricing, configurable options, add-ons | `frontend_product_categories`, `frontend_products`, price plans and cycles, D2 options, D3 add-ons |
| Hosting services | Rackflow `Service` matched through the existing link endpoint (`app/api/billing.py:1531`) where it exists, plus `service_billings` with interval, next due date and price components |
| Invoices and items | `invoices` and lines, imported as history: original number and status kept, no emails, no webhooks, no recalculation of tax |
| Transactions | `payments` with mapped gateway names and fee fields; refunds as `refunds` |
| Credit and credit log | One opening ledger entry per client, or the full log if the totals reconcile |
| Promotions | `coupons` v2 |
| Tax rules | `tax_rules`, class standard |
| Currencies | `currencies` |
| Tickets and replies | `tickets`, `ticket_messages`, attachments if present |
| Quotes | `quotes` (after F1) |

**Cutover procedure.** Import, verify (unpaid invoice total, credit total and service counts equal per currency), freeze the WHMCS cron, run a final delta import, then enable Rackflow automation with dry run first. Imported services start with automation paused until the operator confirms the next due dates.

**Points to decide at implementation.** Stripe tokens can carry over only if the operator keeps the same Stripe account; PayPal agreements and other gateway tokens cannot. WHMCS password hashes are only usable if Rackflow's login accepts that hash format; otherwise force a reset. Invoice number collisions are avoided with a reserved series for imported documents.

**Plugins.** The importer is an import-source plugin; Blesta or FOSSBilling readers could follow.
Depends on: A (history and adjustments), C (tax rules), D (products, cycles, groups, currencies), B (paused automation flag).
Risks: data volume (batch and checkpoint), dirty source data (duplicate emails, orphan invoices), and ending up with Rackflow invoices that disagree with WHMCS totals. The mitigation is the dry-run reconciliation and keeping imported documents read-only.
Size: **L**.

## 9. Out of scope or optional

- Domain registration, transfer and registrar modules: not a Rackflow product area. If wanted later, a registrar plugin type.
- SSL, SiteJet and similar WHMCS add-ons, product bundles, cross-sell, client mass mail, time billing: not planned.
- **Affiliates:** optional, size **M**. Referral links, commission rules and payouts as ledger credits, so it depends on A3 and D6. Could be a plugin.
- Ticket parity (email piping, canned replies, SLAs): outside billing; tracked separately.
- Reseller-to-client invoicing: resellers bill their own clients outside Rackflow today. Keep that boundary unless an operator asks otherwise.

## 10. Dependencies between workstreams

```
A0 --> A1 --> A2 --> A4
        |      |  \
        |      |   --> A3 --> B5, B8, F5
        |      --> A5, F2
        --> C --> F1, F6, E1 (tax reports), G (tax rules)
A1 + D1 --> A6
A0 + A1 --> B3 --> B4 (+F4) --> B7
B6 --> F3
D1 --> D2, D3, D4, D5 --> D6
A + C + D --> G
A4 --> E3
```

Reading the graph: A0 and A1 unblock almost everything. C and D1 can start as soon as A1's line model is settled. B needs A2 and A3 for late fees and credit. G is last because its target tables must exist first.

## 11. Compatibility and risk register

| Risk | Where it bites | Mitigation |
|---|---|---|
| Money correctness | Rounding, double charges, lost payments, ledger drift | Integer minor units; named rounding; property tests for proration and tax; idempotency keys; row locks; shadow mode before switching engines; E2 integrity job; never edit published documents |
| Live-data migrations | `invoices`, `payments`, `credit_ledger_entries`, `service_billings` hold production money (the repository already carries 132 Alembic migrations) | Additive, nullable columns; batched backfills; reads switched behind settings; contract steps a release later; no backfill of historic invoices; every migration tested on SQLite and MySQL; relaxing the `amount_cents > 0` check (`reseller.py:497`) needs a MySQL-specific migration |
| `/api/billing` and the WHMCS module | Kenzi's own production use | No route, field or semantic changes to `app/api/billing.py` or `whmcs/modules/servers/rackflow/`; new code must not require WHMCS-side configuration; a contract test that records the module's calls and response shapes |
| Existing reseller behaviour | Resellers pay real invoices and are suspended by the worker | The reseller path is the reference implementation: keep its tests green at every step; new retail code goes beside it behind the `AccountFunder` interface; no change to retry days, grace or hold semantics unless a setting says so |
| Public API shapes | `/api/client/commerce/*`, `/api/reseller/*`, front end in `frontend/` | Additive fields only; new endpoints for new concepts; front end updated in the same change as any field it reads |
| Automation side effects | Real emails, suspensions, terminations | Jobs disabled by default; dry run; per-job switches; never apply new schedules to existing cycles retroactively |
| Legal and tax | Wrong tax, wrong invoice wording | Tax off by default; rule packs documented as starting points; validity dates, no in-place edits of used rules; snapshots on invoices |
| Gateway failures mid-operation | Refunds and charges that time out | Pending rows with idempotency keys and a reconciler; never mark success before the gateway confirms |
| Plugin API churn | Gateways and validators move behind a plugin boundary | Keep the current `PaymentGateway` protocol as the contract when converting; align names with `docs/plugin-system.md` once it exists |
| Scope | The whole is multi-quarter work | Each phase below ends in something an operator can use |

## 12. Recommended build order

Sizes: S is under about three working days, M is one to three weeks, L is more than three weeks, for one engineer.

| Phase | Work | Size | Why now |
|---|---|---|---|
| 0 | A0 close the retail money path; E2 integrity checks; contract tests for `/api/billing` and the WHMCS module; B1 settings store | M + S + S + S | Retail billing is not trustworthy today; later work needs a green base and settings |
| 1 | A1 invoice model v2; A2 credit and debit notes; A3 credit balance for all accounts; A4 refunds; D1 cycles | L + M + M + M + M | The ledger foundation; D1 is small and needed for renewals and proration |
| 2 | C tax engine (with admin UI and a first rule pack); B2 to B5 and B7 to B8 automation; F4 email templates | L + L + M | Needs A1 to A3; delivers the biggest operator-visible change: invoices before due, reminders, late fees, suspend and terminate, correct tax |
| 3 | D2 configurable options; D3 add-ons; D4 currencies; D5 client groups; D6 promotions v2; A6 proration; A5 manual invoices and billable items; B6 saved retail methods | L + M + M + M + M + M + M + L | Catalogue depth and billing flexibility on top of a correct ledger; a WHMCS importer spike (mapping only, no writes) runs here to expose modelling gaps early |
| 4 | E1 reports; E3 accounting export; F1 quotes; F2 mass pay; F3 gateway choice; F5 account features; F6 invoice templates | M + M + M + M + S + M + M | Polish that completes replacement for a typical operator |
| 5 | G WHMCS import; D7 trials; B9 grouping and continuous generation; D8 usage billing; affiliates | L + S + L + L + M | Needs everything above; each is optional or low frequency |

A rough total, one engineer working serially, is on the order of 12 to 18 months. The phases are ordered so that each stops at a point where Rackflow is safe and more useful than before; Phase 0 and Phase 1 are the minimum for retail billing to be correct, and Phase 2 is the minimum for an operator to stop running a daily manual chase.

## 13. Decisions for the owner

1. **Gapless numbering.** Is gapless invoice numbering a hard requirement (it drives how voids and proforma work), or is unique and ordered enough?
2. **Default proration policy.** Daily proration for upgrades, and credit on downgrade or no credit?
3. **First rule packs.** Which regions first (UK and EU VAT are the natural start)?
4. **Retail renewal charging.** Until saved retail methods exist (B6), is "invoice is emailed and paid by the client" acceptable for retail renewals?
5. **Reseller balances.** Retire `resellers.cached_balance_cents` in favour of the account-level cache once A3 is proven, or keep both indefinitely?
6. **Affiliates and quotes.** Where should these sit relative to Phase 4?
7. **Plugin seam names.** Confirm or adjust section 1 once `docs/plugin-system.md` exists.

**Decided defaults** (relayed by the project chat, 2026-11-21; implement these):

1. Invoice numbering is unique and ordered by default, with a per-series "gapless" option. In gapless mode numbers are allocated only at publish or payment, and a void never frees a number.
2. Daily proration for upgrades; a downgrade credits the account balance; refund on cancellation is off by default. All three are settings.
3. The first tax rule packs are UK VAT and EU VAT, the latter with a VIES tax-ID validator plugin.
4. Until saved retail payment methods exist (B6), retail renewal invoices are emailed and the client pays them in the client area.
5. Keep both reseller balance caches until A3 has run in shadow mode with zero mismatches, then retire `resellers.cached_balance_cents` in a later contract step.
6. Quotes in phase 4; affiliates in phase 5, as a plugin.
7. Seam names aligned with `docs/plugin-system.md` in section 1.

## 14. Internal billing events

Event names are not listed here. The one list of event names, with their permissions and payload fields, is the plugin catalogue `app/plugin_host/catalogue.py` (documented in `docs/plugin-system.md` sections 6.2.1 and 17.1). A billing fact that other code or plugins should see is added there as an `EventSpec` with permission `billing.read` and published with `app.plugin_host.events.emit(db, name, payload)`, which also queues the outbound commerce webhook for names in that catalogue. This section describes the settlement function and the in-process subscriber list that billing uses for its own follow-up steps.

### 14.1 The settlement function

`settle_invoice(db, invoice_id, *, payment=None)` in `app/services/billing/settlement.py` is the only place an invoice becomes paid. Every path that records money ends there: `PaymentOrchestrator.finalize_success` (card webhook, saved-method charge, USDT) and reseller credit both call it through `InvoiceService.mark_paid_if_fully_allocated`, and admin mark-paid first records a `Payment(gateway="manual")` for the outstanding amount (`app/services/billing/manual_payments.py`).

It locks the invoice row, sums succeeded payment allocations and then:

- if they cover the total and the invoice was not yet paid, sets it PAID and fires the invoice paid event once (on that transition only, so replays and repeat calls fire nothing);
- if a paid invoice has dropped below its total, reopens it (unchanged behaviour);
- if money arrives for a voided order invoice, or a retail invoice is overpaid, keeps the invoice as it is and records a refund review (section 14.4). A reseller invoice that is overpaid still raises, as before.

It never marks anything paid before the gateway has confirmed the payment; pending card payments do not count.

### 14.2 Subscriber list API

The invoice paid event is a plain in-process list, not a bus:

- `subscribe_invoice_paid(name, handler, *, applies=..., isolated=True)` registers or replaces (by name) a handler `handler(db, InvoicePaid(invoice, payment))`; `applies(invoice)` filters which invoices it sees.
- `unsubscribe_invoice_paid(name)` removes one; `invoice_paid_subscribers()` lists the names in run order.
- Handlers run in registration order inside the caller's transaction. An `isolated` handler (the default, and every default subscriber) runs in its own savepoint; if it raises, its work is rolled back and logged and the settlement stands. A non-isolated handler runs bare and must neither raise nor commit; none of the defaults uses that.
- `InvoicePaid` also carries `source`, set to `admin_mark_paid` by admin mark-paid and otherwise empty.

Default subscribers, in order (`app/services/billing/settlement_subscribers.py`):

| Name | Applies to | What it does |
|---|---|---|
| `coupon_usage` | invoices with an order | counts the order's coupon use once per order (`app/services/billing/coupons.py`) |
| `complete_billing_cycle` | retail cycle invoices | marks the cycle paid and schedules the next period by `interval_months`; reseller cycles are still completed by the recurring billing worker, because that can unsuspend services and lift reseller holds |
| `publish_invoice_paid` | invoices with a billing account, and any invoice settled by admin mark-paid | the only `emit(db, "invoice.paid", ...)` call, payload `invoice_id`, `billing_account_id`, `invoice_number`, `order_id`. Before A0, admin mark-paid published for every invoice and the card webhook for invoices with an account; both still do, once per settlement. A reseller invoice marked paid by an admin has no account, so `billing_account_id` is present and null, as it was before A0 (the catalogue requires the key, not a value) |
| `invoice_paid_email` | invoices with a billing account | queues the `invoice_paid` email (idempotency key `invoice:{id}:paid`) |
| `fulfil_order` | order charge invoices | moves the order to PAID_PENDING_FULFILLMENT and queues it; fulfilment (`CheckoutService.mark_paid_and_fulfill`, which emits `order.paid`) runs after the settlement commits |

Fulfilment never runs inside the settlement transaction, on any path. Provisioning commits, which would end the caller's transaction half way (before its audit row) and which a savepoint cannot survive, and a provisioning error must not undo money already received. So `fulfil_order` only queues the order, and every settlement path ends its transaction with `settlement.commit_and_fulfil(db)` instead of `db.commit()`: it commits the settlement together with the caller's audit rows, then fulfils each queued order in a transaction of its own (`run_deferred_fulfilment`). The payment webhook, which settles inside a savepoint, calls `run_deferred_fulfilment` after leaving it. The paths are admin mark-paid (`POST /api/admin/commerce/invoices/{id}/mark-paid`), reseller panel pay (`POST /api/reseller-panel/invoices/{id}/pay`, credit and saved-method charges for reseller account orders), the Stripe and PayPal webhooks, the USDT worker and admin USDT reconcile (USDT pays top-up invoices only, so nothing is queued there today). The recurring billing worker and reseller deploy settle only cycle and deploy invoices, which have no order, and keep their own commits. A queued order is fulfilled only if its invoice is PAID under lock, so a queue left by a transaction that rolled back does nothing. An order whose fulfilment raises stays in PAID_PENDING_FULFILLMENT for an admin to accept again; provisioning failures inside fulfilment still end in PROVISION_ERROR with a refund review, as before.

### 14.2.1 Lock order

Billing row locks are taken through `app/services/billing_locks.py` (`BillingLocks`), in the order of its `LOCK_ORDER`: service billing, reseller, billing account, order, invoice, payment, billing cycle, coupon, refund, number counter. This is the order of section 0 of `docs/billing-phase1-design.md` with the service billing first (the recurring worker starts from it and then settles the cycle invoice), the order before its invoice, and the billing cycle and coupon after the payment. Every path that locks a billing cycle holds its service billing first, so the billing row also serialises work on its cycles; `BillingLocks.for_invoice` locks the service billing of a cycle invoice before the invoice, and the worker's paid-cycle recovery locks the service billing before the cycle. A transaction never takes an earlier kind after a later one; re-locking a row it already holds is allowed. `BillingLocks.for_invoice` reads the invoice without a lock to learn its order (and, with `reseller=True`, its reseller), locks those, then the invoice; `InvoiceService.get_for_update` and `OrderDAO.get_for_update` go through it. Every lock flushes the session and re-reads the row (`populate_existing`), so a status checked after the lock is the committed one. The payment webhook and the USDT watcher look the payment up without a lock and then lock reseller, order, invoice and payment in that order, so the reseller credit ledger, which locks the reseller again later, only re-locks. Rows that open a gateway transaction (the webhook event claim, the USDT cursor and deposit) come before all of these. The tests check the sequence of lock calls on SQLite; lock waits and deadlocks themselves can only be exercised on MySQL. `PaymentRefundService.refund` looks the payment up without a lock and then locks through `BillingLocks.for_invoice(..., reseller=True)` and `BillingLocks.payments`, so a refund takes reseller, order, invoice and payment in order before the reversal re-locks them. Not yet on this order: the reseller recurring worker, which is outside A0 and moves in phase 1 (P1-D). Card payment start still calls the gateway while holding the invoice lock, so that two clicks cannot open two PaymentIntents; phase 1 section 0 removes gateway calls under locks.

### 14.2.2 Coupons and refund reviews (A0 review fixes)

- A coupon discounts the first invoice of an order only. `repeating` and `forever` coupons are accepted at checkout (an A0 test pins acceptance of a correctly configured repeating coupon); the quote carries `coupon_first_invoice_only: true` and the discount line on the quote and the invoice reads `Coupon CODE (first invoice only)`. Recurring discounts are deferred to D6.
- `max_uses` and `max_uses_per_account` are checked unlocked at quote time and again under the coupon row lock when the use is counted at payment. At payment the per-account count is a locking read of the account's redemptions, so on MySQL under REPEATABLE READ it sees redemptions committed after the transaction's snapshot. A use over a limit at payment is still counted (the client paid the discounted price), logged and audited as `billing.coupon_limit_exceeded` with `review: true`.
- Cancelling an order that holds no money (unpaid, or every payment reversed or refunded) releases its coupon use (`release_coupon_usage`: the redemption row is deleted, `used_count` decremented, audit `billing.coupon_usage_released`), idempotently under the coupon lock. While money waits for a refund review (provisioning failure, paid then cancelled) the use stays counted. Usage is only counted at payment, so an unpaid cancelled order has nothing to release. An order without an invoice (a zero total) counts its use when it is fulfilled and releases it again if provisioning fails, because no money was taken.
- Invoice-level refund review markers carry the reason (`[refund-review:{reason}:invoice-{id}]`; older notes with `[refund-review:invoice-{id}]` are still read, by the `({reason});` they contain). A second reason for the same invoice is recorded (note line and `billing.refund_review` audit row with `already_under_review` listing the earlier reasons) but does not list the same money a second time.

### 14.2.3 Renewals and cancellation (A0 review fixes)

- The recurring worker selects due billing rows (locked, `skip_locked` on MySQL) and then, because its first commit releases those locks, locks each row again by id with `populate_existing` and the same due conditions before processing it; a row another worker took, or that is no longer due, is skipped. Paid-cycle recovery reads candidate cycles without a lock and locks the service billing, then the cycle, re-checking both. This was already so on the reseller path before A0; with one worker nothing changes. The worker lease (`recurring_billing_worker_lease_seconds`) already keeps a second worker out while it is held, but it is not renewed during a batch, so a batch that outlasts the lease could overlap the next worker; the per-row lock closes that gap.
- `terminate_service` no longer marks a service TERMINATED when the power-off fails. It audits `billing.service_termination_failed` and raises; at the end of the period the billing stays due (`cancel_at` passed, not cancelled, nothing charged), `last_failure_code` is `end_of_period_terminate_failed`, the pass counts it under `manual_review`, and the next pass retries. An immediate cancel whose power-off fails sets `cancel_at` to now instead of cancelling the billing, so the worker retries the termination, and the response gains `termination_pending: true`. The audit row is enough for alerting now; an integrity detector for billings whose `cancel_at` passed more than a day ago without a cancellation would make it visible on the billing health page and is worth adding with E2.
- `request_cancellation` locks and re-reads the service billing (first in the lock order) before copying `next_charge_at` into `cancel_at`.
- Migration `billing_a0_renewals` backfills a retail billing from the most recent fulfilled order item of its service with a known interval, else the most recent item with a known interval, else monthly.
- The worker loop reads the stored interval in a thread (`asyncio.to_thread`), each pass.

### 14.3 Event names billing uses

| Name | Status |
|---|---|
| `invoice.paid` | emitted, by `publish_invoice_paid` only |
| `order.paid` | emitted, by `CheckoutService.mark_paid_and_fulfill` only |
| `invoice.created`, `invoice.overdue`, `payment.failed`, `payment.refunded`, `credit.applied`, `service.suspended` for non-payment | not emitted by billing yet; when added they go into the plugin catalogue first. (`service.suspended` is already emitted for every suspension by the service flush listener.) |

The retail renewal invoice email uses the email event `invoice_created`, which is an email template name, not a bus event.

### 14.4 Refund reviews

Until credit notes and the refund engine exist (phase 1, A2 and A4), money that may need returning is recorded for a person to decide, and no money moves. `app/services/billing/order_refunds.py` records the review on the order's admin notes, on the payment's metadata where there is one, and as an audit event `billing.refund_review`. The setting `billing_order_refund_policy` (`manual_review`, `original_method` or `credit`) is recorded with each review; in phase 0 every value is handled as a manual review because `PaymentRefundService` marks a refund successful while Stripe still reports it pending and retail accounts have no credit ledger yet (A3).

