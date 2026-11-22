<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import {
    createSeries,
    deleteSeries,
    listSeries,
    makeSeriesDefault,
    updateSeries,
  } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  const TYPES = [
    ['invoice', 'Invoice'],
    ['proforma', 'Proforma'],
    ['credit_note', 'Credit note'],
    ['debit_note', 'Debit note'],
  ];

  let series = [];
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let form = null;
  let formError = '';
  let confirmDelete = null;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      series = await listSeries();
    } catch (err) {
      error = err.message;
      series = [];
    } finally {
      loading = false;
    }
  }

  function typeLabel(value) {
    return TYPES.find(([v]) => v === value)?.[1] || value;
  }

  function openCreate() {
    formError = '';
    form = {
      id: null,
      inUse: false,
      code: '',
      document_type: 'invoice',
      name: '',
      format: '{SERIES}-{NUMBER}',
      number_padding: 5,
      number_source: 'own',
      gapless: false,
      reset_period: 'never',
      start_value: '',
      enabled: true,
      original: null,
    };
  }

  function openEdit(row) {
    formError = '';
    form = {
      id: row.id,
      inUse: row.in_use,
      code: row.code,
      document_type: row.document_type,
      name: row.name,
      format: row.format,
      number_padding: row.number_padding,
      number_source: row.number_source,
      gapless: row.gapless,
      reset_period: row.reset_period,
      start_value: '',
      enabled: row.enabled,
      original: row,
    };
  }

  function preview(f) {
    if (!f) return '';
    const now = new Date();
    const value = String(f.start_value || 1).padStart(Number(f.number_padding) || 0, '0');
    return f.format
      .replaceAll('{SERIES}', f.code || 'CODE')
      .replaceAll('{NUMBER}', value)
      .replaceAll('{YEAR}', String(now.getUTCFullYear()))
      .replaceAll('{MONTH}', String(now.getUTCMonth() + 1).padStart(2, '0'));
  }

  function body() {
    const f = form;
    const start = f.start_value === '' ? undefined : Number(f.start_value);
    if (!f.id) {
      return {
        code: f.code.trim(),
        document_type: f.document_type,
        name: f.name.trim(),
        format: f.format.trim(),
        number_padding: Number(f.number_padding),
        number_source: f.number_source,
        gapless: f.gapless,
        reset_period: f.reset_period,
        enabled: f.enabled,
        ...(start !== undefined && f.number_source === 'own' ? { start_value: start } : {}),
      };
    }
    const o = f.original;
    const changes = {};
    if (f.code.trim() !== o.code) changes.code = f.code.trim();
    if (f.name.trim() !== o.name) changes.name = f.name.trim();
    if (f.format.trim() !== o.format) changes.format = f.format.trim();
    if (Number(f.number_padding) !== o.number_padding) changes.number_padding = Number(f.number_padding);
    if (f.number_source !== o.number_source) changes.number_source = f.number_source;
    if (f.gapless !== o.gapless) changes.gapless = f.gapless;
    if (f.reset_period !== o.reset_period) changes.reset_period = f.reset_period;
    if (f.enabled !== o.enabled) changes.enabled = f.enabled;
    if (start !== undefined) changes.start_value = start;
    return changes;
  }

  async function save() {
    working = true;
    formError = '';
    try {
      const payload = body();
      if (form.id) await updateSeries(form.id, payload);
      else await createSeries(payload);
      success = form.id ? 'Series updated.' : 'Series created.';
      form = null;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      working = false;
    }
  }

  async function act(fn, message) {
    working = true;
    error = '';
    success = '';
    try {
      await fn();
      success = message;
      confirmDelete = null;
      await load();
    } catch (err) {
      error = err.message;
      confirmDelete = null;
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Document Series">
  <svelte:fragment slot="actions">
    <Button on:click={openCreate}>New series</Button>
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  <p class="ba-muted">
    A series sets how invoices, proformas, credit notes and debit notes are numbered. There is one default series for each document type.
    A format change applies only to numbers issued afterwards. A series that has issued numbers cannot be deleted, only disabled.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if series.length === 0}
    <p class="ba-empty">No document series.</p>
  {:else}
    <div class="ba-panel ba-scroll">
      <table class="ba-table">
        <thead>
          <tr><th>Code</th><th>Document</th><th>Name</th><th>Format</th><th>Numbering</th><th>Next</th><th>State</th><th></th></tr>
        </thead>
        <tbody>
          {#each series as row (row.id)}
            <tr>
              <td><strong>{row.code}</strong></td>
              <td>{typeLabel(row.document_type)}</td>
              <td>{row.name}</td>
              <td><span class="ba-mono">{row.format}</span><div class="ba-hint">padding {row.number_padding}</div></td>
              <td>
                {row.number_source === 'own' ? 'Own counter' : 'Invoice number'}
                <div class="ba-hint">{row.gapless ? 'gapless' : 'gaps allowed'}, resets {row.reset_period}</div>
              </td>
              <td>{row.next_value ?? '-'}</td>
              <td>
                {#if row.is_default}<span class="ba-pill ok">default</span>{/if}
                <span class="ba-pill {row.enabled ? 'info' : ''}">{row.enabled ? 'enabled' : 'disabled'}</span>
                {#if row.in_use}<span class="ba-pill">in use</span>{/if}
              </td>
              <td>
                <div class="ba-inline">
                  <button class="ba-link" on:click={() => openEdit(row)}>Edit</button>
                  {#if !row.is_default}
                    <button class="ba-link" disabled={!row.enabled || working} title={row.enabled ? '' : 'Enable the series first'} on:click={() => act(() => makeSeriesDefault(row.id), `${row.code} is now the default ${typeLabel(row.document_type).toLowerCase()} series.`)}>Make default</button>
                    <button class="ba-link" disabled={row.in_use || working} title={row.in_use ? 'A series in use cannot be deleted' : ''} on:click={() => (confirmDelete = row)}>Delete</button>
                  {/if}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if form}
  <Modal title={form.id ? `Edit series ${form.original.code}` : 'New series'} size="large" onClose={() => (form = null)}>
    <form class="ba-form" on:submit|preventDefault={save}>
      <div class="ba-two">
        <label>Code
          <input bind:value={form.code} maxlength="16" disabled={form.inUse} />
          {#if form.inUse}<span class="ba-hint">The code of a series in use cannot change.</span>{/if}
        </label>
        <label>Document type
          <select bind:value={form.document_type} disabled={!!form.id}>
            {#each TYPES as [value, label]}<option {value}>{label}</option>{/each}
          </select>
        </label>
      </div>
      <label>Name<input bind:value={form.name} maxlength="255" /></label>
      <div class="ba-two">
        <label>Format
          <input bind:value={form.format} maxlength="64" class="ba-mono" />
          <span class="ba-hint">Tags: {'{SERIES}'}, {'{NUMBER}'} (required), {'{YEAR}'} (required for yearly reset), {'{MONTH}'}. Example: <strong>{preview(form)}</strong></span>
        </label>
        <label>Number padding (zeros)
          <input type="number" min="0" max="12" bind:value={form.number_padding} />
        </label>
      </div>
      <div class="ba-two">
        <label>Number source
          <select bind:value={form.number_source}>
            <option value="own">Own counter</option>
            <option value="invoice_number">Internal invoice number</option>
          </select>
          {#if form.number_source === 'invoice_number'}<span class="ba-hint">Not allowed together with gapless. Only for invoices that already use their internal number.</span>{/if}
        </label>
        <label>Counter resets
          <select bind:value={form.reset_period} disabled={form.inUse}>
            <option value="never">Never</option>
            <option value="yearly">Every year</option>
          </select>
          {#if form.inUse}<span class="ba-hint">The reset period of a series in use cannot change.</span>{/if}
        </label>
      </div>
      {#if form.number_source === 'own'}
        <label>{form.id ? 'Move the counter to at least' : 'Start at'}
          <input type="number" min="1" bind:value={form.start_value} placeholder={form.id ? `now ${form.original.next_value ?? 1}` : '1'} />
          <span class="ba-hint">Leave empty to keep the counter. The start of a gapless series cannot move once a number is issued in the period.</span>
        </label>
      {/if}
      <label class="ba-check"><input type="checkbox" bind:checked={form.gapless} /> Gapless: a rolled back document returns its number (serialises publishing)</label>
      <label class="ba-check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (form = null)}>Cancel</Button>
      <Button on:click={save} disabled={working}>{working ? 'Saving…' : 'Save'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if confirmDelete}
  <Modal title="Delete series?" onClose={() => (confirmDelete = null)}>
    <p>Delete series <strong>{confirmDelete.code}</strong> ({confirmDelete.name})? Its counters are removed too.</p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirmDelete = null)}>Cancel</Button>
      <Button variant="danger" on:click={() => act(() => deleteSeries(confirmDelete.id), 'Series deleted.')} disabled={working}>Delete</Button>
    </svelte:fragment>
  </Modal>
{/if}
