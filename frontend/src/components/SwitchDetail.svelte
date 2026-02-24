<script>
  import PageHeader from './PageHeader.svelte';
  import { getSwitch, getSwitchPorts, updateSwitchPorts, getLocations, getRacks, getServers, getServer, regenerateSwitchPorts, listCableRuns, createCableRun, deleteCableRun, getSwitchBandwidth } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { onMount } from 'svelte';

  export let switchId;
  export let onBack;

  let switchData = null;
  let ports = [];
  let selectedPortIds = new Set();
  let bulkSpeedInput = '';
  let bulkDescriptionInput = '';
  let bulkEditError = null;
  let bulkSaving = false;
  let locations = [];
  let racks = [];
  let loading = true;
  let error = null;
  let refreshing = false;
  let regeneratingPorts = false;
  let regenerateError = null;
  let regenerateSuccess = null;

  // Connect port modal
  let connectModalPort = null;
  let servers = [];
  let selectedServerId = null;
  let selectedServerPorts = [];
  let cableRunsForServer = [];
  let selectedServerPortId = null;
  let connecting = false;
  let connectError = null;
  let loadingServerPorts = false;

  // Bandwidth data
  let bandwidthData = null;
  let bandwidthLoading = false;
  let bandwidthError = null;
  let bandwidthHours = 24;
  let bandwidthResolution = 0; // 0 = raw (1 min), 5, 15, 60
  let selectedPortForBandwidth = ''; // '' = all ports

  onMount(async () => {
    await loadAllData();
  });

  async function loadAllData() {
    await Promise.all([loadSwitch(), loadPorts(), loadLocations(), loadRacks()]);
  }

  async function loadSwitch() {
    try {
      loading = true;
      error = null;
      switchData = await getSwitch(switchId);
      if (!switchData) {
        error = 'Switch not found';
      }
    } catch (err) {
      error = err.message;
      console.error('Failed to load switch:', err);
    } finally {
      loading = false;
    }
  }

  /** Natural sort so Ethernet1/1, 1/2, ... 1/9, 1/10 order correctly (not 1/1, 1/10, 1/11, 1/2). */
  function naturalSortPorts(portList) {
    if (!portList || !portList.length) return portList;
    return [...portList].sort((a, b) => {
      const na = a.name || '';
      const nb = b.name || '';
      const parts = (s) => s.split(/(\d+)/).filter(Boolean);
      const pa = parts(na);
      const pb = parts(nb);
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const x = pa[i];
        const y = pb[i];
        if (x === undefined) return -1;
        if (y === undefined) return 1;
        const nx = parseInt(x, 10);
        const ny = parseInt(y, 10);
        if (!Number.isNaN(nx) && !Number.isNaN(ny)) {
          if (nx !== ny) return nx - ny;
        } else {
          const c = x.localeCompare(y);
          if (c !== 0) return c;
        }
      }
      return 0;
    });
  }

  async function loadPorts() {
    try {
      const result = await getSwitchPorts(switchId);
      ports = naturalSortPorts(result.ports || []);
      selectedPortIds = new Set();
    } catch (err) {
      console.error('Failed to load ports:', err);
      ports = [];
    }
  }

  async function loadLocations() {
    try {
      locations = await getLocations();
    } catch (err) {
      console.error('Failed to load locations:', err);
    }
  }

  async function loadRacks() {
    try {
      racks = await getRacks();
    } catch (err) {
      console.error('Failed to load racks:', err);
    }
  }

  async function handleRefresh() {
    refreshing = true;
    try {
      await loadAllData();
    } catch (err) {
      console.error('Failed to refresh:', err);
    } finally {
      refreshing = false;
    }
  }

  async function handleRegeneratePorts() {
    regeneratingPorts = true;
    regenerateError = null;
    regenerateSuccess = null;
    try {
      const result = await regenerateSwitchPorts(switchId);
      regenerateSuccess = result.message || `Successfully regenerated ports: ${result.created} created, ${result.updated} updated`;
      // Reload ports after regeneration
      await loadPorts();
    } catch (err) {
      regenerateError = err.message;
      console.error('Failed to regenerate ports:', err);
    } finally {
      regeneratingPorts = false;
    }
  }

  function toggleSelectPort(portId, checked) {
    const next = new Set(selectedPortIds);
    if (checked) {
      next.add(portId);
    } else {
      next.delete(portId);
    }
    selectedPortIds = next;
  }

  function toggleSelectAll() {
    if (selectedPortIds.size === ports.length) {
      selectedPortIds = new Set();
    } else {
      selectedPortIds = new Set(ports.map(p => p.id));
    }
  }

  function parseSpeedInputToMbps(value) {
    if (!value) return null;
    const v = String(value).trim().toLowerCase();
    if (!v) return null;
    // Examples: "10000", "10g", "10 gbps", "100m", "100 mbps"
    const numMatch = v.match(/^(\d+(\.\d+)?)/);
    if (!numMatch) return null;
    const num = parseFloat(numMatch[1]);
    if (Number.isNaN(num) || num <= 0) return null;
    if (v.includes('g')) {
      return Math.round(num * 1000);
    }
    // Treat plain number or with 'm' as Mbps
    return Math.round(num);
  }

  async function applyBulkPortEdit() {
    bulkEditError = null;
    if (!selectedPortIds.size) {
      bulkEditError = 'Select at least one port to edit.';
      return;
    }
    const shouldUpdateSpeed = bulkSpeedInput.trim().length > 0;
    const shouldUpdateDescription = bulkDescriptionInput.trim().length > 0;
    if (!shouldUpdateSpeed && !shouldUpdateDescription) {
      bulkEditError = 'Enter a physical speed and/or description to apply.';
      return;
    }

    let speedMbps = null;
    if (shouldUpdateSpeed) {
      speedMbps = parseSpeedInputToMbps(bulkSpeedInput);
      if (speedMbps == null) {
        bulkEditError = 'Could not parse speed. Use values like "25G", "10G", or "10000" (Mbps).';
        return;
      }
    }

    const updates = [];
    for (const port of ports) {
      if (selectedPortIds.has(port.id)) {
        const upd = { id: port.id };
        if (shouldUpdateSpeed) upd.speed_mbps = speedMbps;
        if (shouldUpdateDescription) upd.description = bulkDescriptionInput.trim();
        updates.push(upd);
      }
    }
    if (!updates.length) {
      bulkEditError = 'No ports matched selection.';
      return;
    }

    try {
      bulkSaving = true;
      await updateSwitchPorts(switchId, updates);
      bulkSpeedInput = '';
      bulkDescriptionInput = '';
      selectedPortIds = new Set();
      await loadPorts();
    } catch (err) {
      console.error('Failed to update switch ports:', err);
      bulkEditError = err.message || 'Failed to update switch ports.';
    } finally {
      bulkSaving = false;
    }
  }

  function getStatusBadgeClass(adminStatus, operStatus) {
    if (adminStatus === 2) return 'disabled'; // Admin down
    if (operStatus === 1) return 'enabled'; // Operational up
    return 'warning'; // Operational down or other
  }

  function getStatusText(adminStatus, operStatus) {
    if (adminStatus === 2) return 'Admin Down';
    if (operStatus === 1) return 'Up';
    if (operStatus === 2) return 'Down';
    return 'Unknown';
  }

  function formatSpeed(speedMbps) {
    if (!speedMbps) return 'N/A';
    if (speedMbps >= 1000) {
      return `${speedMbps / 1000} Gbps`;
    }
    return `${speedMbps} Mbps`;
  }

  function getLocationName(locationId) {
    const location = locations.find(l => l.id === locationId);
    return location ? location.name : 'N/A';
  }

  function getRackName(rackId) {
    const rack = racks.find(r => r.id === rackId);
    return rack ? rack.name : 'N/A';
  }

  $: connectedPortIds = new Set((cableRunsForServer || []).flatMap(cr => {
    if (cr.end_a?.type === 'server' && Number(cr.end_a?.device_id) === Number(selectedServerId)) return [cr.end_a.id];
    if (cr.end_b?.type === 'server' && Number(cr.end_b?.device_id) === Number(selectedServerId)) return [cr.end_b.id];
    return [];
  }));
  $: availableServerPorts = (selectedServerPorts || []).filter(p => !connectedPortIds.has(p.id));

  function openConnectModal(port) {
    connectModalPort = port;
    connectError = null;
    selectedServerId = null;
    selectedServerPortId = null;
    selectedServerPorts = [];
    cableRunsForServer = [];
    servers = [];
    // Load servers after opening modal so UI updates immediately
    getServers()
      .then((list) => { servers = list || []; })
      .catch((err) => {
        connectError = err.message;
        servers = [];
      });
  }

  function closeConnectModal() {
    connectModalPort = null;
    selectedServerId = null;
    selectedServerPortId = null;
  }

  async function onServerChange() {
    selectedServerPortId = null;
    selectedServerPorts = [];
    cableRunsForServer = [];
    if (!selectedServerId) return;
    loadingServerPorts = true;
    connectError = null;
    try {
      const [server, runs] = await Promise.all([
        getServer(selectedServerId),
        listCableRuns({ serverId: selectedServerId })
      ]);
      selectedServerPorts = server?.network_ports || [];
      cableRunsForServer = runs || [];
    } catch (err) {
      connectError = err.message;
    } finally {
      loadingServerPorts = false;
    }
  }

  async function handleConnect() {
    if (!connectModalPort || !selectedServerPortId) return;
    connecting = true;
    connectError = null;
    try {
      await createCableRun({
        port_a: { type: 'switch', id: connectModalPort.id },
        port_b: { type: 'server', id: selectedServerPortId }
      });
      await loadPorts();
      closeConnectModal();
    } catch (err) {
      connectError = err.message;
    } finally {
      connecting = false;
    }
  }

  async function handleDisconnect(port) {
    if (!port?.cable_run?.id) return;
    if (!confirm(`Disconnect ${port.name} from server port?`)) return;
    try {
      await deleteCableRun(port.cable_run.id);
      await loadPorts();
    } catch (err) {
      console.error('Failed to disconnect:', err);
      alert(err.message);
    }
  }

  async function loadBandwidth() {
    if (!switchId) return;
    bandwidthLoading = true;
    bandwidthError = null;
    try {
      bandwidthData = await getSwitchBandwidth(switchId, bandwidthHours, selectedPortForBandwidth || undefined, bandwidthResolution);
    } catch (err) {
      bandwidthError = err.message;
      bandwidthData = null;
    } finally {
      bandwidthLoading = false;
    }
  }

  $: bandwidthPortOptions = [{ id: '', name: 'All ports' }, ...(ports.map(p => ({ id: p.name, name: p.name })))];

  function formatBytes(n) {
    if (n == null || n === undefined) return '—';
    if (n >= 1e12) return (n / 1e12).toFixed(2) + ' TB';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + ' KB';
    return String(n);
  }
</script>

<div class="switch-detail">
  <PageHeader title={switchData ? `Switch: ${switchData.name}` : 'Switch Details'} />
  
  {#if loading}
    <div class="content-body">
      <p>Loading...</p>
    </div>
  {:else if error}
    <div class="content-body">
      <div class="error-message">
        <p>{error}</p>
        <button class="btn-secondary" on:click={onBack}>Back to Switches</button>
      </div>
    </div>
  {:else if switchData}
    <div class="content-body">
      <div class="header-actions">
        <button class="refresh-button" on:click={handleRefresh} disabled={refreshing}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
        <button class="btn-secondary" on:click={onBack}>Back to Switches</button>
      </div>

      <!-- Switch Information -->
      <div class="card">
        <div class="card-header">
          <h2>Switch Information</h2>
        </div>
        <div class="card-body">
          <div class="info-grid">
            <div class="info-item">
              <label>Name:</label>
              <span>{switchData.name}</span>
            </div>
            <div class="info-item">
              <label>Model:</label>
              <span>{switchData.model || 'N/A'}</span>
            </div>
            <div class="info-item">
              <label>Serial Number:</label>
              <span>{switchData.serial_number || 'N/A'}</span>
            </div>
            <div class="info-item">
              <label>Firmware Version:</label>
              <span>{switchData.firmware_version || 'N/A'}</span>
            </div>
            <div class="info-item">
              <label>Port Count:</label>
              <span>{switchData.port_count || ports.length || 'N/A'}</span>
            </div>
            <div class="info-item">
              <label>Location:</label>
              <span>{getLocationName(switchData.location_id)}</span>
            </div>
            <div class="info-item">
              <label>Rack:</label>
              <span>
                {#if switchData.rack_id}
                  {getRackName(switchData.rack_id)}
                  {#if switchData.rack_unit != null}
                    {(switchData.rack_units ?? 1) > 1 ? `(U${switchData.rack_unit}–${switchData.rack_unit + (switchData.rack_units ?? 1) - 1}, ${switchData.rack_units}U)` : `(U${switchData.rack_unit})`}
                  {/if}
                {:else}
                  N/A
                {/if}
              </span>
            </div>
            <div class="info-item">
              <label>Plugin:</label>
              <span>{switchData.plugin_name || 'N/A'}</span>
            </div>
            <div class="info-item">
              <label>Status:</label>
              <span class="status-badge" class:enabled={switchData.enabled} class:disabled={!switchData.enabled}>
                {switchData.enabled ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            {#if switchData.description}
              <div class="info-item full-width">
                <label>Description:</label>
                <span>{switchData.description}</span>
              </div>
            {/if}
          </div>
        </div>
      </div>

      <!-- Ports Table -->
      <div class="card">
        <div class="card-header">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>Ports ({ports.length})</h2>
            {#if ports.length === 0}
              <button class="btn-primary" on:click={handleRegeneratePorts} disabled={regeneratingPorts}>
                {regeneratingPorts ? 'Regenerating...' : 'Regenerate Ports'}
              </button>
            {/if}
          </div>
        </div>
        <div class="card-body">
          {#if regenerateError}
            <div class="error-message" style="margin-bottom: 1rem; padding: 0.75rem; background: var(--danger-bg); color: var(--danger-text); border-radius: 0.25rem;">
              {regenerateError}
            </div>
          {/if}
          {#if regenerateSuccess}
            <div class="success-message" style="margin-bottom: 1rem; padding: 0.75rem; background: var(--success-bg); color: var(--success-text); border-radius: 0.25rem;">
              {regenerateSuccess}
            </div>
          {/if}
          {#if ports.length === 0}
            <div style="text-align: center; padding: 2rem;">
              <p style="margin-bottom: 1rem;">No ports found. Ports are populated in the background when a switch is added—refresh in a moment, or click "Regenerate Ports" to fetch them now.</p>
              <p style="margin-bottom: 1.5rem; color: var(--text-secondary); font-size: 0.9rem;">Regenerating ports may take 30–60 seconds for large switches.</p>
            </div>
          {:else}
            <div class="bulk-edit-toolbar">
              <div class="bulk-edit-fields">
                <label>
                  Physical speed
                  <input
                    type="text"
                    placeholder="e.g. 25G, 10G, 10000"
                    bind:value={bulkSpeedInput}
                  />
                </label>
                <label>
                  Description
                  <input
                    type="text"
                    placeholder="Set description for selected ports"
                    bind:value={bulkDescriptionInput}
                  />
                </label>
                <button
                  type="button"
                  class="btn-small btn-secondary"
                  on:click={applyBulkPortEdit}
                  disabled={bulkSaving}
                >
                  {bulkSaving ? 'Saving…' : 'Apply to selected ports'}
                </button>
              </div>
              {#if bulkEditError}
                <div class="error-message" style="margin-top: 0.5rem;">{bulkEditError}</div>
              {/if}
            </div>
            <table class="ports-table">
              <thead>
                <tr>
                  <th><input type="checkbox" on:change={toggleSelectAll} checked={selectedPortIds.size === ports.length} /></th>
                  <th>Port Name</th>
                  <th>Physical speed</th>
                  <th>Connected To</th>
                  <th>Cable Type</th>
                  <th>Description</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {#each ports as port}
                  <tr>
                    <td><input type="checkbox" checked={selectedPortIds.has(port.id)} on:change={(e) => toggleSelectPort(port.id, e.currentTarget.checked)} /></td>
                    <td><strong>{port.name}</strong></td>
                    <td>{formatSpeed(port.speed_mbps)}</td>
                    <td>
                      {#if port.cable_run}
                        {#if port.cable_run.other_end_device_id != null}
                          <a href={port.cable_run.other_end_type === 'server' ? `/admin/servers/${port.cable_run.other_end_device_id}` : `/admin/switches/${port.cable_run.other_end_device_id}`} class="link">
                            {port.cable_run.other_end_device_name || port.cable_run.other_end_type} – {port.cable_run.other_end_port_name || port.cable_run.other_end_port_id}
                          </a>
                        {:else}
                          <span class="text-muted">{port.cable_run.other_end_port_name || `Port #${port.cable_run.other_end_port_id}`}</span>
                        {/if}
                      {:else}
                        <span class="text-muted">Not connected</span>
                      {/if}
                    </td>
                    <td>{port.cable_run?.cable_type || 'N/A'}</td>
                    <td>{port.description || 'N/A'}</td>
                    <td>
                      {#if port.cable_run}
                        <button type="button" class="btn-small btn-danger" on:click={() => handleDisconnect(port)} title="Disconnect">
                          Disconnect
                        </button>
                      {:else}
                        <button type="button" class="btn-small btn-primary" on:click|stopPropagation={() => openConnectModal(port)} title="Connect to server port">
                          Connect
                        </button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
        </div>
      </div>

      <!-- Bandwidth (stored SNMP data) -->
      <div class="card">
        <div class="card-header">
          <h2>Bandwidth (SNMP data)</h2>
        </div>
        <div class="card-body">
          <p class="text-muted" style="margin-bottom: 0.5rem;">Stored port counters from the SNMP bandwidth poller. Run the poller service to collect data.</p>
          <p class="bandwidth-help">Each row shows traffic <strong>in that interval</strong>: bytes transferred and rate (Mbps) since the previous sample. Values never decrease.</p>
          <div class="bandwidth-controls">
            <div class="form-inline">
              <label for="bw-port">Port</label>
              <select id="bw-port" bind:value={selectedPortForBandwidth}>
                {#each bandwidthPortOptions as opt}
                  <option value={opt.id ?? ''}>{opt.name}</option>
                {/each}
              </select>
            </div>
            <div class="form-inline">
              <label for="bw-hours">Time range</label>
              <select id="bw-hours" bind:value={bandwidthHours}>
                <option value={24}>24 h</option>
                <option value={48}>48 h</option>
                <option value={168}>168 h (1 week)</option>
              </select>
            </div>
            <div class="form-inline">
              <label for="bw-resolution">Sample interval</label>
              <select id="bw-resolution" bind:value={bandwidthResolution}>
                <option value={0}>1 min (raw)</option>
                <option value={5}>5 min</option>
                <option value={15}>15 min</option>
                <option value={60}>1 hour</option>
              </select>
            </div>
            <button type="button" class="btn-primary" on:click={loadBandwidth} disabled={bandwidthLoading}>
              {bandwidthLoading ? 'Loading…' : 'Load bandwidth'}
            </button>
          </div>
          {#if bandwidthError}
            <div class="error-message" style="margin-top: 1rem; padding: 0.75rem; background: var(--danger-bg); color: var(--danger-text); border-radius: 0.25rem;">
              {bandwidthError}
            </div>
          {/if}
          {#if bandwidthData && bandwidthData.ports && bandwidthData.ports.length > 0}
            {#each bandwidthData.ports as portData}
              <div class="bandwidth-port-block">
                <h3 class="bandwidth-port-title">Port: {portData.port_identifier}</h3>
                {#if portData.samples && portData.samples.length > 0}
                  <div class="table-wrap">
                    <table class="ports-table bandwidth-table">
                      <thead>
                        <tr>
                          <th>Time (UTC)</th>
                          <th>Bytes in (interval)</th>
                          <th>Bytes out (interval)</th>
                          <th>Rate in (Mbps)</th>
                          <th>Rate out (Mbps)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {#each portData.samples.slice(-50) as sample}
                          <tr>
                            <td>{sample.sampled_at ? new Date(sample.sampled_at).toLocaleString() : '—'}</td>
                            <td>{sample.bytes_in_interval != null ? formatBytes(sample.bytes_in_interval) : '—'}</td>
                            <td>{sample.bytes_out_interval != null ? formatBytes(sample.bytes_out_interval) : '—'}</td>
                            <td>{sample.rate_in_mbps != null ? sample.rate_in_mbps.toFixed(2) : '—'}</td>
                            <td>{sample.rate_out_mbps != null ? sample.rate_out_mbps.toFixed(2) : '—'}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  </div>
                  <p class="text-muted" style="font-size: 0.85rem; margin-top: 0.5rem;">Showing last 50 samples. Total: {portData.samples.length}.</p>
                {:else}
                  <p class="text-muted">No samples in this range.</p>
                {/if}
              </div>
            {/each}
          {:else if bandwidthData && bandwidthData.ports && bandwidthData.ports.length === 0}
            <p class="text-muted" style="margin-top: 1rem;">No bandwidth data for the selected period. Ensure the SNMP bandwidth poller is running.</p>
          {/if}
        </div>
      </div>
    </div>
  {/if}

  <!-- Connect port modal (class name avoids Bootstrap .modal which uses display:none) -->
  {#if connectModalPort}
    <div class="connect-modal-backdrop" role="presentation" on:click={closeConnectModal} on:keydown={(e) => e.key === 'Escape' && closeConnectModal()}>
      <div class="connect-modal-box" role="dialog" aria-labelledby="connect-modal-title" on:click|stopPropagation>
        <h2 id="connect-modal-title">Connect {connectModalPort.name} to server port</h2>
        {#if connectError}
          <div class="error-message" style="margin-bottom: 1rem; padding: 0.75rem; background: var(--danger-bg); color: var(--danger-text); border-radius: 0.25rem;">
            {connectError}
          </div>
        {/if}
        <div class="form-group">
          <label for="connect-server">Server</label>
          <select id="connect-server" bind:value={selectedServerId} on:change={onServerChange} disabled={connecting}>
            <option value={null}>Select a server…</option>
            {#each servers as s}
              <option value={s.id}>{s.name}</option>
            {/each}
          </select>
        </div>
        {#if selectedServerId}
          <div class="form-group">
            <label for="connect-port">Server port</label>
            {#if loadingServerPorts}
              <p class="text-muted">Loading ports…</p>
            {:else}
              <select id="connect-port" bind:value={selectedServerPortId} disabled={connecting}>
                <option value={null}>Select a port…</option>
                {#each availableServerPorts as p}
                  <option value={p.id}>{p.name}{#if p.mac_address} ({p.mac_address}){/if}</option>
                {/each}
              </select>
              {#if availableServerPorts.length === 0 && selectedServerPorts.length > 0}
                <p class="text-muted">All ports on this server are already connected.</p>
              {/if}
            {/if}
          </div>
        {/if}
        <div class="connect-modal-actions">
          <button type="button" class="btn-secondary" on:click={closeConnectModal} disabled={connecting}>Cancel</button>
          <button type="button" class="btn-primary" on:click={handleConnect} disabled={connecting || !selectedServerPortId}>
            {connecting ? 'Connecting…' : 'Connect'}
          </button>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .switch-detail {
    min-height: 100vh;
  }

  .content-body {
    padding: 2rem;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .header-actions {
    display: flex;
    gap: 1rem;
    margin-bottom: 2rem;
    align-items: center;
  }

  .refresh-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
    border-radius: 0.5rem;
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
  }

  .refresh-button:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .refresh-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .refresh-button svg {
    width: 1rem;
    height: 1rem;
  }

  .btn-primary,
  .btn-secondary {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
    box-shadow: var(--shadow-sm);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
    border-color: var(--accent-color);
    color: white;
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    margin-bottom: 2rem;
    box-shadow: var(--shadow-md);
  }

  .card-header {
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-tertiary);
  }

  .card-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .card-body {
    padding: 1.5rem;
  }

  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
    gap: 1rem;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .info-item.full-width {
    grid-column: 1 / -1;
  }

  .info-item label {
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .info-item span {
    color: var(--text-primary);
  }

  .ports-table {
    width: 100%;
    border-collapse: collapse;
    background: var(--bg-primary);
  }

  .ports-table thead {
    background: var(--bg-tertiary);
  }

  .ports-table th {
    padding: 0.75rem;
    text-align: left;
    font-weight: 600;
    color: var(--text-primary);
    border-bottom: 2px solid var(--border-color);
  }

  .ports-table td {
    padding: 0.75rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
  }

  .ports-table tbody tr:hover {
    background: var(--bg-secondary);
  }

  .link {
    color: var(--accent-color);
    text-decoration: none;
  }

  .link:hover {
    text-decoration: underline;
  }

  .text-muted {
    color: var(--text-secondary);
    font-style: italic;
  }

  .error-message {
    padding: 2rem;
    text-align: center;
  }

  .error-message p {
    color: var(--danger-color);
    margin-bottom: 1rem;
  }

  .btn-small {
    padding: 0.35rem 0.6rem;
    font-size: 0.8rem;
    border-radius: 0.35rem;
    cursor: pointer;
    border: 1px solid transparent;
  }

  .btn-small.btn-primary {
    background: var(--accent-color);
    color: white;
    border-color: var(--accent-color);
  }

  .btn-small.btn-primary:hover {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .btn-small.btn-danger {
    background: var(--danger-bg);
    color: var(--danger-text);
    border-color: var(--danger-color, #c44);
  }

  .btn-small.btn-danger:hover {
    filter: brightness(1.1);
  }

  .connect-modal-backdrop {
    position: fixed;
    inset: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .connect-modal-box {
    position: relative;
    z-index: 1;
    flex-shrink: 0;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    padding: 1.5rem;
    min-width: 320px;
    max-width: 90vw;
    box-shadow: var(--shadow-md);
  }

  .connect-modal-box h2 {
    margin: 0 0 1rem 0;
    font-size: 1.15rem;
  }

  .connect-modal-box .form-group {
    margin-bottom: 1rem;
  }

  .connect-modal-box .form-group label {
    display: block;
    margin-bottom: 0.35rem;
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .connect-modal-box .form-group select {
    width: 100%;
    padding: 0.5rem;
    border-radius: 0.35rem;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .connect-modal-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    margin-top: 1.25rem;
  }

  .bandwidth-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.5rem;
  }

  .bandwidth-controls .form-inline {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .bandwidth-controls .form-inline label {
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .bandwidth-controls select {
    padding: 0.35rem 0.6rem;
    border-radius: 0.35rem;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .bandwidth-port-block {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .bandwidth-port-block:first-of-type {
    margin-top: 1rem;
    padding-top: 0;
    border-top: none;
  }

  .bandwidth-port-title {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .bandwidth-table {
    font-size: 0.875rem;
  }

  .bandwidth-table th,
  .bandwidth-table td {
    padding: 0.5rem 0.75rem;
  }

  .bandwidth-help {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }

  .bandwidth-help strong {
    color: var(--text-primary);
  }
</style>
