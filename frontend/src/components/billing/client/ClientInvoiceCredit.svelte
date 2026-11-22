<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert } from '../../ui/index.js';
  import AdjustmentTable from '../AdjustmentTable.svelte';
  import ApplyCreditForm from '../ApplyCreditForm.svelte';
  import InvoiceBalance from '../InvoiceBalance.svelte';
  import {
    billingErrorMessage,
    clientApplyCredit,
    clientDownloadAdjustmentPdf,
    clientListAdjustments,
  } from '../../../lib/billingClientApi.js';
  import { invoiceDueCents, invoiceIsUnpaid } from '../../../lib/billingFormat.js';

  /** The invoice as `GET /api/client/commerce/invoices/{id}` returns it. */
  export let invoice;
  /** The `/credit` summary, or null when credit is off for this account. */
  export let credit = null;
  export let impersonating = false;

  const dispatch = createEventDispatcher();

  let notes = [];
  let notesError = '';
  let busyId = null;

  $: hasNotes = Boolean(invoice?.balance && (invoice.balance.credit_cents > 0 || invoice.balance.debit_cents > 0));
  $: if (invoice?.id !== undefined) loadNotes(invoice.id, hasNotes);
  $: canApply = Boolean(credit?.enabled) && invoice?.purpose !== 'credit_topup' && invoiceIsUnpaid(invoice);

  let loadedFor = null;
  async function loadNotes(invoiceId, wanted) {
    const marker = `${invoiceId}:${wanted}`;
    if (loadedFor === marker) return;
    loadedFor = marker;
    notesError = '';
    if (!wanted) {
      notes = [];
      return;
    }
    try {
      const all = await clientListAdjustments({ limit: 100 });
      if (loadedFor === marker) notes = all.filter((note) => note.invoice_id === invoiceId);
    } catch (err) {
      notes = [];
      notesError = billingErrorMessage(err, 'Credit and debit notes could not be loaded.');
    }
  }

  async function downloadNote(event) {
    const note = event.detail;
    busyId = note.id;
    notesError = '';
    try {
      await clientDownloadAdjustmentPdf(note);
    } catch (err) {
      notesError = billingErrorMessage(err, 'The note could not be downloaded.');
    } finally {
      busyId = null;
    }
  }

  const apply = (amountCents, key) => clientApplyCredit(invoice.id, amountCents, key);
</script>

<h3>Balance</h3>
<InvoiceBalance balance={invoice.balance} currency={invoice.currency} />

{#if hasNotes}
  <h3>Credit and debit notes</h3>
  {#if notesError}<Alert type="danger">{notesError}</Alert>{/if}
  <AdjustmentTable {notes} showInvoice={false} compact {busyId} emptyText="No notes to show for this invoice." on:download={downloadNote} />
{/if}

{#if canApply}
  <ApplyCreditForm
    available={credit.available_cents}
    due={invoiceDueCents(invoice)}
    currency={invoice.currency}
    {apply}
    blockedReason={impersonating ? 'Credit cannot be applied while viewing as this client.' : ''}
    on:applied={(event) => dispatch('applied', event.detail)}
  />
{/if}

<style>
  h3 { margin: 18px 0 8px; font-size: 15px; }
</style>
