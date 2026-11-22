<script>
  import { createEventDispatcher } from 'svelte';
  import { navigate } from '../../../lib/router.js';
  import { Alert, Button } from '../../ui/index.js';
  import { billingErrorMessage, clientCreateCreditTopUp } from '../../../lib/billingClientApi.js';
  import { ledgerTypeLabel } from '../../../lib/billingFormat.js';
  import { formatMoney, parseUsdToCents } from '../../../lib/resellerMoney.js';

  /** The `/credit` summary: balance, limits and recent ledger entries. */
  export let credit;
  export let impersonating = false;

  const dispatch = createEventDispatcher();

  let amount = '';
  let working = false;
  let error = '';

  $: currency = credit?.currency || 'USD';
  $: money = (cents) => formatMoney(cents, currency);

  async function createTopUp() {
    if (working) return;
    error = '';
    let cents;
    try {
      cents = parseUsdToCents(amount);
    } catch (err) {
      error = err.message;
      return;
    }
    working = true;
    try {
      const invoice = await clientCreateCreditTopUp(cents);
      dispatch('changed');
      navigate(`/client/billing/${invoice.id}`);
    } catch (err) {
      error = billingErrorMessage(err, 'The top-up invoice could not be created.', { currency });
    } finally {
      working = false;
    }
  }
</script>

<div class="panel">
  <h2>Account credit</h2>
  <dl>
    <div><dt>Balance</dt><dd>{money(credit.balance_cents)}</dd></div>
    <div><dt>Available to spend</dt><dd>{money(credit.available_cents)}</dd></div>
  </dl>

  <h3>Add credit</h3>
  {#if impersonating}
    <p class="muted">Credit cannot be added while viewing as this client.</p>
  {:else}
    <p class="muted">
      We create an invoice for the amount; the credit is added to your balance once it is paid.
      {#if credit.topup_min_cents && credit.topup_max_cents}
        Between {money(credit.topup_min_cents)} and {money(credit.topup_max_cents)}.
      {/if}
    </p>
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    <form on:submit|preventDefault={createTopUp}>
      <label>
        <span>Amount ({currency})</span>
        <input inputmode="decimal" placeholder="25.00" bind:value={amount} disabled={working} />
      </label>
      <Button type="submit" disabled={working || amount.trim() === ''}>{working ? 'Creating…' : 'Create top-up invoice'}</Button>
    </form>
  {/if}

  <h3>History</h3>
  {#if credit.entries.length === 0}
    <p class="muted">No credit activity yet.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead><tr><th>Date</th><th>Type</th><th>Description</th><th class="num">Amount</th><th class="num">Balance</th></tr></thead>
        <tbody>
          {#each credit.entries as entry (entry.id)}
            <tr>
              <td>{new Date(entry.created_at).toLocaleString()}</td>
              <td>{ledgerTypeLabel(entry.entry_type)}</td>
              <td>
                {entry.description || '—'}
                {#if entry.invoice_id}
                  <a href={`/client/billing/${entry.invoice_id}`}>Invoice #{entry.invoice_id}</a>
                {/if}
              </td>
              <td class="num" class:neg={entry.amount_cents < 0}>{entry.amount_cents > 0 ? '+' : ''}{money(entry.amount_cents)}</td>
              <td class="num">{money(entry.balance_after_cents)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 20px; box-shadow: var(--shadow-sm); }
  h2 { margin: 0 0 12px; font-size: 17px; }
  h3 { margin: 24px 0 10px; font-size: 15px; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; font-size: 18px; }
  .muted { color: var(--text-secondary); font-size: 14px; margin: 0 0 12px; }
  form { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input { padding: 9px 10px; width: 180px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .neg { color: var(--danger-text); }
  td a { margin-left: 6px; color: var(--portal-accent); }
</style>
