<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Spinner } from '../ui/index.js';
  import { adminCommerceListAuditEvents } from '../../lib/api.js';

  let events = [];
  let loading = true;
  let error = '';
  let actionFilter = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      events = await adminCommerceListAuditEvents(actionFilter ? { action: actionFilter } : {});
    } catch (err) {
      error = err.message || 'Failed to load audit events';
      events = [];
    } finally {
      loading = false;
    }
  }
</script>

<PageHeader title="Commerce Audit" />

<div class="page">
  <div class="filters">
    <label>Action filter
      <input type="search" bind:value={actionFilter} placeholder="e.g. order.accept" on:keydown={(e) => e.key === 'Enter' && load()} />
    </label>
    <button class="btn" on:click={load}>Apply</button>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if events.length === 0}
    <p class="empty">No audit events match this filter.</p>
  {:else}
    <div class="panel">
      <table>
        <thead>
          <tr><th>Time</th><th>Action</th><th>Resource</th><th>Account</th><th>IP</th></tr>
        </thead>
        <tbody>
          {#each events as row (row.id)}
            <tr>
              <td>{new Date(row.created_at).toLocaleString()}</td>
              <td><code>{row.action}</code></td>
              <td>{row.resource_type}{row.resource_id ? ` #${row.resource_id}` : ''}</td>
              <td>{row.billing_account_id ?? '—'}</td>
              <td>{row.ip ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { display: flex; gap: 12px; align-items: end; margin-bottom: 18px; }
  .filters label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .btn { padding: 9px 14px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); cursor: pointer; font-weight: 600; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .empty { color: var(--text-tertiary); padding: 24px; }
</style>
