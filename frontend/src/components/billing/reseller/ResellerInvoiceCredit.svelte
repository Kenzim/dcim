<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert } from '../../ui/index.js';
  import AdjustmentTable from '../AdjustmentTable.svelte';
  import ApplyCreditForm from '../ApplyCreditForm.svelte';
  import InvoiceBalance from '../InvoiceBalance.svelte';
  import {
    billingErrorMessage,
    resellerApplyCredit,
    resellerDownloadAdjustmentPdf,
    resellerGetBalance,
  } from '../../../lib/billingClientApi.js';
  import { invoiceDueCents, invoiceIsUnpaid } from '../../../lib/billingFormat.js';

  /** The invoice as `GET /api/reseller-panel/invoices/{id}` returns it (with `adjustments`). */
  export let invoice;
  /** Bump to reload the credit balance, for example after a payment. */
  export let refreshKey = 0;

  const dispatch = createEventDispatcher();

  let available = 0;
  let balanceError = '';
  let notesError = '';
  let busyId = null;

  $: notes = invoice?.adjustments || [];
  $: canApply = invoice?.purpose !== 'credit_topup' && invoiceIsUnpaid(invoice);
  $: loadBalance(refreshKey);

  async function loadBalance() {
    balanceError = '';
    try {
      const { balance_cents: balance } = await resellerGetBalance();
      available = Math.max(0, Number(balance) || 0);
    } catch (err) {
      available = 0;
      balanceError = billingErrorMessage(err, 'Your credit balance could not be loaded.');
    }
  }

  async function downloadNote(event) {
    const note = event.detail;
    busyId = note.id;
    notesError = '';
    try {
      await resellerDownloadAdjustmentPdf(note);
    } catch (err) {
      notesError = billingErrorMessage(err, 'The note could not be downloaded.');
    } finally {
      busyId = null;
    }
  }

  const apply = (amountCents, key) => resellerApplyCredit(invoice.id, amountCents, key);

  function applied(event) {
    available = Math.max(0, Number(event.detail.balance_cents) || 0);
    dispatch('applied', event.detail);
  }
</script>

<h3>Balance</h3>
<InvoiceBalance balance={invoice.balance} currency={invoice.currency} />

{#if notes.length}
  <h3>Credit and debit notes</h3>
  {#if notesError}<Alert type="danger">{notesError}</Alert>{/if}
  <AdjustmentTable {notes} showInvoice={false} {busyId} on:download={downloadNote} />
{/if}

{#if canApply}
  {#if balanceError}<Alert type="danger">{balanceError}</Alert>{/if}
  <ApplyCreditForm
    {available}
    due={invoiceDueCents(invoice)}
    currency={invoice.currency}
    {apply}
    on:applied={applied}
  />
{/if}

<style>
  h3 { margin: 18px 0 8px; font-size: 15px; }
</style>
