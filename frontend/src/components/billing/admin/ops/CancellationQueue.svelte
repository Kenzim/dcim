<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import {
    listCancellationRequests,
    newIdempotencyKey,
    requestCancellation,
    searchServices,
    withdrawCancellation,
  } from '../../../../lib/billingOpsAdminApi.js';
  import { label, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  const STATUSES = ['pending', 'scheduled', 'completed', 'withdrawn', 'failed'];
  const STATUS_TONE = { pending: 'warn', scheduled: 'info', completed: 'ok', withdrawn: '', failed: 'bad' };
  const limit = 50;

  let rows = [];
  let total = null;
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let offset = 0;
  let filters = { status: '', service_id: '', billing_account_id: '', reseller_id: '' };

  let creating = null;
  let createError = '';
  let serviceQuery = '';
  let serviceResults = [];
  let searching = false;
  let searchError = '';

  let withdrawing = null;
  let withdrawError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      const page = await listCancellationRequests({ ...filters, limit, offset });
      rows = page.items;
      total = page.total;
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function applyFilters() {
    offset = 0;
    load();
  }

  function clearFilters() {
    filters = { status: '', service_id: '', billing_account_id: '', reseller_id: '' };
    applyFilters();
  }

  function openCreate() {
    creating = {
      key: newIdempotencyKey('cancel'),
      service_id: '',
      type: 'end_of_period',
      reason: '',
      invoice_action: 'cancel_unpaid',
      confirmed: false,
    };
    createError = '';
    serviceQuery = '';
    serviceResults = [];
    searchError = '';
  }

  async function findServices() {
    const text = serviceQuery.trim();
    if (!text) return;
    searching = true;
    searchError = '';
    try {
      serviceResults = await searchServices(text);
      if (serviceResults.length === 0) searchError = 'No service matches.';
    } catch (err) {
      searchError = err.message;
    } finally {
      searching = false;
    }
  }

  function chooseService(service) {
    creating.service_id = service.id;
    creating = creating;
  }

  async function submitCreate() {
    const serviceId = Number(creating.service_id);
    if (!Number.isInteger(serviceId) || serviceId <= 0) {
      createError = 'Choose a service, or enter its ID.';
      return;
    }
    if (creating.type === 'immediate' && !creating.confirmed) {
      createError = 'Tick the box to confirm that the service ends now.';
      return;
    }
    working = true;
    createError = '';
    try {
      const body = { service_id: serviceId, type: creating.type, invoice_action: creating.invoice_action };
      if (creating.reason.trim()) body.reason = creating.reason.trim();
      const result = await requestCancellation(body, creating.key);
      success = result.request?.status === 'completed'
        ? `Service ${serviceId} was cancelled now.`
        : result.termination_pending
          ? `Cancellation requested for service ${serviceId}, but the service has not been shut down yet. It is pending termination.`
          : `Cancellation requested for service ${serviceId}${result.cancel_at ? `; it ends ${when(result.cancel_at)}` : ''}.`;
      creating = null;
      await load();
    } catch (err) {
      createError = err.message;
    } finally {
      working = false;
    }
  }

  async function confirmWithdraw() {
    working = true;
    withdrawError = '';
    try {
      await withdrawCancellation(withdrawing.id);
      success = `Cancellation request #${withdrawing.id} was withdrawn.`;
      withdrawing = null;
      await load();
    } catch (err) {
      withdrawError = err.message;
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Cancellation Requests">
  <svelte:fragment slot="actions">
    <Button on:click={openCreate}>Request a cancellation</Button>
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  <p class="ba-muted ba-wide-note">
    Requests from clients, resellers and staff. An end-of-period request keeps the service until its paid period ends; an immediate request ends it now.
    A scheduled request can be withdrawn until it takes effect.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  <div class="ba-panel" style="margin-top:18px">
    <div class="ba-toolbar">
      <label>Status
        <select bind:value={filters.status} on:change={applyFilters}>
          <option value="">All</option>
          {#each STATUSES as value}<option {value}>{label(value)}</option>{/each}
        </select>
      </label>
      <label>Service ID
        <input type="number" min="1" bind:value={filters.service_id} on:change={applyFilters} />
      </label>
      <label>Billing account ID
        <input type="number" min="1" bind:value={filters.billing_account_id} on:change={applyFilters} />
      </label>
      <label>Reseller ID
        <input type="number" min="1" bind:value={filters.reseller_id} on:change={applyFilters} />
      </label>
      <Button variant="secondary" size="small" on:click={clearFilters}>Clear filters</Button>
    </div>

    {#if loading}
      <div class="ba-state"><Spinner /></div>
    {:else if rows.length === 0}
      <p class="ba-empty">No cancellation requests match.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead>
            <tr><th>Request</th><th>Service</th><th>Account</th><th>Type</th><th>Status</th><th>Effective</th><th>Unpaid invoices</th><th>Source</th><th>Reason</th><th></th></tr>
          </thead>
          <tbody>
            {#each rows as row (row.id)}
              <tr>
                <td>#{row.id}<div class="ba-sub">{when(row.created_at)}</div></td>
                <td>{row.service_id}</td>
                <td>
                  {#if row.reseller_id}Reseller {row.reseller_id}{:else if row.billing_account_id}Account {row.billing_account_id}{:else}-{/if}
                </td>
                <td>{row.type === 'immediate' ? 'Immediate' : 'End of period'}</td>
                <td>
                  <span class="ba-pill {STATUS_TONE[row.status] || ''}">{label(row.status)}</span>
                  {#if row.completed_at}<div class="ba-sub">Done {when(row.completed_at)}</div>{/if}
                </td>
                <td>{when(row.effective_at)}</td>
                <td>{row.invoice_action === 'keep' ? 'Kept' : 'Cancelled'}</td>
                <td>{label(row.source)}{row.requested_by_user_id ? ` (user ${row.requested_by_user_id})` : ''}</td>
                <td>{row.reason || '-'}</td>
                <td>
                  {#if row.status === 'scheduled'}
                    <button class="ba-link" on:click={() => { withdrawing = row; withdrawError = ''; }}>Withdraw</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <div class="ba-inline" style="margin-top:12px">
      <Button variant="secondary" size="small" disabled={offset === 0 || loading} on:click={() => { offset = Math.max(0, offset - limit); load(); }}>Previous</Button>
      <span class="ba-muted">
        Showing {rows.length ? offset + 1 : 0} to {offset + rows.length}{total !== null ? ` of ${total}` : ''}
        {#if total !== null && total > limit} (page {Math.floor(offset / limit) + 1} of {Math.ceil(total / limit)}){/if}
      </span>
      <Button variant="secondary" size="small" disabled={(total !== null ? offset + rows.length >= total : rows.length < limit) || loading} on:click={() => { offset += limit; load(); }}>Next</Button>
    </div>
  </div>
</div>

{#if creating}
  <Modal title="Request a cancellation" onClose={() => (creating = null)}>
    <form class="ba-form" on:submit|preventDefault={submitCreate}>
      <div>
        <label>Find a service
          <span class="ba-inline">
            <input bind:value={serviceQuery} placeholder="Name, owner or external ID" style="flex:1" on:keydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); findServices(); } }} />
            <Button variant="secondary" size="small" on:click={findServices} disabled={searching}>Search</Button>
          </span>
        </label>
        {#if searchError}<p class="ba-hint">{searchError}</p>{/if}
        {#if serviceResults.length}
          <ul class="svc-list">
            {#each serviceResults as service (service.id)}
              <li>
                <button type="button" class="ba-link" on:click={() => chooseService(service)}>
                  #{service.id} {service.name}
                </button>
                <span class="ba-hint">{service.owner_username || service.owner_email || ''} · {label(service.status)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
      <label>Service ID
        <input type="number" min="1" bind:value={creating.service_id} />
      </label>
      <label>When
        <select bind:value={creating.type}>
          <option value="end_of_period">At the end of the paid period</option>
          <option value="immediate">Immediately</option>
        </select>
      </label>
      <label>Unpaid invoices of the service
        <select bind:value={creating.invoice_action}>
          <option value="cancel_unpaid">Cancel those for periods the client will not have</option>
          <option value="keep">Keep them</option>
        </select>
      </label>
      <label>Reason (optional)
        <textarea bind:value={creating.reason} rows="3" maxlength="1000"></textarea>
      </label>
      {#if creating.type === 'immediate'}
        <Alert type="warning">An immediate cancellation shuts the service down now. This cannot be undone.</Alert>
        <label class="ba-check"><input type="checkbox" bind:checked={creating.confirmed} /> I confirm that the service ends now</label>
      {/if}
      {#if createError}<div class="ba-error">{createError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (creating = null)}>Cancel</Button>
      <Button variant={creating.type === 'immediate' ? 'danger' : 'primary'} on:click={submitCreate} disabled={working}>Request cancellation</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if withdrawing}
  <Modal title="Withdraw cancellation request #{withdrawing.id}" onClose={() => (withdrawing = null)}>
    <p>
      Withdraw the request to cancel service {withdrawing.service_id}? The service stays active and renews as normal.
      Withdrawing does not restore invoices the request already cancelled.
    </p>
    {#if withdrawError}<div class="ba-error">{withdrawError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (withdrawing = null)}>Keep the request</Button>
      <Button variant="danger" on:click={confirmWithdraw} disabled={working}>Withdraw request</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .svc-list { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 4px; max-height: 160px; overflow: auto; }
  .svc-list li { display: flex; gap: 10px; align-items: baseline; }
</style>
