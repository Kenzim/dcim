<script>
  import PageHeader from './PageHeader.svelte';
  import { getRacks, getRackServers, getLocations } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { onMount } from 'svelte';

  export let locationId;
  export let row;
  export let onBack;

  let racks = [];
  let rackServers = {};
  let location = null;
  let loading = true;
  let error = null;

  onMount(async () => {
    await loadRowData();
  });

  async function loadRowData() {
    try {
      loading = true;
      error = null;
      const [racksData, locationsData] = await Promise.all([
        getRacks(Number(locationId), Number(row)),
        getLocations()
      ]);
      
      racks = racksData.sort((a, b) => (a.row_position || 0) - (b.row_position || 0));
      location = locationsData.find(l => l.id === Number(locationId));
      
      // Load servers for each rack
      for (const rack of racks) {
        try {
          const servers = await getRackServers(rack.id);
          rackServers[rack.id] = servers.reduce((acc, server) => {
            if (server.rack_unit) {
              acc[server.rack_unit] = server;
            }
            return acc;
          }, {});
        } catch (err) {
          console.error(`Failed to load servers for rack ${rack.id}:`, err);
          rackServers[rack.id] = {};
        }
      }
    } catch (err) {
      error = err.message;
      console.error('Failed to load row data:', err);
    } finally {
      loading = false;
    }
  }

  function getServerAtUnit(rackId, unit) {
    return rackServers[rackId]?.[unit] || null;
  }

  function getMaxUnits() {
    return Math.max(...racks.map(r => r.units), 42);
  }
</script>

<PageHeader title={location ? `Row ${row} - ${location.name}` : `Row ${row}`} />

<div class="row-view-container">
  {#if loading}
    <div class="loading">Loading row...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if racks.length === 0}
    <div class="empty-state">No racks found in this row</div>
  {:else}
    <div class="row-view-header">
      <div class="row-info">
        <h2>Row {row}</h2>
        {#if location}
          <p class="location-name">{location.name}</p>
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

    <div class="racks-row">
      {#each racks as rack}
        <div class="rack-column">
          <div class="rack-header-compact">
            <h3>{rack.name}</h3>
            {#if rack.row_position !== null && rack.row_position !== undefined}
              <span class="position-badge">Pos {rack.row_position}</span>
            {/if}
          </div>
          <div class="rack-units-compact">
            {#each Array(rack.units) as _, i}
              {@const unit = rack.units - i}
              {@const server = getServerAtUnit(rack.id, unit)}
              <div class="rack-unit-compact" class:occupied={server !== null}>
                <div class="unit-number-compact">{unit}</div>
                <div class="unit-content-compact">
                  {#if server}
                    <div class="server-slot-compact">
                      <div class="server-name-compact">{server.name}</div>
                      <div class="server-ip-compact">{server.server_ip}</div>
                    </div>
                  {:else}
                    <div class="empty-slot-compact"></div>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/each}
    </div>

    <div class="row-summary">
      <div class="summary-item">
        <span class="summary-label">Racks:</span>
        <span class="summary-value">{racks.length}</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Total Units:</span>
        <span class="summary-value">{racks.reduce((sum, r) => sum + r.units, 0)}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Occupied:</span>
        <span class="summary-value">{Object.values(rackServers).reduce((sum, servers) => sum + Object.keys(servers).length, 0)}U</span>
      </div>
    </div>
  {/if}
</div>

<style>
  .row-view-container {
    padding: 32px;
  }

  .row-view-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 24px;
  }

  .row-info h2 {
    margin: 0 0 8px 0;
    font-size: 28px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .location-name {
    margin: 0;
    font-size: 16px;
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

  .racks-row {
    display: flex;
    gap: 16px;
    overflow-x: auto;
    padding-bottom: 16px;
    margin-bottom: 24px;
  }

  .rack-column {
    flex: 0 0 auto;
    min-width: 200px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .rack-header-compact {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border-color);
  }

  .rack-header-compact h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .position-badge {
    padding: 2px 8px;
    background: var(--accent-color);
    color: white;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
  }

  .rack-units-compact {
    display: flex;
    flex-direction: column-reverse;
    gap: 1px;
    max-height: 500px;
    overflow-y: auto;
  }

  .rack-unit-compact {
    display: flex;
    align-items: center;
    min-height: 24px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 2px;
    transition: all 0.2s ease;
  }

  .rack-unit-compact:hover {
    background: var(--bg-tertiary);
  }

  .rack-unit-compact.occupied {
    background: var(--accent-bg);
    border-color: var(--accent-color);
  }

  .unit-number-compact {
    width: 30px;
    padding: 4px 6px;
    font-weight: 600;
    font-size: 10px;
    color: var(--text-secondary);
    text-align: center;
    border-right: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .rack-unit-compact.occupied .unit-number-compact {
    color: var(--accent-color);
    font-weight: 700;
  }

  .unit-content-compact {
    flex: 1;
    padding: 4px 6px;
  }

  .server-slot-compact {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .server-name-compact {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 11px;
    line-height: 1.2;
  }

  .server-ip-compact {
    font-size: 9px;
    color: var(--text-secondary);
    line-height: 1.2;
  }

  .empty-slot-compact {
    height: 16px;
  }

  .row-summary {
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
    color: #ef4444;
  }
</style>
