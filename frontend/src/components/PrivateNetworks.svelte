<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import { navigate } from '../lib/router.js';
  import {
    getLocations,
    getServices,
    listClients,
    listPrivateNetworkPools,
    createPrivateNetworkPool,
    updatePrivateNetworkPool,
    deletePrivateNetworkPool,
    listPrivateNetworks,
    createPrivateNetwork,
    deletePrivateNetwork,
    syncPrivateNetwork,
    attachPrivateNetwork,
    detachPrivateNetwork,
  } from '../lib/api.js';

  const ATTACHABLE_TYPES = ['vm', 'bare_metal'];
  const emptyPoolForm = () => ({
    location_id: '',
    vlan_start: 2000,
    vlan_end: 3899,
    bridge: 'vmbr0',
    max_networks_per_client: 5,
    max_networks_per_service: 4,
    reserved_cidrs: '',
  });
  const parseCidrList = (text) => text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const emptyNetworkForm = () => ({ owner_user_id: '', location_id: '', name: '', cidr: '' });

  let loading = false;
  let working = false;
  let error = '';
  let success = '';
  let pools = [];
  let networks = [];
  let locations = [];
  let clients = [];

  let showCreatePool = false;
  let poolForm = emptyPoolForm();
  let showCreateNetwork = false;
  let networkForm = emptyNetworkForm();

  let expandedId = null;
  let ownerServices = [];
  let attachForm = { service_id: '', ip_address: '' };

  $: locationsWithoutPool = locations.filter((l) => !pools.some((p) => p.location_id === l.id));
  $: poolLocations = pools.filter((p) => p.enabled);
  $: problemCount = networks.filter(
    (n) => !n.switch_synced || n.status !== 'active' || n.attachments.some((a) => a.state !== 'active'),
  ).length;
  $: expanded = networks.find((n) => n.id === expandedId) || null;
  $: attachableServices = expanded
    ? ownerServices.filter(
        (s) => ATTACHABLE_TYPES.includes(s.service_type) && !expanded.attachments.some((a) => a.service_id === s.id),
      )
    : [];

  async function loadAll() {
    loading = true;
    error = '';
    try {
      const [poolRows, networkRows, locationRows, clientRows] = await Promise.all([
        listPrivateNetworkPools(),
        listPrivateNetworks(),
        getLocations(),
        listClients(),
      ]);
      pools = poolRows || [];
      networks = networkRows || [];
      locations = locationRows || [];
      clients = clientRows || [];
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function run(action, message) {
    working = true;
    error = '';
    success = '';
    try {
      await action();
      success = message;
      await loadAll();
      return true;
    } catch (err) {
      error = err.message;
      return false;
    } finally {
      working = false;
    }
  }

  async function submitPool() {
    const created = await run(
      () =>
        createPrivateNetworkPool({
          location_id: Number(poolForm.location_id),
          vlan_start: Number(poolForm.vlan_start),
          vlan_end: Number(poolForm.vlan_end),
          bridge: poolForm.bridge.trim(),
          max_networks_per_client: Number(poolForm.max_networks_per_client),
          max_networks_per_service: Number(poolForm.max_networks_per_service),
          reserved_cidrs: parseCidrList(poolForm.reserved_cidrs),
        }),
      'Pool created',
    );
    if (created) {
      showCreatePool = false;
      poolForm = emptyPoolForm();
    }
  }

  function togglePool(pool) {
    return run(
      () => updatePrivateNetworkPool(pool.id, { enabled: !pool.enabled }),
      pool.enabled ? 'Pool disabled: no new networks in this location' : 'Pool enabled',
    );
  }

  function toggleProxyAccess(pool, field) {
    const labels = {
      proxy_access_enabled: ['Proxy fast path offered', 'Proxy fast path no longer offered'],
      proxy_access_requires_product: ['Fast path now needs a product flagged proxy_fast_path', 'Fast path is free'],
    };
    const next = !pool[field];
    return run(
      () => updatePrivateNetworkPool(pool.id, { [field]: next }),
      `${labels[field][next ? 0 : 1]} in ${pool.location_name}`,
    );
  }

  function editReservedRanges(pool) {
    const current = (pool.reserved_cidrs || []).join(', ');
    const value = prompt(
      `Reserved ranges for ${pool.location_name} (comma separated CIDRs). Clients cannot create networks overlapping them.`,
      current,
    );
    if (value === null || value.trim() === current) return null;
    return run(
      () => updatePrivateNetworkPool(pool.id, { reserved_cidrs: parseCidrList(value) }),
      `Reserved ranges for ${pool.location_name} saved`,
    );
  }

  function updateClientLimit(pool, value) {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0 || parsed === pool.max_networks_per_client) return null;
    return run(
      () => updatePrivateNetworkPool(pool.id, { max_networks_per_client: parsed }),
      `Limit for ${pool.location_name} set to ${parsed} networks per client`,
    );
  }

  function removePool(pool) {
    if (!confirm(`Delete the private network pool for ${pool.location_name}?`)) return null;
    return run(() => deletePrivateNetworkPool(pool.id), 'Pool deleted');
  }

  async function submitNetwork() {
    const created = await run(
      () =>
        createPrivateNetwork({
          owner_user_id: Number(networkForm.owner_user_id),
          location_id: Number(networkForm.location_id),
          name: networkForm.name.trim(),
          cidr: networkForm.cidr.trim() || null,
        }),
      'Private network created',
    );
    if (created) {
      showCreateNetwork = false;
      networkForm = emptyNetworkForm();
    }
  }

  async function toggleExpanded(network) {
    if (expandedId === network.id) {
      expandedId = null;
      return;
    }
    expandedId = network.id;
    attachForm = { service_id: '', ip_address: '' };
    ownerServices = [];
    try {
      const rows = await getServices({ owner_user_id: network.owner_user_id, status_filter: 'active' });
      ownerServices = Array.isArray(rows) ? rows : rows?.items || [];
    } catch (err) {
      error = err.message;
    }
  }

  async function submitAttach(network) {
    const attached = await run(
      () =>
        attachPrivateNetwork(network.id, {
          service_id: Number(attachForm.service_id),
          ip_address: attachForm.ip_address.trim() || null,
        }),
      'Service attached',
    );
    if (attached) attachForm = { service_id: '', ip_address: '' };
  }

  function detach(network, attachment) {
    if (!confirm(`Detach ${attachment.service_name || 'this service'} from ${network.name}?`)) return null;
    return run(() => detachPrivateNetwork(network.id, attachment.id), 'Service detached');
  }

  function removeNetwork(network) {
    if (!confirm(`Delete private network ${network.name} (VLAN ${network.vlan_id})?`)) return null;
    return run(() => deletePrivateNetwork(network.id), 'Private network deleted');
  }

  function forceRemoveNetwork(network) {
    const warning =
      `Drop ${network.name} without removing VLAN ${network.vlan_id} from the switches?\n\n` +
      'Only do this when a switch is gone for good. The VLAN stays configured wherever it still exists.';
    if (!confirm(warning)) return null;
    return run(() => deletePrivateNetwork(network.id, { skipSwitches: true }), 'Private network record dropped');
  }

  function resync(network) {
    return run(() => syncPrivateNetwork(network.id), `VLAN ${network.vlan_id} pushed to the switches`);
  }

  onMount(loadAll);
</script>

<PageHeader title="Private Networks" />
<div class="page">
  {#if error}<div class="error">{error}</div>{/if}
  {#if success}<div class="success">{success}</div>{/if}

  <div class="summary">
    <div class="stat"><span class="label">Locations with a pool</span><span class="value">{pools.length}</span></div>
    <div class="stat"><span class="label">Networks</span><span class="value">{networks.length}</span></div>
    <div class="stat">
      <span class="label">Attached services</span>
      <span class="value">{networks.reduce((n, row) => n + row.attachments.length, 0)}</span>
    </div>
    <div class="stat" class:stat-warn={problemCount > 0}>
      <span class="label">Needing attention</span><span class="value">{problemCount}</span>
    </div>
  </div>

  <div class="actions">
    <Button on:click={() => (showCreateNetwork = true)} disabled={!poolLocations.length}>Create Network</Button>
    <Button variant="secondary" on:click={() => (showCreatePool = true)} disabled={!locationsWithoutPool.length}>
      Add Location Pool
    </Button>
    <Button variant="secondary" on:click={loadAll} disabled={loading}>Refresh</Button>
  </div>

  <section>
    <h3>Location pools</h3>
    <p class="hint">
      Each location hands out one VLAN per network from its range. The range must be allowed on the trunks between the
      switches and on the hypervisor uplinks, and the bridge must be VLAN-aware on every Proxmox node.
    </p>
    <div class="table">
      <div class="row head pool-row">
        <div>Location</div><div>VLAN range</div><div>Bridge</div><div>In use</div><div>Per client</div>
        <div>Per service</div><div>Enabled</div><div>Actions</div>
      </div>
      {#each pools as pool (pool.id)}
        <div class="row pool-row">
          <div>{pool.location_name}</div>
          <div class="mono">{pool.vlan_start} to {pool.vlan_end}</div>
          <div class="mono">{pool.bridge}</div>
          <div>{pool.networks_in_use}/{pool.capacity}</div>
          <div>
            <input
              type="number"
              min="0"
              class="limit-input"
              value={pool.max_networks_per_client}
              title="How many networks one client may create in this location"
              on:change={(e) => updateClientLimit(pool, e.target.value)}
            />
          </div>
          <div>
            {pool.max_networks_per_service}
            {#if pool.reserved_cidrs?.length}
              <small class="reserved" title={pool.reserved_cidrs.join(', ')}>{pool.reserved_cidrs.length} reserved range{pool.reserved_cidrs.length === 1 ? '' : 's'}</small>
            {/if}
            {#if pool.proxy_access_enabled}
              <small class="reserved">proxy fast path{pool.proxy_access_requires_product ? ' (product)' : ''}</small>
            {/if}
          </div>
          <div>{pool.enabled ? 'yes' : 'no'}</div>
          <div class="row-actions">
            <button class="tiny" disabled={working} on:click={() => togglePool(pool)}>
              {pool.enabled ? 'Disable' : 'Enable'}
            </button>
            <button class="tiny" disabled={working} on:click={() => editReservedRanges(pool)}>Reserved ranges</button>
            <button
              class="tiny"
              disabled={working}
              title="Let clients turn on a private network to the proxy runners in this location"
              on:click={() => toggleProxyAccess(pool, 'proxy_access_enabled')}
            >{pool.proxy_access_enabled ? 'Stop fast path' : 'Offer fast path'}</button>
            {#if pool.proxy_access_enabled}
              <button
                class="tiny"
                disabled={working}
                title="Require an active product whose catalogue specs set proxy_fast_path: true"
                on:click={() => toggleProxyAccess(pool, 'proxy_access_requires_product')}
              >{pool.proxy_access_requires_product ? 'Make fast path free' : 'Sell fast path'}</button>
            {/if}
            <button
              class="tiny danger"
              disabled={working || pool.networks_in_use > 0}
              title={pool.networks_in_use > 0 ? 'Delete the networks in this location first' : 'Delete pool'}
              on:click={() => removePool(pool)}
            >Delete</button>
          </div>
        </div>
      {:else}
        <div class="empty-row">
          {loading ? 'Loading...' : 'No pools yet. Add one to offer private networks in a location.'}
        </div>
      {/each}
    </div>
  </section>

  <section>
    <h3>Networks</h3>
    <div class="table">
      <div class="row head network-row">
        <div>Name</div><div>Client</div><div>Location</div><div>VLAN</div><div>Range</div><div>Services</div>
        <div>State</div><div>Actions</div>
      </div>
      {#each networks as network (network.id)}
        <div class="row network-row">
          <div>
            <button class="link-btn" on:click={() => toggleExpanded(network)}>
              {expandedId === network.id ? '▾' : '▸'} {network.name}
            </button>
            {#if network.kind === 'proxy_access'}<span class="badge ok" title="Proxy access network: fixed range, joined by every proxy runner in the location">fast path</span>{/if}
          </div>
          <div>
            <button class="link-btn" on:click={() => navigate(`/admin/users/${network.owner_user_id}`)}>
              {network.owner_username || `#${network.owner_user_id}`}
            </button>
          </div>
          <div>{network.location_name}</div>
          <div class="mono">{network.vlan_id}</div>
          <div class="mono" title={network.storage_endpoint_ip ? `${network.storage_endpoint_ip} reserved for storage` : ''}>{network.cidr}</div>
          <div>{network.attachments.length}</div>
          <div>
            {#if network.status !== 'active'}
              <span class="badge warn">{network.status}</span>
            {:else if !network.switch_synced}
              <span class="badge warn" title={network.last_error || ''}>switches pending</span>
            {:else}
              <span class="badge ok">active</span>
            {/if}
          </div>
          <div class="row-actions">
            <button class="tiny" disabled={working} on:click={() => resync(network)}>Sync</button>
            <button
              class="tiny danger"
              disabled={working || network.attachments.some((a) => !a.proxy_runner_id)}
              title={network.attachments.some((a) => !a.proxy_runner_id) ? 'Detach every service first' : 'Delete network'}
              on:click={() => removeNetwork(network)}
            >Delete</button>
          </div>
        </div>

        {#if expandedId === network.id}
          <div class="detail">
            {#if network.last_error}
              <p class="error">Last switch error: {network.last_error}</p>
              {#if network.status === 'deleting'}
                <button class="tiny danger" disabled={working} on:click={() => forceRemoveNetwork(network)}>
                  Drop record without touching the switches
                </button>
              {/if}
            {/if}

            <div class="table">
              <div class="row head attachment-row">
                <div>Service</div><div>Type</div><div>Address</div><div>MAC</div><div>State</div><div>Actions</div>
              </div>
              {#each network.attachments as attachment (attachment.id)}
                <div class="row attachment-row">
                  <div>
                    {#if attachment.proxy_runner_id}
                      <span>Proxy runner {attachment.proxy_runner_name || `#${attachment.proxy_runner_id}`}</span>
                    {:else if attachment.service_id}
                      <button class="link-btn" on:click={() => navigate(`/admin/services/${attachment.service_id}`)}>
                        #{attachment.service_id} {attachment.service_name || ''}
                      </button>
                    {:else}
                      <span class="hint">service deleted</span>
                    {/if}
                  </div>
                  <div>{attachment.proxy_runner_id ? 'Runner' : attachment.service_type === 'vm' ? 'VM' : attachment.service_type === 'bare_metal' ? 'Dedicated' : '—'}</div>
                  <div class="mono">{attachment.ip_address ? `${attachment.ip_address}/${attachment.prefix_length}` : 'subnet IPs'}</div>
                  <div class="mono">{attachment.mac_address || '—'}</div>
                  <div>
                    <span class="badge" class:ok={attachment.state === 'active'} class:warn={attachment.state !== 'active'}>
                      {attachment.state}
                    </span>
                    {#if attachment.last_error}<div class="error small">{attachment.last_error}</div>{/if}
                  </div>
                  <div>
                    <button class="tiny danger" disabled={working} on:click={() => detach(network, attachment)}>
                      Detach
                    </button>
                  </div>
                </div>
              {:else}
                <div class="empty-row">No services attached.</div>
              {/each}
            </div>

            {#if network.status === 'active'}
              <div class="attach-form">
                <select bind:value={attachForm.service_id}>
                  <option value="">Attach a service of {network.owner_username || 'this client'}</option>
                  {#each attachableServices as service (service.id)}
                    <option value={service.id}>
                      #{service.id} {service.name} ({service.service_type === 'vm' ? 'VM' : 'Dedicated'})
                    </option>
                  {/each}
                </select>
                <input bind:value={attachForm.ip_address} placeholder="Address (optional, next free if empty)" />
                <Button size="small" disabled={working || !attachForm.service_id} on:click={() => submitAttach(network)}>
                  Attach
                </Button>
              </div>
              <p class="hint">
                Only services in {network.location_name} can join. A VM gets an extra network card tagged with VLAN
                {network.vlan_id}; a dedicated server gets the VLAN tagged on its uplink port.
              </p>
            {/if}
          </div>
        {/if}
      {:else}
        <div class="empty-row">{loading ? 'Loading...' : 'No private networks yet.'}</div>
      {/each}
    </div>
  </section>
</div>

{#if showCreatePool}
  <div class="overlay" role="presentation" on:click|self={() => (showCreatePool = false)}>
    <div class="modal">
      <h3>Add Location Pool</h3>
      <select bind:value={poolForm.location_id}>
        <option value="">Select location</option>
        {#each locationsWithoutPool as loc (loc.id)}
          <option value={loc.id}>{loc.name}</option>
        {/each}
      </select>
      <div class="pair">
        <label class="field-label">First VLAN<input type="number" min="2" max="3967" bind:value={poolForm.vlan_start} /></label>
        <label class="field-label">Last VLAN<input type="number" min="2" max="3967" bind:value={poolForm.vlan_end} /></label>
      </div>
      <p class="hint">VLANs 3968 and above are reserved by NX-OS. Keep the range clear of the public, PXE and management VLANs.</p>
      <label class="field-label">Proxmox bridge<input bind:value={poolForm.bridge} placeholder="vmbr0" /></label>
      <div class="pair">
        <label class="field-label">Networks per client
          <input type="number" min="0" bind:value={poolForm.max_networks_per_client} />
        </label>
        <label class="field-label">Networks per service
          <input type="number" min="1" max="16" bind:value={poolForm.max_networks_per_service} />
        </label>
      </div>
      <label class="field-label">Reserved ranges
        <textarea rows="3" bind:value={poolForm.reserved_cidrs} placeholder="10.16.0.0/12"></textarea>
      </label>
      <p class="hint">
        One CIDR per line. Clients cannot create networks overlapping these (for example your own management ranges);
        they are only told the range is not available, never which ranges are reserved.
      </p>
      <div class="actions">
        <Button variant="secondary" on:click={() => (showCreatePool = false)}>Cancel</Button>
        <Button on:click={submitPool} disabled={working || !poolForm.location_id || !poolForm.bridge.trim()}>Create</Button>
      </div>
    </div>
  </div>
{/if}

{#if showCreateNetwork}
  <div class="overlay" role="presentation" on:click|self={() => (showCreateNetwork = false)}>
    <div class="modal">
      <h3>Create Private Network</h3>
      <select bind:value={networkForm.owner_user_id}>
        <option value="">Select client</option>
        {#each clients as c (c.user_id)}
          <option value={c.user_id}>{c.username}{c.email ? ` (${c.email})` : ''}</option>
        {/each}
      </select>
      <select bind:value={networkForm.location_id}>
        <option value="">Select location</option>
        {#each poolLocations as pool (pool.id)}
          <option value={pool.location_id}>{pool.location_name}</option>
        {/each}
      </select>
      <input bind:value={networkForm.name} maxlength="64" placeholder="Network name" />
      <input bind:value={networkForm.cidr} placeholder="Private range (default 10.0.0.0/24)" />
      <p class="hint">Networks created here do not count against the client's limit.</p>
      <div class="actions">
        <Button variant="secondary" on:click={() => (showCreateNetwork = false)}>Cancel</Button>
        <Button
          on:click={submitNetwork}
          disabled={working || !networkForm.owner_user_id || !networkForm.location_id || !networkForm.name.trim()}
        >Create</Button>
      </div>
    </div>
  </div>
{/if}

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
  .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  @media (max-width: 900px) { .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .stat {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stat .label { color: var(--text-secondary); font-size: 0.85rem; }
  .stat .value { font-size: 1.4rem; font-weight: 600; color: var(--text-primary); }
  .stat-warn { border-color: var(--warning-color, #b7791f); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  section {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h3 { margin: 0; font-size: 1.05rem; }
  input, select {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 8px;
    width: 100%;
  }
  .table { display: flex; flex-direction: column; overflow-x: auto; }
  .row {
    display: grid;
    gap: 8px;
    padding: 10px 8px;
    border-bottom: 1px solid var(--border-color);
    align-items: center;
    font-size: 0.9rem;
  }
  .row.head { font-weight: 600; color: var(--text-secondary); }
  .pool-row { grid-template-columns: 1.2fr 1.1fr 0.8fr 0.8fr 0.8fr 0.8fr 0.6fr 1.2fr; min-width: 820px; }
  .network-row { grid-template-columns: 1.4fr 1.1fr 1fr 0.6fr 1.1fr 0.6fr 1fr 1.1fr; min-width: 880px; }
  .attachment-row { grid-template-columns: 1.5fr 0.8fr 1.1fr 1.2fr 1.3fr 0.7fr; min-width: 760px; }
  .detail {
    padding: 12px 12px 16px 24px;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-secondary);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .attach-form { display: grid; grid-template-columns: 1.4fr 1fr auto; gap: 8px; align-items: center; }
  @media (max-width: 768px) { .attach-form { grid-template-columns: 1fr; } }
  .limit-input { width: 70px; padding: 4px 6px; }
  .empty-row { padding: 16px 8px; color: var(--text-secondary); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .row-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .tiny {
    width: fit-content;
    padding: 4px 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.8rem;
  }
  .tiny.danger { color: var(--danger-color); border-color: var(--danger-color); }
  .tiny:disabled { opacity: 0.5; cursor: not-allowed; }
  .link-btn { background: none; border: none; color: var(--accent-color); cursor: pointer; padding: 0; text-align: left; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    font-size: 0.8rem;
  }
  .badge.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .badge.warn { color: var(--warning-color, #b7791f); border-color: var(--warning-color, #b7791f); }
  .hint { margin: 0; color: var(--text-secondary); font-size: 0.85rem; }
  .reserved { display: block; font-size: 0.75rem; color: var(--text-secondary); }
  textarea { padding: 7px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font-family: inherit; }
  .field-label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; color: var(--text-secondary); }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .error { color: var(--danger-color); margin: 0; }
  .error.small { font-size: 0.8rem; margin-top: 4px; }
  .success { color: var(--success-color, #2e7d32); }
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
    padding: 16px;
  }
  .modal {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 20px;
    width: min(480px, 100%);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
</style>
