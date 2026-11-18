<script>
  /**
   * Power history for one scope (PDU, feed, rack, server). The parent passes a loader
   * (hours) => response and a builder (response) => charts, where a chart is
   * { title, unit: 'W' | 'A', series, referenceLines }.
   * The panel owns the range selector, loading and error states and the energy figure.
   */
  import { onMount } from 'svelte';
  import Spinner from './ui/Spinner.svelte';
  import Alert from './ui/Alert.svelte';
  import PowerChart from './ui/PowerChart.svelte';
  import { POWER_RANGES, formatKwh, energyNote } from '../lib/powerFormat.js';

  export let loader;
  export let build;
  export let title = 'Power history';
  export let reloadKey = null;
  export let chartHeight = 220;

  let hours = 24;
  let loading = false;
  let error = null;
  let data = null;
  let charts = [];
  let mounted = false;
  let requestId = 0;

  onMount(() => {
    mounted = true;
    load();
  });

  $: if (mounted) reloadKey, load();

  async function load() {
    const mine = ++requestId;
    loading = true;
    error = null;
    try {
      const response = await loader(Number(hours));
      if (mine !== requestId) return;
      data = response;
      charts = build(response) || [];
    } catch (e) {
      if (mine !== requestId) return;
      error = e.message || 'Failed to load power history.';
      data = null;
      charts = [];
    } finally {
      if (mine === requestId) loading = false;
    }
  }

  $: unavailable = data && data.available === false;
</script>

<div class="history-panel">
  <div class="history-header">
    <h3>{title}</h3>
    <select class="range-select" bind:value={hours} on:change={load} aria-label="History range">
      {#each POWER_RANGES as r}
        <option value={r.hours}>{r.label}</option>
      {/each}
    </select>
  </div>

  {#if loading && !data}
    <div class="history-state"><Spinner size="small" /> <span>Loading power history…</span></div>
  {:else if error}
    <Alert type="warning">{error}</Alert>
  {:else if data}
    {#if data.reason}
      <div class="history-note" class:history-note-warn={unavailable}>{data.reason}</div>
    {/if}
    {#if !unavailable}
      {#if data.energy && data.energy.kwh != null}
        <div class="energy-line">
          <strong>{formatKwh(data.energy.kwh)}</strong> used in this range
          <span class="energy-note">({energyNote(data.energy)})</span>
        </div>
      {/if}
      {#each charts as chart (chart.title)}
        <div class="chart-block">
          <div class="chart-title">{chart.title}</div>
          <div class="chart-box" style="height: {chartHeight}px">
            <PowerChart
              series={chart.series}
              unit={chart.unit}
              referenceLines={chart.referenceLines || []}
              hours={data.hours}
              minHeight={chartHeight - 20}
            />
          </div>
          {#if chart.series.length > 1}
            <div class="legend">
              {#each chart.series as s (s.id)}
                <span class="legend-item"><span class="legend-dot" style="background: {s.color}" />{s.label}</span>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
      <div class="resolution-note">
        {data.resolution === 'raw' ? 'Every poll' : data.resolution === '5m' ? 'Five-minute averages' : 'Hourly averages'}, shown in
        {Math.round(data.bucket_seconds / 60)}-minute steps. The shaded band is the minimum to maximum.
      </div>
    {/if}
  {/if}
</div>

<style>
  .history-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .history-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .history-header h3 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .range-select {
    padding: 5px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 13px;
    font-family: inherit;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
  }

  .history-state {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 24px 0;
    color: var(--text-secondary);
    font-size: 14px;
  }

  .history-note {
    font-size: 13px;
    color: var(--text-secondary);
  }

  .history-note-warn {
    padding: 14px;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
    color: var(--text-tertiary);
  }

  .energy-line {
    font-size: 14px;
    color: var(--text-primary);
  }

  .energy-note,
  .resolution-note {
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .chart-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 4px;
  }

  .chart-box {
    width: 100%;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 6px;
    font-size: 12.5px;
    color: var(--text-secondary);
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
</style>
