<script>
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import { 
    getServices, 
    getVmServices,
    getBareMetalServices,
    getService,
    getExternalUsers,
    getExternalUser,
    listProxmoxClusters,
    listCatalogProducts,
    listVmTemplates,
    getProxmoxClusterInventory,
    createAdminVmService,
    provisionVmService,
    vmPowerAction,
    destroyVmGuest,
    recreateVmGuest,
    deleteServiceCompletely,
    updateAdminServiceStatus,
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let mode = 'unified'; // unified | vm | bare_metal

  let services = [];
  let externalUsers = [];
  let loading = true;
  let error = null;
  let activeTab = 'services'; // 'services' or 'users'
  /** 'all' | 'bare_metal' | 'vm' — split VM vs bare metal in the list */
  let serviceTypeFilter = mode === 'vm' ? 'vm' : mode === 'bare_metal' ? 'bare_metal' : 'all';
  let statusFilter = 'all';
  let provisioningSourceFilter = 'all';
  let selectedService = null;
  let selectedServiceStatusDraft = 'pending';
  let selectedUser = null;
  let proxmoxClusters = [];
  let showInternalVmForm = false;
  let internalVmBusy = false;
  let internalVmError = null;
  let provisionVmBusy = false;
  let provisionVmError = null;
  let vmActionBusy = false;
  let vmActionError = null;
  let catalogProducts = [];
  let catalogVmTemplates = [];
  let catalogLoading = false;
  let catalogLoadError = null;
  /** Proxmox nodes from RackFlow inventory for the selected cluster */
  let proxmoxClusterNodes = [];
  let proxmoxNodesLoading = false;
  let proxmoxNodesError = null;
  /** When true, node name is free-text (migration, new node, stale inventory) */
  let internalVmCustomNode = false;
  /** If false, create pending VM without cluster/node/vmid */
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
  };

  onMount(async () => {
    if (mode !== 'unified') activeTab = 'services';
    serviceTypeFilter = mode === 'vm' ? 'vm' : mode === 'bare_metal' ? 'bare_metal' : 'all';
    await Promise.all([loadServices(), loadExternalUsers(), loadProxmoxClusters()]);
  });

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
      if (statusFilter !== 'all') params.status_filter = statusFilter;
      if (provisioningSourceFilter !== 'all') params.provisioning_source = provisioningSourceFilter;
      if (mode === 'vm') {
        services = await getVmServices(params);
      } else if (mode === 'bare_metal') {
        services = await getBareMetalServices(params);
      } else {
        if (serviceTypeFilter !== 'all') params.service_type = serviceTypeFilter;
        services = await getServices(params);
      }
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

  async function viewServiceDetails(serviceId) {
    try {
      provisionVmError = null;
      selectedService = await getService(serviceId);
      selectedServiceStatusDraft = selectedService?.status || 'pending';
    } catch (err) {
      alert('Failed to load service details: ' + err.message);
    }
  }

  function openService(service) {
    if (!service?.id) return;
    if (mode === 'vm') {
      navigate(`/admin/vm-services/${service.id}`);
      return;
    }
    if (mode === 'bare_metal') {
      navigate(`/admin/bare-metal-services/${service.id}`);
      return;
    }
    viewServiceDetails(service.id);
  }

  function vmProvisionStrategy(s) {
    return s?.vm_strategy_name || s?.config?.vm_plan?.strategy_name || null;
  }

  function canProvisionVm(s) {
    if (!s || s.service_type !== 'vm') return false;
    if (s.status === 'terminated') return false;
    const st = s.config?.vm_provision?.status;
    if (st === 'success' || st === 'running') return false;
    if (s.proxmox_cluster_id == null || !(String(s.proxmox_node_name || '').trim()) || s.proxmox_vmid == null) {
      return false;
    }
    if (s.vm_template_id == null || s.vm_template_id === undefined) return false;
    const strat = vmProvisionStrategy(s);
    if (!strat || strat === 'stub') return false;
    if (strat === 'cloudinit_clone' && (s.vm_ip_allocation_id == null || s.vm_ip_allocation_id === '')) {
      return false;
    }
    return strat === 'cloudinit_clone' || strat === 'guest_agent';
  }

  async function runProvisionVm() {
    if (!selectedService?.id || !canProvisionVm(selectedService)) return;
    provisionVmError = null;
    provisionVmBusy = true;
    try {
      selectedService = await provisionVmService(selectedService.id);
      await loadServices();
    } catch (e) {
      provisionVmError = e.message || String(e);
    } finally {
      provisionVmBusy = false;
    }
  }

  async function runVmPower(action) {
    if (!selectedService?.id) return;
    vmActionError = null;
    vmActionBusy = true;
    try {
      selectedService = await vmPowerAction(selectedService.id, action);
      await loadServices();
    } catch (e) {
      vmActionError = e.message || String(e);
    } finally {
      vmActionBusy = false;
    }
  }

  async function runVmDestroy() {
    if (!selectedService?.id) return;
    if (!confirm('Stop and destroy VM guest? Service and VMID reservation remain attached to this service.')) return;
    vmActionError = null;
    vmActionBusy = true;
    try {
      selectedService = await destroyVmGuest(selectedService.id);
      await loadServices();
    } catch (e) {
      vmActionError = e.message || String(e);
    } finally {
      vmActionBusy = false;
    }
  }

  async function runVmRecreate() {
    if (!selectedService?.id) return;
    vmActionError = null;
    vmActionBusy = true;
    try {
      selectedService = await recreateVmGuest(selectedService.id);
      await loadServices();
    } catch (e) {
      vmActionError = e.message || String(e);
    } finally {
      vmActionBusy = false;
    }
  }

  async function runDeleteService() {
    if (!selectedService?.id) return;
    const name = selectedService.name || `service ${selectedService.id}`;
    if (!confirm(`Delete ${name} completely? This is permanent and removes related records.`)) return;
    vmActionError = null;
    vmActionBusy = true;
    try {
      await deleteServiceCompletely(selectedService.id);
      closeDetails();
      await loadServices();
    } catch (e) {
      vmActionError = e.message || String(e);
    } finally {
      vmActionBusy = false;
    }
  }

  async function viewUserDetails(userId) {
    try {
      selectedUser = await getExternalUser(userId);
    } catch (err) {
      alert('Failed to load user details: ' + err.message);
    }
  }

  function closeDetails() {
    selectedService = null;
    selectedServiceStatusDraft = 'pending';
    selectedUser = null;
  }

  async function runUpdateServiceStatus() {
    if (!selectedService?.id) return;
    vmActionError = null;
    vmActionBusy = true;
    try {
      selectedService = await updateAdminServiceStatus(selectedService.id, selectedServiceStatusDraft);
      selectedServiceStatusDraft = selectedService.status;
      await loadServices();
    } catch (e) {
      vmActionError = e.message || String(e);
    } finally {
      vmActionBusy = false;
    }
  }

  async function runTerminateServiceStatus() {
    if (!selectedService?.id) return;
    if (!confirm(`Set ${selectedService.name || `service ${selectedService.id}`} to terminated?`)) return;
    selectedServiceStatusDraft = 'terminated';
    await runUpdateServiceStatus();
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

<PageHeader title={mode === 'vm' ? 'VM Services' : mode === 'bare_metal' ? 'Bare Metal Services' : 'Services & External Users'} />

<div class="services-container">
  {#if mode === 'unified'}
    <div class="tabs">
      <button 
        class="tab-button" 
        class:active={activeTab === 'services'}
        on:click={() => { activeTab = 'services'; loadServices(); }}
      >
        Services ({services.length})
      </button>
      <button 
        class="tab-button" 
        class:active={activeTab === 'users'}
        on:click={() => { activeTab = 'users'; }}
      >
        External Users ({externalUsers.length})
      </button>
    </div>
  {/if}

  {#if activeTab === 'services'}
    <div class="services-section">
      {#if mode === 'unified'}
        <div class="service-type-subtabs" role="tablist" aria-label="Service kind">
          <button
            type="button"
            role="tab"
            class="subtab-button"
            class:active={serviceTypeFilter === 'bare_metal'}
            on:click={() => { serviceTypeFilter = 'bare_metal'; showInternalVmForm = false; loadServices(); }}
          >
            Bare metal
          </button>
          <button
            type="button"
            role="tab"
            class="subtab-button"
            class:active={serviceTypeFilter === 'vm'}
            on:click={() => { serviceTypeFilter = 'vm'; loadServices(); }}
          >
            Virtual machines
          </button>
          <button
            type="button"
            role="tab"
            class="subtab-button"
            class:active={serviceTypeFilter === 'all'}
            on:click={() => { serviceTypeFilter = 'all'; loadServices(); }}
          >
            All types
          </button>
        </div>
      {/if}

      <div class="section-header">
        <div class="filters">
          <label for="status-filter">Filter by Status:</label>
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
          {#if mode !== 'bare_metal' && (serviceTypeFilter === 'vm' || serviceTypeFilter === 'all' || mode === 'vm')}
            <button type="button" class="btn-primary" on:click={toggleInternalVmForm}>
              {showInternalVmForm ? 'Hide' : '+'} Create pending VM
            </button>
          {/if}
        </div>
      </div>

      {#if showInternalVmForm && (mode !== 'bare_metal') && (serviceTypeFilter === 'vm' || serviceTypeFilter === 'all' || mode === 'vm')}
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
                    {t.name} — Proxmox: {t.proxmox_template_name} (id {t.id})
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
            <p class="hint full-width">Placement omitted — service stays pending until you set cluster/node/vmid (future admin edit or API).</p>
            {/if}
            <label class="full-width">Description <input bind:value={internalVmForm.description} /></label>
          </div>
          <button type="button" class="btn-primary" disabled={internalVmBusy} on:click={submitInternalVm}>
            {internalVmBusy ? 'Creating…' : 'Create pending VM'}
          </button>
        </div>
      {/if}

      {#if loading}
        <div class="loading">Loading services...</div>
      {:else if error}
        <div class="error">Error: {error}</div>
      {:else if services.length === 0}
        <div class="empty-state">
          <p>
            {#if serviceTypeFilter === 'bare_metal'}
              No bare metal services match these filters.
            {:else if serviceTypeFilter === 'vm'}
              No VM services match these filters.
            {:else}
              No services match these filters.
            {/if}
          </p>
        </div>
      {:else}
        <div class="services-table-wrap">
          <table class="services-table">
            <thead>
              <tr>
                <th>Name</th>
                {#if mode === 'unified'}<th>Type</th>{/if}
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
                  {#if mode === 'unified'}
                    <td>
                      <span
                        class="type-badge"
                        class:type-badge-bare_metal={service.service_type === 'bare_metal'}
                        class:type-badge-vm={service.service_type === 'vm'}
                        class:type-badge-http_proxy={service.service_type === 'http_proxy'}
                      >{serviceTypeLabel(service.service_type)}</span>
                    </td>
                  {/if}
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
  {:else}
    <div class="users-section">
      {#if loading}
        <div class="loading">Loading external users...</div>
      {:else if externalUsers.length === 0}
        <div class="empty-state">
          <p>No external users found.</p>
        </div>
      {:else}
        <div class="users-grid">
          {#each externalUsers as user}
            <button
              type="button"
              class="user-card"
              on:click={() => viewUserDetails(user.id)}
            >
              <div class="user-header">
                <h3>{user.external_username || user.external_user_id}</h3>
                <span class="integration-badge">{user.integration_name}</span>
              </div>
              <div class="user-details">
                <div class="detail-item">
                  <span class="detail-label">External User ID:</span>
                  <span>{user.external_user_id}</span>
                </div>
                {#if user.external_email}
                  <div class="detail-item">
                    <span class="detail-label">Email:</span>
                    <span>{user.external_email}</span>
                  </div>
                {/if}
                <div class="detail-item">
                  <span class="detail-label">Services:</span>
                  <span>{user.service_count}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">Created:</span>
                  <span>{formatDate(user.created_at)}</span>
                </div>
              </div>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if selectedService}
  <button type="button" class="modal-overlay" tabindex="-1" on:click={(e) => e.target === e.currentTarget && closeDetails()}>
    <div class="modal-content" role="dialog">
      <div class="modal-header">
        <h3>{selectedService.service_type === 'vm' ? 'VM Service' : 'Bare Metal Service'} Details</h3>
        <button class="btn-icon-only" on:click={closeDetails}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-group">
          <div class="detail-row">
            <span class="detail-label">Name:</span>
            <span>{selectedService.name}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Service type:</span>
            <span>{serviceTypeLabel(selectedService.service_type)}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Status:</span>
            <span class={getStatusBadgeClass(selectedService.status)}>{selectedService.status}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Server:</span>
            <span>
              {#if selectedService.server_id != null}
                {selectedService.server_name} (ID: {selectedService.server_id})
              {:else}
                VM service (no RackFlow server)
              {/if}
            </span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Provisioning source:</span>
            <span>{selectedService.provisioning_source || 'billing'}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">External User ID:</span>
            <span>{selectedService.external_user_external_id ?? '— (internal)'}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Owner user:</span>
            <span>{selectedService.owner_username || 'Unassigned'}</span>
          </div>
          {#if selectedService.product_code}
            <div class="detail-row">
              <span class="detail-label">Product / OS profile:</span>
              <span>{selectedService.product_code} / {selectedService.os_code || '—'}</span>
            </div>
          {/if}
          {#if selectedService.vm_template_id != null && selectedService.vm_template_id !== undefined}
            <div class="detail-row">
              <span class="detail-label">VM template id:</span>
              <span>{selectedService.vm_template_id}</span>
            </div>
          {/if}
          {#if selectedService.service_type === 'vm' && (selectedService.vm_ip_address || selectedService.vm_ip_allocation_id != null)}
            <div class="detail-row">
              <span class="detail-label">VM IP (pool):</span>
              <span>
                {selectedService.vm_ip_address || '—'}
                {#if selectedService.vm_ip_allocation_id != null}
                  <span class="muted"> (allocation id {selectedService.vm_ip_allocation_id})</span>
                {/if}
              </span>
            </div>
          {/if}
          {#if selectedService.service_type === 'vm' && vmProvisionStrategy(selectedService)}
            <div class="detail-row">
              <span class="detail-label">OS strategy:</span>
              <span>{vmProvisionStrategy(selectedService)}</span>
            </div>
          {/if}
          {#if selectedService.service_type === 'vm' && selectedService.config?.vm_provision}
            <div class="detail-row">
              <span class="detail-label">Last provision:</span>
              <span>
                {selectedService.config.vm_provision.status || '—'}
                {#if selectedService.config.vm_provision.error}
                  <span class="hint warning"> — {selectedService.config.vm_provision.error}</span>
                {/if}
              </span>
            </div>
          {/if}
          {#if selectedService.service_type === 'vm' && selectedService.vm_guest_state}
            <div class="detail-row">
              <span class="detail-label">VM guest state:</span>
              <span>{selectedService.vm_guest_state}</span>
            </div>
          {/if}
          {#if selectedService.proxmox_cluster_id != null && selectedService.proxmox_cluster_id !== undefined}
            <div class="detail-row">
              <span class="detail-label">Proxmox placement:</span>
              <span>cluster_id={selectedService.proxmox_cluster_id}, node={selectedService.proxmox_node_name}, vmid={selectedService.proxmox_vmid}</span>
            </div>
          {/if}
          {#if selectedService.external_service_id}
            <div class="detail-row">
              <span class="detail-label">External Service ID:</span>
              <span>{selectedService.external_service_id}</span>
            </div>
          {/if}
          {#if selectedService.description}
            <div class="detail-row">
              <span class="detail-label">Description:</span>
              <span>{selectedService.description}</span>
            </div>
          {/if}
          <div class="detail-row">
            <span class="detail-label">Created:</span>
            <span>{formatDate(selectedService.created_at)}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Updated:</span>
            <span>{formatDate(selectedService.updated_at)}</span>
          </div>
          {#if selectedService.terminated_at}
            <div class="detail-row">
              <span class="detail-label">Terminated:</span>
              <span>{formatDate(selectedService.terminated_at)}</span>
            </div>
          {/if}
          {#if selectedService.service_type === 'vm' && provisionVmError}
            <div class="error full-width" style="margin-top: 0.75rem;">{provisionVmError}</div>
          {/if}
          {#if selectedService.service_type === 'vm' && vmActionError}
            <div class="error full-width" style="margin-top: 0.75rem;">{vmActionError}</div>
          {/if}
        </div>
      </div>
      <div class="modal-footer">
        <select class="status-select" bind:value={selectedServiceStatusDraft} disabled={vmActionBusy || provisionVmBusy}>
          <option value="pending">pending</option>
          <option value="active">active</option>
          <option value="suspended">suspended</option>
          <option value="terminated">terminated</option>
        </select>
        <button
          type="button"
          class="btn-secondary"
          disabled={vmActionBusy || provisionVmBusy || selectedServiceStatusDraft === selectedService.status}
          on:click|stopPropagation={runUpdateServiceStatus}
        >
          Save Status
        </button>
        <button
          type="button"
          class="btn-danger"
          disabled={vmActionBusy || provisionVmBusy}
          on:click|stopPropagation={runTerminateServiceStatus}
        >
          Terminate Service
        </button>
        {#if selectedService.service_type === 'vm'}
          <button type="button" class="btn-secondary" disabled={vmActionBusy} on:click|stopPropagation={() => runVmPower('on')}>Power On</button>
          <button type="button" class="btn-secondary" disabled={vmActionBusy} on:click|stopPropagation={() => runVmPower('off')}>Power Off</button>
          <button type="button" class="btn-secondary" disabled={vmActionBusy} on:click|stopPropagation={() => runVmPower('reboot')}>Reboot</button>
          <button type="button" class="btn-secondary" disabled={vmActionBusy} on:click|stopPropagation={runVmDestroy}>Stop + Destroy VM</button>
          <button type="button" class="btn-secondary" disabled={vmActionBusy} on:click|stopPropagation={runVmRecreate}>Recreate VM</button>
        {/if}
        {#if selectedService.service_type === 'vm' && canProvisionVm(selectedService)}
          <button
            type="button"
            class="btn-primary"
            disabled={provisionVmBusy}
            on:click|stopPropagation={runProvisionVm}
          >
            {provisionVmBusy
              ? 'Provisioning…'
              : vmProvisionStrategy(selectedService) === 'guest_agent'
                ? 'Create VM on Proxmox (clone + start)'
                : 'Create VM on Proxmox (clone + cloud-init IP)'}
          </button>
        {/if}
        <button
          type="button"
          class="btn-danger"
          disabled={vmActionBusy || provisionVmBusy}
          on:click|stopPropagation={runDeleteService}
        >
          Delete Service Completely
        </button>
        <button class="btn-secondary" on:click={closeDetails}>Close</button>
      </div>
    </div>
  </button>
{/if}

{#if selectedUser}
  <button type="button" class="modal-overlay" tabindex="-1" on:click={(e) => e.target === e.currentTarget && closeDetails()}>
    <div class="modal-content" role="dialog">
      <div class="modal-header">
        <h3>External User Details</h3>
        <button class="btn-icon-only" on:click={closeDetails}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-group">
          <div class="detail-row">
            <span class="detail-label">External User ID:</span>
            <span>{selectedUser.external_user_id}</span>
          </div>
          {#if selectedUser.external_username}
            <div class="detail-row">
              <span class="detail-label">Username:</span>
              <span>{selectedUser.external_username}</span>
            </div>
          {/if}
          {#if selectedUser.external_email}
            <div class="detail-row">
              <span class="detail-label">Email:</span>
              <span>{selectedUser.external_email}</span>
            </div>
          {/if}
          <div class="detail-row">
            <span class="detail-label">Integration:</span>
            <span>{selectedUser.integration_name} (ID: {selectedUser.integration_id})</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Services:</span>
            <span>{selectedUser.service_count}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Created:</span>
            <span>{formatDate(selectedUser.created_at)}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Updated:</span>
            <span>{formatDate(selectedUser.updated_at)}</span>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeDetails}>Close</button>
      </div>
    </div>
  </button>
{/if}

<style>
  .services-container {
    padding: 32px;
  }

  .tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 24px;
    border-color: var(--border-color);
  }

  .tab-button {
    padding: 12px 24px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
    color: var(--text-secondary);
    cursor: pointer;
    transition: color 0.2s ease, border-color 0.2s ease;
    margin-bottom: -2px;
  }

  .tab-button:hover {
    color: var(--text-primary);
  }

  .tab-button.active {
    color: var(--accent-color);
    border-bottom-color: var(--accent-color);
  }

  .service-type-subtabs {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border-color);
  }

  .subtab-button {
    padding: 8px 16px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
  }

  .subtab-button:hover {
    color: var(--text-primary);
    border-color: var(--text-tertiary);
  }

  .subtab-button.active {
    color: var(--accent-color);
    border-color: var(--accent-color);
    background: var(--bg-primary);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .filters {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .filters label {
    font-weight: 600;
    color: var(--text-primary);
  }

  .filters select {
    padding: 8px 12px;
    border-color: var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }

  .users-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
    gap: 20px;
  }

  .user-card {
    display: block;
    width: 100%;
    margin: 0;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 20px;
    box-shadow: var(--shadow-sm);
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    cursor: pointer;
    text-align: left;
    font: inherit;
    color: inherit;
  }

  .user-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-md);
    border-color: var(--text-tertiary);
  }

  .user-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .type-badge {
    padding: 3px 10px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.02em;
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

  .user-header h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
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

  .integration-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    background: var(--info-bg);
    color: var(--info-text);
  }

  .user-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .services-table-wrap {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    overflow: auto;
    background: var(--bg-secondary);
  }

  .services-table {
    width: 100%;
    min-width: 980px;
    border-collapse: collapse;
    font-size: 13px;
  }

  .services-table th,
  .services-table td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    vertical-align: middle;
  }

  .services-table thead th {
    color: var(--text-secondary);
    font-weight: 700;
    background: var(--bg-tertiary);
    position: sticky;
    top: 0;
    z-index: 1;
  }

  .services-table tbody tr:last-child td {
    border-bottom: none;
  }

  .clickable-row {
    cursor: pointer;
  }

  .clickable-row:hover {
    background: var(--bg-tertiary);
  }

  .name-cell {
    font-weight: 700;
    color: var(--text-primary);
  }

  .detail-item {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 14px;
    gap: 12px;
  }

  .detail-item span:last-child {
    color: var(--text-primary);
    font-weight: 500;
    word-break: break-word;
    text-align: right;
  }

  .detail-label {
    font-weight: 600;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

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

  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-content {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 90%;
    max-width: 600px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-xl);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .modal-body {
    padding: 24px;
  }

  .detail-group {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border-color);
    gap: 12px;
  }

  .detail-row:last-child {
    border-bottom: none;
  }

  .detail-row .detail-label {
    flex-shrink: 0;
  }

  .detail-row span:last-child {
    color: var(--text-primary);
    font-weight: 500;
    text-align: right;
    word-break: break-word;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 12px;
    padding: 20px 24px;
    border-color: var(--border-color);
  }

  .status-select {
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    min-width: 150px;
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover {
    background: var(--accent-color);
  }

  .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    color: var(--text-primary);
    border-radius: 6px;
    transition: background 0.2s ease, color 0.2s ease;
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
    transform: translateY(-1px);
    color: var(--text-primary);
  }

  .btn-icon-only svg {
    width: 18px;
    height: 18px;
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
  }

  .btn-primary {
    padding: 10px 20px;
    background: var(--accent-color);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-danger {
    padding: 10px 20px;
    background: var(--danger-color);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
