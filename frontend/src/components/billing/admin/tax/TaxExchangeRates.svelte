<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { createExchangeRate, deleteExchangeRate, listExchangeRates, previewExchangeRate } from '../../../../lib/billingTaxAdminApi.js';
  import { when } from './taxUi.js';

  let rates = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let filters = { period: '', currency: '' };

  let adding = false;
  let form = { period: '', currency: '', units_per_gbp: '' };
  let formError = '';
  let confirmAdd = false;
  let preview = null;
  let fillConfirmed = false;
  let checking = false;
  let deleting = null;
  let deleteError = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    if (filters.period && !/^\d{4}-(0[1-9]|1[0-2])$/.test(filters.period)) {
      error = 'The period filter is a month as YYYY-MM.';
      loading = false;
      return;
    }
    if (filters.currency && !/^[A-Za-z]{3}$/.test(filters.currency)) {
      error = 'The currency filter is a three-letter code.';
      loading = false;
      return;
    }
    try {
      rates = (await listExchangeRates({ period: filters.period, currency: filters.currency, limit: 500 })).items;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function openAdd() {
    adding = true;
    confirmAdd = false;
    preview = null;
    fillConfirmed = false;
    form = { period: '', currency: '', units_per_gbp: '' };
    formError = '';
  }

  function checkForm() {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(form.period.trim())) return 'Enter the month as YYYY-MM, for example 2026-11.';
    if (!/^[A-Za-z]{3}$/.test(form.currency.trim())) return 'Enter a three-letter currency code.';
    if (form.currency.trim().toUpperCase() === 'GBP') return 'GBP is the base currency and has no rate.';
    if (!/^\d+(\.\d{1,6})?$/.test(form.units_per_gbp.trim()) || Number(form.units_per_gbp) <= 0) {
      return 'Enter the units of the currency per £1 as published by HMRC, with up to six decimals.';
    }
    return '';
  }

  $: pendingCount = preview ? preview.pending_invoices + preview.pending_notes : 0;
  $: change = preview && preview.previous
    ? (Number(form.units_per_gbp) - Number(preview.previous.units_per_gbp)) / Number(preview.previous.units_per_gbp)
    : null;

  async function review() {
    formError = checkForm();
    if (formError) return;
    checking = true;
    try {
      preview = await previewExchangeRate(form.period.trim(), form.currency.trim().toUpperCase());
      fillConfirmed = false;
      confirmAdd = true;
    } catch (err) {
      formError = err.message;
    } finally {
      checking = false;
    }
  }

  async function save() {
    working = true;
    try {
      const row = await createExchangeRate({
        period: form.period.trim(),
        currency: form.currency.trim().toUpperCase(),
        units_per_gbp: form.units_per_gbp.trim(),
        confirm: pendingCount > 0 && fillConfirmed,
      });
      success = `Stored ${row.currency} ${row.units_per_gbp} per £1 for ${row.period}.`
        + (row.filled_invoices ? ` ${row.filled_invoices} invoices waiting for this month's rate, and their notes, are filled in.` : '');
      adding = false;
      await load();
    } catch (err) {
      confirmAdd = false;
      formError = err.message;
    } finally {
      working = false;
    }
  }

  async function confirmDelete() {
    working = true;
    deleteError = '';
    try {
      await deleteExchangeRate(deleting.id);
      success = `Deleted the ${deleting.currency} rate for ${deleting.period}.`;
      deleting = null;
      await load();
    } catch (err) {
      deleteError = err.message;
    } finally {
      working = false;
    }
  }
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Exchange rates for sterling VAT</h3>
    <Button on:click={openAdd}>Enter a rate</Button>
  </div>
  <p class="ba-muted">
    UK VAT on an invoice in another currency is also shown in sterling, at HMRC's monthly rate for the month of the tax point. The daily job fetches the rates; enter HMRC's published figure by hand only when the fetch cannot run. A stored rate is never edited. One that no invoice or note has used may be deleted, to correct a typing mistake.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  <div class="ba-toolbar">
    <label>Month<input bind:value={filters.period} placeholder="YYYY-MM" style="width:110px" on:change={load} /></label>
    <label>Currency<input bind:value={filters.currency} maxlength="3" placeholder="e.g. USD" style="width:90px" on:change={load} /></label>
  </div>
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if rates.length === 0}
    <p class="ba-empty">No rates are stored.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Month</th><th>Currency</th><th class="ba-num">Units per £1</th><th>Source</th><th>Stored</th><th>Hand entered</th><th></th></tr></thead>
        <tbody>
          {#each rates as rate (rate.id)}
            <tr>
              <td>{rate.period}</td>
              <td>{rate.currency}</td>
              <td class="ba-num ba-mono">{rate.units_per_gbp}</td>
              <td>{rate.source}</td>
              <td>{when(rate.fetched_at)}</td>
              <td>
                {#if rate.hand_entered}
                  <span class="ba-pill info">by hand</span>
                  <div class="ba-hint">{rate.compared_at ? `compared with HMRC ${when(rate.compared_at)}` : 'not compared with HMRC yet'}</div>
                {:else}-{/if}
              </td>
              <td><button class="ba-link" on:click={() => { deleting = rate; deleteError = ''; }}>Delete</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if adding}
  <Modal title="Enter an exchange rate" onClose={() => (adding = false)}>
    {#if !confirmAdd}
      <form class="ba-form" on:submit|preventDefault={review}>
        <div class="ba-two">
          <label>Month<input bind:value={form.period} placeholder="YYYY-MM" /></label>
          <label>Currency<input bind:value={form.currency} maxlength="3" placeholder="e.g. USD" /></label>
        </div>
        <label>Units of the currency per £1
          <input bind:value={form.units_per_gbp} inputmode="decimal" placeholder="e.g. 1.265400" />
          <span class="ba-hint">As HMRC publishes it in its monthly exchange rate file.</span>
        </label>
        {#if formError}<div class="ba-error">{formError}</div>{/if}
      </form>
    {:else}
      <p>
        Store <strong>{form.currency.trim().toUpperCase()} {form.units_per_gbp.trim()}</strong> per £1 for <strong>{form.period.trim()}</strong>?
      </p>
      {#if change !== null}
        <p class={Math.abs(change) > 0.05 ? 'ba-error' : 'ba-muted'}>
          {preview.previous.period} was {preview.previous.units_per_gbp} per £1: this is {(change * 100).toFixed(1)}% {change >= 0 ? 'higher' : 'lower'}.{#if Math.abs(change) > 0.05} Check the figure against HMRC's file.{/if}
        </p>
      {/if}
      {#if pendingCount > 0}
        <Alert type="warning">
          Storing this rate fills the sterling VAT of <strong>{preview.pending_invoices}</strong> invoices and <strong>{preview.pending_notes}</strong> notes of that month at once. Their figures cannot be changed afterwards, and the rate can then no longer be deleted.
        </Alert>
        <label class="ba-inline"><input type="checkbox" bind:checked={fillConfirmed} /> I have checked the rate against HMRC's file</label>
      {:else}
        <p class="ba-muted">No invoice or note is waiting for this rate.</p>
      {/if}
      <p class="ba-muted">The entry is audited with your name, and the daily job compares it with HMRC's file once.</p>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    {/if}
    <svelte:fragment slot="footer">
      {#if confirmAdd}
        <Button variant="secondary" on:click={() => (confirmAdd = false)}>Back</Button>
        <Button on:click={save} disabled={working || (pendingCount > 0 && !fillConfirmed)}>Store the rate</Button>
      {:else}
        <Button variant="secondary" on:click={() => (adding = false)}>Cancel</Button>
        <Button on:click={review} disabled={checking}>Review</Button>
      {/if}
    </svelte:fragment>
  </Modal>
{/if}

{#if deleting}
  <Modal title="Delete this rate?" onClose={() => (deleting = null)}>
    <p>Delete the {deleting.currency} rate for {deleting.period} ({deleting.units_per_gbp} per £1)? It is refused if an invoice or note has used it.</p>
    {#if deleteError}<div class="ba-error">{deleteError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (deleting = null)}>Cancel</Button>
      <Button variant="danger" on:click={confirmDelete} disabled={working}>Delete</Button>
    </svelte:fragment>
  </Modal>
{/if}
