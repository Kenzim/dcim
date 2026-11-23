<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import {
    createTaxExemption,
    listTaxClasses,
    listTaxExemptions,
    revokeTaxExemption,
    searchCommerceBillingAccounts,
  } from '../../../../lib/billingTaxAdminApi.js';
  import { fromInput, when } from './taxUi.js';

  export let accountId = '';
  export let compact = false;

  let rows = [];
  let classes = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let filterAccount = accountId ? String(accountId) : '';
  let activeOnly = false;

  let creating = false;
  let form = blank();
  let formError = '';
  let accountSearch = '';
  let accountMatches = [];
  let accountSearching = false;
  let accountSearchError = '';
  let chosenAccount = null;
  let revoking = null;
  let revokeError = '';

  onMount(async () => {
    try {
      classes = await listTaxClasses();
    } catch (err) {
      error = err.message;
    }
    await load();
  });

  async function load() {
    loading = true;
    error = '';
    try {
      rows = await listTaxExemptions({ billing_account_id: filterAccount.trim(), active_only: activeOnly ? 'true' : '' });
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function blank() {
    return { billing_account_id: accountId ? String(accountId) : '', reason: '', certificate_ref: '', valid_from: '', valid_to: '', tax_class_ids: [] };
  }

  function openCreate() {
    creating = true;
    form = blank();
    formError = '';
    accountSearch = '';
    accountMatches = [];
    accountSearchError = '';
    chosenAccount = null;
  }

  const accountName = (account) => account.reseller_name || account.username || 'Account';

  async function findAccounts() {
    const text = accountSearch.trim();
    if (!text) {
      accountMatches = [];
      return;
    }
    accountSearching = true;
    accountSearchError = '';
    try {
      accountMatches = await searchCommerceBillingAccounts({ q: text, limit: 8 });
      if (!accountMatches.length) accountSearchError = 'No billing account matches.';
    } catch (err) {
      accountSearchError = err.message;
    } finally {
      accountSearching = false;
    }
  }

  function chooseAccount(account) {
    chosenAccount = account;
    form.billing_account_id = String(account.id);
    accountMatches = [];
    accountSearchError = '';
  }

  function clearAccount() {
    chosenAccount = null;
    form.billing_account_id = '';
  }

  async function save() {
    if (!/^[1-9]\d*$/.test(String(form.billing_account_id).trim())) {
      formError = 'Choose the billing account.';
      return;
    }
    if (!form.reason.trim()) {
      formError = 'A reason is required.';
      return;
    }
    working = true;
    try {
      const body = {
        scope: 'account',
        billing_account_id: Number(form.billing_account_id),
        reason: form.reason.trim(),
        tax_class_ids: form.tax_class_ids.map(Number),
      };
      if (form.certificate_ref.trim()) body.certificate_ref = form.certificate_ref.trim();
      if (form.valid_from) body.valid_from = fromInput(form.valid_from);
      if (form.valid_to) body.valid_to = fromInput(form.valid_to);
      await createTaxExemption(body);
      success = 'Exemption created.';
      creating = false;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      working = false;
    }
  }

  async function confirmRevoke() {
    working = true;
    revokeError = '';
    try {
      await revokeTaxExemption(revoking.id);
      success = `Exemption #${revoking.id} revoked.`;
      revoking = null;
      await load();
    } catch (err) {
      revokeError = err.message;
    } finally {
      working = false;
    }
  }

  function classText(ids) {
    if (!ids?.length) return 'all classes';
    return ids.map((id) => classes.find((c) => c.id === id)?.code || `#${id}`).join(', ');
  }

  function state(row) {
    if (row.revoked_at) return ['revoked', 'bad'];
    const now = new Date();
    if (new Date(row.valid_from) > now) return ['not started', 'info'];
    if (row.valid_to && new Date(row.valid_to) <= now) return ['expired', ''];
    return ['active', 'ok'];
  }
</script>

<div class={compact ? '' : 'ba-panel'}>
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Exemptions</h3>
    <Button size={compact ? 'small' : 'default'} on:click={openCreate}>New exemption</Button>
  </div>
  <p class="ba-muted">An exemption removes tax from an account's invoices, for all classes or only the chosen ones, for as long as it is valid. Revoking stops it applying to later invoices.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if !compact}
    <div class="ba-toolbar">
      <label>Billing account<input bind:value={filterAccount} inputmode="numeric" placeholder="All accounts" style="width:140px" on:change={load} /></label>
      <label class="ba-inline" style="gap:8px;font-weight:500"><input type="checkbox" bind:checked={activeOnly} on:change={load} /> Not revoked</label>
    </div>
  {/if}
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if rows.length === 0}
    <p class="ba-empty">No exemptions.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>#</th><th>Account</th><th>Reason</th><th>Certificate</th><th>Classes</th><th>Valid</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {#each rows as row (row.id)}
            {@const st = state(row)}
            <tr>
              <td>{row.id}</td>
              <td>#{row.billing_account_id}</td>
              <td>{row.reason}</td>
              <td>{row.certificate_ref || '-'}</td>
              <td>{classText(row.tax_class_ids)}</td>
              <td>{when(row.valid_from)} to {row.valid_to ? when(row.valid_to) : 'open'}</td>
              <td><span class="ba-pill {st[1]}" title={row.revoked_at ? `Revoked ${when(row.revoked_at)}` : ''}>{st[0]}</span></td>
              <td>
                {#if !row.revoked_at}
                  <button class="ba-link" on:click={() => { revoking = row; revokeError = ''; }}>Revoke</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if creating}
  <Modal title="New tax exemption" onClose={() => (creating = false)}>
    <form class="ba-form" on:submit|preventDefault={save}>
      {#if accountId}
        <p class="ba-muted">Billing account #{accountId}</p>
      {:else}
        <div class="ba-form">
          <span>Billing account</span>
          {#if chosenAccount}
            <div class="ba-inline">
              <strong>#{chosenAccount.id} {accountName(chosenAccount)}</strong>
              {#if chosenAccount.email}<span class="ba-hint">{chosenAccount.email}</span>{/if}
              <button type="button" class="ba-link" on:click={clearAccount}>Change</button>
            </div>
          {:else}
            <div class="ba-inline">
              <input bind:value={accountSearch} placeholder="Name, email or account number" aria-label="Search billing accounts" on:keydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); findAccounts(); } }} />
              <Button variant="secondary" size="small" on:click={findAccounts} disabled={accountSearching}>Search</Button>
            </div>
            {#if accountSearchError}<span class="ba-hint">{accountSearchError}</span>{/if}
            {#if accountMatches.length}
              <ul class="picker">
                {#each accountMatches as account (account.id)}
                  <li><button type="button" class="ba-link" on:click={() => chooseAccount(account)}>#{account.id} {accountName(account)}{account.email ? ` (${account.email})` : ''}</button></li>
                {/each}
              </ul>
            {/if}
          {/if}
        </div>
      {/if}
      <label>Reason<input bind:value={form.reason} maxlength="255" placeholder="e.g. Charity, certificate on file" /></label>
      <label>Certificate reference (optional)<input bind:value={form.certificate_ref} maxlength="128" /></label>
      <div class="ba-two">
        <label>Valid from (blank for now)<input type="datetime-local" bind:value={form.valid_from} /></label>
        <label>Valid to (blank for open ended)<input type="datetime-local" bind:value={form.valid_to} /></label>
      </div>
      <label>Classes covered (none chosen means all)
        <select multiple size="5" bind:value={form.tax_class_ids}>
          {#each classes as c (c.id)}<option value={String(c.id)}>{c.code}: {c.name}</option>{/each}
        </select>
      </label>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (creating = false)}>Cancel</Button>
      <Button on:click={save} disabled={working}>Create</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if revoking}
  <Modal title="Revoke exemption #{revoking.id}?" onClose={() => (revoking = null)}>
    <p>Revoke the exemption of account #{revoking.billing_account_id} ({revoking.reason})? Later invoices will be taxed again. Published invoices are not changed. This cannot be undone; create a new exemption to restore it.</p>
    {#if revokeError}<div class="ba-error">{revokeError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (revoking = null)}>Cancel</Button>
      <Button variant="danger" on:click={confirmRevoke} disabled={working}>Revoke</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .picker { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
</style>
