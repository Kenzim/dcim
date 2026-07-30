<script>
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import {
    getServices,
    getExternalUsers,
    listProxmoxClusters,
    listCatalogProducts,
    listVmTemplates,
    getProxmoxClusterInventory,
    createAdminVmService,
    createAdminHttpProxyService,
    listIpamSubnets,
    listProxySubnetGroups,
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  let services = [];
  let externalUsers = [];
  let loading = true;
  let error = null;

  let q = '';
  let serviceTypeFilter = 'all'; // all | bare_metal | vm | http_proxy
  let statusFilter = 'all';
  let provisioningSourceFilter = 'all';
  let searchDebounce;

  let proxmoxClusters = [];
  let showInternalVmForm = false;
  let internalVmBusy = false;
  let internalVmError = null;
  let catalogProducts = [];
  let catalogVmTemplates = [];
  let catalogLoading = false;
  let catalogLoadError = null;
  let proxmoxClusterNodes = [];
  let proxmoxNodesLoading = false;
  let proxmoxNodesError = null;
  let internalVmCustomNode = false;
  let vmFormSetPlacement = false;
  let internalVmForm = {
    name: '',
    product_code: '',
    vm_template_id: '',
    proxmox_cluster_id: '',
    proxmox_node_name: '',
    proxmox_vmid: '',
    description: '',
    external_user_id: '',
    external_service_id: '',
    auto_provision: true,
  };

  let showProxyForm = false;
  let proxyBusy = false;
  let proxyError = null;
  let ipamSubnets = [];
  let proxySubnetGroups = [];
  let proxyForm = {
    name: '',
    product_code: '',
    ip_count: '',
    subnet_group_id: '',
    subnet_id: '',
    description: '',
    external_user_id: '',
    external_service_id: '',
  };

  onMount(async () => {
    await Promise.all([loadServices(), loadExternalUsers(), loadProxmoxClusters()]);
  });

  function onSearchInput() {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(loadServices, 300);
  }

  async function loadProxmoxClusters() {
    try {
      proxmoxClusters = await listProxmoxClusters();
    } catch (e) {
      console.warn('Proxmox clusters not loaded:', e);
      proxmoxClusters = [];
    }
  }

  async function loadServices() {
    try {
      loading = true;
      error = null;
      const params = {};
      if (q.trim()) params.q = q.trim();
      if (statusFilter !== 'all') params.status_filter = statusFilter;
      if (provisioningSourceFilter !== 'all') params.provisioning_source = provisioningSourceFilter;
      if (serviceTypeFilter !== 'all') params.service_type = serviceTypeFilter;
      services = await getServices(params);
    } catch (err) {
      error = err.message;
      console.error('Failed to load services:', err);
    } finally {
      loading = false;
    }
  }

  $: vmCatalogProducts = (catalogProducts || []).filter(
    (p) => p.family_service_type === 'vm' && p.enabled !== false,
  );

  $: proxyCatalogProducts = (catalogProducts || []).filter(
    (p) => p.family_service_type === 'http_proxy' && p.enabled !== false,
  );

  $: selectedVmCatalogProduct = vmCatalogProducts.find(
    (p) => p.code === internalVmForm.product_code,
  );

  $: vmTemplatesForSelectedProduct = selectedVmCatalogProduct
    ? catalogVmTemplates.filter((t) =>
        (selectedVmCatalogProduct.vm_template_ids || []).includes(t.id),
      )
    : [];

  async function loadVmCatalog() {
    catalogLoading = true;
    catalogLoadError = null;
    try {
      const [prods, tpls] = await Promise.all([listCatalogProducts(), listVmTemplates()]);
      catalogProducts = prods || [];
      catalogVmTemplates = tpls || [];
    } catch (e) {
      catalogLoadError = e.message || String(e);
      catalogProducts = [];
      catalogVmTemplates = [];
    } finally {
      catalogLoading = false;
    }
  }

  async function toggleInternalVmForm() {
    showInternalVmForm = !showInternalVmForm;
    internalVmError = null;
    if (showInternalVmForm) {
      await loadVmCatalog();
      const cid = parseInt(String(internalVmForm.proxmox_cluster_id), 10);
      if (Number.isFinite(cid)) {
        await loadProxmoxNodesForCluster(cid);
      }
    }
  }

  async function toggleProxyForm() {
    showProxyForm = !showProxyForm;
    proxyError = null;
    if (showProxyForm) {
      await loadVmCatalog();
      try {
        const [subnets, groups] = await Promise.all([
          listIpamSubnets(),
          listProxySubnetGroups().catch(() => []),
        ]);
        ipamSubnets = subnets || [];
        proxySubnetGroups = groups || [];
      } catch (e) {
        ipamSubnets = [];
        proxySubnetGroups = [];
      }
    }
  }

  async function submitProxy() {
    proxyError = null;
    proxyBusy = true;
    try {
      if (!proxyForm.name.trim()) throw new Error('Name is required');
      const payload = { name: proxyForm.name.trim() };
      if (proxyForm.product_code.trim()) payload.product_code = proxyForm.product_code.trim();
      if (proxyForm.description.trim()) payload.description = proxyForm.description.trim();
      const ipCount = parseInt(String(proxyForm.ip_count), 10);
      if (Number.isFinite(ipCount)) payload.ip_count = ipCount;
      const subnetGroupId = parseInt(String(proxyForm.subnet_group_id), 10);
      if (Number.isFinite(subnetGroupId)) payload.subnet_group_id = subnetGroupId;
      const subnetId = parseInt(String(proxyForm.subnet_id), 10);
      if (Number.isFinite(subnetId)) payload.subnet_id = subnetId;
      const extId = parseInt(String(proxyForm.external_user_id), 10);
      if (Number.isFinite(extId)) payload.external_user_id = extId;
      if (proxyForm.external_service_id.trim()) payload.external_service_id = proxyForm.external_service_id.trim();
      const created = await createAdminHttpProxyService(payload);
      proxyForm = {
        name: '',
        product_code: '',
        ip_count: '',
        subnet_group_id: '',
        subnet_id: '',
        description: '',
        external_user_id: '',
        external_service_id: '',
      };
      showProxyForm = false;
      await loadServices();
      if (created?.id) openService(created);
    } catch (e) {
      proxyError = e.message || String(e);
    } finally {
      proxyBusy = false;
    }
  }

  function onInternalVmProductChange() {
    internalVmForm.vm_template_id = '';
  }

  async function loadProxmoxNodesForCluster(clusterId) {
    proxmoxNodesError = null;
    proxmoxClusterNodes = [];
    if (!Number.isFinite(clusterId)) {
      return;
    }
    proxmoxNodesLoading = true;
    try {
      const inv = await getProxmoxClusterInventory(clusterId);
      proxmoxClusterNodes = (inv.nodes || [])
        .map((n) => ({
          node_name: n.node_name,
          enabled: n.enabled !== false,
        }))
        .filter((n) => n.node_name)
        .sort((a, b) => a.node_name.localeCompare(b.node_name));
    } catch (e) {
      proxmoxNodesError = e.message || String(e);
      proxmoxClusterNodes = [];
    } finally {
      proxmoxNodesLoading = false;
    }
  }

  async function onInternalVmClusterChange() {
    internalVmForm.proxmox_node_name = '';
    internalVmCustomNode = false;
    const cid = parseInt(String(internalVmForm.proxmox_cluster_id), 10);
    await loadProxmoxNodesForCluster(cid);
  }

  async function loadExternalUsers() {
    try {
      externalUsers = await getExternalUsers();
    } catch (err) {
      console.error('Failed to load external users:', err);
    }
  }

  function getStatusBadgeClass(status) {
    switch (status.toLowerCase()) {
      case 'active':
        return 'status-badge status-active';
      case 'suspended':
        return 'status-badge status-suspended';
      case 'terminated':
        return 'status-badge status-terminated';
      case 'pending':
        return 'status-badge status-pending';
      default:
        return 'status-badge';
    }
  }

  function formatDate(dateString) {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleString();
  }

  function serviceTypeLabel(t) {
    switch ((t || '').toLowerCase()) {
      case 'vm':
        return 'VM';
      case 'bare_metal':
        return 'Bare metal';
      case 'http_proxy':
        return 'HTTP proxy';
      default:
        return t || '—';
    }
  }

  function openService(service) {
    if (!service?.id) return;
    navigate(`/admin/services/${service.id}`);
  }

  async function submitInternalVm() {
    internalVmError = null;
    internalVmBusy = true;
    try {
      if (!internalVmForm.name.trim()) throw new Error('Name is required');
      const tmplId = parseInt(String(internalVmForm.vm_template_id), 10);
      if (!internalVmForm.product_code.trim() || !Number.isFinite(tmplId)) {
        throw new Error('Choose a VM product and a VM template from the lists (template must be linked to that product in Product catalog).');
      }
      const payload = {
        name: internalVmForm.name.trim(),
        product_code: internalVmForm.product_code.trim(),
        vm_template_id: tmplId,
        description: internalVmForm.description.trim() || undefined,
      };
      const extId = parseInt(String(internalVmForm.external_user_id), 10);
      if (Number.isFinite(extId)) {
        payload.external_user_id = extId;
      }
      if (internalVmForm.external_service_id.trim()) {
        payload.external_service_id = internalVmForm.external_service_id.trim();
      }
      payload.auto_provision = internalVmForm.auto_provision;
      if (vmFormSetPlacement) {
        const cid = parseInt(String(internalVmForm.proxmox_cluster_id), 10);
        const vmid = parseInt(String(internalVmForm.proxmox_vmid), 10);
        if (!Number.isFinite(cid) || !internalVmForm.proxmox_node_name.trim() || !Number.isFinite(vmid)) {
          throw new Error('With placement enabled: choose cluster, node, and vmid (or turn placement off for a pending-only record).');
        }
        payload.proxmox_cluster_id = cid;
        payload.proxmox_node_name = internalVmForm.proxmox_node_name.trim();
        payload.proxmox_vmid = vmid;
      }
      await createAdminVmService(payload);
      internalVmForm = {
        name: '',
        product_code: '',
        vm_template_id: '',
        proxmox_cluster_id: '',
        proxmox_node_name: '',
        proxmox_vmid: '',
        description: '',
        external_user_id: '',
        external_service_id: '',
        auto_provision: true,
      };
      proxmoxClusterNodes = [];
      internalVmCustomNode = false;
      vmFormSetPlacement = false;
      proxmoxNodesError = null;
      showInternalVmForm = false;
      await loadServices();
    } catch (e) {
      internalVmError = e.message || String(e);
    } finally {
      internalVmBusy = false;
    }
  }
</script>

<PageHeader title="Services" />

<div class="admin-page services-container">
  <div class="section-header">
    <div class="filters">
      <input
        class="search-input"
        type="search"
        placeholder="Search name, owner, external id…"
        bind:value={q}
        on:input={onSearchInput}
      />
      <label for="type-filter">Type:</label>
      <select id="type-filter" bind:value={serviceTypeFilter} on:change={loadServices}>
        <option value="all">All types</option>
        <option value="bare_metal">Bare metal</option>
        <option value="vm">Virtual machine</option>
        <option value="http_proxy">HTTP proxy</option>
      </select>
      <label for="status-filter">Status:</label>
      <select id="status-filter" bind:value={statusFilter} on:change={loadServices}>
        <option value="all">All</option>
        <option value="active">Active</option>
        <option value="suspended">Suspended</option>
        <option value="terminated">Terminated</option>
        <option value="pending">Pending</option>
      </select>
      <label for="prov-filter">Source:</label>
      <select id="prov-filter" bind:value={provisioningSourceFilter} on:change={loadServices}>
        <option value="all">All</option>
        <option value="billing">Billing</option>
        <option value="internal">Internal test</option>
      </select>
      {#if serviceTypeFilter === 'vm' || serviceTypeFilter === 'all'}
        <button type="button" class="btn-primary" on:click={toggleInternalVmForm}>
          {showInternalVmForm ? 'Hide' : '+'} Create pending VM
        </button>
      {/if}
      {#if serviceTypeFilter === 'http_proxy' || serviceTypeFilter === 'all'}
        <button type="button" class="btn-primary" on:click={toggleProxyForm}>
          {showProxyForm ? 'Hide' : '+'} Create proxy service
        </button>
      {/if}
    </div>
  </div>

  {#if showProxyForm && (serviceTypeFilter === 'http_proxy' || serviceTypeFilter === 'all')}
    <div class="internal-vm-panel">
      <h4>Create HTTP/SOCKS proxy service</h4>
      <p class="hint">
        Creates an <strong>http_proxy</strong> service with no rack Server; IP(s) are auto-assigned from IPAM
        immediately. Pick a proxy <strong>product</strong> to use its catalog defaults (IP count / subnet group),
        or leave it blank and set overrides below. A single-subnet override wins over a subnet group.
      </p>
      {#if proxyError}<div class="error">{proxyError}</div>{/if}
      <div class="form-grid">
        <label>Service name <input bind:value={proxyForm.name} placeholder="unique name" /></label>
        <label>
          Proxy product (optional)
          <select bind:value={proxyForm.product_code}>
            <option value="">— no product (use overrides below) —</option>
            {#each proxyCatalogProducts as p}
              <option value={p.code}>{p.name} ({p.code})</option>
            {/each}
          </select>
        </label>
        <label>
          IP count override
          <input type="number" min="1" max="32" bind:value={proxyForm.ip_count} placeholder="default: 1" />
        </label>
        <label>
          Subnet group override
          <select bind:value={proxyForm.subnet_group_id}>
            <option value="">Inherit from product / any enabled</option>
            {#each proxySubnetGroups.filter((g) => g.enabled !== false) as g}
              <option value={String(g.id)}>{g.name} ({g.code})</option>
            {/each}
          </select>
        </label>
        <label>
          Single subnet override (lab)
          <select bind:value={proxyForm.subnet_id}>
            <option value="">None (use group / any)</option>
            {#each ipamSubnets as s}
              <option value={String(s.id)}>{s.name} ({s.cidr})</option>
            {/each}
          </select>
        </label>
        <label>
          Billing owner (optional)
          <select bind:value={proxyForm.external_user_id}>
            <option value="">Internal / lab — no external user</option>
            {#each externalUsers as u}
              <option value={String(u.id)}>
                {u.external_username || u.external_user_id} — {u.integration_name} (id {u.id})
              </option>
            {/each}
          </select>
        </label>
        <label>
          External service id (optional)
          <input bind:value={proxyForm.external_service_id} placeholder="e.g. WHMCS service id" />
        </label>
        <label class="full-width">Description <input bind:value={proxyForm.description} /></label>
      </div>
      <button type="button" class="btn-primary" disabled={proxyBusy} on:click={submitProxy}>
        {proxyBusy ? 'Creating…' : 'Create proxy service'}
      </button>
    </div>
  {/if}

  {#if showInternalVmForm && (serviceTypeFilter === 'vm' || serviceTypeFilter === 'all')}
    <div class="internal-vm-panel">
      <h4>Create pending VM service</h4>
      <p class="hint">
        Adds a <strong>VM</strong> row in <code>pending</code> (no RackFlow <strong>Server</strong>). Pick catalog
        <strong>product</strong> + <strong>template</strong>. Leave placement unchecked until the VM exists in Proxmox;
        then edit placement via API or recreate with placement. Optional: link a billing <strong>external user</strong>.
      </p>
      <p class="hint muted">
        Uncheck placement to create a placeholder service; check it when cluster / node / vmid are known. Nodes load
        from synced inventory when placement is on.
      </p>
      <p class="hint warning">
        <strong>VM IP pool required:</strong> creation reserves the next free address from <strong>Admin → VM IP
        allocations</strong>. Pending VMs without a cluster only use pool rows with <em>no</em> cluster restriction;
        if placement is set, cluster-specific rows (or unrestricted) apply. If the pool is empty, creation returns an
        error.
      </p>
      {#if catalogLoadError}<div class="error">{catalogLoadError}</div>{/if}
      {#if internalVmError}<div class="error">{internalVmError}</div>{/if}
      {#if catalogLoading}<p class="hint">Loading catalog…</p>{/if}
      <div class="form-grid">
        <label>Service name <input bind:value={internalVmForm.name} placeholder="unique name" /></label>
        <label>
          VM product
          <select
            bind:value={internalVmForm.product_code}
            on:change={onInternalVmProductChange}
            disabled={catalogLoading}
          >
            <option value="">— select product —</option>
            {#each vmCatalogProducts as p}
              <option value={p.code}>{p.name} ({p.code})</option>
            {/each}
          </select>
        </label>
        <label>
          VM template
          <select bind:value={internalVmForm.vm_template_id} disabled={catalogLoading || !internalVmForm.product_code}>
            <option value="">— select template —</option>
            {#each vmTemplatesForSelectedProduct as t}
              <option value={String(t.id)}>
                {t.name}{t.code ? ` (${t.code})` : ''} — Proxmox: {t.proxmox_template_name} (id {t.id})
              </option>
            {/each}
          </select>
        </label>
        {#if internalVmForm.product_code && vmTemplatesForSelectedProduct.length === 0}
          <p class="hint warning full-width">
            No templates linked to this product. In <strong>Product catalog</strong>, edit the product and assign VM
            templates.
          </p>
        {/if}
        <label>
          Billing owner (optional)
          <select bind:value={internalVmForm.external_user_id}>
            <option value="">Internal / lab — no external user</option>
            {#each externalUsers as u}
              <option value={String(u.id)}>
                {u.external_username || u.external_user_id} — {u.integration_name} (id {u.id})
              </option>
            {/each}
          </select>
        </label>
        <label>
          External service id (optional)
          <input bind:value={internalVmForm.external_service_id} placeholder="e.g. WHMCS service id" />
        </label>
        <label class="full-width check-row">
          <input type="checkbox" bind:checked={internalVmForm.auto_provision} />
          <span>Auto provision now (place, reserve VMID, clone &amp; power on in background)</span>
        </label>
        {#if internalVmForm.auto_provision && !vmFormSetPlacement}
          <p class="hint full-width">Node auto-selected from Proxmox inventory (by free RAM) for the chosen template.</p>
        {/if}
        <label class="full-width check-row">
          <input type="checkbox" bind:checked={vmFormSetPlacement} />
          <span>Set Proxmox placement now (cluster, node, vmid)</span>
        </label>
        {#if vmFormSetPlacement}
        <label>Cluster
          <select bind:value={internalVmForm.proxmox_cluster_id} on:change={onInternalVmClusterChange}>
            <option value="">— select —</option>
            {#each proxmoxClusters as c}
              {@const cid = c.id ?? c.cluster_id}
              {@const cname = c.name ?? c.cluster_name ?? `Cluster ${cid}`}
              <option value={cid}>{cname} (id {cid})</option>
            {/each}
          </select>
        </label>
        <label class="full-width check-row">
          <input type="checkbox" bind:checked={internalVmCustomNode} />
          <span>Type Proxmox node manually (e.g. after migration or before inventory sync)</span>
        </label>
        {#if proxmoxNodesError}
          <p class="hint warning full-width">Could not load nodes: {proxmoxNodesError}</p>
        {/if}
        {#if internalVmCustomNode}
          <label>
            Proxmox node name
            <input bind:value={internalVmForm.proxmox_node_name} placeholder="exact node name in cluster" />
          </label>
        {:else}
          <label>
            Proxmox node
            <select
              bind:value={internalVmForm.proxmox_node_name}
              disabled={!internalVmForm.proxmox_cluster_id || proxmoxNodesLoading}
            >
              <option value="">{proxmoxNodesLoading ? 'Loading nodes…' : '— select node —'}</option>
              {#each proxmoxClusterNodes as n}
                <option value={n.node_name}>
                  {n.node_name}{n.enabled === false ? ' (disabled in inventory)' : ''}
                </option>
              {/each}
            </select>
          </label>
        {/if}
        {#if !internalVmCustomNode && internalVmForm.proxmox_cluster_id && !proxmoxNodesLoading && proxmoxClusterNodes.length === 0 && !proxmoxNodesError}
          <p class="hint warning full-width">
            No nodes in inventory for this cluster. Sync it from <strong>Admin → Proxmox</strong>, or enable “Type Proxmox
            node manually” above.
          </p>
        {/if}
        <label>VMID <input type="number" bind:value={internalVmForm.proxmox_vmid} /></label>
        {:else}
        <p class="hint full-width">
          {#if internalVmForm.auto_provision}
            Placement omitted — RackFlow will auto-place from inventory and provision in the background.
          {:else}
            Placement omitted — service stays pending until you set cluster/node/vmid (future admin edit or API).
          {/if}
        </p>
        {/if}
        <label class="full-width">Description <input bind:value={internalVmForm.description} /></label>
      </div>
      <button type="button" class="btn-primary" disabled={internalVmBusy} on:click={submitInternalVm}>
        {internalVmBusy ? 'Creating…' : internalVmForm.auto_provision ? 'Create & provision VM' : 'Create pending VM'}
      </button>
    </div>
  {/if}

  {#if loading}
    <div class="loading">Loading services...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if services.length === 0}
    <div class="empty-state">
      <p>No services match these filters.</p>
    </div>
  {:else}
    <div class="admin-data-table">
      <table class="services-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Status</th>
            <th>Owner</th>
            <th>Server</th>
            <th>Assigned VM IP</th>
            <th>Source</th>
            <th>External User ID</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {#each services as service}
            <tr class="clickable-row" on:click={() => openService(service)}>
              <td class="name-cell">{service.name}</td>
              <td>
                <span
                  class="type-badge"
                  class:type-badge-bare_metal={service.service_type === 'bare_metal'}
                  class:type-badge-vm={service.service_type === 'vm'}
                  class:type-badge-http_proxy={service.service_type === 'http_proxy'}
                >{serviceTypeLabel(service.service_type)}</span>
              </td>
              <td><span class={getStatusBadgeClass(service.status)}>{service.status}</span></td>
              <td>{service.owner_username || 'Unassigned'}</td>
              <td>
                {#if service.server_name}
                  {service.server_name}
                {:else if service.proxmox_cluster_id != null}
                  cluster {service.proxmox_cluster_id} / {service.proxmox_node_name || '?'} / vm {service.proxmox_vmid ?? '—'}
                {:else}
                  —
                {/if}
              </td>
              <td>{service.vm_ip_address || '—'}</td>
              <td>{service.provisioning_source || 'billing'}</td>
              <td>{service.external_user_external_id ?? '—'}</td>
              <td>{formatDate(service.created_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .filters {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
  }

  .filters label {
    font-weight: 600;
    color: var(--text-primary);
  }

  .search-input {
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    min-width: 220px;
  }

  .filters select {
    padding: 8px 32px 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) .filters select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }

  .type-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }

  .type-badge-bare_metal {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .type-badge-vm {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .type-badge-http_proxy {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-badge {
    text-transform: capitalize;
  }

  .status-active {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-suspended {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-terminated {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .status-pending {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .services-table {
    min-width: 980px;
  }

  .clickable-row {
    cursor: pointer;
  }

  .name-cell {
    font-weight: 600;
    color: var(--text-primary);
  }

  .internal-vm-panel {
    margin-bottom: 24px;
    padding: 16px;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background: var(--bg-secondary);
  }

  .internal-vm-panel h4 {
    margin: 0 0 8px 0;
  }

  .internal-vm-panel .hint {
    margin: 0 0 12px 0;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .internal-vm-panel .hint.muted {
    font-size: 12px;
    opacity: 0.92;
  }

  .internal-vm-panel .hint.warning {
    color: var(--warning-text);
    background: var(--warning-bg);
    padding: 8px 10px;
    border-radius: 8px;
  }

  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 12px;
  }

  .form-grid label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .form-grid label.full-width {
    grid-column: 1 / -1;
  }

  .form-grid label.check-row {
    flex-direction: row;
    align-items: flex-start;
    gap: 10px;
    font-weight: 500;
    cursor: pointer;
  }

  .form-grid label.check-row input[type='checkbox'] {
    margin-top: 3px;
    flex-shrink: 0;
  }

  .form-grid input,
  .form-grid select {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    font-weight: 500;
    background-color: var(--bg-primary);
    color: var(--text-primary);
  }
  .form-grid select {
    padding-right: 32px;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) .form-grid select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  /* Buttons: shared .btn-* styles in app.css (uses --accent-contrast in dark mode) */
</style>
