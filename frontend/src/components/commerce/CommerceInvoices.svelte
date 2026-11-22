<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Spinner } from '../ui/index.js';
  import { formatMoney } from '../../lib/resellerMoney.js';
  import { listInvoices } from '../../lib/billingAdminApi.js';
  import AdminInvoiceDetail from '../billing/admin/AdminInvoiceDetail.svelte';

  const STATUSES = ['draft', 'open', 'pending_action', 'partially_paid', 'overdue', 'collections', 'paid', 'refunded', 'void', 'failed'];

  let invoices = [];
  let selectedId = null;
  let loading = true;
  let error = '';
  let statusFilter = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      invoices = await listInvoices({ limit: 200, ...(statusFilter ? { status: statusFilter } : {}) });
    } catch (err) {
      error = err.message || 'Failed to load invoices';
      invoices = [];
    } finally {
      loading = false;
    }
  }

  async function refreshList() {
    try {
      invoices = await listInvoices({ limit: 200, ...(statusFilter ? { status: statusFilter } : {}) });
    } catch (_) {
      // The detail view shows its own errors; a stale list is acceptable.
    }
  }
</script>

<PageHeader title="Commerce Invoices" />

<div class="page">
  <div class="filters">
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        {#each STATUSES as status}<option value={status}>{status.replaceAll('_', ' ')}</option>{/each}
      </select>
    </label>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    <div class="layout">
      <div class="panel list">
        {#if invoices.length === 0}
          <p class="empty">No invoices match this filter.</p>
        {:else}
          {#each invoices as row (row.id)}
            <button class:selected={selectedId === row.id} on:click={() => (selectedId = row.id)}>
              <span><strong>{row.display_number || `#${row.invoice_number}`}</strong><small>{(row.status_detail || row.status).replaceAll('_', ' ')}</small></span>
              <span>{formatMoney(row.amount_cents, row.currency)}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="detail">
        {#if !selectedId}
          <p class="empty panel">Choose an invoice to view details.</p>
        {:else}
          <AdminInvoiceDetail invoiceId={selectedId} on:changed={refreshList} />
        {/if}
      </article>
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { margin-bottom: 16px; }
  .filters label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  select { margin-left: 8px; padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); text-transform: capitalize; }
  .state { min-height: 220px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(260px, .55fr) minmax(0, 1.45fr); gap: 18px; align-items: start; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
  .list > button.selected, .list > button:hover { background: var(--portal-accent-soft); }
  .list small { display: block; margin-top: 3px; color: var(--text-tertiary); text-transform: capitalize; }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  @media (max-width: 980px) { .layout { grid-template-columns: 1fr; } }
</style>
