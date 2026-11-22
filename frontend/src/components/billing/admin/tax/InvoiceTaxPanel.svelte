<script>
  import { formatMoney } from '../../../../lib/resellerMoney.js';
  import { label, when } from './taxUi.js';

  export let invoice;

  const SOURCES = { hmrc_monthly: 'HMRC monthly exchange rate' };

  function money(cents) {
    return formatMoney(cents, invoice.currency || 'USD');
  }

  $: summary = Array.isArray(invoice?.tax_summary) ? invoice.tax_summary : [];
  $: notes = Array.isArray(invoice?.legal_notes) ? invoice.legal_notes : [];
  $: hasTax = summary.length > 0 || (invoice?.tax_cents !== undefined && invoice?.tax_cents !== null);
  $: hasSterling = !!invoice?.fx_period;
  $: sterlingPending = hasSterling && (invoice.tax_total_gbp_cents == null || invoice.fx_rate == null);
</script>

{#if hasTax || hasSterling}
  <div class="ba-panel">
    <h3>Tax</h3>
    {#if hasTax}
      <dl class="ba-grid">
        <div><dt>Tax</dt><dd>{money(invoice.tax_cents ?? 0)}</dd></div>
        {#if invoice.prices_include_tax !== undefined && invoice.prices_include_tax !== null}
          <div><dt>Prices</dt><dd>{invoice.prices_include_tax ? 'include tax' : 'exclude tax'}</dd></div>
        {/if}
        {#if invoice.tax_point_at}<div><dt>Tax point</dt><dd>{when(invoice.tax_point_at)}</dd></div>{/if}
        {#if invoice.tax_engine}<div><dt>Calculated by</dt><dd>{invoice.tax_engine}</dd></div>{/if}
      </dl>
      {#if summary.length}
        <div class="ba-scroll" style="margin-top:12px">
          <table class="ba-table">
            <thead><tr><th>Tax</th><th>Rate</th><th>Jurisdiction</th><th class="ba-num">Base</th><th class="ba-num">Tax</th></tr></thead>
            <tbody>
              {#each summary as row}
                <tr>
                  <td>{row.name}{#if row.reverse_charge} <span class="ba-pill info">reverse charge</span>{/if}</td>
                  <td>{row.rate_percent}%</td>
                  <td>{row.jurisdiction}</td>
                  <td class="ba-num">{money(row.base_cents)}</td>
                  <td class="ba-num">{money(row.tax_cents)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#each notes as note}
        <p class="ba-hint" style="margin-top:8px">{note.text || note.key}</p>
      {/each}
    {/if}

    {#if hasSterling}
      <h3 style="margin-top:16px">VAT in sterling</h3>
      {#if sterlingPending}
        <p>
          <span class="ba-pill warn">pending</span>
          The sterling VAT total waits for HMRC's rate for {invoice.fx_period}. It is filled in once the rate is stored (see
          <a href="/admin/billing/tax?tab=rates">Exchange rates</a>).
        </p>
      {:else}
        <dl class="ba-grid">
          <div class="ba-due"><dt>VAT total in sterling</dt><dd>{formatMoney(invoice.tax_total_gbp_cents, 'GBP')}</dd></div>
          <div><dt>Rate</dt><dd>{invoice.currency} {invoice.fx_rate} = £1</dd></div>
          <div><dt>Month</dt><dd>{invoice.fx_period}</dd></div>
          <div><dt>Source</dt><dd>{SOURCES[invoice.fx_source] || label(invoice.fx_source)}</dd></div>
        </dl>
      {/if}
    {/if}
  </div>
{/if}
