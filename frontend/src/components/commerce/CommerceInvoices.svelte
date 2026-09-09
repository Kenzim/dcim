<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import {
    adminCommerceDownloadInvoicePdf,
    adminCommerceGetInvoice,
    adminCommerceListInvoices,
    adminCommerceMarkInvoicePaid,
    adminCommerceVoidInvoice,
    downloadPdfBlob,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  let invoices = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let statusFilter = '';
  let markPaidModal = { open: false, reason: '' };
  let voidModal = { open: false };

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      invoices = await adminCommerceListInvoices(statusFilter ? { status: statusFilter } : {});
      if (selected) selected = await adminCommerceGetInvoice(selected.id);
    } catch (err) {
      error = err.message || 'Failed to load invoices';
      invoices = [];
    } finally {
      loading = false;
    }
  }

  async function showInvoice(id) {
    working = true;
    try {
      selected = await adminCommerceGetInvoice(id);
    } catch (err) {
      error = err.message || 'Failed to load invoice';
    } finally {
      working = false;
    }
  }

  async function downloadPdf() {
    if (!selected) return;
    working = true;
    error = '';
    try {
      const blob = await adminCommerceDownloadInvoicePdf(selected.id);
      downloadPdfBlob(blob, `invoice-${selected.invoice_number}.pdf`);
    } catch (err) {
      error = err.message || 'PDF download failed';
    } finally {
      working = false;
    }
  }

  async function confirmMarkPaid() {
    if (!selected || !markPaidModal.reason.trim()) {
      markPaidModal = { ...markPaidModal, error: 'Reason is required.' };
      return;
    }
    working = true;
    error = '';
    try {
      selected = await adminCommerceMarkInvoicePaid(selected.id, markPaidModal.reason.trim());
      success = 'Invoice marked paid.';
      markPaidModal = { open: false, reason: '' };
      await load();
    } catch (err) {
      error = err.message || 'Failed to mark paid';
    } finally {
      working = false;
    }
  }

  async function confirmVoid() {
    if (!selected) return;
    working = true;
    error = '';
    try {
      selected = await adminCommerceVoidInvoice(selected.id);
      success = 'Invoice voided.';
      voidModal = { open: false };
      await load();
    } catch (err) {
      error = err.message || 'Failed to void invoice';
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Commerce Invoices" />

<div class="page">
  <div class="filters">
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        <option value="open">Open</option>
        <option value="overdue">Overdue</option>
        <option value="paid">Paid</option>
        <option value="void">Void</option>
      </select>
    </label>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    <div class="layout">
      <div class="panel list">
        {#if invoices.length === 0}
          <p class="empty">No invoices match this filter.</p>
        {:else}
          {#each invoices as row (row.id)}
            <button class:selected={selected?.id === row.id} on:click={() => showInvoice(row.id)}>
              <span><strong>#{row.invoice_number}</strong><small>{row.status}</small></span>
              <span>{formatMoney(row.amount_cents, row.currency)}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="panel detail">
        {#if working && !selected}<Spinner />{:else if !selected}
          <p class="empty">Choose an invoice to view details.</p>
        {:else}
          <div class="head">
            <div><h2>Invoice #{selected.invoice_number}</h2><p>{selected.description || selected.purpose?.replaceAll('_', ' ') || 'Commerce invoice'}</p></div>
            <span class="status">{selected.status}</span>
          </div>
          <dl>
            <div><dt>Total</dt><dd>{formatMoney(selected.amount_cents, selected.currency)}</dd></div>
            <div><dt>Remaining</dt><dd>{formatMoney(selected.remaining_cents ?? selected.amount_cents, selected.currency)}</dd></div>
            <div><dt>Account</dt><dd>#{selected.billing_account_id}</dd></div>
            <div><dt>Created</dt><dd>{new Date(selected.created_at).toLocaleString()}</dd></div>
          </dl>
          <div class="actions">
            <Button variant="secondary" on:click={downloadPdf} disabled={working}>Download PDF</Button>
            {#if selected.status !== 'paid' && selected.status !== 'void'}
              <Button on:click={() => (markPaidModal = { open: true, reason: '' })}>Mark paid</Button>
              <Button variant="danger" on:click={() => (voidModal = { open: true })}>Void</Button>
            {/if}
          </div>
          {#if selected.payments?.length}
            <h3>Payments</h3>
            <ul>
              {#each selected.payments as payment}
                <li><span>{payment.gateway} · {payment.status}</span><strong>{formatMoney(payment.amount_cents, payment.currency)}</strong></li>
              {/each}
            </ul>
          {/if}
        {/if}
      </article>
    </div>
  {/if}
</div>

{#if markPaidModal.open}
  <Modal title="Mark invoice paid" onClose={() => (markPaidModal = { open: false, reason: '' })}>
    <p>Record manual payment for invoice #{selected?.invoice_number}.</p>
    <label>Reason <textarea bind:value={markPaidModal.reason} rows="3" placeholder="e.g. Wire transfer ref 12345"></textarea></label>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (markPaidModal = { open: false, reason: '' })}>Cancel</Button>
      <Button on:click={confirmMarkPaid} disabled={working}>Mark paid</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if voidModal.open}
  <Modal title="Void invoice?" onClose={() => (voidModal = { open: false })}>
    <p>Void invoice #{selected?.invoice_number}? This cannot be undone.</p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (voidModal = { open: false })}>Cancel</Button>
      <Button variant="danger" on:click={confirmVoid} disabled={working}>Void invoice</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 28px 32px 36px; }
  .filters { margin-bottom: 16px; }
  .filters label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  select { margin-left: 8px; padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 220px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(280px, .85fr) minmax(0, 1.15fr); gap: 18px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
  .list > button.selected, .list > button:hover { background: var(--portal-accent-soft); }
  .list small { display: block; margin-top: 3px; color: var(--text-tertiary); text-transform: capitalize; }
  .detail { padding: 20px; }
  .head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .head h2 { margin: 0 0 4px; }
  .head p { margin: 0; color: var(--text-secondary); text-transform: capitalize; }
  .status { padding: 5px 10px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); font-size: 12px; font-weight: 700; text-transform: capitalize; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0 0 16px; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); }
  dd { margin: 4px 0 0; font-weight: 700; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  h3 { margin: 16px 0 8px; font-size: 15px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border-color); }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  textarea { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
</style>
