<script>
  import { onMount } from 'svelte';
  import {
    listPermissionSets,
    getPermissionSetsCatalog,
    updateServicePermissions,
    getServiceEffectivePermissions,
  } from '../lib/api.js';
  import { createEventDispatcher } from 'svelte';

  export let serviceId;
  export let serviceType = null;
  export let permissionSetId = null;
  export let permissionOverrides = null;

  const dispatch = createEventDispatcher();

  const GROUP_LABELS = {
    bare_metal: 'Bare Metal',
    vm: 'VM',
    http_proxy: 'HTTP Proxy',
  };

  let presets = [];
  let catalog = [];
  let effective = null;
  let loading = true;
  let error = null;
  let busy = false;
  let message = null;

  let selectedPresetId = '';
  let overridesDraft = {};

  $: relevantCatalog = serviceType
    ? catalog.filter((entry) => (entry.service_types || []).includes(serviceType))
    : catalog;

  async function loadAll() {
    loading = true;
    error = null;
    try {
      const [presetRows, catalogRows, effectiveRows] = await Promise.all([
        listPermissionSets(),
        getPermissionSetsCatalog(),
        getServiceEffectivePermissions(serviceId),
      ]);
      presets = presetRows;
      catalog = catalogRows;
      effective = effectiveRows;
    } catch (err) {
      error = err.message || 'Failed to load permissions';
    } finally {
      loading = false;
    }
  }

  function resetDraft() {
    selectedPresetId = permissionSetId ? String(permissionSetId) : '';
    overridesDraft = { ...(permissionOverrides || {}) };
  }

  $: serviceId, permissionSetId, permissionOverrides, resetDraft();

  onMount(loadAll);

  function toggleOverride(key) {
    const current = overridesDraft[key];
    if (current === undefined) {
      overridesDraft = { ...overridesDraft, [key]: true };
    } else if (current === true) {
      overridesDraft = { ...overridesDraft, [key]: false };
    } else {
      const next = { ...overridesDraft };
      delete next[key];
      overridesDraft = next;
    }
  }

  async function save() {
    busy = true;
    error = null;
    message = null;
    try {
      await updateServicePermissions(serviceId, {
        permission_set_id: selectedPresetId ? Number(selectedPresetId) : null,
        permission_overrides: Object.keys(overridesDraft).length > 0 ? overridesDraft : null,
      });
      message = 'Permissions updated.';
      await loadAll();
      dispatch('saved');
    } catch (err) {
      error = err.message || 'Failed to update permissions';
    } finally {
      busy = false;
    }
  }
</script>

<section class="panel">
  <div class="panel-head">
    <h3>Client permissions</h3>
    <p>Controls which actions the client can take on this service. Falls through to the client's / product's preset, then system defaults, when unset here.</p>
  </div>

  {#if loading}
    <p class="muted">Loading…</p>
  {:else}
    {#if error}<div class="error">{error}</div>{/if}
    {#if message}<div class="success">{message}</div>{/if}

    <div class="form-row">
      <label>Service-level preset
        <select bind:value={selectedPresetId}>
          <option value="">No preset (inherit from client/product)</option>
          {#each presets as ps}
            <option value={String(ps.id)}>{ps.name}</option>
          {/each}
        </select>
      </label>
      <button type="button" class="btn-secondary" disabled={busy} on:click={save}>
        {busy ? 'Saving…' : 'Save'}
      </button>
    </div>

    {#if relevantCatalog.length > 0}
      <div class="overrides">
        <p class="form-help">
          Sparse per-service overrides (final word). Click to cycle: <strong>inherit</strong> &rarr;
          <strong>grant</strong> &rarr; <strong>deny</strong>.
        </p>
        <div class="perm-list">
          {#each relevantCatalog as entry}
            {@const state = overridesDraft[entry.key]}
            <button
              type="button"
              class="perm-chip"
              class:perm-grant={state === true}
              class:perm-deny={state === false}
              on:click={() => toggleOverride(entry.key)}
            >
              <span class="perm-state">
                {state === true ? 'Grant' : state === false ? 'Deny' : 'Inherit'}
              </span>
              <span class="perm-label">{entry.label}</span>
            </button>
          {/each}
        </div>
      </div>
    {/if}

    {#if effective}
      <div class="effective">
        <p class="form-help">Effective permissions (resolved right now):</p>
        <div class="effective-grid">
          {#each Object.entries(effective) as [key, value]}
            <span class="effective-chip" class:on={value} class:off={!value}>
              {key}: {value ? 'allowed' : 'denied'}
            </span>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</section>

<style>
  .panel {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px 18px;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .panel-head h3 { margin: 0 0 4px; font-size: 1rem; }
  .panel-head p { margin: 0; font-size: 13px; color: var(--text-secondary); max-width: 60ch; }
  .muted { color: var(--text-secondary); }
  .error { color: var(--danger-color); }
  .success { color: var(--success-text); }
  .form-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  .form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  .form-row select {
    min-width: 220px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-secondary);
    color: var(--text-primary);
  }
  .btn-secondary {
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    font-weight: 600;
    cursor: pointer;
  }
  .btn-secondary:disabled { opacity: 0.6; cursor: not-allowed; }
  .form-help { margin: 0; font-size: 12px; color: var(--text-secondary); }
  .perm-list { display: flex; flex-direction: column; gap: 6px; }
  .perm-chip {
    display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
    padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-color);
    background: var(--bg-tertiary); color: var(--text-primary); cursor: pointer; font-size: 13px;
  }
  .perm-state { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; min-width: 52px; color: var(--text-secondary); }
  .perm-chip.perm-grant { border-color: var(--success-text); }
  .perm-chip.perm-grant .perm-state { color: var(--success-text); }
  .perm-chip.perm-deny { border-color: var(--danger-color); }
  .perm-chip.perm-deny .perm-state { color: var(--danger-color); }
  .effective-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .effective-chip {
    font-size: 12px; padding: 4px 10px; border-radius: 12px; font-weight: 600;
    background: var(--bg-tertiary); color: var(--text-secondary);
  }
  .effective-chip.on { background: var(--success-bg); color: var(--success-text); }
  .effective-chip.off { background: var(--danger-bg); color: var(--danger-text); }
</style>
