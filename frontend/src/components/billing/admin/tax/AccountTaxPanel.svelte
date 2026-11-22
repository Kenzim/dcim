<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../../ui/index.js';
  import {
    getCommerceBillingAccount,
    listTaxIdChecks,
    recheckTaxId,
    updateAccountTax,
  } from '../../../../lib/billingTaxAdminApi.js';
  import TaxExemptions from './TaxExemptions.svelte';
  import { TAX_ID_TONE, label, when } from './taxUi.js';

  export let billingAccountId;

  let account = null;
  let latestCheck = null;
  let recheckedStatus = '';
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let exempt = false;
  let reason = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      const [acct, checks] = await Promise.all([
        getCommerceBillingAccount(billingAccountId),
        listTaxIdChecks({ billing_account_id: billingAccountId, limit: 1 }),
      ]);
      account = acct;
      latestCheck = checks[0] || null;
      exempt = !!acct.tax_exempt;
      reason = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function saveFlag() {
    if (exempt && !reason.trim()) {
      error = 'An exempt account needs a reason.';
      return;
    }
    working = true;
    error = '';
    success = '';
    try {
      const saved = await updateAccountTax(billingAccountId, {
        tax_exempt: exempt,
        tax_exempt_reason: exempt ? reason.trim() : null,
      });
      account = { ...account, tax_exempt: saved.tax_exempt };
      success = saved.tax_exempt ? 'The account is now exempt from tax.' : 'The account is no longer exempt from tax.';
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  async function recheck() {
    working = true;
    error = '';
    success = '';
    try {
      const result = await recheckTaxId(billingAccountId);
      recheckedStatus = result.tax_id_status;
      success = `The tax ID was checked again: ${label(result.tax_id_status)}.`;
      latestCheck = (await listTaxIdChecks({ billing_account_id: billingAccountId, limit: 1 }))[0] || null;
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  $: status = recheckedStatus || latestCheck?.status || (account?.profile?.tax_id ? 'unchecked' : '');
</script>

<div class="ba-panel">
  <h3>Tax <small>billing account #{billingAccountId}</small></h3>
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if !account}
    {#if error}<Alert type="danger">{error}</Alert>{/if}
  {:else}
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    {#if success}<Alert type="success">{success}</Alert>{/if}
    <p class="ba-muted">{account.reseller_name || account.username || 'Account'}{account.email ? ` (${account.email})` : ''}</p>

    <h3 style="margin-top:14px">Exempt from tax</h3>
    <div class="ba-form" style="max-width:520px">
      <label class="ba-check"><input type="checkbox" bind:checked={exempt} /> This whole account is exempt</label>
      {#if exempt}
        <label>Reason<input bind:value={reason} maxlength="255" placeholder={account.tax_exempt ? 'Enter the reason again to save' : 'Why is the account exempt?'} /></label>
        {#if account.tax_exempt}<span class="ba-hint">The stored reason is not shown by the API; saving replaces it.</span>{/if}
      {/if}
      <div class="ba-inline">
        <Button on:click={saveFlag} disabled={working || (!exempt && !account.tax_exempt)}>Save</Button>
        <span class="ba-hint">Currently {account.tax_exempt ? 'exempt' : 'not exempt'}. Recorded in the audit log.</span>
      </div>
    </div>

    <h3 style="margin-top:18px">Tax ID</h3>
    {#if account.profile?.tax_id}
      <dl class="ba-grid">
        <div><dt>Tax ID</dt><dd class="ba-mono">{account.profile.tax_id}</dd></div>
        <div><dt>Country</dt><dd>{account.profile.country || '-'}</dd></div>
        <div><dt>Status</dt><dd><span class="ba-pill {TAX_ID_TONE[status] ?? ''}">{label(status)}</span></dd></div>
        <div><dt>Last check</dt><dd>{latestCheck ? when(latestCheck.checked_at) : 'never'}</dd></div>
        <div><dt>Checked by</dt><dd>{latestCheck ? latestCheck.validator || 'format only' : '-'}</dd></div>
        <div><dt>Result expires</dt><dd>{latestCheck ? when(latestCheck.expires_at) : '-'}</dd></div>
      </dl>
      <div class="ba-actions">
        <Button variant="secondary" on:click={recheck} disabled={working}>Recheck now</Button>
      </div>
    {:else}
      <p class="ba-empty">The profile holds no tax ID.</p>
    {/if}

    <div style="margin-top:18px">
      <TaxExemptions accountId={billingAccountId} compact />
    </div>
  {/if}
</div>
