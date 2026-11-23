<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import {
    createTaxRegistration,
    listTaxRegistrations,
    updateTaxRegistration,
  } from '../../../../lib/billingTaxAdminApi.js';
  import { REGISTRATION_SCHEMES, day, fromInput, label, toInput } from './taxUi.js';

  let rows = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let editor = null;
  let form = blank();
  let original = null;
  let formError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      rows = await listTaxRegistrations();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function blank() {
    return { jurisdiction: '', scheme: 'domestic', tax_number: '', country: '', valid_from: '', valid_to: '', invoice_series_id: '', invoice_profile_id: '', enabled: true };
  }

  function toForm(row) {
    return {
      jurisdiction: row.jurisdiction, scheme: row.scheme, tax_number: row.tax_number, country: row.country,
      valid_from: toInput(row.valid_from), valid_to: toInput(row.valid_to),
      invoice_series_id: row.invoice_series_id ? String(row.invoice_series_id) : '',
      invoice_profile_id: row.invoice_profile_id ? String(row.invoice_profile_id) : '',
      enabled: !!row.enabled,
    };
  }

  function values(f) {
    const optionalId = (text) => (String(text).trim() ? Number(text) : null);
    return {
      jurisdiction: f.jurisdiction.trim().toUpperCase(),
      scheme: f.scheme,
      tax_number: f.tax_number.trim(),
      country: f.country.trim().toUpperCase(),
      valid_from: fromInput(f.valid_from),
      valid_to: fromInput(f.valid_to),
      invoice_series_id: optionalId(f.invoice_series_id),
      invoice_profile_id: optionalId(f.invoice_profile_id),
      enabled: f.enabled,
    };
  }

  function openCreate() {
    editor = { id: null };
    form = blank();
    formError = '';
  }

  function openEdit(row) {
    editor = row;
    form = toForm(row);
    original = values(form);
    formError = '';
  }

  function check() {
    if (form.jurisdiction.trim().length < 2) return 'Enter the jurisdiction, for example GB, DE or EU_OSS.';
    if (!form.tax_number.trim()) return 'Enter the tax number.';
    if (form.country.trim().length !== 2) return 'The country is a two-letter code.';
    for (const key of ['invoice_series_id', 'invoice_profile_id']) {
      const text = String(form[key]).trim();
      if (text && !/^[1-9]\d*$/.test(text)) return 'Series and profile ids are positive whole numbers.';
    }
    return '';
  }

  async function save() {
    formError = check();
    if (formError) return;
    working = true;
    try {
      const now = values(form);
      if (editor.id === null) {
        const body = Object.fromEntries(Object.entries(now).filter(([key, v]) => v !== null && !(key === 'valid_from' && !v)));
        await createTaxRegistration(body);
        success = 'Registration created.';
      } else {
        const changes = Object.fromEntries(Object.entries(now).filter(([key, v]) => v !== original[key]));
        if ('valid_from' in changes && !changes.valid_from) delete changes.valid_from;
        if (!Object.keys(changes).length) {
          formError = 'Nothing has changed.';
          working = false;
          return;
        }
        await updateTaxRegistration(editor.id, changes);
        success = `Registration #${editor.id} updated.`;
      }
      editor = null;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      working = false;
    }
  }

  function state(row) {
    const now = new Date();
    if (!row.enabled) return ['disabled', ''];
    if (new Date(row.valid_from) > now) return ['not started', 'info'];
    if (row.valid_to && new Date(row.valid_to) <= now) return ['ended', ''];
    return ['active', 'ok'];
  }
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Seller registrations</h3>
    <Button on:click={openCreate}>New registration</Button>
  </div>
  <p class="ba-muted">The tax numbers the seller is registered under. A rule for a jurisdiction applies only while an enabled registration for it is valid.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if rows.length === 0}
    <p class="ba-empty">No registrations.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>#</th><th>Jurisdiction</th><th>Scheme</th><th>Tax number</th><th>Country</th><th>Valid</th><th>Series</th><th>Profile</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {#each rows as row (row.id)}
            {@const st = state(row)}
            <tr>
              <td>{row.id}</td>
              <td>{row.jurisdiction}</td>
              <td>{label(row.scheme)}</td>
              <td><span class="ba-mono">{row.tax_number}</span></td>
              <td>{row.country}</td>
              <td>{day(row.valid_from)} to {row.valid_to ? day(row.valid_to) : 'open'}</td>
              <td>{row.invoice_series_id ? `#${row.invoice_series_id}` : 'default'}</td>
              <td>{row.invoice_profile_id ? `#${row.invoice_profile_id}` : '-'}</td>
              <td><span class="ba-pill {st[1]}">{st[0]}</span></td>
              <td><button class="ba-link" on:click={() => openEdit(row)}>Edit</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if editor}
  <Modal title={editor.id === null ? 'New registration' : `Edit registration #${editor.id}`} onClose={() => (editor = null)}>
    <form class="ba-form" on:submit|preventDefault={save}>
      <div class="ba-two">
        <label>Jurisdiction<input bind:value={form.jurisdiction} maxlength="16" placeholder="GB, DE, EU_OSS" /></label>
        <label>Scheme
          <select bind:value={form.scheme}>{#each REGISTRATION_SCHEMES as [value, text]}<option {value}>{text}</option>{/each}</select>
        </label>
      </div>
      <div class="ba-two">
        <label>Tax number<input bind:value={form.tax_number} maxlength="64" /></label>
        <label>Country<input bind:value={form.country} maxlength="2" placeholder="GB" /></label>
      </div>
      <div class="ba-two">
        <label>Valid from (blank for now)<input type="datetime-local" bind:value={form.valid_from} /></label>
        <label>Valid to (blank for open ended)<input type="datetime-local" bind:value={form.valid_to} /></label>
      </div>
      <div class="ba-two">
        <label>Invoice series id (optional)<input bind:value={form.invoice_series_id} inputmode="numeric" /></label>
        <label>Invoice profile id (optional)<input bind:value={form.invoice_profile_id} inputmode="numeric" /></label>
      </div>
      <label class="ba-check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editor = null)}>Cancel</Button>
      <Button on:click={save} disabled={working}>Save</Button>
    </svelte:fragment>
  </Modal>
{/if}
