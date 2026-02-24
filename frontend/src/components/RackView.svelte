<script>
  import PageHeader from './PageHeader.svelte';
  import { getRack, getRackServers, getSwitches } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let rackId;
  export let onBack;

  let rack = null;
  let servers = [];
  let switches = [];
  let loading = true;
  let error = null;

  // Map of rack unit to device: { type: 'server'|'switch', data: {...} }
  let rackMap = {};

  onMount(async () => {
    await loadRackData();
  });

  async function loadRackData() {
    try {
      loading = true;
      error = null;
      [rack, servers, switches] = await Promise.all([
        getRack(rackId),
        getRackServers(rackId),
        getSwitches(null, rackId)
      ]);
      
      // Build map of rack units to devices. Servers can occupy multiple units (rack_units); fill each unit they occupy.
      rackMap = {};
      servers.forEach(server => {
        if (server.rack_unit) {
          const size = server.rack_units || 1;
          for (let u = 0; u < size; u++) {
            rackMap[server.rack_unit + u] = { type: 'server', data: server };
          }
        }
      });
      (switches || []).forEach(sw => {
        if (sw.rack_unit) {
          const size = sw.rack_units || 1;
          for (let u = 0; u < size; u++) {
            rackMap[sw.rack_unit + u] = { type: 'switch', data: sw };
          }
        }
      });
    } catch (err) {
      error = err.message;
      console.error('Failed to load rack data:', err);
    } finally {
      loading = false;
    }
  }

  function getDeviceAtUnit(unit) {
    return rackMap[unit] || null;
  }

  /** True if this unit is the top (start) of the device, so we render the slot and span. */
  function isStartOfDevice(unit) {
    const device = rackMap[unit];
    if (!device) return true;
    if (device.type === 'server') return device.data.rack_unit === unit;
    if (device.type === 'switch') return device.data.rack_unit === unit;
    return true;
  }

  /** Number of units this slot spans (for servers/switches that occupy multiple U). */
  function getSlotSpan(unit) {
    const device = rackMap[unit];
    if (!device || !isStartOfDevice(unit)) return 1;
    if (device.type === 'server') return device.data.rack_units || 1;
    if (device.type === 'switch') return device.data.rack_units || 1;
    return 1;
  }

  /** Total occupied U for summary (servers and switches count their rack_units). */
  $: occupiedUnits = rack ? servers.reduce((sum, s) => sum + (s.rack_units || 1), 0) + (switches || []).reduce((sum, sw) => sum + (sw.rack_units || 1), 0) : 0;
</script>

<PageHeader title={rack ? `Rack: ${rack.name}` : 'Rack View'} />

<div class="rack-view-container">
  {#if loading}
    <div class="loading">Loading rack...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if !rack}
    <div class="empty-state">Rack not found</div>
  {:else}
    <div class="rack-view-header">
      <div class="rack-info">
        <h2>{rack.name}</h2>
        <p class="rack-meta">{rack.units}U Rack</p>
        {#if rack.description}
          <p class="rack-description">{rack.description}</p>
        {/if}
      </div>
      {#if onBack}
        <button class="btn-secondary" on:click={onBack}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="width: 18px; height: 18px; margin-right: 8px;">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          Back
        </button>
      {/if}
    </div>

    <div class="rack-visualization">
      <!-- Units count down from top: 42 at top, 1 at bottom -->
      <div class="rack-units">
        {#each Array(rack.units) as _, i}
          {@const unit = rack.units - i}
          {@const device = getDeviceAtUnit(unit)}
          {@const startOfDevice = isStartOfDevice(unit)}
          {@const span = getSlotSpan(unit)}
          <div
            class="rack-unit"
            class:occupied={device !== null && startOfDevice}
            class:switch-slot={device?.type === 'switch'}
            class:continuation={device !== null && !startOfDevice}
            style={startOfDevice && span > 1 ? `height: ${span * 24}px; min-height: ${span * 24}px;` : (device && !startOfDevice ? 'height: 0; min-height: 0; overflow: hidden; border: none; margin: -1px 0 0 0;' : '')}
          >
            <div class="unit-number">
              {#if device && !startOfDevice}
                <!-- continuation: no number -->
              {:else}
                {span > 1 ? `${unit}-${unit + span - 1}` : unit}
              {/if}
            </div>
            <div class="unit-content">
              {#if device && startOfDevice}
                {#if device.type === 'server'}
                  <a href="/admin/servers/{device.data.id}" class="device-slot server-slot">
                    <span class="device-name">{device.data.name}{span > 1 ? ` (${span}U)` : ''}</span>
                    <div class="device-tooltip">
                      <div><strong>Server</strong>{#if span > 1} <span>({span}U)</span>{/if}</div>
                      <div><strong>IP:</strong> {device.data.server_ip || 'N/A'}</div>
                      {#if device.data.description}
                        <div><strong>Description:</strong> {device.data.description}</div>
                      {/if}
                    </div>
                  </a>
                {:else}
                  <a href="/admin/switches/{device.data.id}" class="device-slot switch-slot">
                    <span class="device-name">{device.data.name}{span > 1 ? ` (${span}U)` : ''}</span>
                    <div class="device-tooltip">
                      <div><strong>Switch</strong>{#if span > 1} <span>({span}U)</span>{/if}</div>
                      {#if device.data.model}
                        <div><strong>Model:</strong> {device.data.model}</div>
                      {/if}
                      {#if device.data.description}
                        <div><strong>Description:</strong> {device.data.description}</div>
                      {/if}
                    </div>
                  </a>
                {/if}
              {:else if !device}
                <div class="empty-slot">Empty</div>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </div>

    <div class="rack-summary">
      <div class="summary-item">
        <span class="summary-label">Total Units:</span>
        <span class="summary-value">{rack.units}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Occupied:</span>
        <span class="summary-value">{occupiedUnits}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Available:</span>
        <span class="summary-value">{rack.units - occupiedUnits}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Utilization:</span>
        <span class="summary-value">{rack.units ? Math.round((occupiedUnits / rack.units) * 100) : 0}%</span>
      </div>
    </div>
  {/if}
</div>

<style>
  .rack-view-container {
    padding: 32px;
  }

  .rack-view-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 24px;
  }

  .rack-info h2 {
    margin: 0 0 8px 0;
    font-size: 28px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .rack-meta {
    margin: 0 0 8px 0;
    font-size: 16px;
    color: var(--text-secondary);
  }

  .rack-description {
    margin: 0;
    font-size: 14px;
    color: var(--text-secondary);
  }

  .btn-secondary {
    display: flex;
    align-items: center;
    padding: 10px 20px;
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-secondary:hover {
    background: var(--accent-color);
    color: white;
  }

  .rack-visualization {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 24px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    /* 30% wider, 10% taller; aligned far left */
    max-width: 416px;
    margin-left: 0;
    margin-right: auto;
  }

  .rack-units {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .rack-unit {
    display: flex;
    align-items: center;
    min-height: 24px;
    height: 24px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    transition: background 0.15s ease, border-color 0.15s ease;
    position: relative;
  }

  .rack-unit:hover {
    background: var(--bg-tertiary);
  }

  .rack-unit.occupied {
    background: var(--accent-bg);
    border-color: var(--accent-color);
  }

  .unit-number {
    width: 40px;
    min-width: 40px;
    padding: 0 6px;
    font-weight: 600;
    font-size: 14px;
    line-height: 24px;
    color: var(--text-secondary);
    text-align: center;
    border-right: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .rack-unit.occupied .unit-number {
    color: var(--accent-color);
    font-weight: 700;
  }

  .rack-unit.continuation {
    border: none;
    background: transparent;
  }

  .rack-unit.continuation .unit-number,
  .rack-unit.continuation .unit-content {
    visibility: hidden;
  }

  .unit-content {
    flex: 1;
    padding: 0 10px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-slot {
    position: relative;
    display: block;
    text-decoration: none;
    color: inherit;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .device-slot.server-slot {
    color: var(--text-primary);
  }

  .device-slot.switch-slot .device-name {
    color: var(--info-color);
  }

  .device-name {
    font-weight: 600;
    font-size: 15px;
    line-height: 24px;
    cursor: help;
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .device-tooltip {
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 4px;
    padding: 6px 10px;
    background: rgba(0, 0, 0, 0.9);
    color: white;
    border-radius: 4px;
    font-size: 11px;
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease;
    z-index: 1000;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  }

  .device-tooltip::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    border: 6px solid transparent;
    border-top-color: rgba(0, 0, 0, 0.9);
  }

  .device-slot:hover .device-tooltip {
    opacity: 1;
  }

  .rack-unit.switch-slot.occupied {
    border-color: var(--info-color);
  }

  .empty-slot {
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 24px;
    font-style: italic;
  }

  .rack-summary {
    display: flex;
    gap: 32px;
    padding: 20px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    flex-wrap: wrap;
  }

  .summary-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .summary-label {
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .summary-value {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }
</style>
