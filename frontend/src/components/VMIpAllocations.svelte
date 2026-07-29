<script>
  import { onMount, onDestroy } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import MultiSelect from './ui/MultiSelect.svelte';
  import Button from './ui/Button.svelte';
  import { navigate } from '../lib/router.js';
  import {
    listVmIpAllocations,
    listVmIpAllocationTags,
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
  let knownTags = [];
  let selectedIds = [];

  let showCreateModal = false;
  let showBulkAddModal = false;
  let showBulkEditModal = false;

  // Filters/search — combine server-side to stay fast even with large pools.
  let filters = {
    q: '',
    batch_tag: '',
    enabled: '',
    assigned: '',
    cluster_id: '',
  };
  let searchDebounceHandle;

  let createForm = {
    ip_address: '',
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: true,
    batch_tag: '',
  };
  let bulkAddForm = {
    start_ip: '',
    end_ip: '',
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: true,
    batch_tag: '',
  };
  let bulkAddTagError = '';
  let bulkEditForm = {
    subnet_mask: '',
    gateway: '',
    bridge_name: '',
    cluster_ids: [],
    enabled: '',
    batch_tag: '',
  };

  function activeFilterParams() {
    const params = {};
    if (filters.q.trim()) params.q = filters.q.trim();
    if (filters.batch_tag) params.batch_tag = filters.batch_tag;
    if (filters.enabled !== '') params.enabled = filters.enabled;
    if (filters.assigned !== '') params.assigned = filters.assigned;
    if (filters.cluster_id !== '') params.cluster_id = filters.cluster_id;
    return params;
  }

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [ipRows, clusterRows, tagRows] = await Promise.all([
        listVmIpAllocations(activeFilterParams()),
        listProxmoxClusters(),
        listVmIpAllocationTags(),
      ]);
      allocations = ipRows;
      clusters = clusterRows;
      knownTags = tagRows;
      selectedIds = selectedIds.filter((id) => allocations.some((a) => a.id === id));
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function refetchAllocationsOnly() {
    // Filter changes shouldn't re-fetch clusters/tags every keystroke.
    loading = true;
    error = '';
    listVmIpAllocations(activeFilterParams())
      .then((ipRows) => {
        allocations = ipRows;
        selectedIds = selectedIds.filter((id) => allocations.some((a) => a.id === id));
      })
      .catch((err) => {
        error = err.message;
      })
      .finally(() => {
        loading = false;
      });
  }

  function onSearchInput() {
    clearTimeout(searchDebounceHandle);
    searchDebounceHandle = setTimeout(refetchAllocationsOnly, 300);
  }

  function onFilterChange() {
    refetchAllocationsOnly();
  }

  function clearFilters() {
    filters = { q: '', batch_tag: '', enabled: '', assigned: '', cluster_id: '' };
    refetchAllocationsOnly();
  }

  // Reference `filters` directly (not just via activeFilterParams()) so Svelte's
  // dependency tracking actually re-runs this when any filter field changes.
  $: hasActiveFilters = !!filters && Object.keys(activeFilterParams()).length > 0;

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

  function openLinkedService(row) {
    if (!row.assigned_service_id) return;
    navigate(`/admin/services/${row.assigned_service_id}`);
  }

  async function submitCreate() {
    try {
      await createVmIpAllocation({
        ...createForm,
        bridge_name: createForm.bridge_name || null,
        cluster_ids: createForm.cluster_ids.map((v) => Number(v)),
        batch_tag: createForm.batch_tag.trim() || null,
      });
      showCreateModal = false;
      success = 'IP allocation created.';
      createForm = {
        ip_address: '',
        subnet_mask: '',
        gateway: '',
        bridge_name: '',
        cluster_ids: [],
        enabled: true,
        batch_tag: '',
      };
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitBulkAdd() {
    const tag = bulkAddForm.batch_tag.trim();
    if (!tag) {
      bulkAddTagError = 'A batch tag is required so these IPs can be found and managed together later.';
      return;
    }
    bulkAddTagError = '';
    try {
      const result = await createVmIpAllocationsBulk({
        ...bulkAddForm,
        bridge_name: bulkAddForm.bridge_name || null,
        cluster_ids: bulkAddForm.cluster_ids.map((v) => Number(v)),
        batch_tag: tag,
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
    if (bulkEditForm.batch_tag !== '') {
      payload.batch_tag = bulkEditForm.batch_tag === '-' ? null : bulkEditForm.batch_tag;
    }

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
  onDestroy(() => clearTimeout(searchDebounceHandle));

  $: clusterOptions = clusters.map((cluster) => ({
    value: String(cluster.cluster_id),
    label: cluster.cluster_name || `Cluster ${cluster.cluster_id}`,
  }));
</script>

<PageHeader title="VM IP Allocations" />
<div class="page">
  <div class="actions">
    <Button on:click={() => (showCreateModal = true)}>Add Single IP</Button>
    <Button variant="secondary" on:click={() => (showBulkAddModal = true)}>Bulk Add Range</Button>
    <Button
      variant="secondary"
      on:click={() => (showBulkEditModal = true)}
      disabled={!selectedIds.length}
    >
      Bulk Edit Selected ({selectedIds.length})
    </Button>
  </div>

  <div class="filter-bar">
    <input
      class="search-input"
      placeholder="Search IP, gateway, bridge…"
      bind:value={filters.q}
      on:input={onSearchInput}
    />
    <select bind:value={filters.batch_tag} on:change={onFilterChange}>
      <option value="">All tags</option>
      {#each knownTags as tag}
        <option value={tag}>{tag}</option>
      {/each}
    </select>
    <select bind:value={filters.assigned} on:change={onFilterChange}>
      <option value="">Assigned: any</option>
      <option value="true">Assigned only</option>
      <option value="false">Free only</option>
    </select>
    <select bind:value={filters.enabled} on:change={onFilterChange}>
      <option value="">Enabled: any</option>
      <option value="true">Enabled only</option>
      <option value="false">Disabled only</option>
    </select>
    <select bind:value={filters.cluster_id} on:change={onFilterChange}>
      <option value="">All clusters</option>
      {#each clusters as cluster}
        <option value={String(cluster.cluster_id)}>{cluster.cluster_name || `Cluster ${cluster.cluster_id}`}</option>
      {/each}
    </select>
    {#if hasActiveFilters}
      <button class="tiny" on:click={clearFilters}>Clear filters</button>
    {/if}
    <span class="result-count">{allocations.length} IP{allocations.length === 1 ? '' : 's'}</span>
  </div>

  {#if error}<div class="error">{error}</div>{/if}
  {#if success}<div class="success">{success}</div>{/if}
  {#if loading}
    <p>Loading...</p>
  {:else}
    <div class="table">
      <div class="row head">
        <div><input type="checkbox" on:change={toggleSelectAll} checked={selectedIds.length && selectedIds.length === allocations.length} /></div>
        <div>IP</div><div>Mask</div><div>Gateway</div><div>Bridge</div><div>Tag</div><div>Linked Service</div><div>Clusters</div><div>Actions</div>
      </div>
      {#each allocations as row}
        <div class="row">
          <div><input type="checkbox" checked={selectedIds.includes(row.id)} on:change={() => toggleSelect(row.id)} /></div>
          <div class="mono">{row.ip_address}</div>
          <div>{row.subnet_mask}</div>
          <div class="mono">{row.gateway}</div>
          <div>{row.bridge_name || '-'}</div>
          <div>
            {#if row.batch_tag}
              <span class="tag-chip">{row.batch_tag}</span>
            {:else}
              -
            {/if}
          </div>
          <div>
            {#if row.assigned_service_id}
              <button class="link-btn" on:click={() => openLinkedService(row)}>
                #{row.assigned_service_id} {row.assigned_service_name || ''}{row.assigned_service_type ? ` (${row.assigned_service_type})` : ''}
              </button>
            {:else}
              -
            {/if}
          </div>
          <div class="clusters">{clusterNames(row)}</div>
          <div><button class="tiny danger" on:click={() => remove(row)}>Delete</button></div>
        </div>
      {:else}
        <div class="empty-row">No IP allocations match the current filters.</div>
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
    <input
      bind:value={createForm.batch_tag}
      placeholder="Tag (optional)"
      list="known-tags"
    />
    <MultiSelect
      label="Clusters"
      options={clusterOptions}
      bind:value={createForm.cluster_ids}
      size={5}
      emptyText="No clusters available"
    />
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
    <div class="tag-field">
      <input
        bind:value={bulkAddForm.batch_tag}
        placeholder="Batch tag — pick an existing one or type a new one"
        list="known-tags"
        class:has-error={!!bulkAddTagError}
      />
      <span class="hint">Required. Identifies this batch so the IPs can be found, filtered, and managed together later.</span>
      {#if bulkAddTagError}<span class="field-error">{bulkAddTagError}</span>{/if}
      {#if knownTags.length}
        <div class="tag-suggestions">
          {#each knownTags as tag}
            <button type="button" class="tag-suggestion" on:click={() => (bulkAddForm.batch_tag = tag)}>{tag}</button>
          {/each}
        </div>
      {/if}
    </div>
    <MultiSelect
      label="Clusters"
      options={clusterOptions}
      bind:value={bulkAddForm.cluster_ids}
      size={5}
      emptyText="No clusters available"
    />
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
    <input bind:value={bulkEditForm.batch_tag} placeholder="Tag (empty keeps, '-' clears)" list="known-tags" />
    <MultiSelect
      label="Clusters (optional replace)"
      options={clusterOptions}
      bind:value={bulkEditForm.cluster_ids}
      size={5}
      emptyText="No clusters available"
    />
    <select bind:value={bulkEditForm.enabled}>
      <option value="">Keep enabled state</option>
      <option value="true">Set enabled</option>
      <option value="false">Set disabled</option>
    </select>
    <div class="actions"><button class="tiny" on:click={() => (showBulkEditModal = false)}>Cancel</button><button on:click={submitBulkEdit}>Apply</button></div>
  </div></div>
{/if}

<datalist id="known-tags">
  {#each knownTags as tag}
    <option value={tag} />
  {/each}
</datalist>

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 10px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .filter-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 4px 0; }
  .search-input { min-width: 220px; flex: 1 1 220px; max-width: 320px; }
  .result-count { font-size: 12px; color: var(--text-secondary); margin-left: auto; white-space: nowrap; }
  .table { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .row { display: grid; grid-template-columns: 34px 140px 120px 140px 110px 130px 220px 1fr 80px; gap: 8px; align-items: center; padding: 6px 8px; border-bottom: 1px solid var(--border-color); font-size: 12px; min-width: 1000px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
  .head { font-size: 11px; text-transform: uppercase; color: var(--text-secondary); background: var(--bg-secondary); font-weight: 600; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .clusters { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty-row { padding: 20px; text-align: center; color: var(--text-secondary); font-size: 13px; }
  .error { color: var(--danger-color); }
  .success { color: #7ef0b8; }
  .tag-chip {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 5px;
    background: color-mix(in srgb, var(--accent-color) 16%, transparent);
    color: var(--accent-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: inline-block;
    max-width: 100%;
  }
  .link-btn {
    background: none;
    border: none;
    color: var(--accent-color);
    cursor: pointer;
    padding: 0;
    font-size: 12px;
    text-align: left;
    text-decoration: underline;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
    display: block;
  }
  .link-btn:hover { opacity: 0.85; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal { width: min(580px, 92vw); max-height: 90vh; overflow: auto; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
  .tag-field { display: flex; flex-direction: column; gap: 4px; }
  .tag-field .hint { font-size: 11px; color: var(--text-secondary); }
  .tag-field .field-error { font-size: 11px; color: var(--danger-color); }
  .tag-suggestions { display: flex; gap: 6px; flex-wrap: wrap; }
  .tag-suggestion {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 999px;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    color: var(--text-secondary);
    cursor: pointer;
    width: fit-content;
  }
  .tag-suggestion:hover { color: var(--text-primary); border-color: var(--accent-color); }
  input.has-error { border-color: var(--danger-color); }
  input, select {
    background-color: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 8px;
  }
  select {
    padding-right: 32px;
    appearance: none;
    background-color: var(--bg-secondary);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: #fff; cursor: pointer; }
  .tiny { padding: 4px 8px; font-size: 11px; border: 1px solid var(--border-color); background: var(--bg-secondary); color: var(--text-primary); }
  .danger { border-color: var(--danger-color); color: var(--danger-color); }
</style>
