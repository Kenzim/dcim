<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button, Modal } from '../../ui/index.js';
  import { billingErrorMessage } from '../../../lib/billingClientApi.js';
  import { formatDate } from '../../../lib/billingFormat.js';
  import { cancellationStatusLabel, cancellationTypeLabel } from '../../../lib/billingTaxFormat.js';

  /** `async ({ when, reason, invoiceAction }) => result`. For a client the result has `request`; for a reseller it is the summary of the new request. */
  export let submit;
  /** The name shown in the confirmation. */
  export let serviceName = 'this service';
  /** Resellers choose what happens to unpaid invoices; clients do not. */
  export let askInvoiceAction = false;
  export let disabled = false;
  export let disabledReason = '';

  const dispatch = createEventDispatcher();

  let open = false;
  let confirming = false;
  let when = 'end_of_period';
  let invoiceAction = 'cancel_unpaid';
  let reason = '';
  let working = false;
  let error = '';
  let result = null;

  function start() {
    open = true;
    confirming = false;
    error = '';
    result = null;
  }

  function close() {
    if (working) return;
    open = false;
    confirming = false;
  }

  async function send() {
    working = true;
    error = '';
    try {
      result = await submit({ when, reason: reason.trim(), invoiceAction });
      confirming = false;
      open = false;
      dispatch('requested', result);
    } catch (err) {
      confirming = false;
      error = billingErrorMessage(err, 'The cancellation could not be requested.');
    } finally {
      working = false;
    }
  }

  $: request = result?.request || null;
</script>

{#if request}
  <Alert type="success">
    <span>
      Cancellation requested for {serviceName}: {cancellationStatusLabel(request.status).toLowerCase()},
      {cancellationTypeLabel(request.type).toLowerCase()}{#if request.effective_at && request.type !== 'immediate'} (from {formatDate(request.effective_at)}){/if}.
      {#if result.termination_pending}The service could not be switched off yet; we will keep trying.{/if}
    </span>
  </Alert>
{/if}

<Button variant="danger" on:click={start} {disabled}>Cancel service</Button>
{#if disabled && disabledReason}<p class="hint">{disabledReason}</p>{/if}

{#if open}
  <Modal title="Cancel {serviceName}" onClose={close}>
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    {#if !confirming}
      <fieldset>
        <legend>When should it end?</legend>
        <label class="choice"><input type="radio" bind:group={when} value="end_of_period" /> At the end of the current billing period. You keep the service until then and can change your mind.</label>
        <label class="choice"><input type="radio" bind:group={when} value="immediate" /> Straight away. The service is switched off now and this cannot be undone.</label>
      </fieldset>
      {#if askInvoiceAction}
        <fieldset>
          <legend>Unpaid invoices for periods after the cancellation</legend>
          <label class="choice"><input type="radio" bind:group={invoiceAction} value="cancel_unpaid" /> Cancel them</label>
          <label class="choice"><input type="radio" bind:group={invoiceAction} value="keep" /> Keep them</label>
        </fieldset>
      {/if}
      <label class="reason">Reason (optional)
        <textarea bind:value={reason} rows="3" maxlength="1000"></textarea>
      </label>
    {:else}
      <p>
        {#if when === 'immediate'}
          <strong>{serviceName} will be switched off straight away.</strong> You cannot withdraw this request afterwards.
        {:else}
          <strong>{serviceName} will be cancelled at the end of its current billing period.</strong> You can withdraw the request until then.
        {/if}
      </p>
    {/if}
    <svelte:fragment slot="footer">
      {#if !confirming}
        <Button variant="secondary" on:click={close}>Close</Button>
        <Button variant="danger" on:click={() => (confirming = true)}>Continue</Button>
      {:else}
        <Button variant="secondary" on:click={() => (confirming = false)} disabled={working}>Back</Button>
        <Button variant="danger" on:click={send} disabled={working}>{working ? 'Requesting…' : 'Confirm cancellation'}</Button>
      {/if}
    </svelte:fragment>
  </Modal>
{:else if error}
  <Alert type="danger">{error}</Alert>
{/if}

<style>
  fieldset { border: 0; margin: 0 0 16px; padding: 0; display: grid; gap: 8px; }
  legend { font-size: 13px; font-weight: 700; margin-bottom: 6px; padding: 0; }
  .choice { display: flex; gap: 8px; align-items: flex-start; font-size: 14px; }
  .reason { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  textarea { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); font: inherit; }
  .hint { margin: 6px 0 0; font-size: 12px; color: var(--text-tertiary); }
</style>
