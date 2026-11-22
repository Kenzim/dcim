<script>
  import { onMount } from 'svelte';
  import { Alert, Spinner } from '../../ui/index.js';
  import AdjustmentTable from '../AdjustmentTable.svelte';
  import {
    billingErrorMessage,
    clientDownloadAdjustmentPdf,
    clientListAdjustments,
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
      notes = await clientListAdjustments({ kind, limit: 100 });
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
      await clientDownloadAdjustmentPdf(note);
    } catch (err) {
      error = billingErrorMessage(err, 'The note could not be downloaded.');
    } finally {
      busyId = null;
    }
  }
</script>

<div class="panel">
  <div class="bar">
    <p>Credit notes reduce what you owe on an invoice; debit notes add to it.</p>
    <label>Type
      <select bind:value={kind} on:change={load}>
        <option value="">All</option>
        <option value="credit">Credit notes</option>
        <option value="debit">Debit notes</option>
      </select>
    </label>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    <AdjustmentTable {notes} {busyId} emptyText="No credit or debit notes have been issued on your account." on:download={download} />
  {/if}
</div>

<style>
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 16px; box-shadow: var(--shadow-sm); }
  .bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .bar p { margin: 0; font-size: 14px; color: var(--text-secondary); }
  label { font-size: 13px; font-weight: 600; }
  select { margin-left: 8px; padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 120px; display: grid; place-items: center; gap: 12px; }
</style>
