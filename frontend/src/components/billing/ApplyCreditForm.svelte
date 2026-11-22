<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button } from '../ui/index.js';
  import { billingErrorMessage, newIdempotencyKey } from '../../lib/billingClientApi.js';
  import { formatMoney, parseUsdToCents } from '../../lib/resellerMoney.js';

  /** Credit the account can spend, in cents. */
  export let available = 0;
  /** Amount still due on the invoice, in cents. */
  export let due = 0;
  export let currency = 'USD';
  /** `(amountCents | null, idempotencyKey) => Promise<result>`; null means as much as possible. */
  export let apply;
  /** Reason to show instead of the form, for example impersonation. */
  export let blockedReason = '';

  const dispatch = createEventDispatcher();

  let amount = '';
  let working = false;
  let error = '';
  // One key per user action: kept while a retry of the same action is possible,
  // dropped once the server has answered or the amount changes.
  let pendingKey = null;
  let pendingAmount = null;

  $: maxApplicable = Math.min(Math.max(0, available), Math.max(0, due));

  async function submit() {
    if (working) return;
    error = '';
    let cents = null;
    if (amount.trim() !== '') {
      try {
        cents = parseUsdToCents(amount);
      } catch (err) {
        error = err.message;
        return;
      }
      if (cents > maxApplicable) {
        error = `You can apply at most ${formatMoney(maxApplicable, currency)} to this invoice.`;
        return;
      }
    }
    if (!pendingKey || pendingAmount !== cents) {
      pendingKey = newIdempotencyKey();
      pendingAmount = cents;
    }
    working = true;
    try {
      const result = await apply(cents, pendingKey);
      pendingKey = null;
      amount = '';
      dispatch('applied', result);
    } catch (err) {
      // No HTTP status means the outcome is unknown, so a retry keeps its key.
      if (err.status !== undefined) pendingKey = null;
      error = billingErrorMessage(err, 'Credit could not be applied.', { currency });
    } finally {
      working = false;
    }
  }
</script>

<div class="apply">
  <h3>Pay with account credit</h3>
  {#if blockedReason}
    <p class="hint">{blockedReason}</p>
  {:else if maxApplicable <= 0}
    <p class="hint">
      {available <= 0 ? 'There is no account credit to apply.' : 'Nothing is due on this invoice.'}
    </p>
  {:else}
    <p class="hint">
      {formatMoney(available, currency)} credit available, {formatMoney(due, currency)} due.
      Leave the amount blank to apply {formatMoney(maxApplicable, currency)}.
    </p>
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    <form on:submit|preventDefault={submit}>
      <label>
        <span>Amount (optional)</span>
        <input inputmode="decimal" placeholder={(maxApplicable / 100).toFixed(2)} bind:value={amount} disabled={working} />
      </label>
      <Button type="submit" disabled={working}>{working ? 'Applying…' : 'Apply credit'}</Button>
    </form>
  {/if}
</div>

<style>
  .apply { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border-color); }
  h3 { margin: 0 0 8px; font-size: 15px; }
  .hint { margin: 0 0 10px; font-size: 13px; color: var(--text-secondary); }
  form { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input { padding: 9px 10px; width: 160px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
</style>
