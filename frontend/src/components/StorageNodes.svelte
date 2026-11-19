<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';
  import StorageSharesPanel from './storage/StorageSharesPanel.svelte';
  import StorageNodeVlanPorts from './storage/StorageNodeVlanPorts.svelte';
  import StorageNodeSftp from './storage/StorageNodeSftp.svelte';
  import StorageSftpPanel from './storage/StorageSftpPanel.svelte';
  import StorageLocationPanel from './storage/StorageLocationPanel.svelte';
  import {
    listStorageNodes,
    getStorageNode,
    createStorageNode,
    updateStorageNode,
    rotateStorageNodeKey,
    deleteStorageNode,
    getServerVlanPorts,
    listStorageServices,
    getStorageService,
    linkStorageService,
    resizeStorageService,
    getServers,
    drainStorageNode,
    listStorageLocations,
    getStorageLocation,
    updateStorageLocation,
    setStorageLocationHostKey,
    rebalanceStorageLocation,
    moveStorageService,
  } from '../lib/api.js';
  import { STORAGE_STATUS_LABELS, formatBytes } from '../lib/storage.js';

  let tab = 'nodes';
  let nodes = [];
  let storages = [];
  let loading = true;
  let error = '';
  let success = '';
  let selectedNode = null;
  let selectedStorage = null;
  let revealedKey = null;
  let busy = false;

  let showCreate = false;
  let servers = [];
  let createForm = { server_id: '', name: '', vlan_mode: 'switch', vlan_port_ids: [] };
  let createPreview = null;

  let linkForm = { network_id: '', node_id: '' };
  let resizeGb = '';
  let locations = [];
  let selectedLocation = null;
  let moveForm = { node_id: '', set_preferred: true };

  function initialService() {
    const params = new URLSearchParams(window.location.search);
    const id = Number(params.get('service'));
    return Number.isFinite(id) && id > 0 ? id : null;
  }

  async function load() {
    loading = true;
    error = '';
    try {
      [nodes, storages, locations] = await Promise.all([listStorageNodes(), listStorageServices(), listStorageLocations()]);
    } catch (err) {
      error = err.message || 'Failed to load storage';
    } finally {
      loading = false;
    }
  }

  async function act(action, message) {
    busy = true;
    error = '';
    success = '';
    try {
      const result = await action();
      if (message) success = message;
      return result ?? true;
    } catch (err) {
      error = err.message || 'Request failed';
      return null;
    } finally {
      busy = false;
    }
  }

  async function openNode(node) {
    const detail = await act(() => getStorageNode(node.id));
    if (detail) selectedNode = detail;
  }

  async function openStorage(serviceId) {
    const detail = await act(() => getStorageService(serviceId));
    if (detail) {
      selectedStorage = detail;
      resizeGb = detail.size_gb;
      linkForm = { network_id: '', node_id: '' };
      moveForm = { node_id: '', set_preferred: true };
      tab = 'storage';
    }
  }

  async function saveNode(changes) {
    const updated = await act(() => updateStorageNode(selectedNode.id, changes), 'Storage node saved.');
    if (updated) {
      selectedNode = updated;
      await load();
    }
  }

  function togglePort(list, portId) {
    return list.includes(portId) ? list.filter((p) => p !== portId) : [...list, portId];
  }

  async function rotate() {
    if (!confirm(`Issue a new key for ${selectedNode.name}? The old key stops working at once; update the storage runner.`)) return;
    const out = await act(() => rotateStorageNodeKey(selectedNode.id));
    if (out) revealedKey = { title: `New key for ${selectedNode.name}`, api_key: out.api_key };
  }

  async function removeNode() {
    if (!confirm(`Delete storage node ${selectedNode.name} and its key?`)) return;
    if (await act(() => deleteStorageNode(selectedNode.id), 'Storage node deleted.')) {
      selectedNode = null;
      await load();
    }
  }

  async function openCreate() {
    showCreate = true;
    createForm = { server_id: '', name: '', vlan_mode: 'switch', vlan_port_ids: [] };
    createPreview = null;
    if (servers.length === 0) {
      const all = await act(() => getServers());
      const taken = new Set(nodes.map((n) => n.server_id));
      servers = (Array.isArray(all) ? all : all?.items || []).filter((s) => !taken.has(s.id));
    }
  }

  async function pickServer() {
    createForm.vlan_port_ids = [];
    createPreview = createForm.server_id ? await act(() => getServerVlanPorts(Number(createForm.server_id))) : null;
  }

  async function submitCreate() {
    const out = await act(() =>
      createStorageNode({
        server_id: Number(createForm.server_id),
        name: createForm.name.trim() || null,
        vlan_mode: createForm.vlan_mode,
        vlan_port_ids: createForm.vlan_port_ids,
      }),
    );
    if (out) {
      showCreate = false;
      servers = [];
      revealedKey = { title: `Key for ${out.name}`, api_key: out.api_key };
      selectedNode = out;
      await load();
    }
  }

  async function link() {
    const out = await act(
      () =>
        linkStorageService(selectedStorage.service_id, {
          network_id: Number(linkForm.network_id),
          node_id: linkForm.node_id ? Number(linkForm.node_id) : null,
        }),
      'Storage linked.',
    );
    if (out) {
      selectedStorage = out;
      await load();
    }
  }

  async function toggleDrain() {
    const draining = !selectedNode.draining;
    if (draining && !confirm(`Drain ${selectedNode.name}? Its storage moves to the other nodes one at a time.`)) return;
    const out = await act(
      () => drainStorageNode(selectedNode.id, draining),
      draining ? 'Draining started.' : 'Node can take storage again.',
    );
    if (out) {
      selectedNode = out;
      await load();
    }
  }

  async function openLocation(location) {
    const detail = await act(() => getStorageLocation(location.location_id));
    if (detail) selectedLocation = detail;
  }

  async function saveLocation(changes) {
    const out = await act(() => updateStorageLocation(selectedLocation.location_id, changes), 'Location saved.');
    if (out) {
      selectedLocation = out;
      await load();
    }
  }

  async function locationHostKey(body) {
    const out = await act(() => setStorageLocationHostKey(selectedLocation.location_id, body), 'Host key saved.');
    if (out) {
      selectedLocation = out;
      await load();
    }
  }

  async function rebalance() {
    if (!confirm('Rebalance preferred nodes in this location? Storage then moves one at a time.')) return;
    const out = await act(() => rebalanceStorageLocation(selectedLocation.location_id));
    if (out) {
      success = `Rebalance started: ${out.changed.length} storage service(s) get another preferred node.`;
      const refreshed = await act(() => getStorageLocation(selectedLocation.location_id));
      if (refreshed) selectedLocation = refreshed;
      await load();
    }
  }

  async function moveStorage() {
    const out = await act(
      () =>
        moveStorageService(selectedStorage.service_id, {
          node_id: Number(moveForm.node_id),
          set_preferred: moveForm.set_preferred,
        }),
      'Move requested.',
    );
    if (out) {
      selectedStorage = out;
      await load();
    }
  }

  const nodeLocation = (nodeId) => nodes.find((n) => n.id === nodeId)?.location_id;

  async function resize() {
    const out = await act(() => resizeStorageService(selectedStorage.service_id, Number(resizeGb)), 'Storage resized.');
    if (out) {
      selectedStorage = out;
      await load();
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(revealedKey.api_key);
      success = 'API key copied';
    } catch {
      error = 'Clipboard unavailable';
    }
  }

  const seen = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');

  onMount(async () => {
    await load();
    const serviceId = initialService();
    if (serviceId) await openStorage(serviceId);
  });
</script>

<PageHeader title="Storage" />

<div class="page">
  <p class="intro">
    NFS storage on clients' private networks. Each storage node runs the storage runner, which pulls its desired state from
    <code>GET /api/runner/storage/config</code> with its own key and reports back. A storage is ready only when the node's
    network attachment is applied and the runner reports it ready.
  </p>

  {#if error}<div class="banner error">{error}</div>{/if}
  {#if success}<div class="banner ok">{success}</div>{/if}

  <div class="tabs">
    <button class:active={tab === 'nodes'} on:click={() => (tab = 'nodes')}>Storage nodes</button>
    <button class:active={tab === 'storage'} on:click={() => (tab = 'storage')}>Storage services</button>
    <button class:active={tab === 'locations'} on:click={() => (tab = 'locations')}>Locations</button>
    <span class="spacer"></span>
    {#if tab === 'nodes'}<Button size="small" on:click={openCreate}>Add storage node</Button>{/if}
    <Button variant="secondary" size="small" on:click={load} disabled={loading}>Refresh</Button>
  </div>

  {#if loading}
    <div class="muted">Loading…</div>
  {:else if tab === 'nodes'}
    <div class="table">
      <div class="row head nodes">
        <div>Name</div><div>Server / location</div><div>Status</div><div>VLANs</div><div>Runner</div><div>Ceph / free</div><div>Serving</div>
      </div>
      {#each nodes as node (node.id)}
        <button class="row nodes clickable" class:selected={selectedNode?.id === node.id} on:click={() => openNode(node)}>
          <div>{node.name}</div>
          <div>{node.server_name}<small>{node.location_name || ''}</small></div>
          <div>
            <span class="badge" class:ok={node.enabled && node.online}>
              {!node.enabled ? 'Disabled' : node.online ? 'Online' : 'Offline'}
            </span>
            <small>{seen(node.last_seen_at)}</small>
            {#if node.draining}<span class="badge warn">Draining</span>{/if}
            {#if node.sftp_master}<span class="badge ok">SFTP master</span>{/if}
          </div>
          <div>{node.vlan_mode === 'preconfigured' ? 'Preconfigured' : 'Switch'}</div>
          <div class="mono">{node.runner_version || '—'}</div>
          <div>{node.ceph_ok == null ? '—' : node.ceph_ok ? 'OK' : 'Problem'}<small>{formatBytes(node.free_bytes)}</small></div>
          <div>{node.serving_tenants.length}<small>{node.live_tenants} preferred</small></div>
        </button>
      {:else}
        <div class="empty">No storage nodes yet. Add one from a server, or from the server's page.</div>
      {/each}
    </div>

    {#if selectedNode}
      <div class="card">
        <div class="card-head">
          <h2>{selectedNode.name}</h2>
          <div class="actions">
            <Button size="small" variant="secondary" disabled={busy} on:click={() => saveNode({ enabled: !selectedNode.enabled })}>
              {selectedNode.enabled ? 'Disable' : 'Enable'}
            </Button>
            <Button size="small" variant="secondary" disabled={busy} on:click={toggleDrain}>
              {selectedNode.draining ? 'Undrain' : 'Drain'}
            </Button>
            <Button size="small" variant="secondary" disabled={busy} on:click={rotate}>Rotate key</Button>
            <Button size="small" variant="danger" disabled={busy} on:click={removeNode}>Delete</Button>
          </div>
        </div>
        <p class="meta">
          Server <a href={`/admin/servers/${selectedNode.server_id}`}>{selectedNode.server_name}</a> ·
          key <span class="mono">{selectedNode.api_key_prefix}…</span> ·
          last seen {seen(selectedNode.last_seen_at)} from <span class="mono">{selectedNode.last_seen_ip || '—'}</span> ·
          {selectedNode.in_sync ? 'config applied' : 'config not yet applied'}
        </p>
        {#if selectedNode.last_error}<p class="err">Runner error: {selectedNode.last_error}</p>{/if}
        <p class="meta">
          Failover {selectedNode.ha_active ? 'active' : 'not active'} in this location ·
          {#if selectedNode.ha_state}
            lease client {selectedNode.ha_state.lease_ok === false ? 'unhealthy' : 'healthy'}{selectedNode.ha_state.fenced ? ' · fenced' : ''} ·
          {/if}
          {selectedNode.sftp_master ? 'SFTP master' : 'not the SFTP master'}{selectedNode.draining ? ' · draining' : ''}
        </p>

        <StorageNodeVlanPorts
          node={selectedNode}
          disabled={busy}
          on:save={(e) => saveNode(e.detail)}
        />
        {#key selectedNode.id}
          <StorageNodeSftp node={selectedNode} disabled={busy} on:save={(e) => saveNode(e.detail)} />
        {/key}

        {#if selectedNode.orphans.length}
          <h3>Unknown volumes on the node</h3>
          <p class="hint">The runner found these and does not know them. Nothing removes them automatically.</p>
          <ul class="mono">{#each selectedNode.orphans as orphan}<li>{orphan}</li>{/each}</ul>
        {/if}

        <h3>Served by this node now</h3>
        {#each selectedNode.serving_tenants as t (t.tenant_id)}
          <div class="mini">
            {#if t.service_id}
              <button class="linkish" on:click={() => openStorage(t.service_id)}>{t.service_name}</button>
            {/if}
            {#if t.preferred_node_id !== selectedNode.id}<span class="badge warn">preferred elsewhere</span>{/if}
          </div>
        {:else}
          <p class="hint">None.</p>
        {/each}
        {#if selectedNode.preferred_elsewhere.length}
          <h3>Preferred here, served elsewhere</h3>
          {#each selectedNode.preferred_elsewhere as t (t.tenant_id)}
            <div class="mini">
              <button class="linkish" on:click={() => openStorage(t.service_id)}>Service {t.service_id}</button>
              <small>{t.current_node_id ? `served by ${nodes.find((n) => n.id === t.current_node_id)?.name || t.current_node_id}` : 'not served by any node'}</small>
            </div>
          {/each}
        {/if}

        <h3>Storage placed on this node</h3>
        {#each selectedNode.tenants as t (t.tenant_id)}
          <div class="mini">
            {#if t.service_id}
              <button class="linkish" on:click={() => openStorage(t.service_id)}>{t.service_name}</button>
            {:else}
              <span>Removed service</span>
            {/if}
            <span class="badge" class:ok={t.status === 'ready'}>{STORAGE_STATUS_LABELS[t.status] || t.status}</span>
            <span class="mono">VLAN {t.vlan} · {t.network_cidr} · {t.size_gb} GB</span>
            {#if t.destroy_after}<small>destroyed after {seen(t.destroy_after)}</small>{/if}
          </div>
        {:else}
          <p class="hint">None.</p>
        {/each}
      </div>
    {/if}
  {:else if tab === 'locations'}
    <div class="table">
      <div class="row head locs"><div>Location</div><div>Nodes</div><div>Failover</div><div>SFTP</div></div>
      {#each locations as location (location.location_id)}
        <button class="row locs clickable" class:selected={selectedLocation?.location_id === location.location_id} on:click={() => openLocation(location)}>
          <div>{location.location_name || `Location ${location.location_id}`}</div>
          <div>{location.nodes.length}</div>
          <div><span class="badge" class:ok={location.ha_active}>{location.ha_active ? 'Active' : 'Not active'}</span></div>
          <div>{location.sftp_enabled ? location.sftp_host || 'on' : 'Off'}<small>{location.sftp_virtual_ip || 'per node'}</small></div>
        </button>
      {:else}
        <div class="empty">No locations with storage nodes.</div>
      {/each}
    </div>
    {#if selectedLocation}
      <div class="card">
        <div class="card-head"><h2>{selectedLocation.location_name || `Location ${selectedLocation.location_id}`}</h2></div>
        {#key selectedLocation.updated_at}
          <StorageLocationPanel
            location={selectedLocation}
            disabled={busy}
            on:save={(e) => saveLocation(e.detail)}
            on:hostkey={(e) => locationHostKey(e.detail)}
            on:rebalance={rebalance}
          />
        {/key}
      </div>
    {/if}
  {:else}
    <div class="table">
      <div class="row head storages">
        <div>Service</div><div>Owner</div><div>Status</div><div>Network</div><div>Preferred / current</div><div>Size / used</div>
      </div>
      {#each storages as s (s.tenant_id)}
        <button class="row storages clickable" class:selected={selectedStorage?.tenant_id === s.tenant_id} on:click={() => s.service_id && openStorage(s.service_id)}>
          <div>{s.service_name || 'Removed service'}</div>
          <div>{s.owner_username || '—'}</div>
          <div><span class="badge" class:ok={s.status === 'ready'}>{STORAGE_STATUS_LABELS[s.status] || s.status}</span></div>
          <div class="mono">{s.network_cidr || '—'}{s.vlan ? ` (VLAN ${s.vlan})` : ''}</div>
          <div>{s.preferred_node?.name || s.node?.name || '—'}<small>{s.current_node ? (s.current_node.id === s.preferred_node?.id ? 'serving' : `on ${s.current_node.name}`) : 'not served'}{s.move ? ` · moving to ${s.move.to_node_name}` : ''}</small></div>
          <div>{s.size_gb} GB<small>{formatBytes(s.used_bytes)} used</small></div>
        </button>
      {:else}
        <div class="empty">No NFS storage services.</div>
      {/each}
    </div>

    {#if selectedStorage}
      <div class="card">
        <div class="card-head">
          <h2>{selectedStorage.service_name}</h2>
          {#if selectedStorage.service_id}<a href={`/admin/services/${selectedStorage.service_id}`}>Service record</a>{/if}
        </div>
        <div class="grid">
          <div><span class="field">Status</span><span class="badge" class:ok={selectedStorage.status === 'ready'}>{STORAGE_STATUS_LABELS[selectedStorage.status] || selectedStorage.status}</span>
            {#if selectedStorage.status_detail}<small>{selectedStorage.status_detail}</small>{/if}</div>
          <div><span class="field">Switch attachment</span>{selectedStorage.attachment_state || '—'}
            {#if selectedStorage.attachment_error}<small class="err">{selectedStorage.attachment_error}</small>{/if}</div>
          <div><span class="field">Runner</span>{selectedStorage.runner_state || 'not reported'}
            {#if selectedStorage.runner_error}<small class="err">{selectedStorage.runner_error}</small>{/if}</div>
          <div><span class="field">Preferred node</span>{selectedStorage.preferred_node?.name || '—'}</div>
          <div><span class="field">Current node</span>{selectedStorage.current_node?.name || 'not served'}
            {#if selectedStorage.current_since}<small>since {seen(selectedStorage.current_since)}</small>{/if}</div>
          {#if selectedStorage.move}<div><span class="field">Move</span>to {selectedStorage.move.to_node_name}<small>requested {seen(selectedStorage.move.requested_at)}</small></div>{/if}
          {#if selectedStorage.last_failover_at}<div><span class="field">Last failover</span>{seen(selectedStorage.last_failover_at)}</div>{/if}
          <div><span class="field">Network</span>{selectedStorage.network ? `${selectedStorage.network.name} (VLAN ${selectedStorage.network.vlan_id})` : '—'}</div>
          <div><span class="field">Endpoint</span><span class="mono">{selectedStorage.endpoint_ip || '—'}</span></div>
          <div><span class="field">Owner</span>{selectedStorage.owner_username || '—'}</div>
          {#if selectedStorage.destroy_after}<div><span class="field">Data destroyed after</span>{seen(selectedStorage.destroy_after)}</div>{/if}
        </div>

        {#if selectedStorage.status === 'unlinked'}
          <h3>Link to a private network</h3>
          <div class="inline">
            <input type="number" placeholder="Network ID" bind:value={linkForm.network_id} />
            <select bind:value={linkForm.node_id}>
              <option value="">Least loaded node</option>
              {#each nodes.filter((n) => n.enabled) as n (n.id)}<option value={n.id}>{n.name}</option>{/each}
            </select>
            <Button size="small" disabled={busy || !linkForm.network_id} on:click={link}>Link</Button>
          </div>
        {/if}

        {#if selectedStorage.status !== 'terminated' && selectedStorage.status !== 'unlinked' && selectedStorage.node}
          <h3>Move to another node</h3>
          <div class="inline">
            <select bind:value={moveForm.node_id}>
              <option value="">Choose a node</option>
              {#each nodes.filter((n) => n.enabled && !n.draining && n.location_id === nodeLocation(selectedStorage.node.id) && n.id !== selectedStorage.current_node?.id) as n (n.id)}
                <option value={n.id}>{n.name}</option>
              {/each}
            </select>
            <label class="check"><input type="checkbox" bind:checked={moveForm.set_preferred} /> Make it the preferred node</label>
            <Button size="small" disabled={busy || !moveForm.node_id || !!selectedStorage.move} on:click={moveStorage}>Move</Button>
          </div>
        {/if}

        {#if selectedStorage.status !== 'terminated'}
          <h3>Size</h3>
          <div class="inline">
            <input type="number" min="1" bind:value={resizeGb} />
            <span>GB</span>
            <Button size="small" disabled={busy || Number(resizeGb) === selectedStorage.size_gb} on:click={resize}>Resize</Button>
          </div>
          {#if selectedStorage.status !== 'unlinked'}
            <StorageSharesPanel storage={selectedStorage} on:change={(e) => (selectedStorage = e.detail)} />
          {/if}
          <StorageSftpPanel storage={selectedStorage} on:change={(e) => (selectedStorage = e.detail)} />
        {/if}
      </div>
    {/if}
  {/if}
</div>

{#if showCreate}
  <Modal title="Add storage node" onClose={() => (showCreate = false)}>
    <div class="form">
      <label for="sn-server">Server</label>
      <select id="sn-server" bind:value={createForm.server_id} on:change={pickServer}>
        <option value="">Choose a server</option>
        {#each servers as server (server.id)}<option value={server.id}>{server.name}</option>{/each}
      </select>
      <label for="sn-name">Name</label>
      <input id="sn-name" bind:value={createForm.name} placeholder="store-01 (default: the server's name)" />
      <label for="sn-mode">VLAN mode</label>
      <select id="sn-mode" bind:value={createForm.vlan_mode}>
        <option value="switch">Switch: Rackflow trunks each VLAN</option>
        <option value="preconfigured">Preconfigured: no switch changes</option>
      </select>
      {#if createPreview}
        {#if createForm.vlan_mode === 'switch'}
          <span class="label">Ports that carry customer VLANs (none ticked: the PXE port and its bond)</span>
          {#each createPreview.server_ports as port (port.id)}
            <label class="check">
              <input type="checkbox" checked={createForm.vlan_port_ids.includes(port.id)}
                on:change={() => (createForm.vlan_port_ids = togglePort(createForm.vlan_port_ids, port.id))} />
              {port.name}{port.pxe ? ' (PXE)' : ''}{port.speed_mbps ? `, ${port.speed_mbps / 1000} Gb/s` : ''}
            </label>
          {/each}
          {#if createPreview.vlan_ports.error}<p class="err">{createPreview.vlan_ports.error}</p>{/if}
          {#each createPreview.vlan_ports.ports.filter((p) => p.problem) as p}
            <p class="err">{p.server_port}: {p.problem}</p>
          {/each}
        {/if}
      {/if}
      <div class="form-actions">
        <Button size="small" on:click={submitCreate} disabled={busy || !createForm.server_id}>Create and generate key</Button>
        <Button variant="secondary" size="small" on:click={() => (showCreate = false)}>Cancel</Button>
      </div>
    </div>
  </Modal>
{/if}

{#if revealedKey}
  <Modal title={revealedKey.title} onClose={() => (revealedKey = null)}>
    <p class="warn">Copy this key now. It will not be shown again. Set it as the storage runner's API key.</p>
    <pre class="key-box">{revealedKey.api_key}</pre>
    <div class="form-actions">
      <Button size="small" on:click={copyKey}>Copy</Button>
      <Button variant="secondary" size="small" on:click={() => (revealedKey = null)}>Done</Button>
    </div>
  </Modal>
{/if}

<style>
  .page { padding: 32px; display: flex; flex-direction: column; gap: 12px; }
  .intro { color: var(--text-secondary); margin: 0; max-width: 56rem; line-height: 1.45; }
  .intro code { font-size: 0.9em; color: var(--text-primary); }
  .banner { padding: 0.65rem 0.85rem; border-radius: 6px; }
  .banner.error { background: var(--danger-bg); color: var(--danger-text); }
  .banner.ok { background: var(--success-bg); color: var(--success-text); }
  .tabs { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .tabs button { padding: 6px 12px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .tabs button.active { border-color: var(--accent-color); font-weight: 600; }
  .spacer { flex: 1; }
  .table { display: flex; flex-direction: column; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; overflow-x: auto; }
  .row { display: grid; gap: 10px; padding: 10px 14px; align-items: center; border: 0; border-bottom: 1px solid var(--border-color); font-size: 0.9rem; color: var(--text-primary); background: none; text-align: left; font: inherit; min-width: 760px; }
  .row.nodes { grid-template-columns: 1.1fr 1.3fr 1.2fr 0.9fr 0.8fr 0.9fr 0.5fr; }
  .row.locs { grid-template-columns: 1.3fr 0.6fr 0.9fr 1.4fr; }
  .row.storages { grid-template-columns: 1.3fr 1fr 0.9fr 1.3fr 0.9fr 0.9fr; }
  .row:last-child { border-bottom: none; }
  .row.head { font-weight: 600; font-size: 0.8rem; color: var(--text-secondary); background: var(--bg-secondary); }
  .row.clickable { cursor: pointer; }
  .row.clickable:hover, .row.selected { background: var(--bg-secondary); }
  .row small, .grid small, .mini small { display: block; font-size: 0.75rem; color: var(--text-secondary); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; background: var(--bg-tertiary, var(--bg-secondary)); color: var(--text-secondary); }
  .badge.ok { background: var(--success-bg); color: var(--success-text); }
  .badge.warn { background: var(--warning-bg, var(--bg-secondary)); color: var(--warning-text); }
  .card { border: 1px solid var(--border-color); border-radius: 10px; background: var(--bg-primary); padding: 16px 18px; display: flex; flex-direction: column; gap: 8px; }
  .card-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .card h2 { margin: 0; font-size: 1.15rem; }
  .card h3 { margin: 10px 0 0; font-size: 0.95rem; }
  .card a, .linkish { color: var(--accent-color); }
  .linkish { background: none; border: 0; padding: 0; cursor: pointer; font: inherit; }
  .meta, .hint { margin: 0; color: var(--text-secondary); font-size: 0.85rem; }
  .err { margin: 0; color: var(--danger-color); font-size: 0.85rem; }
  .warn { color: var(--warning-text); margin: 0 0 0.75rem; }
  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
  .grid .field { display: block; font-size: 0.75rem; font-weight: 600; color: var(--text-secondary); }
  .mini { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 0.9rem; }
  .inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .inline input, .inline select, .form input, .form select { padding: 7px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); }
  .empty, .muted { padding: 16px 14px; color: var(--text-secondary); }
  .form { display: flex; flex-direction: column; gap: 0.5rem; }
  .form > label, .label { font-weight: 600; font-size: 0.9rem; color: var(--text-primary); }
  .form label.check { font-weight: 400; display: flex; gap: 6px; align-items: center; }
  .form-actions { display: flex; gap: 8px; margin-top: 0.75rem; }
  .key-box { margin: 0; padding: 0.75rem; background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; word-break: break-all; white-space: pre-wrap; font-size: 0.85rem; }
</style>
