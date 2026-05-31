<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listProxmoxClusters,
    createProxmoxCluster,
    updateProxmoxCluster,
    syncProxmoxCluster,
    getProxmoxClusterInventory,
  } from '../lib/api.js';

  let clusters = [];
  let loading = false;
  let syncingClusterId = null;
  let error = '';
  let success = '';

  let showCreateModal = false;
  let showEditModal = false;
  let editingClusterId = null;
  let expandedClusterIds = [];
  let inventoryByClusterId = {};

  let createForm = { name: '', api_url: '', username: '', password: '', verify_ssl: false };
  let editForm = { name: '', api_url: '', username: '', password: '', verify_ssl: false, enabled: true };

  async function load() {
    loading = true;
    error = '';
    try {
      clusters = await listProxmoxClusters();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function submitCreate() {
    try {
      await createProxmoxCluster(createForm);
      createForm = { name: '', api_url: '', username: '', password: '', verify_ssl: false };
      showCreateModal = false;
      success = 'Cluster created.';
      await load();
    } catch (err) {
      error = err.message;
    }
  }

  function openEdit(cluster) {
    editingClusterId = cluster.cluster_id;
    editForm = {
      name: cluster.cluster_name || '',
      api_url: cluster.api_url || '',
      username: '',
      password: '',
      verify_ssl: false,
      enabled: true,
    };
    showEditModal = true;
  }

  async function submitEdit() {
    if (!editingClusterId) return;
    try {
      const payload = {
        name: editForm.name,
        api_url: editForm.api_url,
        verify_ssl: editForm.verify_ssl,
        enabled: editForm.enabled,
      };
      if (editForm.username) payload.username = editForm.username;
      if (editForm.password) payload.password = editForm.password;
      await updateProxmoxCluster(editingClusterId, payload);
      showEditModal = false;
      editingClusterId = null;
      success = 'Cluster updated.';
      await load();
    } catch (err) {
      error = err.message;
    }
  }

  async function runSync(clusterId) {
    syncingClusterId = clusterId;
    error = '';
    success = '';
    try {
      await syncProxmoxCluster(clusterId);
      success = 'Cluster inventory synced from Proxmox.';
      await load();
      if (expandedClusterIds.includes(clusterId)) {
        await loadInventory(clusterId);
      }
    } catch (err) {
      error = err.message;
    } finally {
      syncingClusterId = null;
    }
  }

  async function loadInventory(clusterId) {
    try {
      inventoryByClusterId = {
        ...inventoryByClusterId,
        [clusterId]: await getProxmoxClusterInventory(clusterId),
      };
    } catch (err) {
      error = err.message;
    }
  }

  async function toggleExpand(clusterId) {
    if (expandedClusterIds.includes(clusterId)) {
      expandedClusterIds = expandedClusterIds.filter((id) => id !== clusterId);
      return;
    }
    expandedClusterIds = [...expandedClusterIds, clusterId];
    if (!inventoryByClusterId[clusterId]) await loadInventory(clusterId);
  }

  function totalTemplates(inv) {
    return (inv?.nodes || []).reduce((sum, node) => sum + (node.templates?.length || 0), 0);
  }

  function totalStorages(inv) {
    return (inv?.nodes || []).reduce((sum, node) => sum + (node.storages?.length || 0), 0);
  }

  onMount(load);
</script>

<PageHeader title="Proxmox Inventory" />
<div class="page">
  <div class="top-actions">
    <button class="action-btn" on:click={() => (showCreateModal = true)}>New Cluster</button>
  </div>
  {#if error}<div class="error">{error}</div>{/if}
  {#if success}<div class="success">{success}</div>{/if}

  {#if loading}
    <p>Loading...</p>
  {:else}
    <div class="table">
      <div class="row head">
        <div>Cluster</div>
        <div>API URL</div>
        <div>Nodes</div>
        <div>Templates</div>
        <div>Storages</div>
        <div>Actions</div>
      </div>
      {#each clusters as c}
        <div class="row">
          <div>{c.cluster_name}</div>
          <div class="mono">{c.api_url}</div>
          <div>{c.node_count}</div>
          <div>{c.template_count}</div>
          <div>{c.storage_count}</div>
          <div class="row-actions">
            <button class="tiny-btn" on:click={() => runSync(c.cluster_id)} disabled={syncingClusterId === c.cluster_id}>
              {syncingClusterId === c.cluster_id ? 'Syncing...' : 'Sync'}
            </button>
            <button class="tiny-btn" on:click={() => openEdit(c)}>Edit</button>
            <button class="tiny-btn" on:click={() => toggleExpand(c.cluster_id)}>
              {expandedClusterIds.includes(c.cluster_id) ? 'Hide' : 'View'}
            </button>
          </div>
        </div>
        {#if expandedClusterIds.includes(c.cluster_id)}
          {#if inventoryByClusterId[c.cluster_id]}
            <div class="inventory-panel">
              <div class="meta-row">
                Nodes: {inventoryByClusterId[c.cluster_id].nodes.length} |
                Templates: {totalTemplates(inventoryByClusterId[c.cluster_id])} |
                Storages: {totalStorages(inventoryByClusterId[c.cluster_id])}
              </div>
              {#each inventoryByClusterId[c.cluster_id].nodes as node}
                <div class="node-card">
                  <strong>{node.node_name}</strong>
                  <div class="small-list">
                    <span class="label">Templates:</span>
                    {#if node.templates.length}
                      {node.templates.map((t) => `${t.vmid}:${t.name}`).join(', ')}
                    {:else}
                      -
                    {/if}
                  </div>
                  <div class="small-list">
                    <span class="label">Storages:</span>
                    {#if node.storages.length}
                      {node.storages.map((s) => `${s.storage_name}${s.storage_type ? ` (${s.storage_type})` : ''}`).join(', ')}
                    {:else}
                      -
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {:else}
            <div class="inventory-panel">Loading inventory...</div>
          {/if}
        {/if}
      {/each}
    </div>
  {/if}
</div>

{#if showCreateModal}
  <div class="overlay">
    <div class="modal">
      <h3>Create Cluster</h3>
      <input bind:value={createForm.name} placeholder="Cluster name" />
      <input bind:value={createForm.api_url} placeholder="https://proxmox:8006" />
      <input bind:value={createForm.username} placeholder="root@pam" />
      <input bind:value={createForm.password} type="password" placeholder="Password" />
      <label><input type="checkbox" bind:checked={createForm.verify_ssl} /> Verify SSL</label>
      <div class="row-actions">
        <button class="tiny-btn" on:click={() => (showCreateModal = false)}>Cancel</button>
        <button on:click={submitCreate}>Create</button>
      </div>
    </div>
  </div>
{/if}

{#if showEditModal}
  <div class="overlay">
    <div class="modal">
      <h3>Edit Cluster</h3>
      <input bind:value={editForm.name} placeholder="Cluster name" />
      <input bind:value={editForm.api_url} placeholder="https://proxmox:8006" />
      <input bind:value={editForm.username} placeholder="New username (optional)" />
      <input bind:value={editForm.password} type="password" placeholder="New password (optional)" />
      <label><input type="checkbox" bind:checked={editForm.verify_ssl} /> Verify SSL</label>
      <label><input type="checkbox" bind:checked={editForm.enabled} /> Enabled</label>
      <div class="row-actions">
        <button class="tiny-btn" on:click={() => { showEditModal = false; editingClusterId = null; }}>Cancel</button>
        <button on:click={submitEdit}>Save</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .top-actions { display: flex; gap: 8px; }
  .action-btn { padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: #fff; cursor: pointer; }
  .table { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
  .row { display: grid; grid-template-columns: minmax(180px, 1.5fr) minmax(220px, 2fr) 70px 90px 90px 200px; gap: 8px; align-items: center; padding: 7px 10px; border-bottom: 1px solid var(--border-color); font-size: 13px; }
  .head { background: var(--bg-secondary); font-size: 12px; color: var(--text-secondary); font-weight: 600; text-transform: uppercase; }
  .inventory-panel { padding: 10px; border-bottom: 1px solid var(--border-color); background: var(--bg-primary); display: flex; flex-direction: column; gap: 8px; }
  .meta-row { color: var(--text-secondary); font-size: 12px; }
  .node-card { border: 1px solid var(--border-color); border-radius: 6px; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
  .small-list { font-size: 12px; color: var(--text-secondary); }
  .label { color: var(--text-primary); }
  input { background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; padding: 8px; }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: white; cursor: pointer; }
  .row-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .tiny-btn { padding: 4px 8px; font-size: 11px; border: 1px solid var(--border-color); border-radius: 5px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal { width: min(560px, 92vw); max-height: 90vh; overflow: auto; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
  .success { color: #7ef0b8; }
  .error { color: var(--danger-color); }
</style>
