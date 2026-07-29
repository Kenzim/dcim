<script>
  import { onMount } from 'svelte';
  import Spinner from './ui/Spinner.svelte';
  import Alert from './ui/Alert.svelte';
  import AggregateTrafficChart from './ui/AggregateTrafficChart.svelte';
  import { getServerGroup, getSwitchBandwidth, getServerBandwidth } from '../lib/api.js';

  // Already-loaded lists from the parent dashboard — no duplicate fetching.
  export let locations = [];
  export let switches = [];
  export let serverGroups = [];

  const PALETTE = [
    '#0891b2',
    '#f97316',
    '#22c55e',
    '#a855f7',
    '#ef4444',
    '#3b82f6',
    '#eab308',
    '#14b8a6',
    '#ec4899',
    '#64748b',
  ];

  const RANGE_OPTIONS = [
    { hours: 6, label: '6h' },
    { hours: 24, label: '24h' },
    { hours: 72, label: '3d' },
    { hours: 168, label: '7d' },
  ];

  let groupBy = 'location'; // 'location' | 'group'
  let hoursRange = 24;
  let loading = false;
  let error = null;
  let series = [];

  let chartWidth = 960;
  let chartHeight = 280;

  const groupMembersCache = new Map();

  onMount(loadSeries);

  function bucketSpanMs(hours) {
    const totalMs = hours * 3600 * 1000;
    const targetPoints = 96;
    return Math.max(60_000, Math.round(totalMs / targetPoints));
  }

  function aggregateFromResults(results, spanMs) {
    const buckets = new Map();
    for (const res of results) {
      if (!res || res.status !== 'fulfilled' || !res.value) continue;
      for (const port of res.value.ports || []) {
        for (const s of port.samples || []) {
          if (!s.sampled_at) continue;
          const t = new Date(s.sampled_at).getTime();
          if (Number.isNaN(t)) continue;
          const bucketT = Math.floor(t / spanMs) * spanMs;
          const val = (s.rate_in_mbps || 0) + (s.rate_out_mbps || 0);
          buckets.set(bucketT, (buckets.get(bucketT) || 0) + val);
        }
      }
    }
    return [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([t, value]) => ({ t, value }));
  }

  async function buildLocationSeries(spanMs) {
    const candidates = locations.filter((loc) => switches.some((sw) => sw.location_id === loc.id));
    return Promise.all(
      candidates.map(async (loc) => {
        const locSwitches = switches.filter((sw) => sw.location_id === loc.id);
        const results = await Promise.allSettled(locSwitches.map((sw) => getSwitchBandwidth(sw.id, hoursRange)));
        return { id: `loc-${loc.id}`, label: loc.name, samples: aggregateFromResults(results, spanMs) };
      })
    );
  }

  async function buildGroupSeries(spanMs) {
    const candidates = serverGroups.filter((g) => (g.server_count || 0) > 0);
    return Promise.all(
      candidates.map(async (group) => {
        let memberIds = groupMembersCache.get(group.id);
        if (!memberIds) {
          const detail = await getServerGroup(group.id).catch(() => null);
          memberIds = (detail?.servers || []).map((s) => s.id);
          groupMembersCache.set(group.id, memberIds);
        }
        const results = await Promise.allSettled(memberIds.map((id) => getServerBandwidth(id, hoursRange)));
        return { id: `grp-${group.id}`, label: group.name, samples: aggregateFromResults(results, spanMs) };
      })
    );
  }

  async function loadSeries() {
    loading = true;
    error = null;
    try {
      const spanMs = bucketSpanMs(hoursRange);
      const built = groupBy === 'location' ? await buildLocationSeries(spanMs) : await buildGroupSeries(spanMs);
      applySeries(built);
    } catch (e) {
      error = e.message || 'Failed to load traffic data.';
    } finally {
      loading = false;
    }
  }

  function applySeries(built) {
    const withData = built.filter((b) => b.samples.length > 0);
    const previousVisible = new Map(series.map((s) => [s.id, s.visible]));
    series = withData
      .map((b, idx) => ({
        ...b,
        color: PALETTE[idx % PALETTE.length],
        visible: previousVisible.has(b.id) ? previousVisible.get(b.id) : true,
        total: b.samples.reduce((sum, s) => sum + s.value, 0),
      }))
      .sort((a, b) => b.total - a.total);
  }

  function toggleSeries(id) {
    series = series.map((s) => (s.id === id ? { ...s, visible: !s.visible } : s));
  }
</script>

<div class="traffic-panel">
  <div class="traffic-header">
    <h2>Aggregate traffic</h2>
    <div class="traffic-controls">
      <select class="control-select" bind:value={groupBy} on:change={loadSeries} aria-label="Group traffic by">
        <option value="location">By location</option>
        <option value="group">By server group</option>
      </select>
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
  {:else if series.length === 0}
    <div class="traffic-empty">
      No bandwidth history yet for {groupBy === 'location' ? 'any location' : 'any server group'}.
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

  .traffic-header h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
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
