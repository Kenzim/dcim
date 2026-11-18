<script>
  /** Dashboard panel: total PDU power over 24 hours or 7 days, with current draw and energy used. */
  import { onMount } from 'svelte';
  import Spinner from './ui/Spinner.svelte';
  import Alert from './ui/Alert.svelte';
  import PowerChart from './ui/PowerChart.svelte';
  import { getPowerHistory } from '../lib/api.js';
  import { formatWatts, formatKwh, energyNote } from '../lib/powerFormat.js';

  const RANGES = [
    { hours: 24, label: '24h' },
    { hours: 168, label: '7d' },
  ];

  let hours = 24;
  let loading = false;
  let error = null;
  let data = null;

  onMount(load);

  async function load() {
    loading = true;
    error = null;
    try {
      data = await getPowerHistory(Number(hours));
    } catch (e) {
      error = e.message || 'Failed to load power data.';
      data = null;
    } finally {
      loading = false;
    }
  }

  $: samples = data?.metrics?.power_w || [];
  $: series = [{ id: 'power', label: 'Total power', color: '#7c3aed', samples, visible: true }];
</script>

<div class="power-panel">
  <div class="power-header">
    <div>
      <h2>Power draw</h2>
      <p class="power-sub">
        {#if data && data.pdu_count > 0}
          Sum of {data.pdu_count} PDU{data.pdu_count === 1 ? '' : 's'}
        {:else}
          All PDUs
        {/if}
      </p>
    </div>
    <select class="control-select" bind:value={hours} on:change={load} aria-label="Power range">
      {#each RANGES as r}
        <option value={r.hours}>{r.label}</option>
      {/each}
    </select>
  </div>

  {#if loading && !data}
    <div class="power-state"><Spinner size="small" /> <span>Loading power…</span></div>
  {:else if error}
    <Alert type="warning">{error}</Alert>
  {:else if data && data.pdu_count === 0}
    <div class="power-state">No PDUs added yet. Add one on the PDUs page to see power draw.</div>
  {:else if data}
    <div class="power-figures">
      <div class="figure">
        <span class="figure-label">Now</span>
        <span class="figure-value">{data.current_power_w != null ? formatWatts(data.current_power_w) : 'n/a'}</span>
      </div>
      <div class="figure" title={energyNote(data.energy)}>
        <span class="figure-label">Energy used ({hours === 24 ? '24 hours' : '7 days'})</span>
        <span class="figure-value">{data.energy?.kwh != null ? formatKwh(data.energy.kwh) : 'n/a'}</span>
      </div>
    </div>
    <div class="power-chart-wrap">
      <PowerChart {series} unit="W" hours={data.hours} minHeight={200} emptyText="No power history yet. The poller records a sample each minute." />
    </div>
  {/if}
</div>

<style>
  .power-panel {
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1;
    min-height: 0;
  }

  .power-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .power-header h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .power-sub {
    margin: 4px 0 0;
    font-size: 12.5px;
    color: var(--text-secondary);
  }

  .control-select {
    padding: 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 13px;
    font-family: inherit;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
  }

  .power-state {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    flex: 1;
    padding: 40px 0;
    color: var(--text-tertiary);
    font-size: 14px;
    text-align: center;
  }

  .power-figures {
    display: flex;
    gap: 28px;
    flex-wrap: wrap;
  }

  .figure {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .figure-label {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .figure-value {
    font-size: 22px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .power-chart-wrap {
    flex: 1;
    min-height: 200px;
    position: relative;
  }
</style>
