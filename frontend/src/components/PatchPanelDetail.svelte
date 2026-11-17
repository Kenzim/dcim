<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    getPatchPanel,
    updatePatchPanelPort,
    listPatchPanels,
    getLocations,
    getRacks,
    getServers,
    getServer,
    getSwitches,
    getSwitchPorts,
    createCableRun,
    deleteCableRun,
  } from '../lib/api.js';
  import { deviceUrl, deviceKindLabel } from '../lib/cablePath.js';

  export let panelId;
  export let onBack = null;

  const SIDES = ['front', 'rear'];
  const SIDE_LABELS = { front: 'Front', rear: 'Rear' };

  let panel = null;
  let locations = [];
  let racks = [];
  let loading = true;
  let error = null;
  let actionError = null;

  let editingNumber = null;
  let labelDraft = '';
  let savingLabel = false;

  let connect = null;
  let farType = 'server';
  let devices = [];
  let devicesLoading = false;
  let deviceId = '';
  let farPorts = [];
  let farPortsLoading = false;
  let farPortId = '';
  let farSide = 'rear';
  let cableType = '';
  let connecting = false;
  let connectError = null;
  let deviceSeq = 0;

  $: cabledCount = panel ? panel.ports.reduce((n, p) => n + (p.front ? 1 : 0) + (p.rear ? 1 : 0), 0) : 0;
  $: cablingUrl = panel
    ? `/admin/cabling?${new URLSearchParams({ location: String(panel.location_id), focus: `patch:${panel.id}` })}`
    : '/admin/cabling';
  $: availableFarPorts = farPorts.filter((p) => !p.taken);

  function placement(p) {
    if (!p.rack_id) return 'Not racked';
    const rack = racks.find((r) => r.id === p.rack_id)?.name || 'Unknown rack';
    if (p.rack_unit == null) return rack;
    const units = p.rack_units || 1;
    return units > 1 ? `${rack}, U${p.rack_unit} to U${p.rack_unit + units - 1}` : `${rack}, U${p.rack_unit}`;
  }

  function endText(end) {
    if (!end) return '';
    const side = end.side ? ` (${end.side})` : '';
    return `${end.device_name || deviceKindLabel(end.type)} ${end.port_name || `#${end.id}`}${side}`;
  }

  function pathSummary(port) {
    const ends = SIDES.map((s) => port[s]?.reaches).filter(Boolean);
    if (!port.front && !port.rear) return null;
    if (ends.length === 2) return { complete: true, text: `${endText(ends[0])} to ${endText(ends[1])}` };
    return { complete: false, text: ends.length ? `${endText(ends[0])}, other side not connected` : 'Not connected through to a device' };
  }

  async function load() {
    error = null;
    try {
      const [p, locs, rks] = await Promise.all([getPatchPanel(panelId), getLocations(), getRacks()]);
      panel = p;
      locations = locs || [];
      racks = rks || [];
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function reloadPanel() {
    panel = await getPatchPanel(panelId);
  }

  function startEditLabel(port) {
    editingNumber = port.number;
    labelDraft = port.label || '';
  }

  async function saveLabel(port) {
    savingLabel = true;
    actionError = null;
    try {
      panel = await updatePatchPanelPort(panel.id, port.number, labelDraft);
      editingNumber = null;
    } catch (err) {
      actionError = err.message;
    } finally {
      savingLabel = false;
    }
  }

  function onLabelKey(event, port) {
    if (event.key === 'Enter') saveLabel(port);
    if (event.key === 'Escape') editingNumber = null;
  }

  async function disconnect(port, side) {
    const info = port[side];
    if (!info) return;
    const target = info.other_end ? ` from ${endText(info.other_end)}` : '';
    if (!confirm(`Disconnect the ${side} of ${port.name}${target}?`)) return;
    actionError = null;
    try {
      await deleteCableRun(info.cable_id);
      await reloadPanel();
    } catch (err) {
      actionError = err.message;
    }
  }

  function openConnect(port, side) {
    connect = { port, side };
    farType = side === 'front' ? 'server' : 'patch';
    farSide = 'rear';
    cableType = '';
    connectError = null;
    loadDevices();
  }

  function closeConnect() {
    connect = null;
  }

  async function loadDevices() {
    const seq = ++deviceSeq;
    devices = [];
    deviceId = '';
    farPorts = [];
    farPortId = '';
    devicesLoading = true;
    try {
      let list;
      if (farType === 'server') list = await getServers();
      else if (farType === 'switch') list = await getSwitches();
      else list = (await listPatchPanels()).filter((p) => p.id !== panel.id);
      if (seq !== deviceSeq) return;
      const sameLocation = (d) => d.location_id === panel.location_id;
      devices = (list || [])
        .map((d) => ({ id: d.id, name: d.name, near: sameLocation(d) }))
        .sort((a, b) => Number(b.near) - Number(a.near) || a.name.localeCompare(b.name));
    } catch (err) {
      if (seq === deviceSeq) connectError = err.message;
    } finally {
      if (seq === deviceSeq) devicesLoading = false;
    }
  }

  async function loadFarPorts() {
    const seq = ++deviceSeq;
    farPorts = [];
    farPortId = '';
    if (!deviceId) return;
    farPortsLoading = true;
    try {
      let ports;
      if (farType === 'server') {
        const server = await getServer(deviceId);
        ports = (server?.network_ports || []).map((p) => ({ id: p.id, name: p.name, taken: !!p.cable_run }));
      } else if (farType === 'switch') {
        const result = await getSwitchPorts(deviceId);
        ports = (result?.ports || []).map((p) => ({ id: p.id, name: p.name, taken: !!p.cable_run }));
      } else {
        const other = await getPatchPanel(deviceId);
        ports = (other?.ports || []).map((p) => ({ id: p.id, name: p.name, taken: !!p[farSide] }));
      }
      if (seq !== deviceSeq) return;
      farPorts = ports;
    } catch (err) {
      if (seq === deviceSeq) connectError = err.message;
    } finally {
      if (seq === deviceSeq) farPortsLoading = false;
    }
  }

  async function submitConnect() {
    if (!connect || !farPortId) return;
    connecting = true;
    connectError = null;
    try {
      await createCableRun({
        port_a: { type: 'patch', id: connect.port.id, side: connect.side },
        port_b: { type: farType, id: Number(farPortId), side: farType === 'patch' ? farSide : null },
        cable_type: cableType.trim() || null,
      });
      await reloadPanel();
      closeConnect();
    } catch (err) {
      connectError = err.message;
    } finally {
      connecting = false;
    }
  }

  onMount(load);
</script>

<PageHeader title={panel ? panel.name : 'Patch Panel'}>
  <svelte:fragment slot="actions">
    {#if panel}<a href={cablingUrl} class="btn-secondary">View in Cabling</a>{/if}
    <button class="btn-secondary" on:click={() => (onBack ? onBack() : history.back())}>Back to patch panels</button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page patch-panel-detail">
  {#if loading}
    <div class="loading">Loading patch panel…</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if panel}
    <div class="summary">
      <div><span class="label">Location</span>{locations.find((l) => l.id === panel.location_id)?.name || 'Unknown'}</div>
      <div><span class="label">Rack</span>{placement(panel)}</div>
      <div><span class="label">Medium</span>{panel.medium === 'fibre' ? 'Fibre' : 'Copper'}</div>
      <div><span class="label">Ports</span>{panel.port_count}</div>
      <div><span class="label">Cables</span>{cabledCount}</div>
      {#if panel.description}<div class="wide"><span class="label">Description</span>{panel.description}</div>{/if}
    </div>
    <p class="hint">
      Each port joins the cable on its front to the cable on its rear. Paths are traced through as many panels as
      they pass, so a server cabled to the front of one port reaches whatever is at the end of the rear cable.
    </p>
    {#if actionError}<div class="form-error">{actionError}</div>{/if}

    <div class="admin-data-table">
      <table>
        <thead>
          <tr>
            <th class="num">#</th>
            <th>Label</th>
            <th>Front</th>
            <th>Rear</th>
            <th>Path</th>
          </tr>
        </thead>
        <tbody>
          {#each panel.ports as port (port.id)}
            {@const path = pathSummary(port)}
            <tr>
              <td class="num">{port.number}</td>
              <td class="label-cell">
                {#if editingNumber === port.number}
                  <!-- svelte-ignore a11y-autofocus -->
                  <input
                    class="label-input"
                    type="text"
                    maxlength="255"
                    placeholder="Port {port.number}"
                    bind:value={labelDraft}
                    on:keydown={(e) => onLabelKey(e, port)}
                    disabled={savingLabel}
                    autofocus
                  />
                  <button type="button" class="btn-primary btn-sm" on:click={() => saveLabel(port)} disabled={savingLabel}>Save</button>
                  <button type="button" class="btn-secondary btn-sm" on:click={() => (editingNumber = null)} disabled={savingLabel}>Cancel</button>
                {:else}
                  <button type="button" class="label-button" title="Rename this port" on:click={() => startEditLabel(port)}>
                    {#if port.label}{port.label}{:else}<span class="muted">Port {port.number}</span>{/if}
                  </button>
                {/if}
              </td>
              {#each SIDES as side}
                {@const info = port[side]}
                <td class="side-cell">
                  {#if info}
                    <div class="side-line">
                      {#if info.other_end}
                        {@const url = deviceUrl(info.other_end.type, info.other_end.device_id)}
                        {#if url}<a href={url} class="link">{info.other_end.device_name}</a>{:else}{info.other_end.device_name}{/if}
                        <span class="mono">{info.other_end.port_name}</span>
                        {#if info.other_end.side}<span class="muted">({info.other_end.side})</span>{/if}
                      {/if}
                    </div>
                    <div class="side-meta">
                      {#if info.cable_type}<span>{info.cable_type}</span>{/if}
                      <button type="button" class="text-button danger" on:click={() => disconnect(port, side)}>Disconnect</button>
                    </div>
                  {:else}
                    <button type="button" class="connect-button" on:click={() => openConnect(port, side)}>+ Connect</button>
                  {/if}
                </td>
              {/each}
              <td class="path-cell" class:incomplete={path && !path.complete}>
                {#if path}{path.text}{:else}<span class="muted">Unused</span>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if connect}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div class="modal-overlay" tabindex="-1" on:click={closeConnect} on:keydown={(e) => e.key === 'Escape' && closeConnect()}>
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <div class="modal-content" role="dialog" aria-labelledby="pp-connect-title" on:click|stopPropagation on:keydown|stopPropagation>
      <div class="modal-header">
        <h3 id="pp-connect-title">Connect {SIDE_LABELS[connect.side].toLowerCase()} of {connect.port.name}</h3>
        <button class="btn-icon-only" on:click={closeConnect} aria-label="Close">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <form class="modal-body" on:submit|preventDefault={submitConnect}>
        {#if connectError}<div class="form-error">{connectError}</div>{/if}
        <div class="form-group">
          <label for="pp-far-type">Cable to</label>
          <select id="pp-far-type" bind:value={farType} on:change={loadDevices} disabled={connecting}>
            <option value="server">Server port</option>
            <option value="switch">Switch port</option>
            <option value="patch">Another patch panel</option>
          </select>
        </div>
        <div class="form-group">
          <label for="pp-far-device">{deviceKindLabel(farType)}</label>
          <select id="pp-far-device" bind:value={deviceId} on:change={loadFarPorts} disabled={connecting || devicesLoading}>
            <option value="">{devicesLoading ? 'Loading…' : 'Select…'}</option>
            {#each devices as d (d.id)}
              <option value={String(d.id)}>{d.name}{d.near ? '' : ' (other location)'}</option>
            {/each}
          </select>
        </div>
        {#if farType === 'patch'}
          <div class="form-group">
            <label for="pp-far-side">Side of that panel</label>
            <select id="pp-far-side" bind:value={farSide} on:change={loadFarPorts} disabled={connecting}>
              <option value="rear">Rear</option>
              <option value="front">Front</option>
            </select>
          </div>
        {/if}
        {#if deviceId}
          <div class="form-group">
            <label for="pp-far-port">Port</label>
            {#if farPortsLoading}
              <p class="muted">Loading ports…</p>
            {:else}
              <select id="pp-far-port" bind:value={farPortId} disabled={connecting}>
                <option value="">Select a port</option>
                {#each availableFarPorts as p (p.id)}
                  <option value={String(p.id)}>{p.name}</option>
                {/each}
              </select>
              {#if farPorts.length && !availableFarPorts.length}
                <small class="muted">Every port there already has a cable{farType === 'patch' ? ' on that side' : ''}.</small>
              {/if}
            {/if}
          </div>
        {/if}
        <div class="form-group">
          <label for="pp-cable-type">Cable type</label>
          <input id="pp-cable-type" type="text" maxlength="64" placeholder="For example Cat6 or OM4" bind:value={cableType} disabled={connecting} />
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeConnect} disabled={connecting}>Cancel</button>
          <button type="submit" class="btn-primary" disabled={connecting || !farPortId}>{connecting ? 'Connecting…' : 'Connect'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .link {
    color: var(--accent-color);
    text-decoration: none;
  }

  .link:hover {
    text-decoration: underline;
  }

  a.btn-secondary {
    text-decoration: none;
  }

  .connect-button {
    padding: 0;
    font: inherit;
    font-size: 12.5px;
    color: var(--text-tertiary);
    background: none;
    border: none;
    cursor: pointer;
  }

  .connect-button:hover {
    color: var(--accent-color);
    text-decoration: underline;
  }

  .summary {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 28px;
    padding: 14px 16px;
    margin-bottom: 12px;
    font-size: 14px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    box-shadow: var(--admin-surface-shadow);
  }

  .summary .wide {
    flex-basis: 100%;
  }

  .summary .label {
    display: block;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-tertiary);
    margin-bottom: 2px;
  }

  .hint {
    font-size: 13px;
    color: var(--text-secondary);
    margin: 0 0 14px;
  }

  .num {
    width: 48px;
    text-align: right;
    color: var(--text-tertiary);
  }

  .label-cell {
    white-space: nowrap;
  }

  .label-input {
    width: 140px;
    padding: 3px 6px;
    font-size: 13px;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
  }

  .label-button,
  .text-button {
    padding: 0;
    font: inherit;
    color: inherit;
    background: none;
    border: none;
    cursor: pointer;
  }

  .label-button:hover {
    color: var(--accent-color);
    text-decoration: underline dotted;
  }

  .text-button {
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .text-button.danger:hover {
    color: var(--danger-text);
  }

  .side-cell {
    min-width: 180px;
  }

  .side-line {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 6px;
    align-items: baseline;
  }

  .side-meta {
    display: flex;
    gap: 10px;
    font-size: 12px;
    color: var(--text-tertiary);
    margin-top: 2px;
  }

  .path-cell {
    font-size: 13px;
    color: var(--text-secondary);
  }

  .path-cell.incomplete {
    color: var(--warning-text);
  }

  .mono {
    font-family: var(--font-mono, monospace);
    font-size: 12.5px;
  }

  .muted {
    color: var(--text-tertiary);
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding: 12px 0 0;
  }
</style>
