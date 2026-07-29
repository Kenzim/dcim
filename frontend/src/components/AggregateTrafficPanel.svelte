<script>
  import { onMount } from 'svelte';
  import Spinner from './ui/Spinner.svelte';
  import Alert from './ui/Alert.svelte';
  import AggregateTrafficChart from './ui/AggregateTrafficChart.svelte';
  import { getAggregateBandwidth } from '../lib/api.js';

  const SERIES_META = {
    in: { color: '#0891b2', label: 'Traffic in' },
    out: { color: '#f97316', label: 'Traffic out' },
  };

  const RANGE_OPTIONS = [
    { hours: 6, label: '6h' },
    { hours: 24, label: '24h' },
    { hours: 72, label: '3d' },
    { hours: 168, label: '7d' },
  ];

  let hoursRange = 24;
  let loading = false;
  let error = null;
  let series = [];
  let trackedPorts = 0;

  let chartWidth = 960;
  let chartHeight = 280;

  onMount(loadSeries);

  async function loadSeries() {
    loading = true;
    error = null;
    try {
      const data = await getAggregateBandwidth(Number(hoursRange));
      trackedPorts = data.tracked_ports || 0;
      const previousVisible = new Map(series.map((s) => [s.id, s.visible]));
      series = (data.series || []).map((s) => {
        const meta = SERIES_META[s.id] || { color: '#64748b', label: s.label || s.id };
        return {
          id: s.id,
          label: meta.label,
          color: meta.color,
          samples: s.samples || [],
          visible: previousVisible.has(s.id) ? previousVisible.get(s.id) : true,
        };
      });
    } catch (e) {
      error = e.message || 'Failed to load traffic data.';
      series = [];
    } finally {
      loading = false;
    }
  }

  function toggleSeries(id) {
    series = series.map((s) => (s.id === id ? { ...s, visible: !s.visible } : s));
  }

  $: hasSamples = series.some((s) => (s.samples || []).length > 0);
</script>

<div class="traffic-panel">
  <div class="traffic-header">
    <div class="traffic-title-block">
      <h2>Aggregate traffic</h2>
      <p class="traffic-sub">
        {#if trackedPorts > 0}
          Sum of {trackedPorts} monitored server port{trackedPorts === 1 ? '' : 's'}
        {:else}
          Monitored server ports only
        {/if}
      </p>
    </div>
    <div class="traffic-controls">
      <select class="control-select" bind:value={hoursRange} on:change={loadSeries} aria-label="Time range">
        {#each RANGE_OPTIONS as opt}
          <option value={opt.hours}>{opt.label}</option>
        {/each}
      </select>
    </div>
  </div>

  {#if loading}
    <div class="traffic-loading"><Spinner size="small" /> <span>Loading traffic…</span></div>
  {:else if error}
    <Alert type="warning">{error}</Alert>
  {:else if !hasSamples}
    <div class="traffic-empty">
      No bandwidth history yet for monitored server ports. Enable “Monitor bandwidth” on a cabled port and run the SNMP poller.
    </div>
  {:else}
    <div class="chart-wrap" bind:clientWidth={chartWidth} bind:clientHeight={chartHeight}>
      <AggregateTrafficChart {series} width={chartWidth} height={chartHeight} />
    </div>
    <div class="traffic-legend">
      {#each series as s (s.id)}
        <button
          type="button"
          class="legend-chip"
          class:legend-dim={!s.visible}
          style="--chip-color: {s.color}"
          on:click={() => toggleSeries(s.id)}
        >
          <span class="legend-dot" />
          {s.label}
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .traffic-panel {
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1;
    min-height: 0;
  }

  .traffic-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  .traffic-title-block h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .traffic-sub {
    margin: 4px 0 0;
    font-size: 12.5px;
    color: var(--text-secondary);
  }

  .traffic-controls {
    display: flex;
    gap: 8px;
  }

  .control-select {
    padding: 6px 30px 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 13px;
    font-family: inherit;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2364748b' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 10px;
  }

  .control-select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }

  .traffic-loading {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-secondary);
    font-size: 14px;
    padding: 40px 0;
    justify-content: center;
    flex: 1;
  }

  .traffic-empty {
    color: var(--text-tertiary);
    font-size: 14px;
    padding: 40px 0;
    text-align: center;
    flex: 1;
  }

  .chart-wrap {
    flex: 1;
    min-height: 200px;
    position: relative;
  }

  .traffic-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    flex-shrink: 0;
  }

  .legend-chip {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: 14px;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    color: var(--text-primary);
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s ease, border-color 0.15s ease;
  }

  .legend-chip:hover {
    border-color: var(--chip-color);
  }

  .legend-chip .legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--chip-color);
    flex-shrink: 0;
  }

  .legend-chip.legend-dim {
    opacity: 0.45;
  }
</style>
