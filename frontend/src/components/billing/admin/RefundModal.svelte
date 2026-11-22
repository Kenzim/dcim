<script>
  import { Button, Modal } from '../../ui/index.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import { centsToInput, createRefund, newIdempotencyKey, parseCents } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  export let invoice;
  export let payment;
  /** Mode of the invoice engine: partial refunds, credit and manual destinations need v2. */
  export let engineMode = '';
  export let onClose;
  export let onDone = () => {};

  const GATEWAY_REFUNDABLE = ['stripe', 'paypal'];
  const reasons = [
    ['customer_request', 'Customer request'],
    ['duplicate', 'Duplicate payment'],
    ['cancellation', 'Cancellation'],
    ['fraud', 'Fraud'],
    ['other', 'Other'],
  ];

  $: currency = payment.currency || invoice.currency || 'USD';
  $: open = (invoice.refunds || [])
    .filter((r) => r.payment_id === payment.id && (r.status === 'pending' || r.status === 'review'))
    .reduce((sum, r) => sum + r.amount_cents, 0);
  $: refundable = Math.max(0, payment.amount_cents - (payment.refunded_cents || 0) - open);
  $: v2 = engineMode === 'v2';
  $: modeKnown = engineMode !== '';
  $: gatewayOk = GATEWAY_REFUNDABLE.includes(payment.gateway);
  $: creditOnly = payment.gateway === 'credit';
  $: destinations = [
    ['gateway', `Back to the original method (${payment.gateway})`, gatewayOk && !creditOnly],
    ['credit', 'To the account credit balance', v2 || !modeKnown],
    ['manual', 'Paid back outside Rackflow (record only)', v2 || !modeKnown],
  ];

  let destination = '';
  let amount = '';
  let reason = 'customer_request';
  let reasonText = '';
  let creditNote = 'auto';
  let working = false;
  let error = '';
  let result = null;
  let key = newIdempotencyKey('refund');

  $: if (!destination) {
    destination = destinations.find((d) => d[2])?.[0] || 'credit';
  }
  $: if (amount === '') amount = centsToInput(refundable);
  $: fullOnly = modeKnown && !v2;

  async function submit() {
    error = '';
    const cents = fullOnly ? refundable : parseCents(amount);
    if (cents === null) {
      error = 'Enter the amount to refund, for example 12.50.';
      return;
    }
    if (cents > refundable) {
      error = `At most ${formatMoney(refundable, currency)} can still be refunded on this payment.`;
      return;
    }
    const body = {
      payment_id: payment.id,
      amount_cents: cents,
      destination,
      reason,
      reason_text: reasonText.trim() || undefined,
    };
    if (creditNote !== 'auto') body.issue_credit_note = creditNote === 'yes';
    working = true;
    try {
      result = await createRefund(body, key);
      onDone(result);
    } catch (err) {
      error = err.message;
      if (err.status) key = newIdempotencyKey('refund');
    } finally {
      working = false;
    }
  }
</script>

<Modal title="Refund payment #{payment.id}" {onClose}>
  {#if result}
    <p>
      {#if result.status === 'pending'}
        The refund was reserved and the gateway has not confirmed it yet. It stays pending until the gateway answers; use "Send now" on the refunds page to retry.
      {:else if result.status === 'succeeded'}
        Refund #{result.id} of {formatMoney(result.amount_cents, result.currency)} succeeded.
      {:else}
        Refund #{result.id} ended as {result.status}{result.failure_message ? `: ${result.failure_message}` : ''}.
      {/if}
    </p>
  {:else}
    <p class="ba-muted">
      {payment.gateway} payment of {formatMoney(payment.amount_cents, currency)}.
      Still refundable: {formatMoney(refundable, currency)}.
      {#if fullOnly}Only a full refund is possible while the invoice engine is {engineMode}.{/if}
    </p>
    <form class="ba-form" on:submit|preventDefault={submit}>
      <label>Refund to
        <select bind:value={destination}>
          {#each destinations as [value, label, enabled]}
            <option {value} disabled={!enabled}>{label}{enabled ? '' : ' (not available)'}</option>
          {/each}
        </select>
        {#if creditOnly}<span class="ba-hint">Applied credit can only go back to the credit balance.</span>{/if}
        {#if payment.gateway === 'usdt_crypto'}<span class="ba-hint">USDT payments cannot be refunded to the gateway.</span>{/if}
        {#if fullOnly}<span class="ba-hint">Credit and manual refunds need the invoice engine in v2.</span>{/if}
      </label>
      <label>Amount ({currency})
        <input bind:value={amount} inputmode="decimal" disabled={fullOnly} />
      </label>
      <div class="ba-two">
        <label>Reason
          <select bind:value={reason}>
            {#each reasons as [value, label]}<option {value}>{label}</option>{/each}
          </select>
        </label>
        <label>Credit note
          <select bind:value={creditNote} disabled={!v2}>
            <option value="auto">Automatic (issue when allowed)</option>
            <option value="yes">Always issue</option>
            <option value="no">Do not issue</option>
          </select>
        </label>
      </div>
      <label>Note (optional)<input bind:value={reasonText} maxlength="512" /></label>
      {#if error}<div class="ba-error">{error}</div>{/if}
    </form>
  {/if}
  <svelte:fragment slot="footer">
    {#if result}
      <Button on:click={onClose}>Close</Button>
    {:else}
      <Button variant="secondary" on:click={onClose}>Cancel</Button>
      <Button on:click={submit} disabled={working || refundable === 0}>{working ? 'Refunding…' : 'Refund'}</Button>
    {/if}
  </svelte:fragment>
</Modal>
