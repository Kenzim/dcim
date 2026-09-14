# Commerce and WHMCS coexistence

Rackflow can run **retail commerce** (native storefront, orders, client portal) alongside the existing **WHMCS billing integration** without replacing `/api/billing` overnight.

## Dual-run model

| Surface | Purpose | Stable during migration? |
|--------|---------|---------------------------|
| `/api/billing` | WHMCS module provisioning, suspend, power, reinstall | **Yes** — unchanged contract |
| `/api/client/commerce/*` | Retail browse, checkout, orders, invoices, support | New; gated by feature flag |
| `/api/admin/commerce/*` | Admin order acceptance, mark-paid, webhooks, GDPR | New; gated by feature flag |
| WHMCS module (`whmcs/modules/servers/rackflow/`) | Legacy hosting products on WHMCS | Continues until products migrate |

Retail clients get a `billing_accounts` row (type `client`) and commerce invoices with `billing_account_id`. Reseller/WHMCS flows keep using `reseller_id` on invoices and services where applicable.

## Feature flag

Commerce is **enabled by default**. There is no public marketing storefront — `/` is login-only (client / admin / reseller after sign-in).

Optional emergency kill switch:

```bash
COMMERCE_RETAIL_ENABLED=false
```

When `false`, `/api/client/commerce/*` returns **503**. Admin store/commerce routes remain available for configuration.

## Migrating hosting to orders

1. **Catalog** — Map WHMCS products to `frontend_products` + `price_plans` linked to existing catalog `products`.
2. **Parallel run** — New retail signups use commerce checkout; existing WHMCS services stay on `/api/billing` until manually moved or renewed through commerce.
3. **Fulfillment** — Paid commerce orders call `ProvisioningService` (the same stack as `/api/billing`, admin, MCP, and reseller) and write `service_vm` / `service_bare_metal` children, deployment jobs, and proxy IPs. Recurring retail services get `service_billings.billing_account_id`.
4. **Cutover** — Disable WHMCS product sales when commerce covers the SKU; keep `/api/billing` for suspend/power/reinstall on legacy lines until terminated.

## Billing API stability

Do **not** remove or rename WHMCS-facing billing endpoints during commerce rollout. Commerce adds parallel tables (`orders`, `billing_accounts`, `invoice_lines`) and nullable extensions on shared tables (`invoices.billing_account_id`, `service_billings.billing_account_id`).

Reseller recurring billing, credit ledger, and gateway webhooks continue to use `reseller_id`. Retail recurring uses `billing_account_id` on `ServiceBilling` and commerce-scoped invoices.

## Operations checklist

- Catalog and payment path are available under Admin → Store / client portal after login; `/` stays login-only.
- Configure Stripe (`STRIPE_SECRET_KEY`) for client invoice pay; otherwise clients see manual pay instructions.
- Register outbound webhooks under `/api/admin/commerce/webhooks` for automation (order.paid, invoice.paid, ticket.created).
- Use admin GDPR endpoints for export/anonymize before deleting retail users.
