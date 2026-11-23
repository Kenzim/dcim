<script>
  import { hasTaxDetail, sterlingVatView } from '../../../lib/billingTaxFormat.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import '../admin/billingAdmin.css';
  import TaxBreakdown from './TaxBreakdown.svelte';

  /** An invoice as the API returns it. Shows nothing when it has no tax detail. */
  export let invoice;
  /** `customer` (client and reseller screens, plain words) or `staff` (admin screens, with the tax point, engine and rates). */
  export let audience = 'customer';

  $: staff = audience === 'staff';
  $: summary = Array.isArray(invoice?.tax_summary) ? invoice.tax_summary : [];
  $: summaryTax = summary.reduce((sum, row) => sum + Number(row.tax_cents || 0), 0);
  $: taxTotal = invoice?.tax_cents ?? (summary.length ? summaryTax : null);
  $: showTax = hasTaxDetail(invoice) || (staff && taxTotal !== null);
  $: sterling = sterlingVatView(invoice);
  $: hasReverseChargeRows = summary.some((row) => row.reverse_charge);
  $: hasPrices = invoice?.prices_include_tax !== undefined && invoice?.prices_include_tax !== null;
</script>

{#if showTax || sterling}
  <section class="tax-panel" class:ba-panel={staff} aria-label="VAT">
    <h3>{staff ? 'Tax' : 'VAT'}</h3>
    {#if showTax}
      {#if staff}
        <dl class="ba-grid">
          {#if taxTotal !== null}<div><dt>Tax</dt><dd>{formatMoney(taxTotal, invoice.currency || 'USD')}</dd></div>{/if}
          {#if hasPrices}<div><dt>Prices</dt><dd>{invoice.prices_include_tax ? 'include tax' : 'exclude tax'}</dd></div>{/if}
          {#if invoice.tax_point_at}<div><dt>Tax point</dt><dd>{new Date(invoice.tax_point_at).toLocaleString()}</dd></div>{/if}
          {#if invoice.tax_engine}<div><dt>Calculated by</dt><dd>{invoice.tax_engine}</dd></div>{/if}
        </dl>
      {/if}
      <div class:staff-table={staff}>
        <TaxBreakdown source={invoice} currency={invoice.currency} {audience} />
      </div>
    {/if}
    {#if sterling}
      {#if staff}<h3 style="margin-top:16px">VAT in sterling</h3>{/if}
      {#if sterling.pending}
        {#if staff}
          <p>
            <span class="ba-pill warn">pending</span>
            The sterling VAT total waits for HMRC's rate for {sterling.period}. It is filled in once the rate is stored (see
            <a href="/admin/billing/tax?tab=rates">Exchange rates</a>).
          </p>
        {:else}
          <p class="sterling">VAT total in pounds sterling: to follow. We use HMRC's exchange rate for {sterling.period}, and it has not been published to us yet.</p>
        {/if}
      {:else}
        <dl class="sterling">
          <div><dt>VAT total in pounds sterling</dt><dd>{sterling.amount}</dd></div>
          <div><dt>Exchange rate</dt><dd>{sterling.rate}</dd></div>
          <div><dt>Rate month</dt><dd>{sterling.period}</dd></div>
          <div><dt>Rate source</dt><dd>{sterling.source}</dd></div>
        </dl>
        {#if staff && hasReverseChargeRows}
          <p class="ba-hint" style="margin-top:8px">Reverse charge rows carry no VAT, so they add nothing to the sterling total.</p>
        {/if}
      {/if}
    {/if}
  </section>
{/if}

<style>
  .tax-panel { margin: 0 0 16px; }
  .tax-panel.ba-panel { margin: 0 0 18px; }
  h3 { margin: 0 0 8px; font-size: 15px; }
  .staff-table { margin-top: 12px; }
  p.sterling { margin: 0; font-size: 13px; color: var(--text-secondary); }
  dl.sterling { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0; }
  dl.sterling div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; }
</style>
