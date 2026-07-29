<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listPermissionSets,
    getPermissionSetsCatalog,
    createPermissionSet,
    updatePermissionSet,
    deletePermissionSet,
  } from '../lib/api.js';

  let presets = [];
  let catalog = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingPreset = null;
  let formData = { name: '', description: '', permissions: {} };
  let formError = null;

  const GROUP_LABELS = {
    bare_metal: 'Bare Metal',
    vm: 'VM',
    http_proxy: 'HTTP Proxy',
  };

  async function loadAll() {
    loading = true;
    error = null;
    try {
      const [presetRows, catalogRows] = await Promise.all([
        listPermissionSets(),
        getPermissionSetsCatalog(),
      ]);
      presets = presetRows;
      catalog = catalogRows;
    } catch (err) {
      error = err.message || 'Failed to load permission presets';
      presets = [];
    } finally {
      loading = false;
    }
  }

  // Group catalog entries by the first service type they apply to, so a key
  // shared across types (e.g. service.view) only renders once, under the
  // most relevant group heading.
  $: groupedCatalog = (() => {
    const groups = { bare_metal: [], vm: [], http_proxy: [] };
    for (const entry of catalog) {
      const primary = entry.service_types?.[0];
      if (groups[primary]) groups[primary].push(entry);
    }
    return groups;
  })();

  function openAddModal() {
    editingPreset = null;
    formData = { name: '', description: '', permissions: {} };
    formError = null;
    showModal = true;
  }

  function openEditModal(preset) {
    editingPreset = preset;
    formData = {
      name: preset.name,
      description: preset.description || '',
      permissions: { ...preset.permissions },
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingPreset = null;
    formError = null;
  }

  function toggleKey(key) {
    const current = formData.permissions[key];
    // Cycle: unset (inherit) -> granted -> denied -> unset
    if (current === undefined) {
      formData.permissions = { ...formData.permissions, [key]: true };
    } else if (current === true) {
      formData.permissions = { ...formData.permissions, [key]: false };
    } else {
      const next = { ...formData.permissions };
      delete next[key];
      formData.permissions = next;
    }
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }
    try {
      formError = null;
      if (editingPreset) {
        await updatePermissionSet(editingPreset.id, formData);
      } else {
        await createPermissionSet(formData);
      }
      closeModal();
      await loadAll();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(preset) {
    if (!confirm(`Delete permission preset "${preset.name}"?`)) return;
    try {
      await deletePermissionSet(preset.id);
      await loadAll();
    } catch (err) {
      alert('Failed to delete permission preset: ' + err.message);
    }
  }

  function grantedCount(preset) {
    return Object.values(preset.permissions || {}).filter((v) => v === true).length;
  }

  onMount(loadAll);
</script>

<PageHeader title="Permission Presets">
  <svelte:fragment slot="actions">
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      New Preset
    </button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page presets-container">
  <p class="admin-page-lead">Reusable client permission sets. Assign these on a product, client, or service to control what actions that client can take (power, IPMI, reinstall, scripts, guest password/network, proxy credentials).</p>

  {#if loading}
    <div class="loading">Loading permission presets...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if presets.length === 0}
    <div class="empty-state">
      <p>No permission presets yet. Click "New Preset" to create one.</p>
    </div>
  {:else}
    <div class="presets-grid">
      {#each presets as preset}
        <div class="preset-card">
          <div class="preset-header">
            <div>
              <h3>{preset.name}</h3>
              <div class="preset-badges">
                {#if preset.is_system}
                  <span class="badge badge-info">System</span>
                {/if}
                <span class="badge badge-success">{grantedCount(preset)} granted</span>
              </div>
            </div>
            <div class="preset-actions">
              <button class="btn-icon-only" on:click={() => openEditModal(preset)} title="Edit">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
              </button>
              {#if !preset.is_system}
                <button class="btn-icon-only btn-danger" on:click={() => handleDelete(preset)} title="Delete">
                  <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              {/if}
            </div>
          </div>
          {#if preset.description}
            <p class="preset-description">{preset.description}</p>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <button type="button" class="modal-overlay" tabindex="-1" on:click={(e) => e.target === e.currentTarget && closeModal()}>
    <div class="modal-content modal-large" role="dialog">
      <div class="modal-header">
        <h3>{editingPreset ? 'Edit Permission Preset' : 'New Permission Preset'}</h3>
        <button class="btn-icon-only" on:click={closeModal}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        {#if formError}
          <div class="form-error">{formError}</div>
        {/if}
        <div class="form-group">
          <label for="preset-name">Name *</label>
          <input id="preset-name" type="text" bind:value={formData.name} placeholder="e.g., Power + IPMI" />
        </div>
        <div class="form-group">
          <label for="preset-description">Description</label>
          <textarea id="preset-description" bind:value={formData.description} rows="2" placeholder="What is this preset for?"></textarea>
        </div>
        <div class="form-group">
          <p class="form-help">
            Click a permission to cycle: <strong>inherit</strong> (unset — falls through to a less specific
            layer) &rarr; <strong>grant</strong> &rarr; <strong>deny</strong>.
          </p>
          {#each Object.entries(groupedCatalog) as [serviceType, entries]}
            {#if entries.length > 0}
              <div class="perm-group">
                <div class="perm-group-label">{GROUP_LABELS[serviceType] || serviceType}</div>
                <div class="perm-list">
                  {#each entries as entry}
                    {@const state = formData.permissions[entry.key]}
                    <button
                      type="button"
                      class="perm-chip"
                      class:perm-grant={state === true}
                      class:perm-deny={state === false}
                      on:click={() => toggleKey(entry.key)}
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
          {/each}
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>{editingPreset ? 'Update' : 'Create'}</button>
      </div>
    </div>
  </button>
{/if}

<style>

  .modal-content .btn-primary {
    display: inline-flex; align-items: center; gap: 6px; padding: 9px 16px;
    background: var(--accent-color); color: var(--accent-contrast, white); border: none;
    border-radius: var(--radius-sm); font-weight: 600; font-size: 13.5px; cursor: pointer;
  }
  .modal-content .btn-primary:hover { background: var(--accent-dark); }
  .btn-icon { width: 15px; height: 15px; }

  .loading, .error, .empty-state { text-align: center; padding: 48px; color: var(--text-secondary); }
  .error { color: var(--danger-color); }

  .presets-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 20px; }
  .preset-card { background: var(--bg-primary); border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1); }
  .preset-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
  .preset-header h3 { margin: 0 0 8px; font-size: 17px; font-weight: 600; color: var(--text-primary); }
  .preset-badges { display: flex; gap: 6px; flex-wrap: wrap; }
  .badge { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .badge-success { background: var(--success-bg); color: var(--success-text); }
  .badge-info { background: var(--info-bg); color: var(--info-text); }
  .preset-actions { display: flex; gap: 8px; }
  .preset-description { margin: 8px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }

  .btn-icon-only {
    background: var(--bg-tertiary); border: 1px solid var(--border-color); padding: 6px; cursor: pointer;
    color: var(--text-primary); border-radius: 6px; transition: background 0.2s ease, color 0.2s ease;
  }
  .btn-icon-only:hover { background: var(--bg-secondary); border-color: var(--accent-color); color: var(--accent-color); }
  .btn-icon-only.btn-danger:hover { background: var(--danger-color); color: white; border-color: var(--danger-color); }
  .btn-icon-only svg { width: 18px; height: 18px; }

  button.modal-overlay { padding: 0; border: none; font: inherit; color: inherit; cursor: default; width: 100%; height: 100%; text-align: left; }
  .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: var(--overlay-bg); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal-content { background: var(--bg-primary); border-radius: 12px; width: 90%; max-width: 640px; max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1); }
  .modal-large { max-width: 720px; }
  .modal-header { display: flex; justify-content: space-between; align-items: center; padding: 20px 24px; border-color: var(--border-color); }
  .modal-header h3 { margin: 0; font-size: 20px; font-weight: 600; }
  .modal-body { padding: 24px; }
  .form-error { background: var(--danger-bg); color: var(--danger-text); padding: 12px; border-radius: 8px; margin-bottom: 16px; }
  .form-group { margin-bottom: 20px; }
  .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: var(--text-primary); }
  .form-group input, .form-group textarea {
    width: 100%; padding: 10px 12px; background: var(--bg-primary); border: 1px solid var(--border-color);
    border-radius: 8px; font-size: 14px; color: var(--text-primary);
  }
  .form-help { margin: 0 0 12px; font-size: 12px; color: var(--text-secondary); }

  .perm-group { margin-bottom: 16px; }
  .perm-group-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-tertiary); margin-bottom: 8px; }
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

  .modal-footer { display: flex; justify-content: flex-end; gap: 12px; padding: 20px 24px; border-color: var(--border-color); }
  .btn-secondary { padding: 10px 20px; background: var(--bg-tertiary); color: var(--text-primary); border: none; border-radius: 8px; font-weight: 600; cursor: pointer; }
  .btn-secondary:hover { background: var(--accent-color); }
</style>
