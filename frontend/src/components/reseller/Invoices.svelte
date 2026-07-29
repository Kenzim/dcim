<script>
  import { onMount } from 'svelte';
  import { loadStripe } from '@stripe/stripe-js';
  import {
    getResellerPanelInvoice,
    getResellerPaymentConfig,
    listResellerPanelInvoices,
    listResellerPanelPaymentMethods,
    payResellerPanelInvoice,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';
  import { Alert, Spinner } from '../ui/index.js';

  let invoices = [];
  let selected;
  let methods = [];
  let config;
  let statusFilter = '';
  let methodId = '';
  let loading = true;
  let working = false;
  let error = '';
  let success = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    const errors = [];
    try {
      invoices = await listResellerPanelInvoices(statusFilter ? { status: statusFilter } : {});
    } catch (err) {
      invoices = [];
      errors.push(err.message || 'Invoices could not be loaded.');
    }
    try {
      methods = await listResellerPanelPaymentMethods();
      methodId = String(methods.find((method) => method.enabled)?.id || '');
    } catch (err) {
      methods = [];
      methodId = '';
      errors.push(err.message || 'Payment methods could not be loaded.');
    }
    try {
      config = await getResellerPaymentConfig();
    } catch (err) {
      config = null;
      errors.push(err.message || 'Payment config could not be loaded.');
    }
    if (selected) {
      try {
        selected = await getResellerPanelInvoice(selected.id);
      } catch (err) {
        errors.push(err.message || 'Selected invoice could not be refreshed.');
      }
    }
    error = errors.filter(Boolean).join(' ') || '';
    loading = false;
  }

  async function showInvoice(id) {
    working = true;
    try {
      selected = await getResellerPanelInvoice(id);
    } catch (err) {
      error = err.message || 'Invoice could not be loaded.';
    } finally {
      working = false;
    }
  }

  async function pay() {
    if (!selected || !methodId) return;
    working = true;
    error = '';
    let clientSecret = null;
    try {
      selected = await payResellerPanelInvoice(selected.id, Number(methodId));
      success = 'Invoice paid.';
    } catch (err) {
      if (err.status !== 402 || !err.detail?.pending_action || !err.detail?.client_secret) {
        error = err.message || 'Invoice payment failed.';
        return;
      }
      clientSecret = err.detail.client_secret;
      if (!config?.stripe?.enabled) {
        error = 'Card authentication is required, but Stripe is not available.';
        return;
      }
      const stripe = await loadStripe(config.stripe.publishable_key);
      const result = await stripe?.confirmCardPayment(clientSecret);
      if (!result || result.error) {
        error = result?.error?.message || 'Card authentication failed.';
        return;
      }
      selected = await getResellerPanelInvoice(selected.id);
      success = 'Card authentication completed. Payment confirmation may take a moment.';
    } finally {
      clientSecret = null;
      working = false;
      invoices = await listResellerPanelInvoices(statusFilter ? { status: statusFilter } : {});
    }
  }
</script>

<section aria-labelledby="invoices-title">
  <div class="heading">
    <div><h1 id="invoices-title">Invoices</h1><p>Review allocations, gateway payments, and outstanding balances.</p></div>
    <label>Status <select bind:value={statusFilter} on:change={load}><option value="">All</option><option value="open">Open</option><option value="pending_action">Pending action</option><option value="overdue">Overdue</option><option value="paid">Paid</option><option value="void">Void</option></select></label>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading invoices…</span></div>
  {:else}
    <div class="layout">
      <div class="panel list-panel">
        {#if invoices.length === 0}
          <p class="empty">No invoices match this filter.</p>
        {:else}
          {#each invoices as invoice (invoice.id)}
            <button class:selected={selected?.id === invoice.id} on:click={() => showInvoice(invoice.id)}>
              <span><strong>#{invoice.invoice_number}</strong><small>{invoice.purpose.replaceAll('_', ' ')}</small></span>
              <span class="right"><strong>{formatMoney(invoice.amount_cents, invoice.currency)}</strong><small>{invoice.status}</small></span>
            </button>
          {/each}
        {/if}
      </div>
      <article class="panel detail">
        {#if working && !selected}<Spinner />{:else if !selected}
          <p class="empty">Choose an invoice to view details.</p>
        {:else}
          <div class="detail-heading"><div><h2>Invoice #{selected.invoice_number}</h2><p>{selected.description || selected.purpose.replaceAll('_', ' ')}</p></div><span>{selected.status}</span></div>
          <dl>
            <div><dt>Total</dt><dd>{formatMoney(selected.amount_cents, selected.currency)}</dd></div>
            <div><dt>Allocated</dt><dd>{formatMoney(selected.allocated_cents, selected.currency)}</dd></div>
            <div><dt>Remaining</dt><dd>{formatMoney(selected.remaining_cents, selected.currency)}</dd></div>
            <div><dt>Created</dt><dd>{new Date(selected.created_at).toLocaleString()}</dd></div>
          </dl>
          <h3>Payments</h3>
          {#if selected.payments?.length}
            <ul>{#each selected.payments as payment}<li><span>{payment.gateway} · {payment.status}</span><strong>{formatMoney(payment.amount_cents, payment.currency)}</strong></li>{/each}</ul>
          {:else}<p class="empty">No gateway payments recorded.</p>{/if}
          {#if ['open', 'overdue', 'pending_action'].includes(selected.status)}
            <div class="pay-box">
              {#if methods.filter((method) => method.enabled).length}
                <label for="invoice-method">Payment method</label>
                <select id="invoice-method" bind:value={methodId}>{#each methods.filter((method) => method.enabled) as method}<option value={String(method.id)}>{method.label || `${method.provider} ${method.last4 ? `•••• ${method.last4}` : ''}`}</option>{/each}</select>
                <button class="primary" on:click={pay} disabled={working || !methodId}>{working ? 'Paying…' : 'Pay invoice'}</button>
              {:else}
                <p class="empty">Add a payment method before paying this invoice.</p>
                <a href="/reseller/payment-methods">Add payment method</a>
              {/if}
            </div>
          {/if}
        {/if}
      </article>
    </div>
  {/if}
</section>

<style>
  .heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 22px; }h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.heading label { font-size: 13px; color: var(--text-secondary); }
  select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }.heading select { margin-left: 6px; }
  .state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }.layout { display: grid; grid-template-columns: minmax(300px, .85fr) minmax(0, 1.35fr); gap: 18px; align-items: start; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }.list-panel { overflow: hidden; }
  .list-panel > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 14px 16px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }
  .list-panel > button:hover, .list-panel > button.selected { background: var(--portal-accent-soft); }.list-panel small { display: block; margin-top: 4px; color: var(--text-tertiary); text-transform: capitalize; }.right { text-align: right; }
  .detail { padding: 22px; }.detail-heading { display: flex; justify-content: space-between; gap: 14px; }.detail-heading h2 { margin: 0 0 5px; }.detail-heading p { margin: 0; color: var(--text-secondary); text-transform: capitalize; }.detail-heading > span { height: fit-content; padding: 5px 9px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); text-transform: capitalize; font-size: 12px; font-weight: 750; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin: 22px 0; }dl div { padding: 11px; border-radius: 8px; background: var(--bg-secondary); }dt { color: var(--text-tertiary); font-size: 12px; }dd { margin: 4px 0 0; font-weight: 750; }h3 { margin: 20px 0 10px; font-size: 15px; }
  ul { list-style: none; margin: 0; padding: 0; }li { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border-color); text-transform: capitalize; }.pay-box { display: grid; gap: 8px; margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--border-color); }.pay-box label { font-size: 13px; font-weight: 700; }.primary { justify-self: start; padding: 10px 15px; border: 0; border-radius: 8px; background: var(--portal-accent); color: var(--portal-accent-contrast); font-weight: 750; cursor: pointer; }.empty { padding: 18px; color: var(--text-tertiary); }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; }.heading { align-items: flex-start; flex-direction: column; } }
</style>
