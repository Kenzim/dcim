<script>
  import { formatMoney } from '../../lib/resellerMoney.js';

  /** The `balance` object of an invoice (all fields integer cents). */
  export let balance = null;
  export let currency = 'USD';

  $: money = (cents) => formatMoney(cents, currency);
  $: hasNotes = balance && (balance.debit_cents > 0 || balance.credit_cents > 0);
</script>

{#if balance}
  <table class="balance" aria-label="Invoice balance">
    <tbody>
      <tr><th scope="row">Invoice total</th><td>{money(balance.total_cents)}</td></tr>
      {#if balance.debit_cents > 0}
        <tr><th scope="row">Debit notes</th><td>+ {money(balance.debit_cents)}</td></tr>
      {/if}
      {#if balance.credit_cents > 0}
        <tr><th scope="row">Credit notes</th><td>− {money(balance.credit_cents)}</td></tr>
      {/if}
      {#if hasNotes}
        <tr class="sub"><th scope="row">Amount payable</th><td>{money(balance.worth_cents)}</td></tr>
      {/if}
      <tr><th scope="row">Paid</th><td>{money(balance.paid_gross_cents)}</td></tr>
      {#if balance.refunded_cents > 0}
        <tr><th scope="row">Refunded</th><td>− {money(balance.refunded_cents)}</td></tr>
      {/if}
      {#if balance.pending_cents > 0}
        <tr><th scope="row">Payment pending</th><td>{money(balance.pending_cents)}</td></tr>
      {/if}
      {#if balance.due_cents < 0}
        <tr class="total"><th scope="row">Overpaid</th><td>{money(-balance.due_cents)}</td></tr>
      {:else}
        <tr class="total"><th scope="row">Amount due</th><td>{money(balance.due_cents)}</td></tr>
      {/if}
    </tbody>
  </table>
{/if}

<style>
  .balance { width: 100%; border-collapse: collapse; font-size: 14px; margin: 0 0 16px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--border-color); text-align: left; }
  th { font-weight: 600; color: var(--text-secondary); }
  td { text-align: right; font-variant-numeric: tabular-nums; }
  .sub th, .sub td { font-weight: 700; color: var(--text-primary); }
  .total th, .total td { font-weight: 750; color: var(--text-primary); border-bottom: 0; background: var(--bg-secondary); }
</style>
