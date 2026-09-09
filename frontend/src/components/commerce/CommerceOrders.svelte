<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import {
    adminCommerceAcceptOrder,
    adminCommerceCancelOrder,
    adminCommerceGetOrder,
    adminCommerceListOrders,
    adminCommerceRetryOrder,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  let orders = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let statusFilter = '';
  let confirmAction = null;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      orders = await adminCommerceListOrders(statusFilter ? { status: statusFilter } : {});
    } catch (err) {
      error = err.message || 'Failed to load orders';
      orders = [];
    } finally {
      loading = false;
    }
  }

  async function showOrder(id) {
    working = true;
    try {
      selected = await adminCommerceGetOrder(id);
    } catch (err) {
      error = err.message || 'Failed to load order';
    } finally {
      working = false;
    }
  }

  async function runAction() {
    if (!confirmAction || !selected) return;
    working = true;
    error = '';
    success = '';
    try {
      if (confirmAction === 'accept') {
        selected = await adminCommerceAcceptOrder(selected.id);
        success = 'Order accepted.';
      } else if (confirmAction === 'retry') {
        selected = await adminCommerceRetryOrder(selected.id);
        success = 'Fulfillment retried.';
      } else if (confirmAction === 'cancel') {
        selected = await adminCommerceCancelOrder(selected.id);
        success = 'Order cancelled.';
      }
      confirmAction = null;
      await load();
    } catch (err) {
      error = err.message || 'Action failed';
    } finally {
      working = false;
    }
  }

  function canAccept(o) {
    return ['pending_acceptance', 'pending_payment', 'paid_pending_fulfillment'].includes(o?.status);
  }
  function canRetry(o) {
    return ['provision_error', 'paid_pending_fulfillment', 'fulfilling'].includes(o?.status);
  }
  function canCancel(o) {
    return o && !['active', 'cancelled'].includes(o.status);
  }
</script>

<PageHeader title="Commerce Orders" />

<div class="page">
  <div class="filters">
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        <option value="pending_acceptance">Pending acceptance</option>
        <option value="pending_payment">Pending payment</option>
        <option value="paid_pending_fulfillment">Paid, pending fulfillment</option>
        <option value="fulfilling">Fulfilling</option>
        <option value="active">Active</option>
        <option value="provision_error">Provision error</option>
        <option value="cancelled">Cancelled</option>
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
        {#if orders.length === 0}
          <p class="empty">No orders in this queue.</p>
        {:else}
          {#each orders as row (row.id)}
            <button class:selected={selected?.id === row.id} on:click={() => showOrder(row.id)}>
              <span><strong>#{row.order_number}</strong><small>{row.status.replaceAll('_', ' ')}</small></span>
              <span>{formatMoney(row.total_cents, row.currency)}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="panel detail">
        {#if working && !selected}<Spinner />{:else if !selected}
          <p class="empty">Select an order to review items and take action.</p>
        {:else}
          <div class="head">
            <div><h2>Order #{selected.order_number}</h2><p>Account #{selected.billing_account_id}</p></div>
            <span class="status">{selected.status.replaceAll('_', ' ')}</span>
          </div>
          <dl>
            <div><dt>Total</dt><dd>{formatMoney(selected.total_cents, selected.currency)}</dd></div>
            <div><dt>Created</dt><dd>{new Date(selected.created_at).toLocaleString()}</dd></div>
            <div><dt>Coupon</dt><dd>{selected.coupon_code || '—'}</dd></div>
            <div><dt>Invoice</dt><dd>{selected.invoice_id ? `#${selected.invoice_id}` : '—'}</dd></div>
          </dl>
          <h3>Line items</h3>
          <ul>
            {#each selected.items || [] as item (item.id)}
              <li>
                <span>{item.name_snapshot}</span>
                <span>{item.fulfill_status}{item.service_id ? ` · service #${item.service_id}` : ''}</span>
              </li>
            {/each}
          </ul>
          <div class="actions">
            {#if canAccept(selected)}<Button on:click={() => (confirmAction = 'accept')}>Accept</Button>{/if}
            {#if canRetry(selected)}<Button variant="secondary" on:click={() => (confirmAction = 'retry')}>Retry fulfill</Button>{/if}
            {#if canCancel(selected)}<Button variant="danger" on:click={() => (confirmAction = 'cancel')}>Cancel</Button>{/if}
          </div>
        {/if}
      </article>
    </div>
  {/if}
</div>

{#if confirmAction}
  <Modal
    title={confirmAction === 'accept' ? 'Accept order?' : confirmAction === 'retry' ? 'Retry fulfillment?' : 'Cancel order?'}
    onClose={() => (confirmAction = null)}
  >
    <p>
      {#if confirmAction === 'accept'}Accept order #{selected?.order_number} and begin fulfillment?{/if}
      {#if confirmAction === 'retry'}Retry provisioning for order #{selected?.order_number}?{/if}
      {#if confirmAction === 'cancel'}Cancel order #{selected?.order_number}? This cannot be undone.{/if}
    </p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirmAction = null)}>Back</Button>
      <Button variant={confirmAction === 'cancel' ? 'danger' : 'primary'} on:click={runAction} disabled={working}>Confirm</Button>
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
  .head p { margin: 0; color: var(--text-secondary); }
  .status { padding: 5px 10px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); font-size: 12px; font-weight: 700; text-transform: capitalize; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0 0 16px; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); }
  dd { margin: 4px 0 0; font-weight: 700; }
  h3 { margin: 16px 0 8px; font-size: 15px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; justify-content: space-between; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
  .actions { display: flex; gap: 10px; margin-top: 18px; flex-wrap: wrap; }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
</style>
