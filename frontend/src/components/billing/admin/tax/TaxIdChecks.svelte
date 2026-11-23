<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../../ui/index.js';
  import { listTaxIdChecks, recheckTaxId } from '../../../../lib/billingTaxAdminApi.js';
  import { TAX_ID_TONE, label, when } from './taxUi.js';

  export let accountId = '';

  const limit = 50;

  let checks = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = null;
  let account = accountId ? String(accountId) : '';
  let text = '';
  let offset = 0;
  let recheckAccount = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      checks = await listTaxIdChecks({ billing_account_id: account.trim(), offset, limit });
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function search() {
    offset = 0;
    load();
  }

  async function recheck(id) {
    if (!/^[1-9]\d*$/.test(String(id).trim())) {
      error = 'Enter the billing account number to recheck.';
      return;
    }
    working = id;
    error = '';
    success = '';
    try {
      const result = await recheckTaxId(Number(id));
      success = `Account #${id}: the tax ID is now ${label(result.tax_id_status)}${result.check ? `, checked by ${result.check.validator || 'the core format check'}` : ''}.`;
      await load();
    } catch (err) {
      error = err.message;
    } finally {
      working = null;
    }
  }

  $: shown = checks.filter((c) => {
    const needle = text.trim().toLowerCase();
    return !needle || [c.normalised_id, c.country, c.status, c.validator, c.error_code].some((v) => String(v ?? '').toLowerCase().includes(needle));
  });
</script>

<div class="ba-panel">
  <h3>Tax ID checks</h3>
  <p class="ba-muted">Results of tax number checks, newest first. A recheck ignores the cached result and asks the validator again now; it needs a billing account whose profile holds a tax ID.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  <div class="ba-toolbar">
    <label>Billing account<input bind:value={account} inputmode="numeric" placeholder="All" style="width:110px" on:change={search} /></label>
    <label>Find in this page<input bind:value={text} placeholder="Number, country, status" /></label>
    <Button variant="secondary" on:click={search}>Search</Button>
    <label>Recheck account<input bind:value={recheckAccount} inputmode="numeric" style="width:110px" /></label>
    <Button on:click={() => recheck(recheckAccount)} disabled={working !== null}>Recheck now</Button>
  </div>
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if shown.length === 0}
    <p class="ba-empty">No checks.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Checked</th><th>Account</th><th>Number</th><th>Type</th><th>Validator</th><th>Status</th><th>Expires</th><th>Reference</th><th></th></tr></thead>
        <tbody>
          {#each shown as check (check.id)}
            <tr>
              <td>{when(check.checked_at)}</td>
              <td>{check.billing_account_id ? `#${check.billing_account_id}` : '-'}</td>
              <td><span class="ba-mono">{check.country} {check.normalised_id}</span></td>
              <td>{label(check.id_type)}</td>
              <td>{check.validator || 'format only'}</td>
              <td><span class="ba-pill {TAX_ID_TONE[check.status] ?? ''}">{label(check.status)}</span>{#if check.error_code}<div class="ba-hint">{check.error_code}</div>{/if}</td>
              <td>{when(check.expires_at)}</td>
              <td><span class="ba-mono">{check.request_ref || '-'}</span></td>
              <td>
                {#if check.billing_account_id}
                  <button class="ba-link" disabled={working !== null} on:click={() => recheck(check.billing_account_id)}>Recheck</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  <div class="ba-inline" style="margin-top:12px">
    <Button variant="secondary" size="small" disabled={offset === 0} on:click={() => { offset = Math.max(0, offset - limit); load(); }}>Previous</Button>
    <span class="ba-muted">Page {Math.floor(offset / limit) + 1}</span>
    <Button variant="secondary" size="small" disabled={checks.length < limit} on:click={() => { offset += limit; load(); }}>Next</Button>
  </div>
</div>
