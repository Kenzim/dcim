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
  let osTypes = [];

  let showCreateModal = false;
  let showEditModal = false;
  let editingTemplateId = null;

  let createForm = {
    name: '',
    description: '',
    os_type: 'Linux - Cloudinit',
    proxmox_template_name: '',
    enabled: true,
  };

  let editForm = {
    name: '',
    description: '',
    os_type: 'Linux - Cloudinit',
    proxmox_template_name: '',
    enabled: true,
  };

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [templateRows, osTypeRows] = await Promise.all([listVmTemplates(), listVmTemplateOsTypes()]);
      templates = templateRows;
      osTypes = osTypeRows;
      if (osTypes.length && !createForm.os_type) createForm.os_type = osTypes[0];
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
      });
      showCreateModal = false;
      createForm = {
        name: '',
        description: '',
        os_type: osTypes[0] || 'Linux - Cloudinit',
        proxmox_template_name: '',
        enabled: true,
      };
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function startEdit(template) {
    editingTemplateId = template.id;
    editForm = {
      name: template.name || '',
      description: template.description || '',
      os_type: template.os_type || (osTypes[0] || 'Linux - Cloudinit'),
      proxmox_template_name: template.proxmox_template_name || '',
      enabled: !!template.enabled,
    };
    showEditModal = true;
  }

  async function submitEdit() {
    if (!editingTemplateId) return;
    try {
      await updateVmTemplate(editingTemplateId, {
        ...editForm,
        description: editForm.description || null,
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

  onMount(loadData);
</script>

<PageHeader title="VM Templates" />
<div class="templates-page">
  <p class="page-hint">
    Each row is a <strong>Proxmox clone source</strong> plus a single <strong>provisioning strategy</strong> field
    (<code>os_type</code>): the value picks the RackFlow plan (e.g. <em>Linux - Cloudinit</em> →
    <code>cloudinit_clone</code>, <em>Linux - Guest agent</em> → <code>guest_agent</code>). There is no separate “OS
    profile” on templates — model and strategy are the same dropdown.
  </p>
  <div class="top-actions">
    <button class="action-btn" on:click={() => (showCreateModal = true)}>New VM Template</button>
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
      <textarea bind:value={createForm.description} rows="3" placeholder="Description (optional)" />
      <label class="field-label">Provisioning strategy</label>
      <p class="field-hint">Same field as API <code>os_type</code> — selects clone + cloud-init vs guest-agent plan shape.</p>
      <select bind:value={createForm.os_type}>
        {#each osTypes as osType}
          <option value={osType}>{osType}</option>
        {/each}
      </select>
      <label class="field-label">Proxmox clone template name</label>
      <p class="field-hint">Exact template/volid name Proxmox uses when cloning.</p>
      <input bind:value={createForm.proxmox_template_name} placeholder="e.g. bookworm" />
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
      <textarea bind:value={editForm.description} rows="3" placeholder="Description (optional)" />
      <label class="field-label">Provisioning strategy</label>
      <p class="field-hint">API <code>os_type</code> — merged model + strategy.</p>
      <select bind:value={editForm.os_type}>
        {#each osTypes as osType}
          <option value={osType}>{osType}</option>
        {/each}
      </select>
      <label class="field-label">Proxmox clone template name</label>
      <input bind:value={editForm.proxmox_template_name} placeholder="e.g. bookworm" />
      <label class="check"><input type="checkbox" bind:checked={editForm.enabled} /> Enabled</label>
      <div class="actions">
        <button class="tiny-btn" on:click={() => { showEditModal = false; editingTemplateId = null; }}>Cancel</button>
        <button on:click={submitEdit}>Save</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .templates-page { padding: 24px; display: flex; flex-direction: column; gap: 12px; }
  .page-hint {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
    max-width: 52rem;
    line-height: 1.45;
  }
  .page-hint code { font-size: 12px; }
  .top-actions { display: flex; gap: 8px; }
  .action-btn { padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: #fff; cursor: pointer; }
  .error { color: var(--danger-color); }
  .template-table { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
  .row { display: grid; grid-template-columns: minmax(200px, 2fr) minmax(140px, 1.2fr) minmax(160px, 1.5fr) 72px 140px; gap: 10px; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--border-color); font-size: 13px; }
  .field-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-top: 4px; }
  .field-hint { font-size: 12px; color: var(--text-tertiary); margin: -4px 0 4px 0; line-height: 1.35; }
  .row:last-child { border-bottom: none; }
  .head { background: var(--bg-secondary); font-size: 12px; font-weight: 600; text-transform: uppercase; color: var(--text-secondary); }
  .meta { color: var(--text-secondary); font-size: 12px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .actions { display: flex; gap: 6px; align-items: center; }
  .tiny-btn { padding: 4px 8px; border: 1px solid var(--border-color); border-radius: 5px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .danger { border-color: var(--danger-color); color: var(--danger-color); }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal-content { width: min(560px, 92vw); max-height: 90vh; overflow: auto; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
  input, select, textarea { background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; padding: 8px; }
  .check { color: var(--text-secondary); font-size: 13px; display: flex; align-items: center; gap: 6px; margin-top: 6px; }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: white; cursor: pointer; }
</style>
