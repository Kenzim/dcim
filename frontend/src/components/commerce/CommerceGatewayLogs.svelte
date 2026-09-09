<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Spinner } from '../ui/index.js';
  import { adminCommerceListGatewayLogs } from '../../lib/api.js';

  let rows = [];
  let loading = true;
  let error = '';
  let gatewayFilter = '';
  let invoiceFilter = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    const filters = {};
    if (gatewayFilter.trim()) filters.gateway = gatewayFilter.trim();
    if (invoiceFilter.trim()) filters.invoice_id = Number(invoiceFilter);
    try {
      rows = await adminCommerceListGatewayLogs(filters);
    } catch (err) {
      error = err.message || 'Failed to load gateway logs';
      rows = [];
    } finally {
      loading = false;
    }
  }
</script>

<PageHeader title="Gateway Logs" />

<div class="page">
  <div class="filters">
    <label>Gateway <input bind:value={gatewayFilter} placeholder="stripe" on:keydown={(e) => e.key === 'Enter' && load()} /></label>
    <label>Invoice ID <input bind:value={invoiceFilter} type="number" on:keydown={(e) => e.key === 'Enter' && load()} /></label>
    <button class="btn" on:click={load}>Apply</button>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if rows.length === 0}
    <p class="empty">No gateway logs found.</p>
  {:else}
    <div class="panel">
      {#each rows as row (row.id)}
        <article class="log">
          <header>
            <strong>{row.gateway}</strong>
            <span>{row.direction} · {row.operation}</span>
            <time>{new Date(row.created_at).toLocaleString()}</time>
          </header>
          <p>HTTP {row.http_status ?? '—'} · Invoice {row.invoice_id ?? '—'} · Payment {row.payment_id ?? '—'}</p>
          {#if row.request_summary}<pre>{JSON.stringify(row.request_summary, null, 2)}</pre>{/if}
          {#if row.response_summary}<pre class="resp">{JSON.stringify(row.response_summary, null, 2)}</pre>{/if}
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { display: flex; gap: 12px; align-items: end; margin-bottom: 16px; flex-wrap: wrap; }
  .filters label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input { padding: 8px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .btn { padding: 9px 14px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); cursor: pointer; font-weight: 600; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { display: grid; gap: 12px; }
  .log { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); padding: 14px 16px; }
  header { display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; margin-bottom: 6px; }
  header span, header time { font-size: 12px; color: var(--text-tertiary); }
  p { margin: 0 0 8px; font-size: 13px; color: var(--text-secondary); }
  pre { margin: 8px 0 0; padding: 10px; border-radius: 8px; background: var(--bg-secondary); font-size: 11px; overflow: auto; max-height: 180px; }
  pre.resp { border-left: 3px solid var(--portal-accent); }
  .empty { color: var(--text-tertiary); padding: 24px; }
</style>
