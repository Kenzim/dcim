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
      
      // Load servers for each rack; servers can occupy multiple units (rack_units)
      for (const rack of racks) {
        try {
          const servers = await getRackServers(rack.id);
          rackServers[rack.id] = servers.reduce((acc, server) => {
            if (server.rack_unit) {
              const size = server.rack_units || 1;
              for (let u = 0; u < size; u++) {
                acc[server.rack_unit + u] = server;
              }
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

  function isStartOfServer(rackId, unit) {
    const server = rackServers[rackId]?.[unit];
    if (!server) return true;
    return server.rack_unit === unit;
  }

  function getSlotSpan(rackId, unit) {
    const server = rackServers[rackId]?.[unit];
    if (!server || !isStartOfServer(rackId, unit)) return 1;
    return server.rack_units || 1;
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
              {@const startOfServer = isStartOfServer(rack.id, unit)}
              {@const span = getSlotSpan(rack.id, unit)}
              <div
                class="rack-unit-compact"
                class:occupied={server !== null && startOfServer}
                class:continuation={server !== null && !startOfServer}
                style={startOfServer && span > 1 ? `height: ${span * 26}px; min-height: ${span * 26}px;` : (server && !startOfServer ? 'height: 0; min-height: 0; overflow: hidden; border: none; margin: 0;' : '')}
              >
                <div class="unit-number-compact">{server && !startOfServer ? '' : (span > 1 ? `${unit}-${unit + span - 1}` : unit)}</div>
                <div class="unit-content-compact">
                  {#if server && startOfServer}
                    <div class="server-slot-compact">
                      <div class="server-name-compact">{server.name}{span > 1 ? ` (${span}U)` : ''}</div>
                      <div class="server-tooltip">
                        <div><strong>IP:</strong> {server.server_ip}</div>
                        {#if server.description}
                          <div><strong>Description:</strong> {server.description}</div>
                        {/if}
                      </div>
                    </div>
                  {:else if !server}
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
        <span class="summary-value">{Object.values(rackServers).reduce((total, unitMap) => {
          const byId = {};
          for (const server of Object.values(unitMap)) {
            if (server && server.rack_unit != null) byId[server.id] = server;
          }
          const rackU = Object.values(byId).reduce((s, server) => s + (server.rack_units || 1), 0);
          return total + rackU;
        }, 0)}U</span>
      </div>
    </div>
  {/if}
</div>

<style>
  .row-view-container {
    padding: 16px 32px;
  }


  .racks-row {
    display: flex;
    gap: 16px;
    overflow-x: auto;
    padding-bottom: 16px;
    margin-bottom: 20px;
    margin-top: 0;
  }

  .rack-column {
    flex: 0 0 auto;
    min-width: 288px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 10px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  /* No rounding on any rack units */
  .rack-units-compact > .rack-unit-compact {
    border-radius: 0;
  }

  .rack-header-compact {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    padding-bottom: 8px;
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
    gap: 0;
  }

  .rack-unit-compact {
    display: flex;
    align-items: center;
    min-height: 26px;
    background: var(--bg-secondary);
    border-left: 1px solid var(--border-color);
    border-right: 1px solid var(--border-color);
    border-top: 1px solid var(--border-color);
    border-bottom: none;
    border-radius: 0;
    transition: all 0.2s ease;
    position: relative;
  }

  /* Only the last unit gets a bottom border to close the rack - must override default */
  .rack-units-compact > .rack-unit-compact:last-child {
    border-bottom: 1px solid var(--border-color) !important;
  }

  /* Occupied units get borders on all sides */
  .rack-unit-compact.occupied {
    border-left: 1px solid var(--accent-color);
    border-right: 1px solid var(--accent-color);
    border-top: 1px solid var(--accent-color);
    border-bottom: 1px solid var(--accent-color);
  }

  /* Occupied last unit keeps its bottom border - must override both default and occupied rule */
  .rack-units-compact > .rack-unit-compact.occupied:last-child {
    border-bottom: 1px solid var(--accent-color) !important;
  }

  /* Occupied units - full highlight borders on all sides, no rounding */
  .rack-unit-compact.occupied {
    background: var(--accent-bg);
    border-radius: 0;
    z-index: 1;
    position: relative;
  }

  .rack-unit-compact:hover:not(.occupied) {
    background: var(--bg-tertiary);
  }

  .unit-number-compact {
    width: 35px;
    padding: 3px 6px;
    font-weight: 600;
    font-size: 11px;
    color: var(--text-secondary);
    text-align: center;
    border-right: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .rack-unit-compact.occupied .unit-number-compact {
    color: var(--accent-color);
    font-weight: 700;
  }

  .rack-unit-compact.continuation {
    border: none;
    background: transparent;
  }

  .rack-unit-compact.continuation .unit-number-compact,
  .rack-unit-compact.continuation .unit-content-compact {
    visibility: hidden;
  }

  .unit-content-compact {
    flex: 1;
    padding: 3px 8px;
  }

  .server-slot-compact {
    position: relative;
  }

  .server-name-compact {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 13px;
    line-height: 1.3;
    cursor: help;
  }

  .server-tooltip {
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 8px;
    padding: 8px 12px;
    background: rgba(0, 0, 0, 0.9);
    color: white;
    border-radius: 6px;
    font-size: 11px;
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease;
    z-index: 1000;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  }

  .server-tooltip::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    border: 6px solid transparent;
    border-top-color: rgba(0, 0, 0, 0.9);
  }

  .server-slot-compact:hover .server-tooltip {
    opacity: 1;
  }

  .empty-slot-compact {
    min-height: 0;
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
    color: var(--danger-color);
  }
</style>
