<script>
  import { onMount } from 'svelte';
  import { navigate } from '../../lib/router.js';
  import { Alert, Button, Spinner, Tabs } from '../ui/index.js';
  import InvoiceStatusBadge from '../billing/InvoiceStatusBadge.svelte';
  import ClientInvoiceCredit from '../billing/client/ClientInvoiceCredit.svelte';
  import ClientNotes from '../billing/client/ClientNotes.svelte';
  import { clientGetCredit } from '../../lib/billingClientApi.js';
  import { invoiceStatusLabel, invoiceStatusValue } from '../../lib/billingFormat.js';
  import {
    clientCommerceDownloadInvoicePdf,
    clientCommerceGetInvoice,
    clientCommerceListInvoices,
    clientCommerceListOrders,
    downloadPdfBlob,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  /** Disable pay actions during admin impersonation. */
  export let impersonating = false;
  /** When set, open this invoice on load. */
  export let invoiceId = null;
  /** 'invoices' | 'orders' | 'notes' */
  export let initialTab = 'invoices';

  let tab = initialTab;
  /** The `/credit` summary while account credit is on, else null. */
  let credit = null;
  let success = '';
  let invoices = [];
  let orders = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let statusFilter = '';

  $: tab = initialTab;

  onMount(async () => {
    await load();
    if (invoiceId) await showInvoice(Number(invoiceId));
  });

  async function load() {
    loading = true;
    error = '';
    const filters = statusFilter ? { status: statusFilter } : {};
    try {
      [invoices, orders, credit] = await Promise.all([
        clientCommerceListInvoices(filters).catch((e) => {
          if (e.status === 503) return [];
          throw e;
        }),
        clientCommerceListOrders().catch((e) => {
          if (e.status === 503) return [];
          throw e;
        }),
        clientGetCredit(1)
          .then((summary) => (summary.enabled ? summary : null))
          .catch(() => null),
      ]);
    } catch (err) {
      error = err.message || 'Failed to load billing data';
    } finally {
      loading = false;
    }
  }

  async function creditApplied(event) {
    const { invoice, balance_cents: balance, applied_cents: applied } = event.detail;
    selected = { type: 'invoice', data: invoice };
    if (credit) credit = { ...credit, balance_cents: balance, available_cents: Math.max(0, balance) };
    success = applied > 0
      ? `${formatMoney(applied, invoice.currency)} of account credit applied.`
      : 'No credit needed to be applied.';
    const filters = statusFilter ? { status: statusFilter } : {};
    invoices = await clientCommerceListInvoices(filters).catch(() => invoices);
  }

  async function showInvoice(id) {
    working = true;
    success = '';
    tab = 'invoices';
    try {
      selected = { type: 'invoice', data: await clientCommerceGetInvoice(id) };
      navigate(`/client/billing/${id}`);
    } catch (err) {
      error = err.message || 'Failed to load invoice';
    } finally {
      working = false;
    }
  }

  async function downloadPdf() {
    if (!selected || selected.type !== 'invoice') return;
    working = true;
    try {
      const blob = await clientCommerceDownloadInvoicePdf(selected.data.id);
      downloadPdfBlob(blob, `invoice-${selected.data.invoice_number}.pdf`);
    } catch (err) {
      error = err.message || 'PDF download failed';
    } finally {
      working = false;
    }
  }

  function openPay() {
    if (!selected || selected.type !== 'invoice') return;
    navigate(`/client/billing/${selected.data.id}`);
  }

  function clearSelection() {
    selected = null;
    navigate('/client/billing');
  }
</script>

<section aria-labelledby="billing-title">
  <div class="page-head">
    <div>
      <h1 id="billing-title">Billing</h1>
      <p class="sub">Invoices, orders, and payment history.</p>
    </div>
  </div>

  <Tabs
    tabs={[
      { id: 'invoices', label: 'Invoices' },
      { id: 'orders', label: 'Orders' },
      { id: 'notes', label: 'Credit and debit notes' },
    ]}
    bind:active={tab}
    on:change={(e) => {
      tab = e.detail.id;
      selected = null;
      success = '';
      if (tab === 'orders') navigate('/client/orders');
      else if (tab === 'notes') navigate('/client/notes');
      else navigate('/client/billing');
    }}
  />

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if tab === 'notes'}
    <ClientNotes />
  {:else if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if tab === 'invoices'}
    {#if credit}
      <p class="credit-strip">
        Account credit: <strong>{formatMoney(credit.available_cents, credit.currency)}</strong> available.
        <a href="/client/account/credit">Manage credit</a>
      </p>
    {/if}
    <div class="layout">
      <div class="panel list">
        <label class="filter">Status
          <select bind:value={statusFilter} on:change={load}>
            <option value="">All</option>
            <option value="open">Open</option>
            <option value="overdue">Overdue</option>
            <option value="paid">Paid</option>
          </select>
        </label>
        {#if invoices.length === 0}
          <p class="empty">No invoices yet. <a href="/client/checkout">Browse products</a> to place an order.</p>
        {:else}
          {#each invoices as row (row.id)}
            <button class:selected={selected?.data?.id === row.id} on:click={() => showInvoice(row.id)}>
              <span><strong>#{row.invoice_number}</strong><small>{invoiceStatusLabel(invoiceStatusValue(row))}</small></span>
              <span>{formatMoney(row.amount_cents, row.currency)}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="panel detail">
        {#if working && !selected}<Spinner />{:else if !selected}
          <p class="empty">Select an invoice to view details or download a PDF.</p>
        {:else}
          <div class="head">
            <div><h2>Invoice #{selected.data.invoice_number}</h2><p>{selected.data.description || 'Commerce invoice'}</p></div>
            <button class="link" on:click={clearSelection}>Back to list</button>
          </div>
          <dl>
            <div><dt>Total</dt><dd>{formatMoney(selected.data.amount_cents, selected.data.currency)}</dd></div>
            <div><dt>Status</dt><dd><InvoiceStatusBadge invoice={selected.data} /></dd></div>
            <div><dt>Due</dt><dd>{selected.data.due_at ? new Date(selected.data.due_at).toLocaleDateString() : '—'}</dd></div>
            <div><dt>Created</dt><dd>{new Date(selected.data.created_at).toLocaleString()}</dd></div>
          </dl>
          <ClientInvoiceCredit invoice={selected.data} {credit} {impersonating} on:applied={creditApplied} />
          <div class="actions">
            <Button variant="secondary" on:click={downloadPdf} disabled={working}>Download PDF</Button>
            {#if ['open', 'overdue', 'pending_action'].includes(selected.data.status)}
              {#if impersonating}
                <p class="hint">Payment is disabled while viewing as this client.</p>
              {:else}
                <Button on:click={openPay} disabled={working}>Pay invoice</Button>
                <p class="hint">Payment gateway integration coming soon — use this link to return to the invoice.</p>
              {/if}
            {/if}
          </div>
        {/if}
      </article>
    </div>
  {:else}
    <div class="panel orders">
      {#if orders.length === 0}
        <p class="empty">No orders yet. <a href="/client/checkout">Order a service</a> to get started.</p>
      {:else}
        <table>
          <thead><tr><th>Order</th><th>Status</th><th>Total</th><th>Created</th></tr></thead>
          <tbody>
            {#each orders as row (row.id)}
              <tr>
                <td>#{row.order_number}</td>
                <td>{row.status.replaceAll('_', ' ')}</td>
                <td>{formatMoney(row.total_cents, row.currency)}</td>
                <td>{new Date(row.created_at).toLocaleString()}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}
</section>

<style>
  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-head h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 14px;
    color: var(--text-tertiary);
  }
  .credit-strip { margin: 16px 0 0; font-size: 14px; color: var(--text-secondary); }
  .credit-strip a { margin-left: 8px; color: var(--portal-accent); font-weight: 600; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(280px, .85fr) minmax(0, 1.15fr); gap: 18px; margin-top: 16px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); box-shadow: var(--shadow-sm); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
  .list > button.selected, .list > button:hover { background: var(--portal-accent-soft); }
  .list small { display: block; margin-top: 3px; color: var(--text-tertiary); text-transform: capitalize; }
  .filter { display: block; padding: 12px 14px; font-size: 13px; font-weight: 600; border-bottom: 1px solid var(--border-color); }
  select { margin-left: 8px; padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .detail { padding: 20px; }
  .head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .head h2 { margin: 0 0 4px; font-size: 20px; }
  .head p { margin: 0; color: var(--text-secondary); }
  .link { background: none; border: none; color: var(--portal-accent); cursor: pointer; font-weight: 600; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0 0 16px; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; }
  .actions { display: grid; gap: 8px; }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .orders { padding: 16px; overflow: auto; margin-top: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  .empty a { color: var(--portal-accent); }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
</style>
