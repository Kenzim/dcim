<script>
  import PageHeader from './PageHeader.svelte';
  import { getRack, getRackServers, getServers } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let rackId;
  export let onBack;

  let rack = null;
  let servers = [];
  let allServers = [];
  let loading = true;
  let error = null;

  // Map of rack unit to server
  let rackMap = {};

  onMount(async () => {
    await loadRackData();
  });

  async function loadRackData() {
    try {
      loading = true;
      error = null;
      [rack, servers] = await Promise.all([
        getRack(rackId),
        getRackServers(rackId)
      ]);
      
      // Build map of rack units to servers
      rackMap = {};
      servers.forEach(server => {
        if (server.rack_unit) {
          rackMap[server.rack_unit] = server;
        }
      });
    } catch (err) {
      error = err.message;
      console.error('Failed to load rack data:', err);
    } finally {
      loading = false;
    }
  }

  function getServerAtUnit(unit) {
    return rackMap[unit] || null;
  }

  function getUnitHeight(server) {
    // Default to 1U if not specified
    // In a real implementation, you might want to store server height in the database
    return 1;
  }
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
      <div class="rack-units">
        {#each Array(rack.units) as _, i}
          {@const unit = rack.units - i}
          {@const server = getServerAtUnit(unit)}
          <div class="rack-unit" class:occupied={server !== null}>
            <div class="unit-number">{unit}</div>
            <div class="unit-content">
              {#if server}
                <div class="server-slot">
                  <div class="server-name">{server.name}</div>
                  <div class="server-details">
                    <span>{server.server_ip}</span>
                    {#if server.description}
                      <span class="server-description">{server.description}</span>
                    {/if}
                  </div>
                </div>
              {:else}
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
        <span class="summary-value">{servers.length}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Available:</span>
        <span class="summary-value">{rack.units - servers.length}U</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Utilization:</span>
        <span class="summary-value">{Math.round((servers.length / rack.units) * 100)}%</span>
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
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .rack-units {
    display: flex;
    flex-direction: column-reverse;
    gap: 2px;
    max-height: 600px;
    overflow-y: auto;
  }

  .rack-unit {
    display: flex;
    align-items: center;
    min-height: 40px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    transition: all 0.2s ease;
  }

  .rack-unit:hover {
    background: var(--bg-tertiary);
  }

  .rack-unit.occupied {
    background: var(--accent-bg);
    border-color: var(--accent-color);
  }

  .unit-number {
    width: 50px;
    padding: 8px 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-align: center;
    border-right: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .rack-unit.occupied .unit-number {
    color: var(--accent-color);
    font-weight: 700;
  }

  .unit-content {
    flex: 1;
    padding: 8px 12px;
  }

  .server-slot {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .server-name {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 14px;
  }

  .server-details {
    display: flex;
    gap: 12px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .server-description {
    font-style: italic;
  }

  .empty-slot {
    color: var(--text-secondary);
    font-size: 14px;
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
    color: #ef4444;
  }
</style>
