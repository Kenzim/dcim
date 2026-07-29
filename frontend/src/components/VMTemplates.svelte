<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listVmTemplates,
    listVmTemplateOsTypes,
    createVmTemplate,
    updateVmTemplate,
    deleteVmTemplate,
  } from '../lib/api.js';

  let loading = false;
  let error = '';
  let templates = [];
  /** @type {string[]} */
  let osTypes = [];
  /** @type {Record<string, any>} */
  let osTypeSchemas = {};

  let showCreateModal = false;
  let showEditModal = false;
  let editingTemplateId = null;

  let createForm = emptyForm();
  let editForm = emptyForm();

  function emptyForm() {
    return {
      code: '',
      name: '',
      description: '',
      os_type: 'Linux - Cloudinit',
      proxmox_template_name: '',
      enabled: true,
      strategy_options: {},
    };
  }

  function schemaFor(osType) {
    return osTypeSchemas[osType] || { option_schema: [], actions: [] };
  }

  function defaultsFor(osType) {
    const schema = schemaFor(osType);
    const opts = {};
    for (const field of schema.option_schema || []) {
      opts[field.name] = field.default;
    }
    return opts;
  }

  function onOsTypeChange(form, osType) {
    form.os_type = osType;
    form.strategy_options = { ...defaultsFor(osType), ...(form.strategy_options || {}) };
    // Reset to strategy defaults for unknown keys on switch
    form.strategy_options = defaultsFor(osType);
  }

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [templateRows, osTypeRows, detailed] = await Promise.all([
        listVmTemplates(),
        listVmTemplateOsTypes(),
        listVmTemplateOsTypes({ detailed: true }),
      ]);
      templates = templateRows;
      osTypes = Array.isArray(osTypeRows) && typeof osTypeRows[0] === 'string'
        ? osTypeRows
        : (detailed || []).map((r) => r.os_type);
      osTypeSchemas = {};
      for (const row of detailed || []) {
        if (row && row.os_type) osTypeSchemas[row.os_type] = row;
      }
      if (osTypes.length && !createForm.os_type) {
        createForm.os_type = osTypes[0];
        createForm.strategy_options = defaultsFor(osTypes[0]);
      }
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function submitCreate() {
    try {
      await createVmTemplate({
        ...createForm,
        description: createForm.description || null,
        strategy_options: createForm.strategy_options || {},
      });
      showCreateModal = false;
      createForm = emptyForm();
      createForm.os_type = osTypes[0] || 'Linux - Cloudinit';
      createForm.strategy_options = defaultsFor(createForm.os_type);
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function startEdit(template) {
    editingTemplateId = template.id;
    const osType = template.os_type || (osTypes[0] || 'Linux - Cloudinit');
    editForm = {
      code: template.code || '',
      name: template.name || '',
      description: template.description || '',
      os_type: osType,
      proxmox_template_name: template.proxmox_template_name || '',
      enabled: !!template.enabled,
      strategy_options: { ...defaultsFor(osType), ...(template.strategy_options || {}) },
    };
    showEditModal = true;
  }

  async function submitEdit() {
    if (!editingTemplateId) return;
    try {
      const { code: _code, ...payload } = editForm;
      await updateVmTemplate(editingTemplateId, {
        ...payload,
        description: editForm.description || null,
        strategy_options: editForm.strategy_options || {},
      });
      showEditModal = false;
      editingTemplateId = null;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function removeTemplate(template) {
    const confirmed = window.confirm(`Delete VM template '${template.name}'?`);
    if (!confirmed) return;
    try {
      await deleteVmTemplate(template.id);
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function toggleClientAction(form, actionName) {
    const list = Array.isArray(form.strategy_options.client_actions)
      ? [...form.strategy_options.client_actions]
      : [];
    const idx = list.indexOf(actionName);
    if (idx >= 0) list.splice(idx, 1);
    else list.push(actionName);
    form.strategy_options.client_actions = list;
  }

  function openCreate() {
    createForm = emptyForm();
    createForm.os_type = osTypes[0] || 'Linux - Cloudinit';
    createForm.strategy_options = defaultsFor(createForm.os_type);
    showCreateModal = true;
  }

  onMount(loadData);
</script>

<PageHeader title="VM Templates" />
<div class="templates-page">
  <p class="page-hint">
    Each row is a <strong>Proxmox clone source</strong> plus a <strong>provisioning strategy</strong>
    (<code>os_type</code>) with optional strategy options (network mode, SMBIOS, client actions).
  </p>
  <div class="top-actions">
    <button class="action-btn" on:click={openCreate}>New VM Template</button>
  </div>

  {#if error}
    <div class="error">{error}</div>
  {/if}

  {#if loading}
    <p>Loading...</p>
  {:else}
    <div class="template-table">
      <div class="row head">
        <div>Name</div>
        <div>Code</div>
        <div>Strategy</div>
        <div>Proxmox clone name</div>
        <div>Products</div>
        <div>Actions</div>
      </div>
      {#if templates.length === 0}
        <div class="row">
          <div>No VM templates yet</div>
          <div>-</div>
          <div>-</div>
          <div>-</div>
          <div>-</div>
          <div>-</div>
        </div>
      {:else}
        {#each templates as tmpl}
          <div class="row">
            <div>
              <strong>{tmpl.name}</strong>
              {#if tmpl.description}
                <div class="meta">{tmpl.description}</div>
              {/if}
            </div>
            <div class="mono">{tmpl.code || '-'}</div>
            <div>{tmpl.os_type}</div>
            <div class="mono">{tmpl.proxmox_template_name}</div>
            <div>{tmpl.product_ids?.length || 0}</div>
            <div class="actions">
              <button class="tiny-btn" on:click={() => startEdit(tmpl)}>Edit</button>
              <button class="tiny-btn danger" on:click={() => removeTemplate(tmpl)}>Delete</button>
            </div>
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</div>

{#if showCreateModal}
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-label="Create VM template">
    <div class="modal-content">
      <h3>Create VM Template</h3>
      <input bind:value={createForm.name} placeholder="Template display name" />
      <label class="field-label">Code (immutable)</label>
      <input bind:value={createForm.code} placeholder="e.g. debian-13" />
      <p class="field-hint">Lowercase letters, digits, hyphens. Cannot be changed after create.</p>
      <textarea bind:value={createForm.description} rows="3" placeholder="Description (optional)" />
      <label class="field-label">Provisioning strategy</label>
      <select
        value={createForm.os_type}
        on:change={(e) => onOsTypeChange(createForm, e.currentTarget.value)}
      >
        {#each osTypes as osType}
          <option value={osType}>{osType}</option>
        {/each}
      </select>
      <label class="field-label">Proxmox clone template name</label>
      <input bind:value={createForm.proxmox_template_name} placeholder="e.g. macos-tahoe" />
      {#each schemaFor(createForm.os_type).option_schema || [] as field}
        {#if field.name !== 'client_actions'}
          <label class="field-label">{field.label}</label>
          {#if field.description}<p class="field-hint">{field.description}</p>{/if}
          {#if field.field_type === 'bool'}
            <label class="check">
              <input type="checkbox" bind:checked={createForm.strategy_options[field.name]} />
              Enabled
            </label>
          {:else if field.field_type === 'select'}
            <select bind:value={createForm.strategy_options[field.name]}>
              {#each field.choices || [] as choice}
                <option value={choice}>{choice}</option>
              {/each}
            </select>
          {:else}
            <input bind:value={createForm.strategy_options[field.name]} />
          {/if}
        {/if}
      {/each}
      <label class="field-label">Client-visible actions</label>
      <p class="field-hint">Also requires matching client permission keys.</p>
      <div class="checks">
        {#each (schemaFor(createForm.os_type).actions || []).filter((a) => a.client_eligible) as action}
          <label class="check">
            <input
              type="checkbox"
              checked={(createForm.strategy_options.client_actions || []).includes(action.name)}
              on:change={() => toggleClientAction(createForm, action.name)}
            />
            {action.label}
          </label>
        {/each}
      </div>
      <label class="check"><input type="checkbox" bind:checked={createForm.enabled} /> Enabled</label>
      <div class="actions">
        <button class="tiny-btn" on:click={() => (showCreateModal = false)}>Cancel</button>
        <button on:click={submitCreate}>Create</button>
      </div>
    </div>
  </div>
{/if}

{#if showEditModal}
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-label="Edit VM template">
    <div class="modal-content">
      <h3>Edit VM Template</h3>
      <input bind:value={editForm.name} placeholder="Template display name" />
      <label class="field-label">Code (immutable)</label>
      <input value={editForm.code} readonly disabled class="readonly" />
      <textarea bind:value={editForm.description} rows="3" placeholder="Description (optional)" />
      <label class="field-label">Provisioning strategy</label>
      <select
        value={editForm.os_type}
        on:change={(e) => onOsTypeChange(editForm, e.currentTarget.value)}
      >
        {#each osTypes as osType}
          <option value={osType}>{osType}</option>
        {/each}
      </select>
      <label class="field-label">Proxmox clone template name</label>
      <input bind:value={editForm.proxmox_template_name} />
      {#each schemaFor(editForm.os_type).option_schema || [] as field}
        {#if field.name !== 'client_actions'}
          <label class="field-label">{field.label}</label>
          {#if field.description}<p class="field-hint">{field.description}</p>{/if}
          {#if field.field_type === 'bool'}
            <label class="check">
              <input type="checkbox" bind:checked={editForm.strategy_options[field.name]} />
              Enabled
            </label>
          {:else if field.field_type === 'select'}
            <select bind:value={editForm.strategy_options[field.name]}>
              {#each field.choices || [] as choice}
                <option value={choice}>{choice}</option>
              {/each}
            </select>
          {:else}
            <input bind:value={editForm.strategy_options[field.name]} />
          {/if}
        {/if}
      {/each}
      <label class="field-label">Client-visible actions</label>
      <div class="checks">
        {#each (schemaFor(editForm.os_type).actions || []).filter((a) => a.client_eligible) as action}
          <label class="check">
            <input
              type="checkbox"
              checked={(editForm.strategy_options.client_actions || []).includes(action.name)}
              on:change={() => toggleClientAction(editForm, action.name)}
            />
            {action.label}
          </label>
        {/each}
      </div>
      <label class="check"><input type="checkbox" bind:checked={editForm.enabled} /> Enabled</label>
      <div class="actions">
        <button class="tiny-btn" on:click={() => (showEditModal = false)}>Cancel</button>
        <button on:click={submitEdit}>Save</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .templates-page { padding: 0 0.5rem 2rem; color: var(--text-primary); }
  .page-hint { color: var(--text-secondary); margin-bottom: 1rem; max-width: 52rem; }
  .top-actions { margin-bottom: 1rem; }
  .action-btn {
    padding: 8px 12px;
    border: none;
    border-radius: 6px;
    background: var(--accent-color);
    color: #fff;
    cursor: pointer;
  }
  .error { color: var(--danger-color); margin: 0.5rem 0; }
  .template-table {
    display: grid;
    gap: 0;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }
  .row {
    display: grid;
    grid-template-columns: 1.8fr 1fr 1.3fr 1.4fr 0.55fr 1fr;
    gap: 0.75rem;
    padding: 0.65rem 0.75rem;
    align-items: center;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
    font-size: 0.9rem;
  }
  .readonly {
    opacity: 0.75;
    cursor: not-allowed;
    background: var(--bg-secondary);
  }
  .row:last-child { border-bottom: none; }
  .row.head {
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    color: var(--text-secondary);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
  }
  .meta { font-size: 0.85rem; color: var(--text-secondary); margin-top: 0.15rem; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.85rem; }
  .actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  button {
    width: fit-content;
    padding: 8px 12px;
    border: none;
    border-radius: 6px;
    background: var(--accent-color);
    color: #fff;
    cursor: pointer;
  }
  .tiny-btn {
    font-size: 0.75rem;
    padding: 4px 8px;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }
  .tiny-btn.danger {
    border-color: var(--danger-color);
    color: var(--danger-color);
    background: transparent;
  }
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal-content {
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    padding: 1.25rem;
    border-radius: 10px;
    width: min(520px, 92vw);
    max-height: 90vh;
    overflow: auto;
    display: grid;
    gap: 0.6rem;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.35);
  }
  .modal-content h3 {
    margin: 0 0 0.25rem;
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-primary);
  }
  .field-label {
    font-weight: 600;
    font-size: 0.85rem;
    margin-top: 0.25rem;
    color: var(--text-primary);
  }
  .field-hint {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0;
  }
  .check {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    font-size: 0.9rem;
    color: var(--text-primary);
  }
  .check input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent-color);
  }
  .checks { display: grid; gap: 0.35rem; }
  input, textarea, select {
    width: 100%;
    padding: 0.5rem 0.65rem;
    box-sizing: border-box;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
  }
  select {
    padding-right: 2rem;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }
  textarea { resize: vertical; min-height: 4.5rem; }
  .modal-content .actions { margin-top: 0.5rem; }
</style>
