<script>
  import { Button, Modal } from '../../ui/index.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import { issueCreditNote, issueDebitNote, newIdempotencyKey, parseCents } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  /** @type {'credit' | 'debit'} */
  export let kind = 'credit';
  export let invoice;
  export let onClose;
  export let onDone = () => {};

  const isCredit = kind === 'credit';
  const reasons = isCredit
    ? [['correction', 'Correction'], ['goodwill', 'Goodwill'], ['cancel', 'Cancellation'], ['write_off', 'Write-off'], ['merge', 'Merged invoices']]
    : [['correction', 'Correction'], ['late_fee', 'Late fee']];

  let mode = isCredit ? 'amount' : 'lines';
  let amount = '';
  let reason = reasons[0][0];
  let reasonText = '';
  let lines = [blankLine()];
  let working = false;
  let error = '';
  let key = newIdempotencyKey(kind === 'credit' ? 'credit-note' : 'debit-note');

  $: remaining = invoice?.balance?.remaining_cents ?? 0;
  $: pending = invoice?.balance?.pending_cents ?? 0;
  $: currency = invoice?.currency || 'USD';

  function blankLine() {
    return { description: '', quantity: '1', unit: '', tax: '' };
  }

  function buildLines() {
    return lines.map((line, index) => {
      const unit = parseCents(line.unit, { allowZero: true });
      const tax = line.tax === '' ? 0 : parseCents(line.tax, { allowZero: true });
      const quantity = Number(line.quantity);
      if (!line.description.trim()) throw new Error(`Line ${index + 1} needs a description.`);
      if (unit === null) throw new Error(`Line ${index + 1}: enter the unit price as an amount such as 12.50.`);
      if (tax === null) throw new Error(`Line ${index + 1}: enter the tax as an amount such as 2.50.`);
      if (!Number.isInteger(quantity) || quantity < 1) throw new Error(`Line ${index + 1}: quantity must be a whole number of at least 1.`);
      return { description: line.description.trim(), quantity, unit_cents: unit, tax_cents: tax };
    });
  }

  async function submit() {
    error = '';
    let body;
    try {
      body = { reason, reason_text: reasonText.trim() || undefined };
      if (isCredit && mode === 'amount') {
        const cents = parseCents(amount);
        if (cents === null) throw new Error('Enter the credit amount, for example 12.50.');
        body.amount_cents = cents;
      } else {
        body.lines = buildLines();
      }
    } catch (err) {
      error = err.message;
      return;
    }
    working = true;
    try {
      const note = await (isCredit ? issueCreditNote : issueDebitNote)(invoice.id, body, key);
      onDone(note);
    } catch (err) {
      error = err.message;
      if (err.status) key = newIdempotencyKey(kind === 'credit' ? 'credit-note' : 'debit-note');
    } finally {
      working = false;
    }
  }
</script>

<Modal title={isCredit ? 'Issue credit note' : 'Issue debit note'} size="large" {onClose}>
  <p class="ba-muted">
    {#if isCredit}
      A credit note reduces what invoice {invoice.display_number} is worth. Left to pay: {formatMoney(remaining, currency)}.
      It cannot take the invoice below what is already paid, and it is refused while a payment is pending{pending ? ` (${formatMoney(pending, currency)} is pending now)` : ''}.
      To return money to the customer, request a refund on the payment instead.
    {:else}
      A debit note raises what invoice {invoice.display_number} is worth, for example a late fee or a correction. Amounts are positive.
    {/if}
  </p>
  <form class="ba-form" on:submit|preventDefault={submit}>
    {#if isCredit}
      <label>Credit by
        <select bind:value={mode}>
          <option value="amount">A single amount (tax split in proportion to the invoice)</option>
          <option value="lines">Lines (you set each amount and tax)</option>
        </select>
      </label>
    {/if}
    {#if isCredit && mode === 'amount'}
      <label>Amount ({currency}, tax included)
        <input bind:value={amount} inputmode="decimal" placeholder="0.00" />
        <span class="ba-hint">At most {formatMoney(remaining, currency)}.</span>
      </label>
    {:else}
      <div class="ba-lines">
        {#each lines as line, index}
          <div class="ba-line">
            <label>Description<input bind:value={line.description} maxlength="512" /></label>
            <label>Qty<input bind:value={line.quantity} inputmode="numeric" /></label>
            <label>Unit price<input bind:value={line.unit} inputmode="decimal" placeholder="0.00" /></label>
            <label>Tax<input bind:value={line.tax} inputmode="decimal" placeholder="0.00" /></label>
            <Button variant="secondary" size="small" disabled={lines.length === 1} on:click={() => (lines = lines.filter((_, i) => i !== index))}>Remove</Button>
          </div>
        {/each}
        <div><Button variant="secondary" size="small" on:click={() => (lines = [...lines, blankLine()])}>Add line</Button></div>
      </div>
    {/if}
    <div class="ba-two">
      <label>Reason
        <select bind:value={reason}>
          {#each reasons as [value, label]}<option {value}>{label}</option>{/each}
        </select>
      </label>
      <label>Note for the record (optional)
        <input bind:value={reasonText} maxlength="512" />
      </label>
    </div>
    {#if error}<div class="ba-error">{error}</div>{/if}
    <button type="submit" hidden aria-hidden="true" tabindex="-1"></button>
  </form>
  <svelte:fragment slot="footer">
    <Button variant="secondary" on:click={onClose}>Cancel</Button>
    <Button on:click={submit} disabled={working}>{working ? 'Issuing…' : isCredit ? 'Issue credit note' : 'Issue debit note'}</Button>
  </svelte:fragment>
</Modal>
