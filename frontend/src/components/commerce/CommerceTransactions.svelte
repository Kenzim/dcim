<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Spinner } from '../ui/index.js';
  import { adminCommerceListTransactions } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  let rows = [];
  let loading = true;
  let error = '';
  let statusFilter = '';
  let gatewayFilter = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    const filters = {};
    if (statusFilter) filters.status = statusFilter;
    if (gatewayFilter.trim()) filters.gateway = gatewayFilter.trim();
    try {
      rows = await adminCommerceListTransactions(filters);
    } catch (err) {
      error = err.message || 'Failed to load transactions';
      rows = [];
    } finally {
      loading = false;
    }
  }
</script>

<PageHeader title="Commerce Transactions" />

<div class="page">
  <div class="filters">
    <label>Gateway <input type="search" bind:value={gatewayFilter} placeholder="stripe, paypal…" on:keydown={(e) => e.key === 'Enter' && load()} /></label>
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        <option value="pending">Pending</option>
        <option value="succeeded">Succeeded</option>
        <option value="failed">Failed</option>
        <option value="refunded">Refunded</option>
      </select>
    </label>
    <button class="btn" on:click={load}>Refresh</button>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if rows.length === 0}
    <p class="empty">No transactions match this filter.</p>
  {:else}
    <div class="panel">
      <table>
        <thead>
          <tr><th>Time</th><th>Gateway</th><th>Status</th><th>Amount</th><th>Invoice</th><th>Account</th></tr>
        </thead>
        <tbody>
          {#each rows as row (row.id)}
            <tr>
              <td>{new Date(row.created_at).toLocaleString()}</td>
              <td>{row.gateway}</td>
              <td>{row.status}</td>
              <td>{formatMoney(row.amount_cents, row.currency)}</td>
              <td>{row.invoice_id ? `#${row.invoice_id}` : '—'}</td>
              <td>{row.billing_account_id ? `#${row.billing_account_id}` : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { display: flex; gap: 14px; flex-wrap: wrap; align-items: end; margin-bottom: 16px; }
  .filters label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input, select { padding: 8px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .btn { padding: 9px 14px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); cursor: pointer; font-weight: 600; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .empty { color: var(--text-tertiary); padding: 24px; }
</style>
