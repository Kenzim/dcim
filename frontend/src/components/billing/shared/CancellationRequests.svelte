<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { Alert, Badge, Button, Modal, Spinner } from '../../ui/index.js';
  import { formatDate } from '../../../lib/billingFormat.js';
  import { billingErrorMessage } from '../../../lib/billingClientApi.js';
  import {
    cancellationCanWithdraw,
    cancellationInvoiceActionLabel,
    cancellationSourceLabel,
    cancellationStatusLabel,
    cancellationStatusVariant,
    cancellationTypeLabel,
  } from '../../../lib/billingTaxFormat.js';

  /** `async (filters) => request[]`, the list call for this audience. */
  export let list;
  /** `async (id) => request`, the withdraw call for this audience. */
  export let withdraw;
  /** `client` or `reseller`: who is reading, for the wording of "requested by". */
  export let viewer = 'client';
  /** Optional `(serviceId) => string` for a link to the service. */
  export let serviceHref = null;
  /** Optional `{ [serviceId]: name }`. */
  export let serviceNames = {};
  export let disabled = false;
  export let emptyText = 'There are no cancellation requests.';

  const dispatch = createEventDispatcher();

  let requests = [];
  let statusFilter = '';
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let confirming = null;

  onMount(reload);

  export async function reload() {
    loading = true;
    error = '';
    try {
      requests = await list({ status: statusFilter, limit: 100 });
    } catch (err) {
      requests = [];
      error = billingErrorMessage(err, 'Cancellation requests could not be loaded.');
    } finally {
      loading = false;
    }
  }

  async function confirmWithdraw() {
    const target = confirming;
    if (!target) return;
    working = true;
    error = '';
    success = '';
    try {
      await withdraw(target.id);
      success = 'The cancellation has been withdrawn. The service carries on as before.';
      confirming = null;
      dispatch('changed');
      await reload();
    } catch (err) {
      confirming = null;
      error = billingErrorMessage(err, 'The cancellation could not be withdrawn.');
      await reload();
    } finally {
      working = false;
    }
  }

  const serviceLabel = (row) => row.service_label || serviceNames[row.service_id] || `Service #${row.service_id}`;
</script>

<div class="panel">
  <div class="bar">
    <slot name="intro" />
    <label>Show
      <select bind:value={statusFilter} on:change={reload}>
        <option value="">All requests</option>
        <option value="scheduled">Scheduled</option>
        <option value="completed">Cancelled</option>
        <option value="withdrawn">Withdrawn</option>
        <option value="pending">Being processed</option>
        <option value="failed">Failed</option>
      </select>
    </label>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if requests.length === 0}
    <p class="empty">{emptyText}</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr><th>Service</th><th>Status</th><th>When</th><th>Takes effect</th><th>Requested</th><th>By</th><th></th></tr>
        </thead>
        <tbody>
          {#each requests as row (row.id)}
            <tr>
              <td>
                {#if serviceHref}<a href={serviceHref(row.service_id)}>{serviceLabel(row)}</a>{:else}{serviceLabel(row)}{/if}
                {#if row.reason}<small title={row.reason}>{row.reason}</small>{/if}
              </td>
              <td>
                <Badge variant={cancellationStatusVariant(row.status)} caps={false}>{cancellationStatusLabel(row.status)}</Badge>
                {#if row.status === 'pending' && row.type === 'immediate'}<small>Waiting for the service to be switched off</small>{/if}
              </td>
              <td>{cancellationTypeLabel(row.type)}<small>{cancellationInvoiceActionLabel(row.invoice_action)}</small></td>
              <td>{formatDate(row.status === 'completed' ? row.completed_at || row.effective_at : row.effective_at)}</td>
              <td>{formatDate(row.created_at)}</td>
              <td>{cancellationSourceLabel(row.source, viewer)}</td>
              <td class="act">
                {#if cancellationCanWithdraw(row)}
                  <Button variant="secondary" size="small" {disabled} on:click={() => (confirming = row)}>Withdraw</Button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if confirming}
  <Modal title="Withdraw this cancellation?" onClose={() => (confirming = null)}>
    <p>
      {serviceLabel(confirming)} is due to be cancelled on <strong>{formatDate(confirming.effective_at)}</strong>.
      If you withdraw the request, the service stays and billing carries on as normal.
    </p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirming = null)} disabled={working}>Keep the cancellation</Button>
      <Button variant="danger" on:click={confirmWithdraw} disabled={working}>{working ? 'Withdrawing…' : 'Withdraw cancellation'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 16px; box-shadow: var(--shadow-sm); }
  .bar { display: flex; justify-content: space-between; align-items: center; gap: 12px 24px; flex-wrap: wrap; margin-bottom: 12px; }
  .bar :global(p) { margin: 0; font-size: 14px; color: var(--text-secondary); }
  label { display: flex; align-items: center; font-size: 13px; font-weight: 600; }
  select { margin-left: 8px; padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 120px; display: grid; place-items: center; gap: 12px; }
  .scroll { overflow-x: auto; }
  table { width: 100%; min-width: 760px; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); white-space: nowrap; }
  td small { display: block; margin-top: 3px; max-width: 240px; color: var(--text-tertiary); font-size: 12px; }
  td a { color: var(--portal-accent); font-weight: 600; }
  .act { text-align: right; white-space: nowrap; }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
</style>
