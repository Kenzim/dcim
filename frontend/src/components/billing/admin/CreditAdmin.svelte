<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import {
    adjustResellerCredit,
    applyResellerCredit,
    getEngines,
    getResellerLedger,
    listBillingAccounts,
    listResellerAccounts,
    listResellerInvoices,
    newIdempotencyKey,
    parseCents,
  } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  const UNPAID = ['open', 'pending_action', 'partially_paid', 'overdue', 'collections'];

  let tab = 'reseller';
  let creditMode = '';

  let resellers = [];
  let resellerId = '';
  let ledger = [];
  let invoices = [];
  let loading = true;
  let ledgerLoading = false;
  let error = '';
  let success = '';
  let working = false;

  let accounts = [];
  let accountSearch = '';
  let accountsLoading = false;

  let adjusting = false;
  let adjustForm = { direction: 'add', amount: '', reason: '' };
  let adjustError = '';
  let adjustKey = '';

  let applying = null;
  let applyAmount = '';
  let applyError = '';
  let applyKey = '';

  onMount(async () => {
    try {
      const [list, engines] = await Promise.all([listResellerAccounts(), getEngines().catch(() => null)]);
      resellers = list;
      creditMode = engines?.engines?.find((e) => e.engine === 'credit')?.mode || '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  });

  async function loadReseller() {
    ledger = [];
    invoices = [];
    if (!resellerId) return;
    ledgerLoading = true;
    error = '';
    try {
      [ledger, invoices] = await Promise.all([
        getResellerLedger(resellerId, { limit: 100 }),
        listResellerInvoices({ reseller_id: resellerId, limit: 100 }),
      ]);
    } catch (err) {
      error = err.message;
    } finally {
      ledgerLoading = false;
    }
  }

  async function refreshAfterChange() {
    const list = await listResellerAccounts();
    resellers = list;
    await loadReseller();
  }

  async function loadAccounts() {
    accountsLoading = true;
    error = '';
    try {
      accounts = await listBillingAccounts({ limit: 100, ...(accountSearch.trim() ? { q: accountSearch.trim() } : {}) });
    } catch (err) {
      error = err.message;
    } finally {
      accountsLoading = false;
    }
  }

  function openAdjust() {
    adjusting = true;
    adjustForm = { direction: 'add', amount: '', reason: '' };
    adjustError = '';
    adjustKey = newIdempotencyKey('admin-adjustment');
  }

  async function submitAdjust() {
    const cents = parseCents(adjustForm.amount);
    if (cents === null) {
      adjustError = 'Enter the amount as for example 25.00. Use the direction to add or remove.';
      return;
    }
    if (!adjustForm.reason.trim()) {
      adjustError = 'A reason is required; it is recorded in the ledger.';
      return;
    }
    working = true;
    try {
      const result = await adjustResellerCredit(resellerId, {
        amount_cents: adjustForm.direction === 'add' ? cents : -cents,
        reason: adjustForm.reason.trim(),
        idempotency_key: adjustKey,
      });
      success = `Credit adjusted. The new balance is ${formatMoney(result.new_balance_cents, selected?.currency || 'USD')}.`;
      adjusting = false;
      await refreshAfterChange();
    } catch (err) {
      adjustError = err.message;
    } finally {
      working = false;
    }
  }

  function openApply(invoice) {
    applying = invoice;
    applyAmount = '';
    applyError = '';
    applyKey = newIdempotencyKey('apply-credit');
  }

  async function submitApply() {
    const body = { idempotency_key: applyKey };
    if (applyAmount.trim()) {
      const cents = parseCents(applyAmount);
      if (cents === null) {
        applyError = 'Enter the amount as for example 10.00, or leave it empty to apply as much as possible.';
        return;
      }
      body.amount_cents = cents;
    }
    working = true;
    try {
      const result = await applyResellerCredit(applying.id, body);
      success = `Applied ${formatMoney(result.applied_cents, applying.currency)} to invoice #${applying.invoice_number}.`;
      applying = null;
      await refreshAfterChange();
    } catch (err) {
      applyError = err.message;
    } finally {
      working = false;
    }
  }

  function label(value) {
    return String(value ?? '').replaceAll('_', ' ');
  }

  function when(value) {
    return value ? new Date(value).toLocaleString() : '-';
  }

  $: selected = resellers.find((r) => String(r.id) === String(resellerId));
  $: currency = invoices[0]?.currency || 'USD';
  $: payable = invoices.filter(
    (inv) => UNPAID.includes(inv.status_detail || inv.status) && inv.purpose !== 'credit_topup' && (inv.balance?.remaining_cents ?? inv.remaining_cents) > 0,
  );
</script>

<PageHeader title="Account Credit" />

<div class="ba-page">
  <div class="ba-actions" style="margin-top:0">
    <Button variant={tab === 'reseller' ? 'primary' : 'secondary'} on:click={() => (tab = 'reseller')}>Reseller credit</Button>
    <Button variant={tab === 'retail' ? 'primary' : 'secondary'} on:click={() => { tab = 'retail'; if (!accounts.length) loadAccounts(); }}>Retail account balances</Button>
  </div>
  {#if creditMode}<p class="ba-hint">Credit engine: <span class="ba-pill {creditMode === 'v2' ? 'ok' : 'info'}">{creditMode}</span></p>{/if}
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if tab === 'reseller'}
    {#if loading}
      <div class="ba-state"><Spinner /></div>
    {:else}
      <div class="ba-toolbar">
        <label>Reseller
          <select bind:value={resellerId} on:change={loadReseller}>
            <option value="">Choose a reseller</option>
            {#each resellers as reseller (reseller.id)}
              <option value={reseller.id}>{reseller.user.username} ({reseller.user.email})</option>
            {/each}
          </select>
        </label>
        {#if selected}<Button on:click={openAdjust}>Adjust credit</Button>{/if}
      </div>

      {#if selected}
        <div class="ba-panel">
          <h3>Balance</h3>
          <dl class="ba-grid">
            <div class="ba-due"><dt>Credit balance</dt><dd>{formatMoney(selected.balance_cents, currency)}</dd></div>
            <div><dt>Status</dt><dd>{label(selected.status)}</dd></div>
            <div><dt>Charge preference</dt><dd>{label(selected.charge_preference)}</dd></div>
          </dl>
        </div>

        <div class="ba-panel">
          <h3>Apply credit to an invoice</h3>
          {#if ledgerLoading}
            <div class="ba-state"><Spinner /></div>
          {:else if payable.length === 0}
            <p class="ba-empty">No unpaid invoices for this reseller.</p>
          {:else}
            <div class="ba-scroll">
              <table class="ba-table">
                <thead><tr><th>Invoice</th><th>Status</th><th>Description</th><th class="ba-num">Due</th><th>Due date</th><th></th></tr></thead>
                <tbody>
                  {#each payable as invoice (invoice.id)}
                    <tr>
                      <td>#{invoice.invoice_number}</td>
                      <td>{label(invoice.status_detail || invoice.status)}</td>
                      <td>{invoice.description || label(invoice.purpose)}</td>
                      <td class="ba-num">{formatMoney(invoice.balance?.remaining_cents ?? invoice.remaining_cents, invoice.currency)}</td>
                      <td>{when(invoice.due_at)}</td>
                      <td><button class="ba-link" on:click={() => openApply(invoice)} disabled={selected.balance_cents <= 0} title={selected.balance_cents <= 0 ? 'No credit to apply' : ''}>Apply credit</button></td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>

        <div class="ba-panel">
          <h3>Ledger <small>latest 100 entries</small></h3>
          {#if ledgerLoading}
            <div class="ba-state"><Spinner /></div>
          {:else if ledger.length === 0}
            <p class="ba-empty">No ledger entries.</p>
          {:else}
            <div class="ba-scroll">
              <table class="ba-table">
                <thead><tr><th>When</th><th>Type</th><th>Description</th><th>Reference</th><th class="ba-num">Amount</th><th class="ba-num">Balance after</th></tr></thead>
                <tbody>
                  {#each ledger as entry (entry.id)}
                    <tr>
                      <td>{when(entry.created_at)}</td>
                      <td>{label(entry.entry_type)}</td>
                      <td>{entry.description || '-'}</td>
                      <td>{entry.invoice_id ? `invoice ${entry.invoice_id}` : entry.reference_type ? label(entry.reference_type) : '-'}{entry.created_by_user_id ? `, by user ${entry.created_by_user_id}` : ''}</td>
                      <td class="ba-num">{formatMoney(entry.amount_cents, currency)}</td>
                      <td class="ba-num">{formatMoney(entry.balance_after_cents, currency)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>
      {/if}
    {/if}
  {:else}
    <p class="ba-muted">
      Balances of retail billing accounts. There is no admin ledger, adjustment or apply-credit endpoint for retail accounts yet, so this view is read only.
    </p>
    <div class="ba-toolbar">
      <label>Search<input bind:value={accountSearch} placeholder="Username or email" on:change={loadAccounts} /></label>
      <Button variant="secondary" on:click={loadAccounts}>Search</Button>
    </div>
    {#if accountsLoading}
      <div class="ba-state"><Spinner /></div>
    {:else if accounts.length === 0}
      <p class="ba-empty">No billing accounts.</p>
    {:else}
      <div class="ba-panel ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Owner</th><th>Type</th><th>Status</th><th class="ba-num">Credit balance</th></tr></thead>
          <tbody>
            {#each accounts as account (account.id)}
              <tr>
                <td>{account.id}</td>
                <td>{account.reseller_name || account.username || '-'}<div class="ba-hint">{account.email || ''}</div></td>
                <td>{label(account.account_type)}</td>
                <td>{label(account.status)}</td>
                <td class="ba-num">{formatMoney(account.credit_balance_cents, account.currency)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

{#if adjusting}
  <Modal title="Adjust credit for {selected?.user.username}" onClose={() => (adjusting = false)}>
    <p class="ba-muted">Current balance {formatMoney(selected?.balance_cents, currency)}. The adjustment is written to the ledger with your user and the reason, and cannot be edited afterwards.</p>
    <form class="ba-form" on:submit|preventDefault={submitAdjust}>
      <div class="ba-two">
        <label>Direction
          <select bind:value={adjustForm.direction}><option value="add">Add credit</option><option value="remove">Remove credit</option></select>
        </label>
        <label>Amount<input bind:value={adjustForm.amount} inputmode="decimal" placeholder="0.00" /></label>
      </div>
      <label>Reason<textarea bind:value={adjustForm.reason} rows="3" maxlength="512"></textarea></label>
      {#if adjustError}<div class="ba-error">{adjustError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (adjusting = false)}>Cancel</Button>
      <Button on:click={submitAdjust} disabled={working}>Record adjustment</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if applying}
  <Modal title="Apply credit to invoice #{applying.invoice_number}" onClose={() => (applying = null)}>
    <p class="ba-muted">Due {formatMoney(applying.balance?.remaining_cents ?? applying.remaining_cents, applying.currency)}; credit available {formatMoney(selected?.balance_cents, applying.currency)}.</p>
    <label class="ba-form">Amount (leave empty to apply as much as possible)
      <input bind:value={applyAmount} inputmode="decimal" placeholder="0.00" />
    </label>
    {#if applyError}<div class="ba-error">{applyError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (applying = null)}>Cancel</Button>
      <Button on:click={submitApply} disabled={working}>Apply credit</Button>
    </svelte:fragment>
  </Modal>
{/if}
