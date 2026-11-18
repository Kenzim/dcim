<script>
  /**
   * Line chart of power history in watts or amps, sized from its container. Wraps
   * AggregateTrafficChart with the unit formatter, a date axis for long ranges and
   * optional reference lines such as a rated or usable current.
   * series: [{ id, label, color, samples: [{ t, value, min, max }] }]
   * referenceLines: [{ value, label, color, dashed }]
   */
  import AggregateTrafficChart from './AggregateTrafficChart.svelte';
  import { formatWatts, formatAmps } from '../../lib/powerFormat.js';

  export let series = [];
  export let unit = 'W';
  export let referenceLines = [];
  export let hours = 24;
  export let minHeight = 180;
  export let emptyText = 'No power history in this range yet.';

  let width = 0;
  let height = 0;

  $: format = unit === 'A' ? formatAmps : formatWatts;
  $: empty = !series.some((s) => (s.samples || []).length > 0);
</script>

<div class="power-chart" style="min-height: {minHeight}px" bind:clientWidth={width} bind:clientHeight={height}>
  {#if width > 0 && height > 0}
    <AggregateTrafficChart
      {series}
      {width}
      {height}
      {format}
      {referenceLines}
      showBand={!empty && series.length === 1}
      showDate={hours > 24}
      {emptyText}
      ariaLabel="Power history chart"
    />
  {/if}
</div>

<style>
  .power-chart {
    width: 100%;
    height: 100%;
    position: relative;
  }
</style>
