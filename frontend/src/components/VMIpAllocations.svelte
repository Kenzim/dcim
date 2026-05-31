<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listVmIpAllocations,
    createVmIpAllocation,
    createVmIpAllocationsBulk,
    bulkUpdateVmIpAllocations,
    deleteVmIpAllocation,
    listProxmoxClusters,
  } from '../lib/api.js';

  let loading = false;
  let error = '';
  let success = '';
  let allocations = [];
  let clusters = [];
  let selectedIds = [];

  let showCreateModal = false;
  let showBulkAddModal = false;
  let showBulkEditModal = false;

  let createForm = {
    ip_address: '',
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: true,
  };
  let bulkAddForm = {
    start_ip: '',
    end_ip: '',
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: true,
  };
  let bulkEditForm = {
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: '',
  };

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [ipRows, clusterRows] = await Promise.all([
        listVmIpAllocations(),
        listProxmoxClusters(),
      ]);
      allocations = ipRows;
      clusters = clusterRows;
      selectedIds = selectedIds.filter((id) => allocations.some((a) => a.id === id));
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function toggleSelect(id) {
    selectedIds = selectedIds.includes(id)
      ? selectedIds.filter((value) => value !== id)
      : [...selectedIds, id];
  }

  function toggleSelectAll() {
    if (selectedIds.length === allocations.length) {
      selectedIds = [];
    } else {
      selectedIds = allocations.map((item) => item.id);
    }
  }

  function clusterNames(row) {
    if (!row.clusters || row.clusters.length === 0) return '-';
    return row.clusters.map((cluster) => cluster.name).join(', ');
  }

  async function submitCreate() {
    try {
      await createVmIpAllocation({
        ...createForm,
        bridge_name: createForm.bridge_name || null,
        cluster_ids: createForm.cluster_ids.map((v) => Number(v)),
      });
      showCreateModal = false;
      success = 'IP allocation created.';
      createForm = { ip_address: '', subnet_mask: '', gateway: '', bridge_name: '', cluster_ids: [], enabled: true };
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitBulkAdd() {
    try {
      const result = await createVmIpAllocationsBulk({
        ...bulkAddForm,
        bridge_name: bulkAddForm.bridge_name || null,
        cluster_ids: bulkAddForm.cluster_ids.map((v) => Number(v)),
      });
      showBulkAddModal = false;
      success = `Bulk add complete: created ${result.created}, skipped ${result.skipped_existing}.`;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitBulkEdit() {
    if (!selectedIds.length) return;
    const payload = { ids: selectedIds };
    if (bulkEditForm.subnet_mask) payload.subnet_mask = bulkEditForm.subnet_mask;
    if (bulkEditForm.gateway) payload.gateway = bulkEditForm.gateway;
    if (bulkEditForm.bridge_name !== '') {
      payload.bridge_name = bulkEditForm.bridge_name === '-' ? null : bulkEditForm.bridge_name;
    }
    if (bulkEditForm.cluster_ids.length) payload.cluster_ids = bulkEditForm.cluster_ids.map((v) => Number(v));
    if (bulkEditForm.enabled !== '') payload.enabled = bulkEditForm.enabled === 'true';

    try {
      const result = await bulkUpdateVmIpAllocations(payload);
      showBulkEditModal = false;
      success = `Bulk edit updated ${result.updated} IPs.`;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function remove(row) {
    if (!window.confirm(`Delete ${row.ip_address}?`)) return;
    try {
      await deleteVmIpAllocation(row.id);
      success = `Deleted ${row.ip_address}.`;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  onMount(loadData);
</script>

<PageHeader title="VM IP Allocations" />
<div class="page">
  <div class="actions">
    <button on:click={() => (showCreateModal = true)}>Add Single IP</button>
    <button on:click={() => (showBulkAddModal = true)}>Bulk Add Range</button>
    <button on:click={() => (showBulkEditModal = true)} disabled={!selectedIds.length}>
      Bulk Edit Selected ({selectedIds.length})
    </button>
  </div>
  {#if error}<div class="error">{error}</div>{/if}
  {#if success}<div class="success">{success}</div>{/if}
  {#if loading}
    <p>Loading...</p>
  {:else}
    <div class="table">
      <div class="row head">
        <div><input type="checkbox" on:change={toggleSelectAll} checked={selectedIds.length && selectedIds.length === allocations.length} /></div>
        <div>IP</div><div>Mask</div><div>Gateway</div><div>Bridge</div><div>Linked Service</div><div>Clusters</div><div>Actions</div>
      </div>
      {#each allocations as row}
        <div class="row">
          <div><input type="checkbox" checked={selectedIds.includes(row.id)} on:change={() => toggleSelect(row.id)} /></div>
          <div class="mono">{row.ip_address}</div>
          <div>{row.subnet_mask}</div>
          <div class="mono">{row.gateway}</div>
          <div>{row.bridge_name || '-'}</div>
          <div>
            {#if row.assigned_service_id}
              #{row.assigned_service_id} {row.assigned_service_name || ''}{row.assigned_service_type ? ` (${row.assigned_service_type})` : ''}
            {:else}
              -
            {/if}
          </div>
          <div class="clusters">{clusterNames(row)}</div>
          <div><button class="tiny danger" on:click={() => remove(row)}>Delete</button></div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showCreateModal}
  <div class="overlay"><div class="modal">
    <h3>Add Single VM IP</h3>
    <input bind:value={createForm.ip_address} placeholder="IP address" />
    <input bind:value={createForm.subnet_mask} placeholder="Subnet mask (e.g. 255.255.240.0 or /20)" />
    <input bind:value={createForm.gateway} placeholder="Gateway IP" />
    <input bind:value={createForm.bridge_name} placeholder="Bridge name (optional)" />
    <select bind:value={createForm.cluster_ids} multiple size="5">
      {#each clusters as cluster}<option value={String(cluster.cluster_id)}>{cluster.cluster_name}</option>{/each}
    </select>
    <label><input type="checkbox" bind:checked={createForm.enabled} /> Enabled</label>
    <div class="actions"><button class="tiny" on:click={() => (showCreateModal = false)}>Cancel</button><button on:click={submitCreate}>Save</button></div>
  </div></div>
{/if}

{#if showBulkAddModal}
  <div class="overlay"><div class="modal">
    <h3>Bulk Add IP Range</h3>
    <input bind:value={bulkAddForm.start_ip} placeholder="Start IP" />
    <input bind:value={bulkAddForm.end_ip} placeholder="End IP" />
    <input bind:value={bulkAddForm.subnet_mask} placeholder="Subnet mask" />
    <input bind:value={bulkAddForm.gateway} placeholder="Gateway IP" />
    <input bind:value={bulkAddForm.bridge_name} placeholder="Bridge name (optional)" />
    <select bind:value={bulkAddForm.cluster_ids} multiple size="5">
      {#each clusters as cluster}<option value={String(cluster.cluster_id)}>{cluster.cluster_name}</option>{/each}
    </select>
    <label><input type="checkbox" bind:checked={bulkAddForm.enabled} /> Enabled</label>
    <div class="actions"><button class="tiny" on:click={() => (showBulkAddModal = false)}>Cancel</button><button on:click={submitBulkAdd}>Run Bulk Add</button></div>
  </div></div>
{/if}

{#if showBulkEditModal}
  <div class="overlay"><div class="modal">
    <h3>Bulk Edit Selected ({selectedIds.length})</h3>
    <input bind:value={bulkEditForm.subnet_mask} placeholder="Subnet mask (leave empty to keep)" />
    <input bind:value={bulkEditForm.gateway} placeholder="Gateway (leave empty to keep)" />
    <input bind:value={bulkEditForm.bridge_name} placeholder="Bridge name (empty keeps, '-' clears)" />
    <select bind:value={bulkEditForm.cluster_ids} multiple size="5">
      {#each clusters as cluster}<option value={String(cluster.cluster_id)}>{cluster.cluster_name}</option>{/each}
    </select>
    <select bind:value={bulkEditForm.enabled}>
      <option value="">Keep enabled state</option>
      <option value="true">Set enabled</option>
      <option value="false">Set disabled</option>
    </select>
    <div class="actions"><button class="tiny" on:click={() => (showBulkEditModal = false)}>Cancel</button><button on:click={submitBulkEdit}>Apply</button></div>
  </div></div>
{/if}

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 10px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .table { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
  .row { display: grid; grid-template-columns: 34px 140px 120px 140px 120px 220px 1fr 80px; gap: 8px; align-items: center; padding: 6px 8px; border-bottom: 1px solid var(--border-color); font-size: 12px; }
  .head { font-size: 11px; text-transform: uppercase; color: var(--text-secondary); background: var(--bg-secondary); font-weight: 600; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .clusters { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .error { color: var(--danger-color); }
  .success { color: #7ef0b8; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal { width: min(580px, 92vw); max-height: 90vh; overflow: auto; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
  input, select { background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; padding: 8px; }
  select[multiple] {
    min-height: 130px;
    height: 130px;
    line-height: 1.35;
    overflow-y: auto;
  }
  select[multiple] option {
    padding: 4px 6px;
  }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: #fff; cursor: pointer; }
  .tiny { padding: 4px 8px; font-size: 11px; border: 1px solid var(--border-color); background: var(--bg-secondary); color: var(--text-primary); }
  .danger { border-color: var(--danger-color); color: var(--danger-color); }
</style>
