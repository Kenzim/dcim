<script>
  import { hasTaxDetail, sterlingVatView } from '../../../lib/billingTaxFormat.js';
  import TaxBreakdown from './TaxBreakdown.svelte';

  /** An invoice as the client or reseller API returns it. Shows nothing when it has no tax detail. */
  export let invoice;

  $: showTax = hasTaxDetail(invoice);
  $: sterling = sterlingVatView(invoice);
</script>

{#if showTax || sterling}
  <section class="tax-panel" aria-label="VAT">
    <h3>VAT</h3>
    {#if showTax}
      <TaxBreakdown source={invoice} currency={invoice.currency} />
    {/if}
    {#if sterling}
      {#if sterling.pending}
        <p class="sterling">VAT total in pounds sterling: to follow. We use HMRC's exchange rate for {sterling.period}, and it has not been published to us yet.</p>
      {:else}
        <dl class="sterling">
          <div><dt>VAT total in pounds sterling</dt><dd>{sterling.amount}</dd></div>
          <div><dt>Exchange rate</dt><dd>{sterling.rate}</dd></div>
          <div><dt>Rate month</dt><dd>{sterling.period}</dd></div>
          <div><dt>Rate source</dt><dd>{sterling.source}</dd></div>
        </dl>
      {/if}
    {/if}
  </section>
{/if}

<style>
  .tax-panel { margin: 0 0 16px; }
  h3 { margin: 0 0 8px; font-size: 15px; }
  p.sterling { margin: 0; font-size: 13px; color: var(--text-secondary); }
  dl.sterling { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0; }
  dl.sterling div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; }
</style>
