<script>
  import { onMount } from 'svelte';
  import { Alert, Spinner } from '../../ui/index.js';
  import AdjustmentTable from '../AdjustmentTable.svelte';
  import {
    billingErrorMessage,
    resellerDownloadAdjustmentPdf,
    resellerListAdjustments,
  } from '../../../lib/billingClientApi.js';

  let notes = [];
  let kind = '';
  let loading = true;
  let busyId = null;
  let error = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      notes = await resellerListAdjustments({ kind, limit: 100 });
    } catch (err) {
      notes = [];
      error = billingErrorMessage(err, 'Credit and debit notes could not be loaded.');
    } finally {
      loading = false;
    }
  }

  async function download(event) {
    const note = event.detail;
    busyId = note.id;
    error = '';
    try {
      await resellerDownloadAdjustmentPdf(note);
    } catch (err) {
      error = billingErrorMessage(err, 'The note could not be downloaded.');
    } finally {
      busyId = null;
    }
  }
</script>

<section aria-labelledby="notes-title">
  <div class="heading">
    <div>
      <h1 id="notes-title">Credit and debit notes</h1>
      <p>Credit notes reduce what you owe on an invoice; debit notes add to it.</p>
    </div>
    <label>Type
      <select bind:value={kind} on:change={load}>
        <option value="">All</option>
        <option value="credit">Credit notes</option>
        <option value="debit">Debit notes</option>
      </select>
    </label>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  <div class="panel">
    {#if loading}
      <div class="state"><Spinner /><span>Loading notes…</span></div>
    {:else}
      <AdjustmentTable {notes} {busyId} emptyText="No credit or debit notes have been issued on your account." on:download={download} />
    {/if}
  </div>
</section>

<style>
  .heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 22px; }
  h1 { margin: 0 0 6px; font-size: 32px; }
  .heading p { margin: 0; color: var(--text-secondary); }
  .heading label { font-size: 13px; color: var(--text-secondary); }
  select { margin-left: 6px; padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); padding: 16px; }
  .state { min-height: 160px; display: grid; place-items: center; align-content: center; gap: 12px; }
  @media (max-width: 780px) { .heading { align-items: flex-start; flex-direction: column; } }
</style>
