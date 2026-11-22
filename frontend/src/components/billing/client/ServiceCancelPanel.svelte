<script>
  import { onMount } from 'svelte';
  import { Alert, Badge, Button, Modal, Spinner } from '../../ui/index.js';
  import { formatDate } from '../../../lib/billingFormat.js';
  import {
    billingErrorMessage,
    clientCancelService,
    clientListCancellationRequests,
    clientWithdrawCancellationRequest,
  } from '../../../lib/billingClientApi.js';
  import {
    cancellationCanWithdraw,
    cancellationStatusLabel,
    cancellationStatusVariant,
    cancellationTypeLabel,
  } from '../../../lib/billingTaxFormat.js';
  import CancelServiceForm from '../shared/CancelServiceForm.svelte';

  export let serviceId;
  export let serviceName = 'this service';
  export let impersonating = false;

  let current = null;
  let loading = true;
  let error = '';
  let confirmingWithdraw = false;
  let working = false;

  onMount(load);

  /** The newest request of this service that is still open or has finished; withdrawn ones are history. */
  async function load() {
    loading = true;
    error = '';
    try {
      const rows = await clientListCancellationRequests({ limit: 200 });
      current = rows.find((row) => Number(row.service_id) === Number(serviceId) && row.status !== 'withdrawn') || null;
    } catch (err) {
      current = null;
      error = billingErrorMessage(err, 'Cancellation requests could not be loaded.');
    } finally {
      loading = false;
    }
  }

  const submit = ({ when, reason }) =>
    clientCancelService(serviceId, { when: when === 'immediate' ? 'immediate' : 'end_of_cycle', reason });

  async function withdraw() {
    working = true;
    error = '';
    try {
      await clientWithdrawCancellationRequest(current.id);
      confirmingWithdraw = false;
      await load();
    } catch (err) {
      confirmingWithdraw = false;
      error = billingErrorMessage(err, 'The cancellation could not be withdrawn.');
      await load();
    } finally {
      working = false;
    }
  }
</script>

<section class="cancel" aria-labelledby="cancel-heading">
  <h2 id="cancel-heading">Cancel this service</h2>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Checking…</span></div>
  {:else if current}
    <p class="line">
      <Badge variant={cancellationStatusVariant(current.status)} caps={false}>{cancellationStatusLabel(current.status)}</Badge>
      {cancellationTypeLabel(current.type)}{#if current.effective_at && current.type !== 'immediate'}, from {formatDate(current.effective_at)}{/if}.
    </p>
    {#if cancellationCanWithdraw(current)}
      <p class="hint">Changed your mind? You can withdraw the request until it takes effect.</p>
      <Button variant="secondary" on:click={() => (confirmingWithdraw = true)} disabled={impersonating}>Withdraw cancellation</Button>
    {/if}
    <p class="hint"><a href="/client/cancellations">All cancellation requests</a></p>
  {:else}
    <p class="hint">Ask for this service to be cancelled. You choose whether it ends now or at the end of the billing period.</p>
    <CancelServiceForm
      {submit}
      {serviceName}
      disabled={impersonating}
      disabledReason={impersonating ? 'Cancelling is switched off while viewing as this client.' : ''}
      on:requested={load}
    />
  {/if}
</section>

{#if confirmingWithdraw}
  <Modal title="Withdraw this cancellation?" onClose={() => (confirmingWithdraw = false)}>
    <p>If you withdraw the request, {serviceName} stays and billing carries on as normal.</p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirmingWithdraw = false)} disabled={working}>Keep the cancellation</Button>
      <Button variant="danger" on:click={withdraw} disabled={working}>{working ? 'Withdrawing…' : 'Withdraw cancellation'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .cancel { display: grid; gap: 10px; justify-items: start; padding: 22px 24px; background: var(--portal-card-bg); border: 1px solid var(--portal-card-border); border-radius: var(--radius-lg); box-shadow: var(--portal-card-shadow); }
  h2 { margin: 0; font-size: 17px; }
  .line { margin: 0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 14px; }
  .hint { margin: 0; font-size: 13px; color: var(--text-secondary); }
  .hint a { color: var(--portal-accent); font-weight: 600; }
  .state { display: flex; gap: 10px; align-items: center; color: var(--text-secondary); }
</style>
