<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from './ui/index.js';
  import {
    getResellerInvoice,
    listResellerInvoices,
    listResellers,
    refundResellerPayment,
  } from '../lib/api.js';
  import { formatMoney } from '../lib/resellerMoney.js';

  let invoices = [];
  let resellers = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let filters = {
    q: '',
    status: '',
    purpose: '',
    reseller_id: '',
  };
  let refundModal = { open: false, payment: null, reason: '', error: '' };

  onMount(async () => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('reseller_id')) {
      filters.reseller_id = String(params.get('reseller_id'));
    }
    try {
      resellers = await listResellers();
    } catch (_) {
      resellers = [];
    }
    await load();
    const invoiceId = Number(params.get('invoice_id'));
    if (Number.isFinite(invoiceId) && invoiceId > 0) {
      await selectInvoice(invoiceId);
    }
  });

  function filterParams() {
    const params = { limit: 200 };
    if (filters.q.trim()) params.q = filters.q.trim();
    if (filters.status) params.status = filters.status;
    if (filters.purpose) params.purpose = filters.purpose;
    if (filters.reseller_id) params.reseller_id = Number(filters.reseller_id);
    return params;
  }

  async function load() {
    loading = true;
    error = '';
    success = '';
    try {
      invoices = await listResellerInvoices(filterParams());
      if (selected) {
        selected = await getResellerInvoice(selected.id);
      }
    } catch (err) {
      error = err.message || 'Failed to load invoices';
      invoices = [];
    } finally {
      loading = false;
    }
  }

  async function selectInvoice(id) {
    working = true;
    error = '';
    try {
      selected = await getResellerInvoice(id);
    } catch (err) {
      error = err.message || 'Failed to load invoice';
    } finally {
      working = false;
    }
  }

  function purposeLabel(value) {
    return String(value || '').replaceAll('_', ' ');
  }

  function dateTime(value) {
    if (!value) return '—';
    return new Date(value).toLocaleString();
  }

  function openRefund(payment) {
    refundModal = { open: true, payment, reason: '', error: '' };
  }

  async function confirmRefund() {
    if (!refundModal.payment) return;
    working = true;
    refundModal = { ...refundModal, error: '' };
    try {
      await refundResellerPayment(refundModal.payment.id, {
        reason: refundModal.reason.trim() || null,
      });
      success = `Payment #${refundModal.payment.id} refunded.`;
      refundModal = { open: false, payment: null, reason: '', error: '' };
      if (selected) {
        selected = await getResellerInvoice(selected.id);
      }
      invoices = await listResellerInvoices(filterParams());
    } catch (err) {
      const detail = err.detail;
      refundModal = {
        ...refundModal,
        error:
          (detail && (detail.message || detail.detail)) ||
          err.message ||
          'Refund failed',
      };
    } finally {
      working = false;
    }
  }
</script>

<div class="page">
  <PageHeader title="Billing" subtitle="Reseller invoices, payments, and gateway refunds" />

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  <section class="panel filters">
    <label>Search
      <input
        type="search"
        placeholder="Invoice #, description, service id"
        bind:value={filters.q}
        on:keydown={(e) => e.key === 'Enter' && load()}
      />
    </label>
    <label>Status
      <select bind:value={filters.status}>
        <option value="">All</option>
        <option value="open">Open</option>
        <option value="overdue">Overdue</option>
        <option value="pending_action">Pending action</option>
        <option value="paid">Paid</option>
        <option value="failed">Failed</option>
        <option value="void">Void</option>
        <option value="draft">Draft</option>
      </select>
    </label>
    <label>Purpose
      <select bind:value={filters.purpose}>
        <option value="">All</option>
        <option value="credit_topup">Credit top-up</option>
        <option value="deploy_charge">Deploy charge</option>
        <option value="cycle_charge">Cycle charge</option>
        <option value="adjustment">Adjustment</option>
      </select>
    </label>
    <label>Reseller
      <select bind:value={filters.reseller_id}>
        <option value="">All</option>
        {#each resellers as reseller}
          <option value={String(reseller.id)}>{reseller.user?.username || `Reseller #${reseller.id}`}</option>
        {/each}
      </select>
    </label>
    <div class="filter-actions">
      <Button on:click={load} disabled={loading}>Apply</Button>
    </div>
  </section>

  {#if loading}
    <div class="state"><Spinner /><span>Loading invoices…</span></div>
  {:else}
    <div class="layout">
      <section class="panel list-panel">
        <h2>Invoices <span class="muted">{invoices.length}</span></h2>
        {#if invoices.length === 0}
          <p class="hint">No invoices match these filters.</p>
        {:else}
          {#each invoices as invoice (invoice.id)}
            <button
              type="button"
              class:selected={selected?.id === invoice.id}
              on:click={() => selectInvoice(invoice.id)}
            >
              <span>
                <strong>#{invoice.invoice_number}</strong>
                <small>
                  {purposeLabel(invoice.purpose)}
                  {#if invoice.reseller?.username}
                    · {invoice.reseller.username}
                  {/if}
                </small>
              </span>
              <span class="right">
                <strong>{formatMoney(invoice.amount_cents, invoice.currency)}</strong>
                <small class="status">{invoice.status}</small>
              </span>
            </button>
          {/each}
        {/if}
      </section>

      <article class="panel detail">
        {#if working && !selected}
          <div class="state"><Spinner /></div>
        {:else if !selected}
          <p class="hint">Select an invoice to inspect payments and refunds.</p>
        {:else}
          <div class="detail-heading">
            <div>
              <h2>Invoice #{selected.invoice_number}</h2>
              <p>{selected.description || purposeLabel(selected.purpose)}</p>
            </div>
            <span class="badge">{selected.status}</span>
          </div>

          <dl>
            <div>
              <dt>Total</dt>
              <dd>{formatMoney(selected.amount_cents, selected.currency)}</dd>
            </div>
            <div>
              <dt>Allocated</dt>
              <dd>{formatMoney(selected.allocated_cents, selected.currency)}</dd>
            </div>
            <div>
              <dt>Remaining</dt>
              <dd>{formatMoney(selected.remaining_cents, selected.currency)}</dd>
            </div>
            <div>
              <dt>Purpose</dt>
              <dd class="cap">{purposeLabel(selected.purpose)}</dd>
            </div>
            <div>
              <dt>Reseller</dt>
              <dd>
                {#if selected.reseller}
                  <a href="/admin/resellers/{selected.reseller.id}">
                    {selected.reseller.username || `Reseller #${selected.reseller.id}`}
                  </a>
                {:else}
                  —
                {/if}
              </dd>
            </div>
            <div>
              <dt>Service</dt>
              <dd>
                {#if selected.service}
                  <a href="/admin/services/{selected.service.id}">
                    {selected.service.name}
                    <span class="muted">({selected.service.service_type})</span>
                  </a>
                {:else if selected.service_id}
                  <a href="/admin/services/{selected.service_id}">Service #{selected.service_id}</a>
                {:else}
                  —
                {/if}
              </dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{dateTime(selected.created_at)}</dd>
            </div>
            <div>
              <dt>Paid</dt>
              <dd>{dateTime(selected.paid_at)}</dd>
            </div>
            <div>
              <dt>Due</dt>
              <dd>{dateTime(selected.due_at)}</dd>
            </div>
          </dl>

          <h3>Payments</h3>
          {#if selected.payments?.length}
            <div class="payments">
              {#each selected.payments as payment (payment.id)}
                <article>
                  <div>
                    <strong class="cap">{payment.gateway} · {payment.status}</strong>
                    <div class="muted">
                      #{payment.id}
                      {#if payment.external_ref}
                        · ref {payment.external_ref}
                      {/if}
                    </div>
                    {#if payment.failure_message}
                      <div class="fail">{payment.failure_message}</div>
                    {/if}
                    {#if payment.refunded_at}
                      <div class="muted">Refunded {dateTime(payment.refunded_at)}</div>
                    {/if}
                  </div>
                  <div class="pay-right">
                    <strong>{formatMoney(payment.amount_cents, payment.currency)}</strong>
                    <div class="muted">{dateTime(payment.processed_at || payment.created_at)}</div>
                    {#if payment.refundable}
                      <Button size="small" variant="danger" on:click={() => openRefund(payment)} disabled={working}>
                        Refund
                      </Button>
                    {:else if payment.refund_block_reason}
                      <div class="muted small">{payment.refund_block_reason}</div>
                    {/if}
                  </div>
                </article>
              {/each}
            </div>
          {:else}
            <p class="hint">No payments recorded for this invoice.</p>
          {/if}
        {/if}
      </article>
    </div>
  {/if}
</div>

{#if refundModal.open && refundModal.payment}
  <Modal title="Refund payment" onClose={() => (refundModal.open = false)}>
    <p>
      Refund {formatMoney(refundModal.payment.amount_cents, refundModal.payment.currency)}
      via <strong class="cap">{refundModal.payment.gateway}</strong>?
      This reverses gateway settlement when applicable and updates reseller credit accounting.
    </p>
    <label class="reason">Reason (optional)
      <input type="text" maxlength="500" bind:value={refundModal.reason} placeholder="Customer request, duplicate charge…" />
    </label>
    {#if refundModal.error}<Alert type="danger">{refundModal.error}</Alert>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (refundModal.open = false)} disabled={working}>Cancel</Button>
      <Button variant="danger" on:click={confirmRefund} disabled={working}>
        {working ? 'Refunding…' : 'Confirm refund'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 32px; color: var(--text-primary); }
  .panel {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 18px;
  }
  .filters {
    display: grid;
    grid-template-columns: minmax(180px, 1.4fr) repeat(3, minmax(120px, 1fr)) auto;
    gap: 12px;
    align-items: end;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 700;
  }
  input, select {
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-primary);
    background: var(--bg-secondary);
  }
  .filter-actions { display: flex; align-items: end; }
  .layout {
    display: grid;
    grid-template-columns: minmax(280px, 0.9fr) minmax(0, 1.4fr);
    gap: 18px;
    align-items: start;
  }
  .list-panel { padding: 0; overflow: hidden; }
  .list-panel h2 {
    margin: 0;
    padding: 16px 18px 12px;
    font-size: 16px;
    border-bottom: 1px solid var(--border-color);
  }
  .list-panel > button {
    width: 100%;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px;
    border: 0;
    border-bottom: 1px solid var(--border-color);
    background: transparent;
    color: var(--text-primary);
    text-align: left;
    cursor: pointer;
  }
  .list-panel > button:hover,
  .list-panel > button.selected { background: var(--bg-tertiary); }
  .list-panel small { display: block; margin-top: 4px; color: var(--text-secondary); text-transform: capitalize; }
  .right { text-align: right; }
  .status { text-transform: capitalize; }
  .detail h2 { margin: 0 0 6px; font-size: 22px; }
  .detail-heading {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    align-items: start;
  }
  .detail-heading p { margin: 0; color: var(--text-secondary); }
  .badge {
    height: fit-content;
    padding: 5px 9px;
    border-radius: 999px;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    text-transform: capitalize;
    font-size: 12px;
    font-weight: 750;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin: 22px 0;
  }
  dl div {
    padding: 11px;
    border-radius: 8px;
    background: var(--bg-secondary);
  }
  dt { color: var(--text-secondary); font-size: 12px; }
  dd { margin: 4px 0 0; font-weight: 700; }
  dd a { color: var(--accent-color); text-decoration: none; font-weight: 700; }
  dd a:hover { text-decoration: underline; }
  h3 { margin: 8px 0 12px; font-size: 15px; }
  .payments { display: grid; gap: 10px; }
  .payments article {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    padding: 12px;
    border: 1px solid var(--border-color);
    border-radius: 9px;
  }
  .pay-right { text-align: right; display: grid; gap: 6px; justify-items: end; }
  .fail { color: var(--danger-color); font-size: 12px; margin-top: 4px; }
  .hint, .muted { color: var(--text-secondary); font-size: 12px; }
  .muted.small { max-width: 180px; }
  .cap { text-transform: capitalize; }
  .state {
    min-height: 220px;
    display: grid;
    place-items: center;
    align-content: center;
    gap: 12px;
  }
  .reason { display: flex; flex-direction: column; gap: 6px; margin: 14px 0; }
  @media (max-width: 960px) {
    .filters { grid-template-columns: 1fr 1fr; }
    .layout { grid-template-columns: 1fr; }
    dl { grid-template-columns: 1fr 1fr; }
  }
</style>
