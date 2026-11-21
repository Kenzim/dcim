# Rackflow billing compared with WHMCS billing

Status: analysis only, written 2026-11-21 on branch `task/billing-feature-comparison-wit-cedb9f` (base commit 5e60719). No code was changed or run for this document; every Rackflow statement comes from reading the code at that commit. Statements about runtime behaviour that I inferred from the code, and did not execute, are marked "inferred".

Companion document: `docs/billing-target-spec.md` (what to build, in what order).

## 1. How to read this

**Goal.** An operator should be able to run a hosting business on Rackflow's billing alone, with WHMCS no longer needed. The owner (Kenzi) will keep using WHMCS himself, so the existing `/api/billing` contract and the WHMCS module in `whmcs/` must keep working throughout.

**Two tracks.** Rackflow has two separate billing models that share some tables:

| Track | Who pays | Money model | Main code |
|---|---|---|---|
| **R** (reseller, B2B) | A `Reseller` | Prepaid credit ledger plus invoices, saved Stripe or PayPal methods, USDT deposits, monthly cycles with dunning | `app/models/reseller.py`, `app/services/{recurring_billing,reseller_billing,credit_ledger,invoice}_service.py`, `app/services/payments/`, `app/api/reseller_panel.py`, `app/api/reseller_admin.py` |
| **C** (retail client) | A `BillingAccount` of type `client` | Storefront, orders, invoices, manual or Stripe payment, coupons, flat tax | `app/models/commerce_*.py`, `app/models/storefront.py`, `app/services/{checkout,commerce_recurring,commerce_lifecycle,email_message}_service.py`, `app/api/commerce_*.py` |

`BillingAccount` also exists with type `reseller` (`app/models/commerce_account.py:45-48`), but `credit_balance_cents` on it is only a mirror of the reseller's cached balance (`app/dao/commerce_dao.py:85`). Nothing writes a retail client's credit balance (grep for `credit_balance_cents` finds only the model, the mirror and one serialiser).

**Status words.** `Has` means implemented and wired. `Partial` means something exists but misses behaviour WHMCS operators rely on. `Missing` means no code path found. The "Track" column says where the feature lives: R, C, both, or none.

**Sources for WHMCS.** WHMCS 9.0 documentation at docs.whmcs.com: Billing and Invoicing (credit and debit notes, prorata billing, invoice management), General Settings (Invoices, Credit), Tax Configuration, Promotions, Configuration Options, Currencies, Client Groups, Quotes, Fraud Protection, Credit Balances, Reports, and the Automation Settings documentation. Where I describe WHMCS behaviour from general product knowledge instead of a page I read, the row says "(general knowledge)". Affiliates and email template pages were not reachable at the URLs I tried, so those rows are general knowledge.

**Plugin system.** The design document `docs/plugin-system.md` does not exist yet on the plugin chat's branch, so "Plugin" notes below name seams, not final APIs. Two existing seams matter: `PaymentGatewayRegistry` (`app/services/payments/registry.py:14`, protocol in `app/services/payments/base.py:97`) for gateways, and the hardware plugin registry in `app/plugins/` (server management only, not billing).

## 2. Findings that change priorities

These came out of the code reading and matter more than any missing WHMCS feature, because they affect whether money is recorded correctly today.

1. **Retail card payments do not appear to settle anything (inferred).** `POST /api/client/commerce/invoices/{id}/pay` creates a Stripe PaymentIntent with metadata but never creates a `Payment` row (`app/api/commerce_client.py:444-493`). The Stripe webhook handler looks up a `Payment` by `external_ref` and silently returns when none exists (`app/services/payments/webhooks.py:159-160`). The only retail settlement paths left are admin "mark paid" (`app/api/commerce_admin.py:427-465`), which sets the invoice to paid without a `Payment` row and without fulfilling the order, and admin "accept" or "retry-fulfill" (`:259-340`), which fulfil but do not touch the invoice. Orders are fulfilled only through those admin endpoints (`mark_paid_and_fulfill` has no other caller; `app/services/checkout_service.py:668`).
2. **Retail renewals appear not to work (inferred).** Fulfilment creates a `ServiceBilling` with `billing_account_id` set (`app/services/commerce_recurring_service.py:47-`), but the recurring worker is reseller-only: the cycle invoice is created with `reseller_id=billing.reseller_id` (`app/services/recurring_billing_service.py:409-413`), which `InvoiceService.create` rejects when both ids are empty (`app/services/invoice_service.py:100-101`), and payment goes through `fund_invoice(db, billing.reseller, ...)` (`recurring_billing_service.py:456`). The worker is off by default (`app/core/config.py:117`). The existing tests build resellers only (`tests/services/test_recurring_billing_service.py`).
3. **Quarterly, semi-annual and annual retail plans would renew monthly.** `ServiceBilling` has no interval column (`app/models/reseller.py:783-877`; only `monthly_price_cents` at `:846`), and every paid cycle advances one calendar month (`recurring_billing_service.py:793`). `CommerceRecurringService.next_due_at` knows about intervals (`commerce_recurring_service.py:29-45`) but is only used for the first date.
4. **Invoice lines do not add up to the invoice total.** The quote's first line carries only the setup amount; the first period's recurring price is a field on that line, not a line (`checkout_service.py:260-268`, `:344-358`), and zero-value lines are skipped. A plan with no setup fee therefore produces an invoice whose lines omit the product price. The option price delta is added to both the setup and the recurring amounts (`:249-250`), so the first invoice includes it twice (inferred from `subtotal = setup + recurring`, `:251`).
5. **Coupon rules are stored but not enforced.** `max_uses_per_account`, `duration` and `duration_months` exist on the model and admin API (`app/api/commerce_store_admin.py:195-197`, `:339-341`) but no service reads them. Discounts apply to the first order only, because renewals bill `ServiceBilling.monthly_price_cents` with no discount.
6. **Refund paths are stubs on the retail side.** A provisioning failure writes "Refund path stub: manual review required." (`checkout_service.py:702`, `:711-714`). Admin order cancel changes status only; it neither voids the invoice nor refunds (`commerce_admin.py:343-381`). Reseller refunds are full-amount only (`app/services/payments/refunds.py:105` passes `payment.amount_cents`).
7. **End-of-period cancellation never terminates (inferred).** `request_cancellation(when="end_of_cycle")` sets the billing row to `CANCELLED` immediately (`app/services/commerce_lifecycle_service.py:72-75`), and the worker only selects `ACTIVE`, `PAST_DUE` and `GRACE` rows (`recurring_billing_service.py:164-181`). I found no code that terminates the service when the paid period ends.
8. **No immutability, no credit notes, no per-line tax.** `InvoicePurpose.CREDIT_NOTE` exists in the enum (`reseller.py:86`) but nothing creates one; `invoices.amount_cents` must be positive (`reseller.py:497`); a void is a status flip.
9. **Seller and buyer details are not frozen on invoices.** The PDF reads the live billing profile (`app/services/invoice_pdf_service.py:17-39`) and the company name, address and tax ID from settings (`:73-80`), so editing a client's address changes old PDFs. Paper size is fixed to US Letter (`:64`).
10. **`commerce_invoice_prefix` is configuration only.** It is defined at `app/core/config.py:148` and used nowhere.

## 3. Catalogue and pricing

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Product groups and products | Products grouped; each has type, welcome email, stock, module settings | Has | C | `FrontendProductCategory`/`FrontendProduct` (`app/models/storefront.py:69`, `:99`) mapped to catalogue `Product`; admin UI `frontend/src/components/store/StoreProducts.svelte`. Reseller track prices per product and group (`ProductPrice`, `ResellerGroupPrice`, `reseller.py:280`, `:307`). |
| Payment type: free, one-time, recurring | Per product | Has | C | `PricePlanPricingModel` free, one_time, recurring (`storefront.py:51-55`). |
| Billing cycles | Monthly, quarterly, semi-annual, annual, biennial, triennial, each with own price and setup fee | Partial | C | Only monthly, quarterly, semiannually, annually (`storefront.py:57-61`). No biennial or triennial. Reseller track is monthly only (`BillingCycle` doc string, `reseller.py:881`). Renewals ignore the interval (finding 3). |
| Setup fee per cycle | Yes | Partial | C | One `setup_cents` per plan (`storefront.py:178`), not per cycle. |
| Configurable options (dropdown, radio, yes/no, quantity) with per-cycle price and setup fee | Yes, per-option per-cycle prices, quantity ranges | Partial | C | Only `select` and `text` (`storefront.py:64-66`). One flat `price_delta_cents` per value (`:256`), applied to every cycle and to setup (`checkout_service.py:130`, `:249-250`). No quantity options, no per-cycle price. |
| Product add-ons billed on their own cycle | Yes, optional prorata to the parent's due date | Missing | C | `ProductAddon` model exists (`storefront.py:262`) with setup and monthly price, but checkout, fulfilment and billing never reference it. |
| Product bundles and cross-sells | Yes | Missing | none | No model. Low priority; plugin or later. |
| Free trials | Promotion or product setting | Missing | C | `PricePlan.trial_days` exists (`storefront.py:179`) but no service uses it. |
| Multi-currency prices with conversion rates | Currencies with base conversion rate, per-currency price rows | Partial | both | Each price plan, invoice and account carries a currency; checkout refuses a mismatch (`checkout_service.py:242-243`). No currency table, no conversion rates, no display formats, no minor-unit handling (cents assumed everywhere, which is wrong for zero-decimal currencies). |
| Client groups (group discount, exempt from suspension, separate invoices) | Yes | Partial | R | Reseller groups price products (`ResellerGroup`, `reseller.py:155`). Nothing for retail clients, no invoice-stage group discount, no suspension exemption, no tax-exempt group. |
| Promotions: percentage, fixed, price override, free setup | Four types, recurring with "recur for N", applies-to lists, requires lists, new-client-only, per-client limit, start and expiry | Partial | C | `Coupon` has percent or fixed, product or category scope, max uses, start and expiry (`app/models/commerce_coupon_tax.py:58-101`). Per-account limit and duration fields are not enforced (finding 5). No price override, no free-setup type, no billing-cycle requirement, no new-client-only, no recurring discount on renewals. Usage count is incremented at order placement, not payment (`checkout_service.py:365-385`). |
| Price quote before purchase | Cart totals | Has | C | `POST /api/client/commerce/quote` (`app/api/commerce_client.py:294`) shares code with checkout (`checkout_service.py:229-308`). |
| Stock and capacity control | Per product quantity | Has | both | Reseller stock quotas (`StockQuota`, `reseller.py:427`); retail via provisioning capacity. Out of scope for billing. |
| Product upgrade and downgrade with proration | Automatic upgrade invoice, prorated; credit on downgrade optional | Partial | C | `POST /services/{id}/upgrade` (`commerce_client.py:554`) creates an `UPGRADE` invoice for the difference of setup fees only, "pending admin approval" (`commerce_lifecycle_service.py:133-205`). No proration, no recurring price change, no downgrade. |
| Prorata billing to a fixed day of month | Prorata date and "charge next month" threshold | Missing | none | `billing_anchor_day` exists (`reseller.py:849`) but only keeps month-end anchoring (`recurring_billing_service.py:72-78`). |
| Usage (metered) billing | Bandwidth, disk and custom metrics on renewal | Missing | none | No usage-to-invoice path. Rackflow has usage data (for example bandwidth samples) that could feed it. Candidate plugin or later phase. |

## 4. Orders and checkout

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Order form and cart with several items | Multi-item cart, order forms | Partial | C | One product per checkout call: `CheckoutRequest` has a single `frontend_product_id` (`commerce_client.py:75-83`); `place_order` creates one item (`checkout_service.py:498-510`). No cart. |
| Order statuses (pending, active, cancelled, fraud) | Yes | Has | C | `OrderStatus` (`app/models/commerce_order.py:48-57`) plus history table. |
| Manual order acceptance | Optional | Has | C | `POST /orders/{id}/accept` (`commerce_admin.py:259`). |
| Auto-provision on payment | Setting per product | Partial | C | Provisioning exists (`checkout_service.py:569-665`) but is triggered only by admin actions (finding 1). |
| Fraud checks | Pluggable modules (FraudLabs Pro, MaxMind), flags order as fraud | Missing | C | `orders.fraud_score` and `client_ip` columns exist (`commerce_order.py:115-116`) and are returned by the API (`commerce_admin.py:129`), but nothing populates the score. Per-account checkout rate limit exists (`config.py:151-152`). Candidate for a fraud-check plugin. |
| Order cancellation cancels the invoice and refunds | Credit note on cancel | Missing | C | Finding 6. |
| Terms of service acceptance | Optional | Has | C | `terms_version` and `terms_accepted_at` on orders (`commerce_order.py:103-104`). |
| Idempotent checkout | Not a WHMCS feature | Has | C | `checkout_nonce` unique (`commerce_order.py:105`; `checkout_service.py:455-459`). |
| Admin-created orders for a client | Yes, with custom promo | Missing | C | No admin create-order endpoint in `commerce_admin.py`. |
| Registration modes and verification | Open registration, email verification | Has | C | `commerce_registration_mode` disabled, invite_only, open (`config.py:150`); verification and reset in `app/api/commerce_auth_public.py`. |
| Domains (register, renew, transfer, registrar modules) | Core WHMCS feature | Missing | none | Not a Rackflow product area. If wanted, a registrar plugin type. |

## 5. Invoicing

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Invoice with line items | Lines with description, amount, tax flag, item link | Partial | C | `InvoiceLine` (`app/models/commerce_invoice_ext.py:21-45`), but reseller invoices have no lines, and retail lines do not sum to the total (finding 4). |
| Invoice states | Draft, Unpaid, Paid, Cancelled, Refunded, Collections, Payment Pending | Partial | both | `InvoiceStatus`: draft, open, paid, void, failed, pending_action, overdue (`reseller.py:90-97`). No refunded, partially paid, or collections. New invoices are created `open` directly (`invoice_service.py:109`); draft is not used. |
| Immutable once published, with credit and debit notes | Yes (current WHMCS) | Missing | none | Finding 8. Void and admin mark-paid edit status in place. |
| Credit notes | Auto-created on cancel, refund, mark-paid with balance, apply credit, merge | Missing | none | Enum value only. |
| Debit notes (late fees, removed credit, overpayment) | Yes | Missing | none | No late fee at all. |
| Manual invoice creation | Draft, add lines, publish and send | Missing | none | No admin create-invoice endpoint; `InvoiceService.create` takes a single amount and a fixed purpose list (`reseller.py:78-87`). |
| Sequential numbering with custom format | Format tags, next number, auto reset, separate sequences for paid, credit and debit | Partial | both | One global integer sequence (`invoice_service.py:43-80`). No format, prefix, reset, or separate series. `commerce_invoice_prefix` unused (finding 10). |
| Proforma invoices | Unpaid shown as proforma, paid numbered separately | Missing | none | Needed for some jurisdictions. |
| Invoice grouping (one invoice for several due items) | Yes, per client or group switch | Missing | R | One `BillingCycle` owns one invoice (`reseller.py:909-915`, unique). |
| Merge, split, reissue invoices | Yes | Missing | none | |
| Client data snapshot on invoice | Optional setting | Missing | C | Finding 9. |
| PDF invoice | Customisable template, paper size, fonts | Partial | C | `InvoicePdfService` (ReportLab, Letter only, fixed layout; `invoice_pdf_service.py:55-`). Endpoints `GET /invoices/{id}/pdf` for client and admin (`commerce_client.py:422`, `commerce_admin.py:411`). No reseller invoice PDF route in `reseller_panel.py`. No template customisation, no logo, no tax breakdown. |
| Invoice email with PDF attached | Yes | Partial | C | `invoice_created` email is queued (`checkout_service.py:419-436`), without attachment. |
| Invoice notes, admin-only notes | Yes | Missing | none | |
| Invoice due date and payment terms | Days before due, per cycle | Partial | both | `due_at` exists. Retail checkout invoices are due immediately (`checkout_service.py:339`); cycle invoices are created at the due moment, not ahead of it (`recurring_billing_service.py:159-166`, `:379`). |
| Admin invoice list and filters | Search by number, client, status, date, description | Partial | both | Retail: status and account filter only (`commerce_admin.py:383-402`). Reseller: separate list (`reseller_admin.py:1063`). No date or text search, no export, no bulk actions. |
| Client invoice list and view | Yes | Has | both | `frontend/src/components/client/ClientBilling.svelte`, `frontend/src/components/reseller/Invoices.svelte`. |

## 6. Payments and gateways

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Gateway module system | Hundreds of modules; capture, 3D Secure, remote input, callbacks | Partial | R | Protocol-based adapters registered in `PaymentGatewayRegistry` (`registry.py:14`; protocol `base.py:97`): Stripe (`stripe_gateway.py`), PayPal (`paypal_gateway.py`), USDT on-chain (`usdt_*.py`). Strong base for plugin conversion. Retail uses Stripe directly via `importlib` in the API handler (`commerce_client.py:470-473`), bypassing the registry. |
| Pay invoice online | Gateway selection on the invoice | Partial | both | R: `POST /invoices/{id}/pay` (`reseller_panel.py:1085`) works through the orchestrator. C: PaymentIntent creation only (finding 1); without Stripe it returns a "manual" message (`commerce_client.py:460-467`). |
| Client chooses gateway per invoice | Setting | Missing | C | One hard-coded path. |
| Stored payment methods, auto-charge | Tokenised card or PayPal agreement | Has (R), Missing (C) | R | `ResellerPaymentMethod` (`reseller.py:604`), Stripe SetupIntent and PayPal vault flows (`orchestrator.py:59-330`), ordered fallback list (`reseller_panel.py:1005`). C: explicit stub returning an empty list (`commerce_client.py:499-507`). |
| Capture before due date | "Process days before due" | Missing | R | Charge is attempted when the cycle comes due (`recurring_billing_service.py:159-166`). |
| Retry failed payments | Retry schedule via automation | Has | R | Retry days `1,3,5` and 7-day grace, configurable by environment (`config.py:112-113`; `recurring_billing_service.py:522-585`). |
| Webhooks and callbacks | Per gateway | Has | R | Stripe and PayPal webhook endpoints (`reseller_panel.py:1127`, `:1168`), idempotency table `GatewayWebhookEvent` (`reseller.py:688`), disputes suspend services (`webhooks.py:195-209`, `:406-414`). |
| Partial payments | Yes | Partial | both | `Payment` rows are summed (`invoice_service.py:192-218`) so partial allocation works in the service layer; no UI for admin to record a partial payment. |
| Admin add payment or transaction | Yes, with fees, gateway, reference | Partial | C | `mark-paid` only (finding 1). No `Payment` row. |
| Transaction list | Filters, fees, refund, export | Partial | both | `GET /transactions` (`commerce_admin.py:496`), `CommerceTransactions.svelte`. No fee or net amount fields on `Payment` (`reseller.py:566-601`). |
| Gateway fees | Recorded per transaction | Missing | none | Needed for accounting exports. |
| Gateway logs | Yes | Has | C | `GET /gateway-logs` (`commerce_admin.py:532`). |
| Cryptocurrency | Via modules | Has | R | USDT watcher and sweeper (`app/services/payments/usdt_*.py`). Strong differentiator; candidate to ship as a plugin. |
| Mass payment (pay several invoices at once) | Setting plus mass-pay invoice | Missing | none | |
| Bank transfer and offline instructions | Gateway module | Partial | C | Only the "manual" message. No instruction text, no reference matching. |

## 7. Credit and refunds

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Client credit balance | Per client, with log | Has (R), Missing (C) | R | Append-only `CreditLedgerEntry` with `balance_after_cents`, reversal link and idempotency key (`reseller.py:706-780`; `credit_ledger_service.py:80-244`), keyed to `reseller_id` non-null. Retail accounts have no ledger. |
| Add funds by client | Min, max, max balance, require approved order | Has (R), Missing (C) | R | Reseller top-up invoices (`reseller_panel.py:875`). No limits settings. |
| Auto-apply credit to new invoices | Setting | Partial | R | R: credit-first or payment-first preference per reseller (`ResellerChargePreference`, `reseller.py:56-58`; `orchestrator.py:439-471`). C: none. |
| Manual apply or remove credit on an invoice | Admin and client | Missing | both | Credit is only consumed by the funding algorithm. |
| Overpayment becomes credit | Automatic | Missing | none | `mark_paid_if_fully_allocated` raises if allocations exceed the total (`invoice_service.py:209-210`). |
| Admin credit adjustment with reason | Yes | Has | R | `POST /resellers/{id}/credit-adjustments` (`reseller_admin.py:1948`), ledger view `:1996`, UI `ResellerDetail.svelte`. |
| Refund a payment to the gateway | Full or partial, creates credit note | Partial | R | `PaymentRefundService` (`refunds.py:42-142`): Stripe and PayPal, plus credit-paid payments reversed in the ledger; full amount only; USDT refused (`:61-62`). Admin endpoint `reseller_admin.py:1163`. |
| Refund to credit balance | Yes | Missing | none | |
| Refund on retail orders | Yes | Missing | C | Findings 1 and 6. The only refund UI is the reseller payments modal in `AdminBilling.svelte` (calls `refundResellerPayment`, `frontend/src/lib/api.js:4288`); `CommerceTransactions.svelte` merely offers a "refunded" status filter. |
| Chargeback and dispute handling | Gateway callbacks, manual | Has | R | `charge.dispute.created` reverses the payment and suspends linked services (`stripe_gateway.py:320`; `webhooks.py:195-209`). |
| Refund on cancellation of unused time | Manual, via admin refund | Missing | none | Needs proration (workstream A). |

## 8. Tax

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Tax rules per country and state | Up to two levels per rule, named | Partial | C | `TaxRate` is one row per country and optional region (`commerce_coupon_tax.py:145-156`, unique on both). One rate applies; no stacking. |
| Two tax levels, compound | Level 1 and 2, compound option | Missing | none | `_tax_cents` picks one rate (`checkout_service.py:203-226`). |
| Inclusive and exclusive prices | Global setting | Missing | none | Exclusive only (`:226`, `:258`). |
| Tax on setup, recurring, add-ons, domains separately | Per item flag | Missing | none | Tax is computed on the whole discounted subtotal (`:256-257`). |
| Tax-exempt clients | Per client, per group | Partial | C | `billing_accounts.tax_exempt` (`commerce_account.py:92`) is honoured (`checkout_service.py:208`); no reason, certificate, or expiry. Nothing in the code sets it: it is only read at checkout and returned by the admin serialiser (`commerce_admin.py:208`), so today it can only be changed in the database. |
| Tax ID collection and validation | VAT number validation by format or with the authority | Partial | C | `billing_profiles.tax_id` stored (`commerce_account.py:131`) and editable by the client (`commerce_client.py:84-97`). No validation, no status. |
| Reverse charge | Zero rate for valid EU business IDs | Missing | none | |
| Seller tax ID on invoice | Yes | Partial | C | From settings, PDF only (`invoice_pdf_service.py:77-80`). |
| Tax breakdown on invoice | Per rule | Missing | none | One "Tax" line (`checkout_service.py:287-295`); `InvoiceLine.tax_cents` is set only on that line (`:354`). |
| Local invoice rules (proforma, sequential numbering, wording) | Proforma and paid numbering; EU VAT, UK VAT, India GST guides | Missing | none | See section 5. |
| Tax on reseller invoices | n/a (WHMCS has no reseller track) | Missing | R | Reseller invoices carry no tax. |
| Tax admin UI | Configuration page | Missing | C | API exists (`commerce_store_admin.py:703-739`); the front end only references the routes in `frontend/src/lib/api.js`, with no component using them. |
| Tax reports | VAT and tax reports | Missing | none | |

## 9. Automation and dunning

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Daily automation run with settings page | Time of day, many switches, stored in the database | Partial | R | Worker loop with a database lease (`app/workers/recurring_billing.py`, `RecurringBillingLease` `reseller.py:945`); settings are environment variables only (`config.py:112-117`). Off unless `run_recurring_billing_worker` is true. |
| Generate invoice N days before due, per cycle | Yes, per-cycle overrides | Missing | none | Finding in section 5. |
| Unpaid reminder before due | Yes | Missing | none | |
| Overdue reminders (up to three) | Yes | Partial | R | Failure notice on each failed attempt and a grace warning on the second (`recurring_billing_service.py:570-585`) through `NotificationOutbox`; not scheduled by days overdue, and nothing for retail. `invoice_overdue` email template exists (`email_message_service.py:25`) but I found no caller. |
| Late fees (fixed or percent, minimum) | Via debit note | Missing | none | |
| Auto-charge card on file | "Process days before due" | Has | R | Section 6. |
| Auto-suspend N days after due | Yes, per product or group exemption | Partial | R | Suspends after retries and grace (`recurring_billing_service.py:639-682`); `block_new` or `suspend_all` per reseller (`reseller.py:67-70`). Not configurable per product; no client-group exemption; no retail. |
| Auto-terminate N days after due | Yes | Missing | none | Nothing terminates for non-payment. |
| Auto-unsuspend on payment | Yes | Has | R | `recurring_billing_service.py:796-799`, `:840-855`. |
| Cancellation requests (immediate or end of period) | Yes, auto-cancels unpaid invoices | Partial | C | Finding 7. |
| Continuous invoice generation | Setting | Missing | none | One cycle at a time (`BillingCycle` unique per due date, `reseller.py:885-888`). |
| Billing hold on non-payment | n/a | Has | R | `billing_hold` on resellers blocks new deploys (`recurring_billing_service.py:630-637`). |
| Automatic invoice cancellation of old unpaid invoices | Setting | Missing | none | |
| Idempotent, crash-safe processing | Not a WHMCS strength | Has | R | Claim tokens, leases, idempotency keys (`reseller.py:880-943`). Keep this design. |

## 10. Client area

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Invoices list and view, PDF download | Yes | Has | both | `ClientBilling.svelte`; reseller `Invoices.svelte`. |
| Pay now | Yes | Partial | both | Finding 1. |
| Add funds | Yes | Has (R), Missing (C) | R | `TopUp.svelte`. |
| Saved payment methods | Yes | Has (R), Missing (C) | R | `PaymentMethods.svelte`; C stub. |
| Order new services, upgrade, cancel | Yes | Partial | C | `ClientCheckout.svelte`; upgrade and cancel endpoints (`commerce_client.py:533`, `:554`). |
| Quotes: view, accept, convert | Yes | Missing | none | |
| Mass pay | Yes | Missing | none | |
| On-demand renewal | Setting | Missing | none | |
| Profile with tax ID | Yes | Has | C | `PUT /profile` (`commerce_client.py:666`). |
| Email history | Yes | Has | C | `GET /emails` (`commerce_client.py:581`). |
| Activity log | Yes | Has | C | `GET /activity` (`:608`). |
| Credit balance and log view | Yes | Has (R), Missing (C) | R | `GET /balance`, `/ledger` (`reseller_panel.py:749`, `:854`). |
| Multiple users per account, contacts | Sub-accounts, permissions | Partial | both | Invites exist (`user_invite` email); no billing contacts with invoice-email routing beyond `invoice_email` (`commerce_account.py:132`). |

## 11. Admin tools

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Client profile with billing summary | Yes | Partial | C | `GET /billing-accounts/{id}` (`commerce_admin.py:625`), `AdminBilling.svelte`. |
| Orders list, accept, retry, cancel | Yes | Has | C | `commerce_admin.py:234-381`, `CommerceOrders.svelte`. |
| Invoices list, mark paid, void | Yes | Partial | C | `commerce_admin.py:383-495`; `CommerceInvoices.svelte`. |
| Transactions list, refund | Yes | Partial | both | Section 6. |
| Email log and retry | Yes | Has | C | `commerce_admin.py:558-582`, `CommerceEmailLog.svelte`. |
| Audit log | Yes | Has | C | `GET /audit-events` (`commerce_admin.py:584`), `AuditService`. |
| Promotions admin | Yes | Has | C | `StoreCoupons.svelte`, `commerce_store_admin.py:664-700`. No delete or usage drill-down. |
| Tax admin UI | Yes | Missing | C | Section 8. |
| Admin roles for billing actions | Granular permissions | Partial | both | Admin dependency only (`AdminDep`); no separate refund or credit permission seen. Not verified in depth. |
| Mass actions on invoices | Yes | Missing | none | |
| Billable items, time billing | Yes | Missing | none | Low priority. |
| Client mass mail | Yes | Missing | none | |
| GDPR export and anonymise | Partial in WHMCS | Has | C | `app/services/gdpr_service.py`, `commerce_admin.py:727-748`. Invoices must be retained for tax law; check anonymisation keeps financial records. |

## 12. Reports and accounting

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Income and sales reports | Daily performance, monthly orders, income by month and product, promotions usage | Missing | none | No billing report endpoints in `commerce_admin.py` or `commerce_store_admin.py`. |
| Aged receivables, unpaid invoices report | Yes | Missing | none | Data is queryable; no endpoint. |
| Client statement and account balance | Yes | Partial | R | Ledger with running balance (`reseller_admin.py:1996`). |
| Tax and VAT reports | Yes | Missing | none | |
| Revenue and MRR dashboards | Add-ons and reports | Missing | none | |
| CSV or accounting export | Via reports and add-ons | Missing | none | Candidate plugin type (Xero, QuickBooks, generic CSV). |
| Reconciliation checks | Weak | Partial | R | USDT deposit reconcile (`reseller_admin.py:1422`); detectors in `app/services/alerting/`. No general ledger-versus-invoice invariant check. |
| Multi-currency reporting | Base currency conversion | Missing | none | |

## 13. Notifications and email

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Editable email templates with merge fields | Template editor, languages, per-product welcome email | Missing | C | Templates are code (`app/services/email_message_service.py:32-`, rendered by `render_email`). Twelve events (`:17-29`). |
| Invoice created, paid, overdue, payment failed emails | Yes | Partial | C | Of the twelve events, only six have a sender: `order_confirmation` and `invoice_created` (`checkout_service.py:403-436`), `service_suspended` on dispute (`webhooks.py:384-398`), `user_invite`, `password_reset` and `verify_email`. A grep for `EmailEvent.` and `EmailMessageService.enqueue` outside the service finds no sender for `welcome`, `invoice_paid`, `invoice_overdue`, `payment_failed`, `service_provisioned` or `ticket_reply`. |
| Reseller notifications | n/a | Has | R | `NotificationEvent` (`app/services/notification_service.py:25-33`) through `NotificationOutbox` (`reseller.py:958`), sent by `app/workers/notification_outbox.py`. |
| Reliable queue, idempotency, retry | Cron-based | Has | both | `EmailMessage` queue with idempotency key (`email_message_service.py:158-210`), `send_queued_batch` (`email_sender.py:73`). |
| Per-client email opt-out, CC | Partial | Partial | C | `cc` column exists on `EmailMessage`; no preference model. |
| Outbound webhooks | Via hooks and API | Partial | C | Three events only: `order.paid`, `invoice.paid`, `ticket.created` (`app/services/commerce_webhook_service.py:23-25`). |
| Other channels (SMS, Slack, Discord) | Add-ons | Partial | C | Discord link exists for clients (`commerce_client.py:696-750`); alerting has its own senders. Notification channels are a plugin candidate. |

## 14. Resellers and affiliates

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Reseller accounts selling to their own clients | Not native (needs reseller add-ons or module-level resellers) | Has | R | Rackflow's strength: reseller panel, client management, product permissions, quotas, API key (`app/api/reseller_panel.py`, `ResellerGroup`, `ResellerClientProductPermission`). |
| Affiliate programme (referral links, commission, payouts) | Built in | Missing | none | No model. Optional workstream; commission could be credit-ledger entries. |
| Reseller wholesale pricing by group | n/a | Has | R | `ResellerGroupPrice` (`reseller.py:307`). |
| Reseller-to-client invoicing | n/a | Missing | R | Resellers bill their clients outside Rackflow; Rackflow invoices only the reseller. Worth recording as a deliberate boundary. |

## 15. Support tickets

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Departments, tickets, replies, statuses, priority | Yes | Has | C | `app/models/support_ticket.py:46-191`; client routes `commerce_client.py:752-839`; admin routes `app/api/commerce_support_admin.py:89-153`; UI `ClientSupport.svelte`, `SupportTickets.svelte`. |
| Staff notes, assignment | Yes | Has | C | `is_staff_note`, `assigned_admin_id`. |
| Attachments | Yes | Has (model) | C | `TicketAttachment` (`support_ticket.py:177-191`); upload path not verified. |
| Link ticket to service | Yes | Has | C | `service_id` on ticket. |
| Email piping (reply by email, create by email) | Yes | Missing | none | Outbound `ticket_reply` only. |
| Canned replies, SLAs, escalation, merge, tags | Yes | Missing | none | Out of billing scope; listed for the import and parity picture. |
| `ticket.created` webhook | n/a | Has | C | `commerce_webhook_service.py:24`. |

## 16. Import and migration

| Feature | WHMCS | Rackflow | Track | Evidence and gaps |
|---|---|---|---|---|
| Import from WHMCS (clients, contacts, products, services, invoices, transactions, credit, tickets) | n/a (WHMCS imports from others via add-ons) | Missing | none | Only coexistence is documented (`docs/commerce-whmcs-coexistence.md`). The WHMCS server module in `whmcs/modules/servers/rackflow/` talks to `/api/billing` (register, suspend, unsuspend, terminate, change package; `app/api/billing.py`), which creates `Service` rows but no billing records (`ServiceBilling` is created only by reseller deploys, `reseller_billing_service.py:748`, and retail fulfilment, `commerce_recurring_service.py:77`). |
| Keep WHMCS as billing front end while Rackflow runs infrastructure | n/a | Has | none | This is Kenzi's own use; the contract must stay stable (`/api/billing/*` routes, `rackflow.php` module functions `CreateAccount`, `SuspendAccount`, `UnsuspendAccount`, `TerminateAccount`, `ChangePackage`). |
| Data export for leaving Rackflow | n/a | Partial | C | GDPR export of one user (`gdpr_service.py:107`). No full invoice export. |

## 17. Biggest gaps in order of consequence

1. The retail money path is not closed (findings 1 to 4, 6, 7). Until it is, retail billing cannot replace anything.
2. No immutable invoices, credit notes or partial refunds, so there is no correct way to fix a mistake after the fact.
3. Tax is a single flat rate with no inclusive pricing, no line-level breakdown, no tax-ID validation and no reverse charge.
4. Automation is a failure-handling loop, not a schedule: nothing generates invoices ahead of the due date, sends reminders, adds late fees or terminates.
5. Catalogue pricing is too coarse (flat option deltas, one setup fee, four cycles, no add-ons, no currencies table).
6. No reporting, no accounting export, no editable emails, no quotes, no WHMCS import.
