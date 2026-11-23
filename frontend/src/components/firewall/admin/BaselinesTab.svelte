<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../ui/index.js';
  import * as api from '../../../lib/vmFirewallApi.js';
  import RuleEditor from '../RuleEditor.svelte';
  import { fieldErrorMap, payloadOf, toDraft } from '../ruleModel.js';

  let baselines = [];
  let locations = [];
  let ipsets = [];
  let groups = [];
  let loading = true;
  let loadError = '';
  let busy = false;
  let error = '';
  let success = '';
  let fieldErrors = {};
  let form = null;

  const TRI = [
    ['anti_spoof', 'Address anti-spoofing'],
    ['macfilter', 'MAC address filter'],
    ['block_management', 'Block management networks'],
    ['client_may_disable', 'Customer may switch protections off'],
  ];
  const SCOPE_LABEL = { global: 'Everywhere', location: 'Location', product: 'Product' };

  const locationId = (loc) => loc.location_id ?? loc.id;
  const locationName = (loc) => loc.name ?? loc.location_name ?? `Location ${locationId(loc)}`;
  const scopeText = (b) =>
    b.scope_type === 'global'
      ? SCOPE_LABEL.global
      : b.scope_type === 'location'
        ? `Location: ${locations.map((l) => [locationId(l), locationName(l)]).find(([id]) => id === b.scope_id)?.[1] ?? '#' + b.scope_id}`
        : `Product #${b.scope_id}`;

  async function load() {
    try {
      [baselines, locations, ipsets, groups] = await Promise.all([
        api.listBaselines(),
        api.listLocations().catch(() => []),
        api.listIpsets('staff').catch(() => []),
        api.listGroups('staff').catch(() => []),
      ]);
      loadError = '';
    } catch (err) {
      loadError = err.message || 'Failed to load the baselines';
    } finally {
      loading = false;
    }
  }

  const reset = () => {
    error = '';
    success = '';
    fieldErrors = {};
  };

  const tri = (value) => (value === true ? 'on' : value === false ? 'off' : '');

  function formFrom(b) {
    const settings = b?.settings || {};
    return {
      id: b?.id ?? null,
      revision: b?.revision,
      name: b?.name || '',
      scope_type: b?.scope_type || 'global',
      scope_id: b?.scope_id != null ? String(b.scope_id) : '',
      enabled: b ? b.enabled !== false : true,
      smtp_out: settings.smtp_out || '',
      tri: Object.fromEntries(TRI.map(([key]) => [key, tri(settings[key])])),
      management_ipset_ids: (settings.management_ipset_ids || []).map(String),
      rules: (b?.rules || []).map(toDraft),
    };
  }

  const startNew = () => {
    reset();
    form = formFrom(null);
  };

  async function startEdit(baseline) {
    reset();
    busy = true;
    try {
      form = formFrom(await api.getBaseline(baseline.id));
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  function settingsBody() {
    const out = {};
    if (form.smtp_out) out.smtp_out = form.smtp_out;
    for (const [key] of TRI) if (form.tri[key]) out[key] = form.tri[key] === 'on';
    if (form.management_ipset_ids.length) out.management_ipset_ids = form.management_ipset_ids.map(Number);
    return out;
  }

  async function save() {
    reset();
    busy = true;
    const body = {
      name: form.name.trim(),
      enabled: form.enabled,
      settings: settingsBody(),
      rules: payloadOf(form.rules),
    };
    try {
      if (form.id == null) {
        body.scope_type = form.scope_type;
        body.scope_id = form.scope_type === 'global' ? null : Number(form.scope_id) || null;
        await api.createBaseline(body);
      } else {
        await api.updateBaseline(form.id, { ...body, revision: form.revision });
      }
      success = 'Baseline saved.';
      form = null;
      await load();
    } catch (err) {
      if (err.code === 'firewall_changed') error = 'Someone else changed this baseline. Close it and open it again to see the latest version.';
      else {
        error = err.message;
        fieldErrors = fieldErrorMap(err);
      }
    } finally {
      busy = false;
    }
  }

  async function remove(baseline) {
    if (!confirm(`Delete the baseline "${baseline.name}"? Every server it covers is updated.`)) return;
    reset();
    busy = true;
    try {
      await api.deleteBaseline(baseline.id);
      success = 'Baseline deleted.';
      await load();
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  const errorAt = (field) => fieldErrors[field] || '';
  const managementSets = () => ipsets.filter((set) => set.purpose === 'management');

  onMount(load);
</script>

<section class="panel">
  <header class="head">
    <div>
      <h3>Baselines</h3>
      <p class="hint">
        A baseline is the provider's own protection. It is merged from everywhere, then the location, then the product, and runs before a customer's rules, so a customer cannot undo it.
      </p>
    </div>
    {#if !form}<Button size="small" disabled={busy || loading} on:click={startNew}>New baseline</Button>{/if}
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
          <input type="text" bind:value={form.name} maxlength="64" disabled={busy} />
          {#if errorAt('name')}<span class="field-error">{errorAt('name')}</span>{/if}
        </label>
        <label>Applies to
          <select bind:value={form.scope_type} disabled={busy || form.id != null}>
            <option value="global">Everywhere</option>
            <option value="location">One location</option>
            <option value="product">One product</option>
          </select>
          {#if errorAt('scope_type')}<span class="field-error">{errorAt('scope_type')}</span>{/if}
        </label>
        {#if form.scope_type === 'location'}
          <label>Location
            <select bind:value={form.scope_id} disabled={busy || form.id != null}>
              <option value="">Choose a location</option>
              {#each locations as loc}<option value={String(locationId(loc))}>{locationName(loc)}</option>{/each}
            </select>
            {#if errorAt('scope_id')}<span class="field-error">{errorAt('scope_id')}</span>{/if}
          </label>
        {:else if form.scope_type === 'product'}
          <label>Product id
            <input type="text" inputmode="numeric" bind:value={form.scope_id} disabled={busy || form.id != null} />
            {#if errorAt('scope_id')}<span class="field-error">{errorAt('scope_id')}</span>{/if}
          </label>
        {/if}
        <label class="check"><input type="checkbox" bind:checked={form.enabled} disabled={busy} /> Enabled</label>
      </div>

      <fieldset>
        <legend>Settings (leave on "inherit" to use the wider baseline)</legend>
        <div class="grid">
          <label>Outgoing mail (port 25)
            <select bind:value={form.smtp_out} disabled={busy}>
              <option value="">Inherit</option>
              <option value="block">Block</option>
              <option value="allow">Allow</option>
            </select>
            {#if errorAt('settings.smtp_out')}<span class="field-error">{errorAt('settings.smtp_out')}</span>{/if}
          </label>
          {#each TRI as [key, label]}
            <label>{label}
              <select bind:value={form.tri[key]} disabled={busy}>
                <option value="">Inherit</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
              {#if errorAt(`settings.${key}`)}<span class="field-error">{errorAt(`settings.${key}`)}</span>{/if}
            </label>
          {/each}
        </div>
        <div class="mgmt">
          <strong>Management IP sets to block</strong>
          {#if managementSets().length === 0}
            <p class="hint">No management IP sets yet. Create one under Groups and IP sets.</p>
          {:else}
            {#each managementSets() as set}
              <label class="check"><input type="checkbox" bind:group={form.management_ipset_ids} value={String(set.id)} disabled={busy} /> {set.name}</label>
            {/each}
          {/if}
          {#each Object.entries(fieldErrors).filter(([k]) => k.startsWith('settings.management_ipset_ids')) as [, message]}
            <span class="field-error">{message}</span>
          {/each}
        </div>
      </fieldset>

      <div>
        <strong>Rules</strong>
        <p class="hint">Run first, in this order, before the customer's own rules.</p>
        <RuleEditor
          bind:rules={form.rules}
          errors={fieldErrors}
          {groups}
          {ipsets}
          staff
          allowLog
          disabled={busy}
          emptyText="This baseline has no rules; its settings still apply."
        />
      </div>

      <div class="bar">
        <Button disabled={busy} on:click={save}>{busy ? 'Saving…' : 'Save baseline'}</Button>
        <Button variant="secondary" disabled={busy} on:click={() => ((form = null), reset())}>Cancel</Button>
      </div>
    </div>
  {:else if baselines.length === 0}
    <p class="hint">No baselines yet.</p>
  {:else}
    <ul class="list">
      {#each baselines as baseline (baseline.id)}
        <li>
          <div class="item-main">
            <strong>{baseline.name}</strong>
            <span class="tag" class:off={baseline.enabled === false}>{baseline.enabled === false ? 'disabled' : 'enabled'}</span>
            <span class="hint">{scopeText(baseline)}</span>
          </div>
          <div class="item-actions">
            <Button size="small" variant="secondary" disabled={busy} on:click={() => startEdit(baseline)}>Edit</Button>
            <Button size="small" variant="danger" disabled={busy} on:click={() => remove(baseline)}>Delete</Button>
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
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); max-width: 70ch; }
  .loading { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
  .form { display: flex; flex-direction: column; gap: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 14px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .check { flex-direction: row; align-items: center; gap: 8px; font-weight: 500; }
  fieldset { border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; }
  legend { font-size: 12px; padding: 0 6px; color: var(--text-secondary); }
  .mgmt { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
  input[type='text'], select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; }
  .bar { display: flex; gap: 10px; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--border-color); flex-wrap: wrap; }
  .list li:first-child { border-top: none; }
  .item-main { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .item-actions { display: flex; gap: 6px; }
  .tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--success-bg); color: var(--success-text); }
  .tag.off { background: var(--bg-tertiary); color: var(--text-secondary); }
  .field-error { color: var(--danger-text); font-size: 12px; font-weight: 500; }
</style>
