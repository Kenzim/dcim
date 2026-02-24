<script>
  import PageHeader from './PageHeader.svelte';
  import { getServerGroup, addServersToGroup, removeServerFromGroup, getServers, updateServerGroup, listISOs, listTempOS, getScripts, getOSTemplates } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let groupId;
  let group = null;
  let allServers = [];
  let loading = true;
  let error = null;
  let showAddServerModal = false;
  let selectedServerIds = [];

  let availableIsos = [];
  let availableTempOs = [];
  let availableScripts = [];
  let availableTemplates = [];
  let savingPermitted = false;

  onMount(async () => {
    if (groupId) {
      await loadGroup();
      await Promise.all([loadAllServers(), loadAvailableOptions()]);
    }
  });

  async function loadGroup() {
    try {
      loading = true;
      error = null;
      group = await getServerGroup(parseInt(groupId, 10));
    } catch (err) {
      error = err.message;
      console.error('Failed to load server group:', err);
    } finally {
      loading = false;
    }
  }

  async function loadAvailableOptions() {
    try {
      [availableIsos, availableTempOs, availableScripts, availableTemplates] = await Promise.all([
        listISOs().catch(() => []),
        listTempOS().catch(() => []),
        getScripts().catch(() => []),
        getOSTemplates().catch(() => []),
      ]);
    } catch (e) {
      console.error('Failed to load available options:', e);
    }
  }

  async function loadAllServers() {
    try {
      allServers = await getServers();
    } catch (err) {
      console.error('Failed to load servers:', err);
    }
  }

  function openAddServerModal() {
    // Pre-select servers already in the group
    selectedServerIds = group.servers.map(s => s.id);
    showAddServerModal = true;
  }

  function closeAddServerModal() {
    showAddServerModal = false;
    selectedServerIds = [];
  }

  async function handleAddServers() {
    try {
      error = null;
      await addServersToGroup(parseInt(groupId, 10), selectedServerIds);
      closeAddServerModal();
      await loadGroup();
    } catch (err) {
      error = err.message;
    }
  }

  async function handleRemoveServer(serverId) {
    if (!confirm('Remove this server from the group?')) {
      return;
    }

    try {
      await removeServerFromGroup(parseInt(groupId, 10), serverId);
      await loadGroup();
    } catch (err) {
      alert('Failed to remove server: ' + err.message);
    }
  }

  function toggleServerSelection(serverId) {
    if (selectedServerIds.includes(serverId)) {
      selectedServerIds = selectedServerIds.filter(id => id !== serverId);
    } else {
      selectedServerIds = [...selectedServerIds, serverId];
    }
  }

  async function savePermittedOptions() {
    if (!group) return;
    try {
      savingPermitted = true;
      error = null;
      await updateServerGroup(parseInt(groupId, 10), {
        name: group.name,
        description: group.description ?? null,
        enable_isos: group.enable_isos,
        permitted_isos: group.permitted_isos || [],
        enable_temp_os: group.enable_temp_os,
        permitted_temp_os: group.permitted_temp_os || [],
        enable_scripts: group.enable_scripts,
        permitted_scripts: group.permitted_scripts || [],
        enable_os_templates: group.enable_os_templates,
        permitted_os_templates: group.permitted_os_templates || [],
      });
      await loadGroup();
    } catch (err) {
      error = err.message;
    } finally {
      savingPermitted = false;
    }
  }

  function setPermitted(listKey, id) {
    const list = group[listKey] || [];
    const next = list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
    group = { ...group, [listKey]: next };
  }
</script>

<PageHeader title="Server Group Details" />

<div class="server-group-detail-container">
  {#if error}
    <div class="alert alert-danger">{error}</div>
  {/if}

  {#if loading}
    <div class="loading">Loading server group...</div>
  {:else if group}
    <div class="group-info">
      <h2>{group.name}</h2>
      {#if group.description}
        <p class="description">{group.description}</p>
      {/if}
      <div class="group-meta">
        <span><strong>{group.server_count}</strong> server{group.server_count !== 1 ? 's' : ''}</span>
      </div>
    </div>

    <div class="group-actions">
      <button class="btn btn-primary" on:click={openAddServerModal}>
        <i class="fa fa-plus"></i> Add Servers
      </button>
      <a href="/admin/server-groups" class="btn btn-secondary">
        <i class="fa fa-arrow-left"></i> Back to Groups
      </a>
    </div>

    <div class="permitted-section">
      <h3>Permitted options for clients (e.g. WHMCS)</h3>
      <p class="permitted-desc">Choose which ISOs, temporary OSs, scripts, and OS templates are available to clients using this server group.</p>

      <div class="permitted-block">
        <label class="permitted-enable">
          <input type="checkbox" bind:checked={group.enable_isos} />
          <strong>Enable ISOs</strong>
        </label>
        <div class="permitted-list">
          {#each availableIsos as iso}
            {@const id = iso.id ?? iso.filename}
            <label class="checkbox-label">
              <input
                type="checkbox"
                checked={(group.permitted_isos || []).includes(id)}
                on:change={() => setPermitted('permitted_isos', id)}
              />
              <span>{iso.name ?? id}{#if iso.size_mb} ({iso.size_mb} MB){/if}</span>
            </label>
          {/each}
          {#if availableIsos.length === 0}
            <span class="muted">No ISOs found. Add files to the isos/ directory.</span>
          {/if}
        </div>
      </div>

      <div class="permitted-block">
        <label class="permitted-enable">
          <input type="checkbox" bind:checked={group.enable_temp_os} />
          <strong>Enable Temporary OSs</strong>
        </label>
        <div class="permitted-list">
          {#each availableTempOs as os}
            <label class="checkbox-label">
              <input
                type="checkbox"
                checked={(group.permitted_temp_os || []).includes(os.id)}
                on:change={() => setPermitted('permitted_temp_os', os.id)}
              />
              <span>{os.name} ({os.id})</span>
            </label>
          {/each}
          {#if availableTempOs.length === 0}
            <span class="muted">No temporary OSs configured.</span>
          {/if}
        </div>
      </div>

      <div class="permitted-block">
        <label class="permitted-enable">
          <input type="checkbox" bind:checked={group.enable_scripts} />
          <strong>Enable Custom Scripts</strong>
        </label>
        <div class="permitted-list">
          {#each availableScripts as script}
            <label class="checkbox-label">
              <input
                type="checkbox"
                checked={(group.permitted_scripts || []).includes(script.id)}
                on:change={() => setPermitted('permitted_scripts', script.id)}
              />
              <span>{script.name}</span>
            </label>
          {/each}
          {#if availableScripts.length === 0}
            <span class="muted">No scripts found.</span>
          {/if}
        </div>
      </div>

      <div class="permitted-block">
        <label class="permitted-enable">
          <input type="checkbox" bind:checked={group.enable_os_templates} />
          <strong>Enable Install OS Templates</strong>
        </label>
        <div class="permitted-list">
          {#each availableTemplates as tmpl}
            <label class="checkbox-label">
              <input
                type="checkbox"
                checked={(group.permitted_os_templates || []).includes(tmpl.id)}
                on:change={() => setPermitted('permitted_os_templates', tmpl.id)}
              />
              <span>{tmpl.name}</span>
            </label>
          {/each}
          {#if availableTemplates.length === 0}
            <span class="muted">No OS templates found.</span>
          {/if}
        </div>
      </div>

      <button class="btn btn-primary" on:click={savePermittedOptions} disabled={savingPermitted}>
        {savingPermitted ? 'Saving...' : 'Save permitted options'}
      </button>
    </div>

    <div class="servers-section">
      <h3>Servers in Group</h3>
      {#if group.servers.length === 0}
        <div class="empty-state">
          <p>No servers in this group. Click "Add Servers" to add servers.</p>
        </div>
      {:else}
        <table class="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>IP Address</th>
              <th>Description</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each group.servers as server}
              <tr>
                <td>
                  <a href="/admin/servers/{server.id}">{server.name}</a>
                </td>
                <td>{server.server_ip}</td>
                <td>{server.description || '-'}</td>
                <td>
                  <button class="btn btn-sm btn-danger" on:click={() => handleRemoveServer(server.id)}>
                    <i class="fa fa-times"></i> Remove
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}
</div>

{#if showAddServerModal}
  <button type="button" class="modal-overlay" tabindex="-1" on:click={(e) => e.target === e.currentTarget && closeAddServerModal()} aria-label="Close modal">
    <div class="modal modal-large" role="dialog">
      <div class="modal-header">
        <h3>Add Servers to Group</h3>
        <button class="btn btn-sm btn-secondary" on:click={closeAddServerModal}>
          <i class="fa fa-times"></i>
        </button>
      </div>
      <div class="modal-body">
        {#if error}
          <div class="alert alert-danger">{error}</div>
        {/if}
        <p>Select servers to add to this group:</p>
        <div class="server-selection">
          {#each allServers as server}
            <label class="checkbox-label">
              <input
                type="checkbox"
                checked={selectedServerIds.includes(server.id)}
                on:change={() => toggleServerSelection(server.id)}
              />
              <span>
                <strong>{server.name}</strong> - {server.server_ip}
                {#if server.description}
                  <br><small>{server.description}</small>
                {/if}
              </span>
            </label>
          {/each}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" on:click={closeAddServerModal}>Cancel</button>
          <button type="button" class="btn btn-primary" on:click={handleAddServers}>
            Add Selected Servers ({selectedServerIds.length})
          </button>
        </div>
      </div>
    </div>
  </button>
{/if}

<style>
  button.modal-overlay {
    padding: 0;
    border: none;
    font: inherit;
    color: inherit;
    cursor: default;
    width: 100%;
    height: 100%;
    text-align: left;
  }

  .server-group-detail-container {
    padding: 20px;
  }

  .group-info {
    margin-bottom: 30px;
  }

  .group-info h2 {
    margin-bottom: 10px;
  }

  .description {
    color: var(--text-secondary);
    margin-bottom: 15px;
  }

  .group-meta {
    color: var(--text-tertiary);
  }

  .group-actions {
    margin-bottom: 30px;
    display: flex;
    gap: 10px;
  }

  .permitted-section {
    margin-top: 30px;
    padding: 24px;
    background: var(--bg-primary);
    border-radius: 8px;
    border: 1px solid var(--border-color);
    box-shadow: var(--shadow-sm);
  }

  .permitted-section h3 {
    margin-bottom: 8px;
    color: var(--text-primary);
  }

  .permitted-desc {
    color: var(--text-secondary);
    margin-bottom: 20px;
    font-size: 0.95rem;
    line-height: 1.5;
  }

  .permitted-block {
    margin-bottom: 24px;
  }

  .permitted-enable {
    display: block;
    margin-bottom: 10px;
    cursor: pointer;
    color: var(--text-primary);
  }

  .permitted-enable input {
    margin-right: 8px;
  }

  .permitted-list {
    margin-left: 24px;
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 10px;
    background: var(--bg-secondary);
  }

  .permitted-section .checkbox-label {
    padding: 8px 10px;
    color: var(--text-primary);
  }

  .permitted-section .checkbox-label:hover {
    background: var(--bg-tertiary);
    border-radius: 4px;
  }

  .muted {
    color: var(--text-tertiary);
    font-size: 0.9rem;
  }

  .servers-section {
    margin-top: 30px;
  }

  .servers-section h3 {
    margin-bottom: 15px;
  }

  .empty-state {
    text-align: center;
    padding: 40px;
    color: var(--text-secondary);
  }

  .server-selection {
    max-height: 400px;
    overflow-y: auto;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 10px;
    background: var(--bg-secondary);
  }

  .checkbox-label {
    display: block;
    padding: 10px;
    border-bottom: 1px solid var(--border-color);
    cursor: pointer;
    color: var(--text-primary);
  }

  .checkbox-label:last-child {
    border-bottom: none;
  }

  .checkbox-label:hover {
    background: var(--bg-tertiary);
  }

  .checkbox-label input {
    margin-right: 10px;
  }

  .modal-large {
    max-width: 600px;
  }
</style>
