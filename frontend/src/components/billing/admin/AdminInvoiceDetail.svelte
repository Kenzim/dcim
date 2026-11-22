<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import { adminCommerceDownloadInvoicePdf, downloadPdfBlob } from '../../../lib/api.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import {
    centsToInput,
    changeInvoiceDueDate,
    getEngines,
    getInvoice,
    listInvoiceAudit,
    markInvoicePaid,
    parseCents,
    voidInvoice,
  } from '../../../lib/billingAdminApi.js';
  import NoteModal from './NoteModal.svelte';
  import RefundModal from './RefundModal.svelte';
  import './billingAdmin.css';

  export let invoiceId;

  const dispatch = createEventDispatcher();
  const UNPAID = ['open', 'pending_action', 'partially_paid', 'overdue', 'collections'];
  const NOTE_STATUSES = ['open', 'pending_action', 'partially_paid', 'overdue', 'collections', 'paid'];

  let invoice = null;
  let history = [];
  let engineMode = '';
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let modal = null;
  let paidForm = blankPaid();
  let voidReason = '';
  let dueDate = '';
  let modalError = '';
  let loadedFor = null;

  $: if (invoiceId && invoiceId !== loadedFor) {
    loadedFor = invoiceId;
    load();
  }

  function blankPaid() {
    return { reason: '', amount: '', writeOff: false };
  }

  async function load({ keepMessages = false } = {}) {
    loading = invoice?.id !== invoiceId;
    error = keepMessages ? error : '';
    if (!keepMessages) success = '';
    try {
      const [detail, engines] = await Promise.all([
        getInvoice(invoiceId),
        getEngines().catch(() => null),
      ]);
      invoice = detail;
      engineMode = engines?.engines?.find((e) => e.engine === 'invoice')?.mode || '';
      history = await loadHistory(detail);
    } catch (err) {
      error = err.message;
      invoice = null;
    } finally {
      loading = false;
    }
  }

  async function loadHistory(detail) {
    if (!detail.billing_account_id) return [];
    try {
      const rows = await listInvoiceAudit({ action: 'invoice.', billing_account_id: detail.billing_account_id, limit: 200 });
      return rows.filter((row) => row.resource_type === 'invoice' && row.resource_id === String(detail.id));
    } catch (_) {
      return [];
    }
  }

  async function afterChange(message) {
    modal = null;
    modalError = '';
    success = message;
    await load({ keepMessages: true });
    dispatch('changed');
  }

  async function run(action, message) {
    working = true;
    modalError = '';
    try {
      await action();
      await afterChange(message);
    } catch (err) {
      modalError = err.message;
    } finally {
      working = false;
    }
  }

  async function downloadPdf() {
    working = true;
    error = '';
    try {
      downloadPdfBlob(await adminCommerceDownloadInvoicePdf(invoice.id), `invoice-${invoice.display_number || invoice.invoice_number}.pdf`);
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  function submitPaid() {
    if (!paidForm.reason.trim()) {
      modalError = 'A reason is required.';
      return;
    }
    const body = { reason: paidForm.reason.trim() };
    if (v2 && paidForm.amount.trim()) {
      const cents = parseCents(paidForm.amount);
      if (cents === null) {
        modalError = 'Enter the amount received, for example 12.50.';
        return;
      }
      body.amount_cents = cents;
      body.write_off_rest = paidForm.writeOff;
    }
    run(() => markInvoicePaid(invoice.id, body), 'Payment recorded.');
  }

  function submitDueDate() {
    run(() => changeInvoiceDueDate(invoice.id, dueDate ? new Date(`${dueDate}T23:59:59`).toISOString() : null), 'Due date changed.');
  }

  function openModal(name) {
    modalError = '';
    success = '';
    if (name === 'paid') {
      paidForm = blankPaid();
      if (v2) paidForm.amount = centsToInput(balance.remaining_cents);
    }
    if (name === 'void') voidReason = '';
    if (name === 'due') dueDate = invoice.due_at ? invoice.due_at.slice(0, 10) : '';
    modal = name;
  }

  function when(value) {
    return value ? new Date(value).toLocaleString() : '-';
  }

  function tone(status) {
    if (status === 'paid') return 'ok';
    if (['void', 'failed', 'refunded'].includes(status)) return 'bad';
    if (['overdue', 'collections', 'partially_paid', 'pending_action'].includes(status)) return 'warn';
    return 'info';
  }

  function label(value) {
    return String(value ?? '').replaceAll('_', ' ');
  }

  function note(row) {
    const meta = row.metadata || {};
    if (row.action === 'invoice.status') {
      return `${label(meta.from)} to ${label(meta.to)}${meta.cause ? `, cause: ${label(meta.cause)}` : ''}${meta.reason ? `, ${meta.reason}` : ''}`;
    }
    return Object.entries(meta).map(([k, v]) => `${label(k)}: ${v}`).join(', ');
  }

  function refundablePayments(inv) {
    return (inv?.payments || []).filter(
      (p) => (p.status === 'succeeded' || p.status === 'partially_refunded') && p.amount_cents - (p.refunded_cents || 0) > 0,
    );
  }

  $: status = invoice?.status_detail || invoice?.status || '';
  $: balance = invoice?.balance || {};
  $: currency = invoice?.currency || 'USD';
  $: v2 = engineMode === 'v2';
  $: unpaid = UNPAID.includes(status);
  $: noteBlock = noteBlockReason(invoice, status, v2, engineMode);
  $: creditBlock = noteBlock || (balance.remaining_cents <= 0 ? 'Nothing is left to credit; the invoice is not owed.' : balance.pending_cents > 0 ? 'A payment is pending at a gateway.' : '');
  $: voidBlock = voidBlockReason(invoice, status, balance);
  $: refundable = refundablePayments(invoice);

  function noteBlockReason(inv, st, isV2, mode) {
    if (!inv) return '';
    if (!isV2) return `Credit and debit notes need the invoice engine in v2 (it is ${mode || 'unknown'}).`;
    if (inv.document_type === 'proforma') return 'A proforma takes no credit or debit notes.';
    if (inv.purpose === 'credit_topup') return 'A credit top-up invoice takes no notes.';
    if (!NOTE_STATUSES.includes(st)) return `Notes cannot be issued on an invoice that is ${label(st)}.`;
    return '';
  }

  function voidBlockReason(inv, st, bal) {
    if (!inv) return '';
    if (!['open', 'overdue', 'pending_action'].includes(st)) return `An invoice that is ${label(st)} cannot be voided.`;
    if (bal.paid_net_cents > 0) return 'Money has been paid on this invoice.';
    if (bal.pending_cents > 0) return 'A payment is pending.';
    if ((inv.adjustments || []).length) return 'A credit or debit note exists.';
    return '';
  }
</script>

{#if loading}
  <div class="ba-state"><Spinner /></div>
{:else if !invoice}
  {#if error}<Alert type="danger">{error}</Alert>{/if}
{:else}
  <div class="head">
    <div>
      <h2>{invoice.document_type === 'proforma' ? 'Proforma' : 'Invoice'} {invoice.display_number}</h2>
      <p class="ba-muted">
        {invoice.description || label(invoice.purpose)} · internal #{invoice.invoice_number}
        {#if invoice.billing_account_id} · account #{invoice.billing_account_id}{/if}
        {#if invoice.reseller_id} · reseller #{invoice.reseller_id}{/if}
      </p>
    </div>
    <span class="ba-pill {tone(status)}">{label(status)}</span>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if status === 'draft'}
    <Alert type="info">This invoice is a draft. There is no admin endpoint to publish or delete a draft yet, so it cannot be changed here.</Alert>
  {/if}
  {#if engineMode && engineMode !== 'v2'}
    <Alert type="info">The invoice engine is {engineMode}. The balance below is the v2 view; notes, partial refunds and refunds to credit only work in v2.</Alert>
  {/if}

  <div class="ba-panel">
    <h3>Balance <small>{formatMoney(invoice.amount_cents, currency)} invoiced, due {when(invoice.due_at)}</small></h3>
    <dl class="ba-grid">
      <div><dt>Total</dt><dd>{formatMoney(balance.total_cents, currency)}</dd></div>
      <div><dt>Debit notes</dt><dd>{formatMoney(balance.debit_cents, currency)}</dd></div>
      <div><dt>Credit notes</dt><dd>{formatMoney(balance.credit_cents, currency)}</dd></div>
      <div><dt>Worth</dt><dd>{formatMoney(balance.worth_cents, currency)}</dd></div>
      <div><dt>Paid (net)</dt><dd>{formatMoney(balance.paid_net_cents, currency)}</dd></div>
      <div><dt>Refunded</dt><dd>{formatMoney(balance.refunded_cents, currency)}</dd></div>
      <div><dt>Pending</dt><dd>{formatMoney(balance.pending_cents, currency)}</dd></div>
      <div class="ba-due"><dt>{balance.due_cents < 0 ? 'Overpaid by' : 'Due'}</dt><dd>{formatMoney(Math.abs(balance.due_cents ?? 0), currency)}</dd></div>
    </dl>
    <div class="ba-actions">
      <Button variant="secondary" on:click={downloadPdf} disabled={working}>Download PDF</Button>
      {#if unpaid}
        <Button on:click={() => openModal('paid')}>Mark paid</Button>
        <Button variant="secondary" on:click={() => openModal('due')}>Change due date</Button>
      {/if}
      <Button variant="secondary" on:click={() => (modal = 'credit')} disabled={!!creditBlock} title={creditBlock}>Issue credit note</Button>
      <Button variant="secondary" on:click={() => (modal = 'debit')} disabled={!!noteBlock} title={noteBlock}>Issue debit note</Button>
      {#if ['open', 'overdue', 'pending_action'].includes(status)}
        <Button variant="danger" on:click={() => openModal('void')} disabled={!!voidBlock} title={voidBlock}>Void</Button>
      {/if}
    </div>
    {#if noteBlock}
      <p class="ba-hint">Notes are unavailable: {noteBlock}</p>
    {:else if creditBlock}
      <p class="ba-hint">Credit notes are unavailable: {creditBlock}</p>
    {/if}
    {#if voidBlock && ['open', 'overdue', 'pending_action'].includes(status)}<p class="ba-hint">Void is unavailable: {voidBlock}</p>{/if}
  </div>

  <div class="ba-panel">
    <h3>Payments</h3>
    {#if !invoice.payments?.length}
      <p class="ba-empty">No payments.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Gateway</th><th>Status</th><th class="ba-num">Amount</th><th class="ba-num">Refunded</th><th>When</th><th></th></tr></thead>
          <tbody>
            {#each invoice.payments as payment (payment.id)}
              <tr>
                <td>{payment.id}</td>
                <td>{payment.gateway}</td>
                <td><span class="ba-pill {tone(payment.status === 'succeeded' ? 'paid' : payment.status)}">{label(payment.status)}</span></td>
                <td class="ba-num">{formatMoney(payment.amount_cents, payment.currency)}</td>
                <td class="ba-num">{formatMoney(payment.refunded_cents, payment.currency)}</td>
                <td>{when(payment.processed_at || payment.created_at)}</td>
                <td>
                  {#if refundable.includes(payment)}
                    <button class="ba-link" on:click={() => (modal = { refund: payment })}>Refund</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="ba-panel">
    <h3>Credit and debit notes</h3>
    {#if !invoice.adjustments?.length}
      <p class="ba-empty">No notes on this invoice.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Number</th><th>Kind</th><th>Reason</th><th>Outcome</th><th class="ba-num">Tax</th><th class="ba-num">Total</th><th>Issued</th></tr></thead>
          <tbody>
            {#each invoice.adjustments as row (row.id)}
              <tr>
                <td>{row.number_display}</td>
                <td>{row.kind}</td>
                <td>{label(row.reason_code)}</td>
                <td>{label(row.outcome)}</td>
                <td class="ba-num">{formatMoney(row.tax_cents, row.currency)}</td>
                <td class="ba-num">{formatMoney(row.total_cents, row.currency)}</td>
                <td>{when(row.issued_at)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="ba-panel">
    <h3>Refunds</h3>
    {#if !invoice.refunds?.length}
      <p class="ba-empty">No refunds on this invoice.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Payment</th><th>To</th><th>Reason</th><th>Status</th><th class="ba-num">Amount</th><th>Requested</th></tr></thead>
          <tbody>
            {#each invoice.refunds as row (row.id)}
              <tr>
                <td>{row.id}</td>
                <td>#{row.payment_id}</td>
                <td>{row.destination}</td>
                <td>{label(row.reason)}</td>
                <td><span class="ba-pill {row.status === 'succeeded' ? 'ok' : row.status === 'failed' ? 'bad' : 'warn'}">{row.status}</span></td>
                <td class="ba-num">{formatMoney(row.amount_cents, row.currency)}</td>
                <td>{when(row.requested_at)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="ba-panel">
    <h3>Status history <small>latest 200 invoice events of this account</small></h3>
    {#if !history.length}
      <p class="ba-empty">No recorded changes.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>When</th><th>Event</th><th>Detail</th><th>By</th></tr></thead>
          <tbody>
            {#each history as row (row.id)}
              <tr>
                <td>{when(row.created_at)}</td>
                <td>{row.action.replace('invoice.', '')}</td>
                <td>{note(row)}</td>
                <td>{row.actor_user_id ? `user #${row.actor_user_id}` : 'system'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}

{#if invoice}
  {#if modal === 'credit' || modal === 'debit'}
    <NoteModal
      kind={modal}
      {invoice}
      onClose={() => (modal = null)}
      onDone={(created) => afterChange(`${created.kind === 'credit' ? 'Credit' : 'Debit'} note ${created.number_display} issued.`)}
    />
  {/if}

  {#if modal && modal.refund}
    <RefundModal
      {invoice}
      payment={modal.refund}
      {engineMode}
      onClose={() => { const done = modal; modal = null; if (done) load({ keepMessages: true }).then(() => dispatch('changed')); }}
    />
  {/if}

  {#if modal === 'paid'}
    <Modal title="Record a manual payment" onClose={() => (modal = null)}>
      <form class="ba-form" on:submit|preventDefault={submitPaid}>
        <p class="ba-muted">Left to pay on invoice {invoice.display_number}: {formatMoney(balance.remaining_cents, currency)}.</p>
        {#if v2}
          <label>Amount received ({currency})
            <input bind:value={paidForm.amount} inputmode="decimal" />
          </label>
          <label class="ba-check"><input type="checkbox" bind:checked={paidForm.writeOff} /> Write off what is still owed after this amount (issues a credit note)</label>
        {/if}
        <label>Reason
          <textarea bind:value={paidForm.reason} rows="3" placeholder="e.g. Wire transfer ref 12345"></textarea>
        </label>
        {#if modalError}<div class="ba-error">{modalError}</div>{/if}
      </form>
      <svelte:fragment slot="footer">
        <Button variant="secondary" on:click={() => (modal = null)}>Cancel</Button>
        <Button on:click={submitPaid} disabled={working}>Record payment</Button>
      </svelte:fragment>
    </Modal>
  {/if}

  {#if modal === 'void'}
    <Modal title="Void invoice?" onClose={() => (modal = null)}>
      <p>Void invoice {invoice.display_number}? The number is kept and never reused. This cannot be undone.</p>
      <label class="ba-form">Reason (optional)<input bind:value={voidReason} maxlength="255" /></label>
      {#if modalError}<div class="ba-error">{modalError}</div>{/if}
      <svelte:fragment slot="footer">
        <Button variant="secondary" on:click={() => (modal = null)}>Cancel</Button>
        <Button variant="danger" on:click={() => run(() => voidInvoice(invoice.id, voidReason.trim()), 'Invoice voided.')} disabled={working}>Void invoice</Button>
      </svelte:fragment>
    </Modal>
  {/if}

  {#if modal === 'due'}
    <Modal title="Change due date" onClose={() => (modal = null)}>
      <label class="ba-form">Due date (leave empty to clear)<input type="date" bind:value={dueDate} /></label>
      {#if modalError}<div class="ba-error">{modalError}</div>{/if}
      <svelte:fragment slot="footer">
        <Button variant="secondary" on:click={() => (modal = null)}>Cancel</Button>
        <Button on:click={submitDueDate} disabled={working}>Save</Button>
      </svelte:fragment>
    </Modal>
  {/if}
{/if}

<style>
  .head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  .head h2 { margin: 0 0 4px; font-size: 20px; }
  .head p { margin: 0; }
</style>
