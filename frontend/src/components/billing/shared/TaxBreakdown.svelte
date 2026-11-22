<script>
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import { legalNoteView, taxEntryLabel } from '../../../lib/billingTaxFormat.js';

  /** A quote or invoice that carries `tax_summary`, `legal_notes` and `reverse_charge`. */
  export let source;
  export let currency = 'USD';

  $: summary = Array.isArray(source?.tax_summary) ? source.tax_summary : [];
  $: notes = (Array.isArray(source?.legal_notes) ? source.legal_notes : []).map(legalNoteView);
  $: reverseCharge = source?.reverse_charge === true || summary.some((entry) => entry.reverse_charge);
</script>

{#if summary.length}
  <table class="tax" aria-label="Tax summary">
    <thead>
      <tr><th scope="col">Tax</th><th scope="col" class="num">On</th><th scope="col" class="num">Amount</th></tr>
    </thead>
    <tbody>
      {#each summary as entry}
        <tr>
          <th scope="row">{taxEntryLabel(entry)}{#if entry.jurisdiction}<small>{entry.jurisdiction}</small>{/if}</th>
          <td class="num">{formatMoney(entry.base_cents, currency)}</td>
          <td class="num">{formatMoney(entry.tax_cents, currency)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
{#if reverseCharge}
  <p class="reverse">The reverse charge applies: no VAT is charged here, and the VAT due is accounted for by you as the customer.</p>
{/if}
{#if notes.length}
  <ul class="notes" aria-label="Tax notes">
    {#each notes as note}<li>{note.text}</li>{/each}
  </ul>
{/if}

<style>
  .tax { width: 100%; border-collapse: collapse; font-size: 13px; margin: 0 0 10px; }
  .tax th, .tax td { padding: 6px 8px; border-bottom: 1px solid var(--border-color); text-align: left; font-weight: 500; }
  .tax thead th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); font-weight: 600; }
  .tax small { margin-left: 6px; color: var(--text-tertiary); }
  .num { text-align: right !important; font-variant-numeric: tabular-nums; }
  .reverse { margin: 0 0 8px; padding: 8px 10px; border-radius: 8px; background: var(--bg-secondary); font-size: 13px; color: var(--text-secondary); }
  .notes { margin: 0 0 12px; padding-left: 18px; font-size: 12px; color: var(--text-tertiary); }
</style>
