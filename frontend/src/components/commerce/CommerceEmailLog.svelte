<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import { adminCommerceListEmailMessages, adminCommerceRetryEmailMessage } from '../../lib/api.js';

  let rows = [];
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let statusFilter = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      rows = await adminCommerceListEmailMessages(statusFilter ? { status: statusFilter } : {});
    } catch (err) {
      error = err.message || 'Failed to load email log';
      rows = [];
    } finally {
      loading = false;
    }
  }

  async function retry(id) {
    working = true;
    error = '';
    success = '';
    try {
      await adminCommerceRetryEmailMessage(id);
      success = 'Email queued for retry.';
      await load();
    } catch (err) {
      error = err.message || 'Retry failed';
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Email Log" />

<div class="page">
  <div class="filters">
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        <option value="pending">Pending</option>
        <option value="sent">Sent</option>
        <option value="failed">Failed</option>
      </select>
    </label>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if rows.length === 0}
    <p class="empty">No email messages logged yet.</p>
  {:else}
    <div class="panel">
      <table>
        <thead>
          <tr><th>Created</th><th>To</th><th>Subject</th><th>Event</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {#each rows as row (row.id)}
            <tr>
              <td>{new Date(row.created_at).toLocaleString()}</td>
              <td>{row.to_address}</td>
              <td>{row.subject}</td>
              <td><code>{row.event || row.template_key || '—'}</code></td>
              <td>{row.status}{row.error ? ` (${row.error})` : ''}</td>
              <td>
                {#if row.status === 'failed'}
                  <Button variant="secondary" on:click={() => retry(row.id)} disabled={working}>Retry</Button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { margin-bottom: 16px; }
  .filters label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  select { margin-left: 8px; padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .empty { color: var(--text-tertiary); padding: 24px; }
</style>
