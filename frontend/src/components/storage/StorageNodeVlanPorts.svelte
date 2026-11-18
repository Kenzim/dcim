<script>
  import { createEventDispatcher } from 'svelte';
  import Button from '../ui/Button.svelte';

  // Storage node detail: vlan_mode, vlan_port_ids, server_ports and vlan_ports.
  export let node;
  export let disabled = false;

  const dispatch = createEventDispatcher();

  let mode = node.vlan_mode;
  let portIds = [...(node.vlan_port_ids || [])];
  let lastNodeKey = '';

  $: {
    const key = `${node.id}:${node.vlan_mode}:${(node.vlan_port_ids || []).join(',')}`;
    if (key !== lastNodeKey) {
      lastNodeKey = key;
      mode = node.vlan_mode;
      portIds = [...(node.vlan_port_ids || [])];
    }
  }
  $: dirty =
    mode !== node.vlan_mode ||
    [...portIds].sort().join(',') !== [...(node.vlan_port_ids || [])].sort().join(',');
  $: preview = node.vlan_ports || { ok: false, ports: [], error: null };
  $: problems = preview.ports.filter((p) => p.problem);

  function toggle(portId) {
    portIds = portIds.includes(portId) ? portIds.filter((p) => p !== portId) : [...portIds, portId];
  }
</script>

<div class="vlan">
  <h3>Customer VLANs</h3>
  <label class="mode">
    VLAN mode
    <select bind:value={mode} {disabled}>
      <option value="switch">Switch</option>
      <option value="preconfigured">Preconfigured</option>
    </select>
  </label>
  <p class="hint">
    <strong>Switch:</strong> Rackflow adds each customer VLAN to the node's switch ports through the switch plugin.
    <strong>Preconfigured:</strong> the node already receives every VLAN, for example pre-trunked ports or a VLAN-aware
    Proxmox bridge; Rackflow makes no switch calls and treats attachments as applied at once.
  </p>

  {#if mode === 'switch'}
    <span class="label">Ports that carry customer VLANs (none ticked: the PXE port and its bond)</span>
    <div class="ports">
      {#each node.server_ports || [] as port (port.id)}
        <label class="check">
          <input type="checkbox" checked={portIds.includes(port.id)} on:change={() => toggle(port.id)} {disabled} />
          {port.name}{port.pxe ? ' (PXE)' : ''}{port.lag_group ? ` [${port.lag_group}]` : ''}{port.speed_mbps ? `, ${port.speed_mbps / 1000} Gb/s` : ''}
        </label>
      {:else}
        <span class="hint">The server has no network ports recorded.</span>
      {/each}
    </div>
  {/if}

  {#if dirty}
    <div><Button size="small" {disabled} on:click={() => dispatch('save', { vlan_mode: mode, vlan_port_ids: portIds })}>Save VLAN settings</Button></div>
  {/if}

  {#if node.vlan_mode === 'preconfigured'}
    <p class="ok">No switch changes are made for this node. Make sure every customer VLAN in the location's pool reaches it.</p>
  {:else}
    {#if !preview.ok}
      <p class="bad">
        Customer VLANs cannot be added to this node yet: storage linked to it will show a problem until this is fixed.
        {#if preview.error}{preview.error}.{/if}
      </p>
    {/if}
    {#if preview.ports.length}
      <div class="table">
        <div class="row head"><div>Server port</div><div>Switch</div><div>Switch port</div><div>VLAN management</div></div>
        {#each preview.ports as p (p.server_port_id)}
          <div class="row" class:problem={p.problem}>
            <div>{p.server_port}</div>
            <div>{p.switch || '—'}</div>
            <div class="mono">{p.switch_port || '—'}</div>
            <div>{p.problem ? p.problem : 'Yes'}</div>
          </div>
        {/each}
      </div>
    {/if}
    {#if problems.length === 0 && preview.ok}
      <p class="ok">Every port is cabled to a switch with VLAN management.</p>
    {/if}
  {/if}
</div>

<style>
  .vlan { display: flex; flex-direction: column; gap: 8px; }
  h3 { margin: 10px 0 0; font-size: 0.95rem; }
  .mode { display: flex; gap: 8px; align-items: center; font-weight: 600; font-size: 0.9rem; }
  select { padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); }
  .hint { margin: 0; color: var(--text-secondary); font-size: 0.85rem; line-height: 1.45; }
  .label { font-weight: 600; font-size: 0.85rem; }
  .ports { display: flex; flex-wrap: wrap; gap: 6px 16px; }
  .check { display: flex; gap: 6px; align-items: center; font-size: 0.85rem; }
  .ok { margin: 0; color: var(--success-text); font-size: 0.85rem; }
  .bad { margin: 0; padding: 8px 10px; border-radius: 6px; background: var(--danger-bg); color: var(--danger-text); font-size: 0.85rem; }
  .table { border: 1px solid var(--border-color); border-radius: 8px; overflow-x: auto; }
  .row { display: grid; grid-template-columns: 1fr 1fr 1fr 1.6fr; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--border-color); font-size: 0.85rem; min-width: 520px; }
  .row:last-child { border-bottom: 0; }
  .row.head { font-weight: 600; color: var(--text-secondary); background: var(--bg-secondary); }
  .row.problem { color: var(--danger-color); font-weight: 600; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
