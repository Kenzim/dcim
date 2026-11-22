<script>
  import { createEventDispatcher } from 'svelte';
  import { Badge, Button } from '../ui/index.js';
  import { formatDate, noteKindLabel, noteOutcomeLabel, noteReasonLabel } from '../../lib/billingFormat.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  /** Credit and debit notes as the notes APIs (or an invoice's `adjustments`) return them. */
  export let notes = [];
  /** Show which invoice each note belongs to. */
  export let showInvoice = true;
  export let busyId = null;
  export let emptyText = 'No credit or debit notes.';

  const dispatch = createEventDispatcher();
</script>

{#if notes.length === 0}
  <p class="empty">{emptyText}</p>
{:else}
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th>Number</th><th>Type</th>{#if showInvoice}<th>Invoice</th>{/if}<th>Reason</th><th>Effect</th><th>Issued</th><th class="num">Total</th><th></th>
        </tr>
      </thead>
      <tbody>
        {#each notes as note (note.id)}
          <tr>
            <td><strong>{note.number_display}</strong></td>
            <td><Badge variant={note.kind === 'debit' ? 'warning' : 'info'} caps={false}>{noteKindLabel(note.kind)}</Badge></td>
            {#if showInvoice}<td>#{note.invoice_id}</td>{/if}
            <td>{noteReasonLabel(note.reason ?? note.reason_code)}</td>
            <td>{noteOutcomeLabel(note.outcome)}</td>
            <td>{formatDate(note.issued_at)}</td>
            <td class="num">{note.kind === 'credit' ? '−' : ''}{formatMoney(note.total_cents, note.currency)}</td>
            <td class="act">
              <Button variant="secondary" size="small" disabled={busyId === note.id} on:click={() => dispatch('download', note)}>
                {busyId === note.id ? 'Preparing…' : 'PDF'}
              </Button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); white-space: nowrap; }
  th { font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .act { text-align: right; }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
</style>
