<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import * as api from '../../lib/vmFirewallApi.js';
  import { fieldErrorMap } from './ruleModel.js';

  /** @type {'staff' | 'customer'} */
  export let audience = 'customer';

  $: staff = audience === 'staff';

  let ipsets = [];
  let loading = true;
  let loadError = '';
  let busy = false;
  let error = '';
  let success = '';
  let fieldErrors = {};
  let form = null;

  const blankEntry = () => ({ cidr: '', comment: '', nomatch: false });
  const blankForm = () => ({
    id: null,
    revision: undefined,
    name: '',
    description: '',
    purpose: 'general',
    client_selectable: false,
    entries: [blankEntry()],
  });

  async function load() {
    try {
      ipsets = await api.listIpsets(audience);
      loadError = '';
    } catch (err) {
      loadError = err.message || 'Failed to load the IP sets';
    } finally {
      loading = false;
    }
  }

  function reset() {
    error = '';
    success = '';
    fieldErrors = {};
  }

  const startNew = () => {
    reset();
    form = blankForm();
  };

  async function startEdit(set) {
    reset();
    busy = true;
    try {
      const full = await api.getIpset(audience, set.id);
      form = {
        id: full.id,
        revision: full.revision,
        name: full.name,
        description: full.description || '',
        purpose: full.purpose || 'general',
        client_selectable: !!full.client_selectable,
        entries: (full.entries || []).map((e) => ({ cidr: e.cidr, comment: e.comment || '', nomatch: !!e.nomatch })),
      };
      if (!form.entries.length) form.entries = [blankEntry()];
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  async function save() {
    reset();
    busy = true;
    const body = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      entries: form.entries
        .filter((e) => e.cidr.trim())
        .map((e) => ({ cidr: e.cidr.trim(), nomatch: e.nomatch, comment: e.comment.trim() || null })),
    };
    if (staff) {
      body.purpose = form.purpose;
      body.client_selectable = form.client_selectable;
    }
    try {
      if (form.id == null) await api.createIpset(audience, body);
      else await api.updateIpset(audience, form.id, { ...body, revision: form.revision });
      success = form.id == null ? 'IP set created.' : 'IP set saved.';
      form = null;
      await load();
    } catch (err) {
      if (err.code === 'firewall_changed') {
        error = 'Someone else changed this IP set. Close it and open it again to see the latest version.';
      } else {
        error = err.message;
        fieldErrors = fieldErrorMap(err);
      }
    } finally {
      busy = false;
    }
  }

  async function remove(set) {
    if (!confirm(`Delete the IP set "${set.name}"?`)) return;
    reset();
    busy = true;
    try {
      await api.deleteIpset(audience, set.id);
      success = 'IP set deleted.';
      await load();
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  const errorAt = (field) => fieldErrors[field] || '';
  const addEntry = () => (form.entries = [...form.entries, blankEntry()]);
  const dropEntry = (index) => (form.entries = form.entries.filter((_, i) => i !== index));

  onMount(load);
</script>

<section class="panel">
  <header class="head">
    <div>
      <h3>{staff ? 'Admin IP sets' : 'IP sets'}</h3>
      <p class="hint">
        An IP set is a named list of addresses or ranges that a rule can use as its source or destination, so you change the list in one place.
        {#if staff}Management sets name the provider networks that customers must not reach.{/if}
      </p>
    </div>
    {#if !form}<Button size="small" disabled={busy || loading} on:click={startNew}>New IP set</Button>{/if}
  </header>

  {#if error}<Alert type="error">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="loading"><Spinner /><span>Loading…</span></div>
  {:else if loadError}
    <Alert type="error">{loadError}</Alert>
  {:else if form}
    <div class="form">
      <div class="grid">
        <label>Name
          <input type="text" bind:value={form.name} maxlength="48" disabled={busy} />
          {#if errorAt('name')}<span class="field-error">{errorAt('name')}</span>{/if}
        </label>
        <label>Description
          <input type="text" bind:value={form.description} maxlength="255" disabled={busy} />
          {#if errorAt('description')}<span class="field-error">{errorAt('description')}</span>{/if}
        </label>
        {#if staff}
          <label>Purpose
            <select bind:value={form.purpose} disabled={busy}>
              <option value="general">General</option>
              <option value="management">Management (networks customers must not reach)</option>
            </select>
            {#if errorAt('purpose')}<span class="field-error">{errorAt('purpose')}</span>{/if}
          </label>
          <label class="check"><input type="checkbox" bind:checked={form.client_selectable} disabled={busy} /> Customers may use this set</label>
        {/if}
      </div>

      <div class="entries">
        <div class="entry head-row"><span>Address or range</span><span>Note</span><span>Exclude</span><span></span></div>
        {#each form.entries as entry, index}
          <div class="entry">
            <div>
              <input type="text" bind:value={entry.cidr} placeholder="203.0.113.0/24" aria-label="Address or range {index + 1}" disabled={busy} />
              {#if errorAt(`entries[${index}].cidr`)}<span class="field-error">{errorAt(`entries[${index}].cidr`)}</span>{/if}
              {#if errorAt(`entries[${index}]`)}<span class="field-error">{errorAt(`entries[${index}]`)}</span>{/if}
            </div>
            <div>
              <input type="text" bind:value={entry.comment} maxlength="120" aria-label="Note {index + 1}" disabled={busy} />
              {#if errorAt(`entries[${index}].comment`)}<span class="field-error">{errorAt(`entries[${index}].comment`)}</span>{/if}
            </div>
            <input type="checkbox" bind:checked={entry.nomatch} aria-label="Exclude entry {index + 1}" title="Exclude this address from the set" disabled={busy} />
            <Button size="small" variant="secondary" disabled={busy} on:click={() => dropEntry(index)}>Remove</Button>
          </div>
        {/each}
        {#if errorAt('entries')}<span class="field-error">{errorAt('entries')}</span>{/if}
        <div><Button size="small" variant="secondary" disabled={busy} on:click={addEntry}>Add address</Button></div>
      </div>

      <div class="bar">
        <Button disabled={busy} on:click={save}>{busy ? 'Saving…' : 'Save IP set'}</Button>
        <Button variant="secondary" disabled={busy} on:click={() => ((form = null), reset())}>Cancel</Button>
      </div>
    </div>
  {:else if ipsets.length === 0}
    <p class="hint">No IP sets yet.</p>
  {:else}
    <ul class="list">
      {#each ipsets as set (set.id)}
        <li>
          <div class="item-main">
            <strong>{set.name}</strong>
            {#if set.purpose === 'management'}<span class="tag warn">management</span>{/if}
            {#if set.client_selectable}<span class="tag">customers may use</span>{/if}
            <span class="hint">{set.description || ''}</span>
            {#if set.entries}<span class="hint">{set.entries.length} {set.entries.length === 1 ? 'entry' : 'entries'}</span>{/if}
          </div>
          <div class="item-actions">
            <Button size="small" variant="secondary" disabled={busy} on:click={() => startEdit(set)}>Edit</Button>
            <Button size="small" variant="danger" disabled={busy} on:click={() => remove(set)}>Delete</Button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .panel { display: flex; flex-direction: column; gap: 12px; border: 1px solid var(--border-color); border-radius: 12px; padding: 16px 18px; background: var(--bg-primary); }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  h3 { margin: 0 0 4px; font-size: 16px; }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .loading { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
  .form { display: flex; flex-direction: column; gap: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px 14px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .check { flex-direction: row; align-items: center; gap: 8px; align-self: end; }
  input[type='text'], select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; width: 100%; min-width: 0; }
  .entries { display: flex; flex-direction: column; gap: 8px; }
  .entry { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) 60px auto; gap: 8px; align-items: start; }
  .head-row { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); align-items: end; }
  .entry input[type='checkbox'] { margin-top: 9px; justify-self: center; }
  .bar { display: flex; gap: 10px; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--border-color); flex-wrap: wrap; }
  .list li:first-child { border-top: none; }
  .item-main { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .item-actions { display: flex; gap: 6px; }
  .tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--info-bg); color: var(--info-text); }
  .tag.warn { background: var(--warning-bg); color: var(--warning-text); }
  .field-error { color: var(--danger-text); font-size: 12px; font-weight: 500; display: block; }
  @media (max-width: 640px) { .entry { grid-template-columns: 1fr; } }
</style>
