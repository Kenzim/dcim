<script>
  import { onMount } from 'svelte';
  import {
    getResellerPanelService,
    listResellerPanelServices,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';
  import { Alert, Spinner } from '../ui/index.js';

  let services = [];
  let selected;
  let billingStatus = '';
  let serviceStatus = '';
  let loading = true;
  let error = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      services = await listResellerPanelServices({
        status: billingStatus,
        service_status: serviceStatus,
      });
    } catch (err) {
      error = err.message || 'Services could not be loaded.';
    } finally {
      loading = false;
    }
  }

  async function openService(id) {
    try {
      selected = await getResellerPanelService(id);
    } catch (err) {
      error = err.message || 'Service details could not be loaded.';
    }
  }

  function date(value) {
    return value ? new Date(value).toLocaleString() : '—';
  }
</script>

<section aria-labelledby="services-title">
  <div class="heading">
    <div><h1 id="services-title">Services</h1><p>Reseller-owned services and recurring billing state.</p></div>
    <div class="filters">
      <label>Service <select bind:value={serviceStatus} on:change={load}><option value="">All</option><option value="active">Active</option><option value="pending">Pending</option><option value="suspended">Suspended</option><option value="terminated">Terminated</option></select></label>
      <label>Billing <select bind:value={billingStatus} on:change={load}><option value="">All</option><option value="active">Active</option><option value="past_due">Past due</option><option value="grace">Grace</option><option value="suspended_nonpayment">Suspended</option><option value="cancelled">Cancelled</option><option value="manual_review">Manual review</option></select></label>
    </div>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading services…</span></div>
  {:else if services.length === 0}
    <div class="empty">No services match these filters.</div>
  {:else}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Service</th><th>Client</th><th>Product</th><th>Status</th><th>Monthly</th><th>Next billing</th><th></th></tr></thead>
        <tbody>
          {#each services as service (service.billing_id)}
            <tr>
              <td><strong>{service.name || `Service ${service.id}`}</strong><small>{service.service_type?.replaceAll('_', ' ')}</small></td>
              <td>{service.client?.username || '—'}<small>{service.client?.email || ''}</small></td>
              <td>{service.product?.name || service.product_code || '—'}</td>
              <td><span class="status">{service.service_status}</span><small>{service.billing_status.replaceAll('_', ' ')}</small></td>
              <td>{formatMoney(service.monthly_price_cents, service.currency)}</td>
              <td>{date(service.next_charge_at)}</td>
              <td><button on:click={() => openService(service.id)}>Details</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if selected}
      <article class="detail">
        <button class="close" on:click={() => (selected = null)} aria-label="Close service details">×</button>
        <h2>{selected.name}</h2>
        <p>{selected.product?.name || selected.product_code || selected.service_type}</p>
        <dl>
          <div><dt>Client</dt><dd>{selected.client?.username || '—'}</dd></div>
          <div><dt>Service status</dt><dd>{selected.service_status}</dd></div>
          <div><dt>Billing status</dt><dd>{selected.billing_status.replaceAll('_', ' ')}</dd></div>
          <div><dt>Next charge</dt><dd>{date(selected.next_charge_at)}</dd></div>
          <div><dt>Monthly cost</dt><dd>{formatMoney(selected.monthly_price_cents, selected.currency)}</dd></div>
          <div><dt>Billing anchor</dt><dd>{selected.billing_anchor_day || '—'}</dd></div>
        </dl>
        <h3>Current billing cycle</h3>
        {#if selected.current_cycle}
          <p>{selected.current_cycle.state.replaceAll('_', ' ')} · {formatMoney(selected.current_cycle.amount_cents, selected.current_cycle.currency)} due {date(selected.current_cycle.due_at)}</p>
        {:else}<p>No billing cycle recorded yet.</p>{/if}
      </article>
    {/if}
  {/if}
</section>

<style>
  .heading { display: flex; justify-content: space-between; gap: 18px; align-items: end; margin-bottom: 22px; }.heading h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.filters { display: flex; gap: 8px; }.filters label { color: var(--text-tertiary); font-size: 12px; }.filters select { display: block; margin-top: 4px; padding: 8px; border: 1px solid var(--border-color); border-radius: 7px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }.empty { padding: 30px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); text-align: center; }.table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }table { width: 100%; border-collapse: collapse; }th, td { padding: 12px 13px; border-bottom: 1px solid var(--border-color); text-align: left; white-space: nowrap; }th { color: var(--text-tertiary); font-size: 10px; text-transform: uppercase; letter-spacing: .05em; }td { color: var(--text-secondary); font-size: 13px; }td strong { color: var(--text-primary); }td small { display: block; margin-top: 3px; color: var(--text-tertiary); text-transform: capitalize; }.status { color: var(--portal-accent); text-transform: capitalize; font-weight: 750; }td button { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 7px; background: var(--bg-secondary); color: var(--portal-accent); font-weight: 700; cursor: pointer; }
  .detail { position: relative; margin-top: 18px; padding: 20px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }.detail h2 { margin: 0 30px 4px 0; }.detail > p { margin: 0; color: var(--text-secondary); }.close { position: absolute; top: 12px; right: 12px; border: 0; background: none; color: var(--text-tertiary); font-size: 24px; cursor: pointer; }dl { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }dt { color: var(--text-tertiary); font-size: 11px; }dd { margin: 4px 0 0; text-transform: capitalize; }h3 { font-size: 14px; }
  @media (max-width: 780px) { .heading { align-items: flex-start; flex-direction: column; }.filters { width: 100%; flex-wrap: wrap; }dl { grid-template-columns: repeat(2, 1fr); } }
</style>
