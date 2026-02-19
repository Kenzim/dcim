<script>
  /**
   * Simple SVG-based traffic graph for rate_in_mbps / rate_out_mbps over time.
   * samples: [{ sampled_at, rate_in_mbps, rate_out_mbps }]
   */
  export let samples = [];
  export let width = 600;
  export let height = 180;
  export let showLegend = true;
  export let graphId = 'default';

  $: chartSamples = (samples || []).slice(-120);
  $: maxRate = Math.max(
    1,
    ...chartSamples.flatMap(s => [
      s.rate_in_mbps ?? 0,
      s.rate_out_mbps ?? 0
    ])
  );
  $: padding = { top: 12, right: 12, bottom: 24, left: 48 };
  $: chartWidth = width - padding.left - padding.right;
  $: chartHeight = height - padding.top - padding.bottom;

  function x(i) {
    if (chartSamples.length <= 1) return padding.left;
    return padding.left + (i / Math.max(1, chartSamples.length - 1)) * chartWidth;
  }
  function yIn(v) {
    return padding.top + chartHeight - (v / maxRate) * chartHeight;
  }
  function yOut(v) {
    return padding.top + chartHeight - (v / maxRate) * chartHeight;
  }

  $: pathIn = chartSamples.length >= 2
    ? chartSamples
        .map((s, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${yIn(s.rate_in_mbps ?? 0)}`)
        .join(' ')
    : '';
  $: pathOut = chartSamples.length >= 2
    ? chartSamples
        .map((s, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${yOut(s.rate_out_mbps ?? 0)}`)
        .join(' ')
    : '';
  $: areaIn = chartSamples.length >= 2
    ? pathIn + ` L ${x(chartSamples.length - 1)} ${padding.top + chartHeight} L ${padding.left} ${padding.top + chartHeight} Z`
    : '';
  $: areaOut = chartSamples.length >= 2
    ? pathOut + ` L ${x(chartSamples.length - 1)} ${padding.top + chartHeight} L ${padding.left} ${padding.top + chartHeight} Z`
    : '';
</script>

<div class="traffic-graph">
  <svg width={width} height={height} viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">
    <defs>
      <linearGradient id="grad-in-{graphId}" x1="0" y1="1" x2="0" y2="0">
        <stop offset="0%" stop-color="var(--accent-color)" stop-opacity="0.25" />
        <stop offset="100%" stop-color="var(--accent-color)" stop-opacity="0" />
      </linearGradient>
      <linearGradient id="grad-out-{graphId}" x1="0" y1="1" x2="0" y2="0">
        <stop offset="0%" stop-color="var(--success-color)" stop-opacity="0.2" />
        <stop offset="100%" stop-color="var(--success-color)" stop-opacity="0" />
      </linearGradient>
    </defs>
    <g class="chart-area">
      {#each [0.25, 0.5, 0.75, 1] as frac}
        <line
          x1={padding.left}
          y1={padding.top + chartHeight - frac * chartHeight}
          x2={width - padding.right}
          y2={padding.top + chartHeight - frac * chartHeight}
          class="grid-line"
        />
      {/each}
      <path d={areaIn} fill="url(#grad-in-{graphId})" />
      <path d={areaOut} fill="url(#grad-out-{graphId})" />
      <path d={pathIn} class="line-in" fill="none" stroke="var(--accent-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
      <path d={pathOut} class="line-out" fill="none" stroke="var(--success-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    </g>
    <text x={padding.left} y={height - 4} class="axis-label">{(maxRate).toFixed(1)} Mbps</text>
  </svg>
  {#if showLegend}
    <div class="legend">
      <span class="legend-item"><span class="legend-dot legend-in"></span> In</span>
      <span class="legend-item"><span class="legend-dot legend-out"></span> Out</span>
    </div>
  {/if}
</div>

<style>
  .traffic-graph {
    display: inline-block;
  }
  .chart-area .grid-line {
    stroke: var(--border-color);
    stroke-width: 1;
    stroke-dasharray: 4 4;
    opacity: 0.6;
  }
  .axis-label {
    font-size: 10px;
    fill: var(--text-secondary);
  }
  .legend {
    display: flex;
    gap: 16px;
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .legend-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
  }
  .legend-in {
    background: var(--accent-color);
  }
  .legend-out {
    background: var(--success-color);
  }
</style>
