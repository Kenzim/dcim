<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import { downloadPdfBlob } from '../../../lib/api.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import { downloadNotePdf, getNote, listNotes } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  const limit = 50;

  let notes = [];
  let filters = { kind: '', billing_account_id: '', reseller_id: '' };
  let offset = 0;
  let hasMore = false;
  let loading = true;
  let error = '';
  let detail = null;
  let working = false;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      const rows = await listNotes({
        kind: filters.kind,
        billing_account_id: filters.billing_account_id.trim(),
        reseller_id: filters.reseller_id.trim(),
        limit: limit + 1,
        offset,
      });
      hasMore = rows.length > limit;
      notes = rows.slice(0, limit);
    } catch (err) {
      error = err.message;
      notes = [];
    } finally {
      loading = false;
    }
  }

  function applyFilters() {
    offset = 0;
    load();
  }

  async function pdf(note) {
    working = true;
    error = '';
    try {
      downloadPdfBlob(await downloadNotePdf(note.id), `${note.kind}-note-${note.number_display}.pdf`);
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  async function open(note) {
    error = '';
    try {
      detail = await getNote(note.id);
    } catch (err) {
      error = err.message;
    }
  }

  function label(value) {
    return String(value ?? '').replaceAll('_', ' ');
  }

  function when(value) {
    return value ? new Date(value).toLocaleString() : '-';
  }
</script>

<PageHeader title="Credit and Debit Notes" />

<div class="ba-page">
  <div class="ba-toolbar">
    <label>Kind
      <select bind:value={filters.kind} on:change={applyFilters}>
        <option value="">Both</option><option value="credit">Credit notes</option><option value="debit">Debit notes</option>
      </select>
    </label>
    <label>Billing account id
      <input bind:value={filters.billing_account_id} inputmode="numeric" placeholder="Any" on:change={applyFilters} />
    </label>
    <label>Reseller id
      <input bind:value={filters.reseller_id} inputmode="numeric" placeholder="Any" on:change={applyFilters} />
    </label>
  </div>
  <p class="ba-muted">Notes are issued from an invoice. Open Invoices in the Billing menu, choose an invoice and use its note buttons.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if notes.length === 0}
    <p class="ba-empty">No notes match.</p>
  {:else}
    <div class="ba-panel ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Number</th><th>Kind</th><th>Invoice</th><th>Owner</th><th>Reason</th><th>Outcome</th><th class="ba-num">Tax</th><th class="ba-num">Total</th><th>Issued</th><th></th></tr></thead>
        <tbody>
          {#each notes as note (note.id)}
            <tr>
              <td><button class="ba-link" on:click={() => open(note)}>{note.number_display}</button></td>
              <td>{note.kind}</td>
              <td>#{note.invoice_id}</td>
              <td>{note.reseller_id ? `reseller #${note.reseller_id}` : note.billing_account_id ? `account #${note.billing_account_id}` : '-'}</td>
              <td>{label(note.reason)}</td>
              <td>{label(note.outcome)}</td>
              <td class="ba-num">{formatMoney(note.tax_cents, note.currency)}</td>
              <td class="ba-num">{formatMoney(note.total_cents, note.currency)}</td>
              <td>{when(note.issued_at)}</td>
              <td><button class="ba-link" disabled={working} on:click={() => pdf(note)}>PDF</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="ba-inline">
      <Button variant="secondary" size="small" disabled={offset === 0} on:click={() => { offset = Math.max(0, offset - limit); load(); }}>Previous</Button>
      <span class="ba-muted">Showing {offset + 1} to {offset + notes.length}</span>
      <Button variant="secondary" size="small" disabled={!hasMore} on:click={() => { offset += limit; load(); }}>Next</Button>
    </div>
  {/if}
</div>

{#if detail}
  <Modal title="{detail.kind === 'credit' ? 'Credit' : 'Debit'} note {detail.number_display}" size="large" onClose={() => (detail = null)}>
    <p class="ba-muted">
      On invoice #{detail.invoice_id}, {label(detail.reason)}{detail.reason_text ? `: ${detail.reason_text}` : ''}. {label(detail.outcome)}, issued {when(detail.issued_at)}
      {#if detail.created_by_user_id} by user #{detail.created_by_user_id}{/if}.
    </p>
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Description</th><th class="ba-num">Qty</th><th class="ba-num">Unit</th><th class="ba-num">Tax</th><th class="ba-num">Total</th></tr></thead>
        <tbody>
          {#each detail.lines as line (line.id)}
            <tr>
              <td>{line.description}</td>
              <td class="ba-num">{line.quantity}</td>
              <td class="ba-num">{formatMoney(line.unit_cents, detail.currency)}</td>
              <td class="ba-num">{formatMoney(line.tax_cents, detail.currency)}</td>
              <td class="ba-num">{formatMoney(line.total_cents, detail.currency)}</td>
            </tr>
          {/each}
          <tr><td colspan="4" class="ba-num"><strong>Subtotal</strong></td><td class="ba-num">{formatMoney(detail.subtotal_cents, detail.currency)}</td></tr>
          <tr><td colspan="4" class="ba-num"><strong>Tax</strong></td><td class="ba-num">{formatMoney(detail.tax_cents, detail.currency)}</td></tr>
          <tr><td colspan="4" class="ba-num"><strong>Total</strong></td><td class="ba-num"><strong>{formatMoney(detail.total_cents, detail.currency)}</strong></td></tr>
        </tbody>
      </table>
    </div>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => pdf(detail)} disabled={working}>Download PDF</Button>
      <Button on:click={() => (detail = null)}>Close</Button>
    </svelte:fragment>
  </Modal>
{/if}
