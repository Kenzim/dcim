<script>
  import { onMount } from 'svelte';
  import Button from '../ui/Button.svelte';
  import StorageNodeVlanPorts from './StorageNodeVlanPorts.svelte';
  import StorageNodeSftp from './StorageNodeSftp.svelte';
  import { listStorageNodes, createStorageNode, updateStorageNode, getServerVlanPorts } from '../../lib/api.js';

  export let serverId;

  let node = null;
  let preview = null;
  let loading = true;
  let busy = false;
  let error = '';
  let showCreate = false;
  let createForm = { name: '', vlan_mode: 'switch', vlan_port_ids: [] };
  let revealedKey = '';

  async function load() {
    loading = true;
    error = '';
    try {
      const rows = await listStorageNodes(serverId);
      node = rows[0] || null;
      preview = node ? null : await getServerVlanPorts(serverId);
    } catch (err) {
      error = err.message || 'Failed to load storage node';
    } finally {
      loading = false;
    }
  }

  async function act(action) {
    busy = true;
    error = '';
    try {
      return await action();
    } catch (err) {
      error = err.message || 'Request failed';
      return null;
    } finally {
      busy = false;
    }
  }

  async function save(changes) {
    const updated = await act(() => updateStorageNode(node.id, changes));
    if (updated) node = updated;
  }

  async function create() {
    const out = await act(() =>
      createStorageNode({
        server_id: Number(serverId),
        name: createForm.name.trim() || null,
        vlan_mode: createForm.vlan_mode,
        vlan_port_ids: createForm.vlan_port_ids,
      }),
    );
    if (out) {
      revealedKey = out.api_key;
      showCreate = false;
      node = out;
    }
  }

  function toggle(portId) {
    const ids = createForm.vlan_port_ids;
    createForm.vlan_port_ids = ids.includes(portId) ? ids.filter((p) => p !== portId) : [...ids, portId];
  }

  onMount(load);
</script>

<section class="right-panel storage-node-panel">
  <h3 class="panel-title">Storage node</h3>
  {#if error}<p class="err">{error}</p>{/if}
  {#if loading}
    <p class="panel-empty">Loading…</p>
  {:else if node}
    {#if revealedKey}
      <div class="key">
        <strong>Copy this runner API key now. It will not be shown again.</strong>
        <code>{revealedKey}</code>
        <Button size="small" variant="secondary" on:click={() => (revealedKey = '')}>Done</Button>
      </div>
    {/if}
    <p>
      <a href="/admin/storage">{node.name}</a> ·
      {!node.enabled ? 'Disabled' : node.online ? 'Online' : 'Offline'} ·
      {node.live_tenants} storage · runner {node.runner_version || 'not reported'}
    </p>
    <div>
      <Button size="small" variant="secondary" disabled={busy} on:click={() => save({ enabled: !node.enabled })}>
        {node.enabled ? 'Disable' : 'Enable'}
      </Button>
    </div>
    <StorageNodeVlanPorts {node} disabled={busy} on:save={(e) => save(e.detail)} />
    <StorageNodeSftp {node} disabled={busy} on:save={(e) => save(e.detail)} />
  {:else if showCreate}
    <label>Name <input bind:value={createForm.name} placeholder="Default: the server's name" /></label>
    <label>VLAN mode
      <select bind:value={createForm.vlan_mode}>
        <option value="switch">Switch: Rackflow trunks each VLAN</option>
        <option value="preconfigured">Preconfigured: no switch changes</option>
      </select>
    </label>
    {#if createForm.vlan_mode === 'switch' && preview}
      <span class="hint">Ports that carry customer VLANs (none ticked: the PXE port and its bond)</span>
      {#each preview.server_ports as port (port.id)}
        <label class="check">
          <input type="checkbox" checked={createForm.vlan_port_ids.includes(port.id)} on:change={() => toggle(port.id)} />
          {port.name}{port.pxe ? ' (PXE)' : ''}
        </label>
      {/each}
      {#if preview.vlan_ports.error}<p class="err">{preview.vlan_ports.error}</p>{/if}
      {#each preview.vlan_ports.ports.filter((p) => p.problem) as p}
        <p class="err">{p.server_port}: {p.problem}</p>
      {/each}
    {/if}
    <div class="actions">
      <Button size="small" disabled={busy} on:click={create}>Make storage node</Button>
      <Button size="small" variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
    </div>
  {:else}
    <p class="panel-empty">This server is not a storage node.</p>
    <div><Button size="small" variant="secondary" on:click={() => (showCreate = true)}>Make storage node</Button></div>
  {/if}
</section>

<style>
  .storage-node-panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 16px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--bg-primary);
  }
  .panel-title { margin: 0; font-size: 1rem; }
  .panel-empty { color: var(--text-secondary); }
  .storage-node-panel p { margin: 0; font-size: 0.9rem; }
  .storage-node-panel a { color: var(--accent-color); }
  .err { color: var(--danger-color); }
  .hint { font-size: 0.85rem; color: var(--text-secondary); }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; font-weight: 600; }
  label.check { flex-direction: row; align-items: center; gap: 6px; font-weight: 400; }
  input:not([type='checkbox']), select { padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); }
  .actions { display: flex; gap: 6px; }
  .key { display: flex; flex-direction: column; gap: 6px; padding: 10px; border-radius: 6px; background: var(--bg-secondary); }
  .key code { word-break: break-all; font-size: 0.8rem; }
</style>
