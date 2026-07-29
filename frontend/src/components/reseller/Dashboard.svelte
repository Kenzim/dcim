<script>
  import { onMount } from 'svelte';
  import { getResellerDashboard } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';
  import { Alert, Spinner } from '../ui/index.js';

  let dashboard;
  let loading = true;
  let error = '';

  onMount(async () => {
    try {
      dashboard = await getResellerDashboard();
    } catch (err) {
      error = err.message || 'Dashboard could not be loaded.';
    } finally {
      loading = false;
    }
  });
</script>

<section aria-labelledby="reseller-dashboard-title">
  <div class="page-heading">
    <div>
      <p class="eyebrow">Reseller control panel</p>
      <h1 id="reseller-dashboard-title">Dashboard</h1>
      <p>Billing, client services, and stock usage at a glance.</p>
    </div>
    <a class="primary-action" href="/reseller/top-up">Add funds</a>
  </div>

  {#if loading}
    <div class="state"><Spinner /><span>Loading account summary…</span></div>
  {:else if error}
    <Alert type="danger">{error}</Alert>
  {:else if dashboard}
    {#if dashboard.billing_hold}
      <Alert type="warning">
        <strong>Billing hold:</strong> {dashboard.billing_hold_reason || 'New deployments are currently blocked.'}
      </Alert>
    {/if}
    {#if dashboard.quota_warning}
      <Alert type="warning">
        {dashboard.quota_warning_count} stock quota {dashboard.quota_warning_count === 1 ? 'is' : 'are'} at or above 80% usage.
        <a href="/reseller/stock">Review stock</a>
      </Alert>
    {/if}
    <div class="stat-grid">
      <article class="stat featured"><span>Credit balance</span><strong>{formatMoney(dashboard.balance_cents)}</strong><small>{dashboard.charge_preference.replace('_', ' ')}</small></article>
      <article class="stat"><span>Monthly wholesale cost</span><strong>{formatMoney(dashboard.monthly_cost_cents)}</strong><small>Active recurring services</small></article>
      <article class="stat"><span>Services</span><strong>{dashboard.service_count}</strong><a href="/reseller/services">View services</a></article>
      <article class="stat"><span>Clients</span><strong>{dashboard.client_count}</strong><a href="/reseller/clients">View clients</a></article>
      <article class="stat"><span>Open invoices</span><strong>{dashboard.open_invoice_count}</strong><a href="/reseller/invoices">View invoices</a></article>
      <article class="stat"><span>Quota warnings</span><strong>{dashboard.quota_warning_count}</strong><a href="/reseller/stock">View usage</a></article>
    </div>
    <div class="policy">
      <div><span>Nonpayment policy</span><strong>{dashboard.nonpayment_policy.replaceAll('_', ' ')}</strong></div>
      <div><span>Payment gateways</span><strong>{[dashboard.stripe_enabled && 'Stripe', dashboard.paypal_enabled && 'PayPal', dashboard.usdt_enabled && 'USDT'].filter(Boolean).join(', ') || 'None configured'}</strong></div>
    </div>
  {/if}
</section>

<style>
  .page-heading { display: flex; justify-content: space-between; gap: 20px; align-items: flex-end; margin-bottom: 24px; }
  h1 { margin: 2px 0 6px; font-size: clamp(28px, 4vw, 38px); }
  p { margin: 0; color: var(--text-secondary); }
  .eyebrow { color: var(--portal-accent); text-transform: uppercase; letter-spacing: .1em; font-size: 11px; font-weight: 800; }
  .primary-action { padding: 11px 18px; border-radius: 9px; background: var(--portal-accent); color: var(--portal-accent-contrast); text-decoration: none; font-weight: 750; white-space: nowrap; }
  .state { min-height: 240px; display: grid; place-items: center; align-content: center; gap: 12px; color: var(--text-secondary); }
  .stat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
  .stat { min-height: 144px; padding: 20px; display: flex; flex-direction: column; gap: 8px; border: 1px solid var(--portal-card-border); border-radius: var(--radius-lg); background: var(--portal-card-bg); box-shadow: var(--portal-card-shadow); }
  .stat.featured { background: linear-gradient(135deg, var(--portal-hero-from), var(--portal-hero-to)); color: var(--portal-hero-text); border: 0; }
  .stat span { color: var(--text-tertiary); font-size: 13px; font-weight: 700; }
  .stat.featured span, .stat.featured small { color: var(--portal-hero-muted); }
  .stat strong { font-size: 28px; overflow-wrap: anywhere; }
  .stat a { margin-top: auto; color: var(--portal-accent); font-size: 13px; font-weight: 700; text-decoration: none; }
  .stat small { color: var(--text-tertiary); text-transform: capitalize; }
  .policy { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 28px; padding: 18px 20px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }
  .policy div { display: grid; gap: 4px; }
  .policy span { color: var(--text-tertiary); font-size: 12px; }
  .policy strong { text-transform: capitalize; }
  @media (max-width: 800px) { .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 520px) { .page-heading { align-items: flex-start; flex-direction: column; } .stat-grid { grid-template-columns: 1fr; } }
</style>
