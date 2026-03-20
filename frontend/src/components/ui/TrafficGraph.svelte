<script>
  /**
   * Responsive SVG traffic graph for rate_in_mbps / rate_out_mbps over time.
   * samples: [{ sampled_at, rate_in_mbps, rate_out_mbps }]
   */
  export let samples = [];
  export let width = 420;
  export let height = 210;
  export let showLegend = true;
  export let graphId = 'default';

  $: chartSamples = (samples || []).slice(-120);
  $: rawMaxRate = Math.max(
    1,
    ...chartSamples.flatMap(s => [
      s.rate_in_mbps ?? 0,
      s.rate_out_mbps ?? 0
    ])
  );
  $: maxRate = niceCeiling(rawMaxRate * 1.05);
  $: padding = { top: 16, right: 16, bottom: 32, left: 62 };
  $: chartWidth = width - padding.left - padding.right;
  $: chartHeight = height - padding.top - padding.bottom;
  $: yTickFractions = [1, 0.75, 0.5, 0.25, 0];
  $: firstSampledAt = chartSamples[0]?.sampled_at || null;
  $: lastSampledAt = chartSamples[chartSamples.length - 1]?.sampled_at || null;
  let hoverIndex = -1;

  function niceCeiling(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const exponent = Math.floor(Math.log10(value));
    const base = 10 ** exponent;
    const normalized = value / base;
    const candidates = [1, 2, 2.5, 5, 10];
    const chosen = candidates.find(c => normalized <= c) ?? 10;
    return chosen * base;
  }

  function formatRate(mbps) {
    const value = Number(mbps ?? 0);
    if (!Number.isFinite(value) || value <= 0) return '0 Mbps';
    if (value >= 1000) {
      const gbps = value / 1000;
      return `${gbps.toFixed(gbps >= 10 ? 1 : 2)} Gbps`;
    }
    if (value < 1) {
      const kbps = value * 1000;
      return `${kbps.toFixed(kbps >= 100 ? 0 : 1)} Kbps`;
    }
    return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} Mbps`;
  }

  function formatShortTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function formatLongTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString();
  }

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

  function handleMove(event) {
    if (chartSamples.length < 1) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const svgX = ((event.clientX - rect.left) / rect.width) * width;
    const clampedX = Math.max(padding.left, Math.min(width - padding.right, svgX));
    const relative = (clampedX - padding.left) / Math.max(chartWidth, 1);
    hoverIndex = Math.max(0, Math.min(chartSamples.length - 1, Math.round(relative * (chartSamples.length - 1))));
  }

  function clearHover() {
    hoverIndex = -1;
  }

  $: hoverSample = hoverIndex >= 0 ? chartSamples[hoverIndex] : null;
  $: hoverX = hoverIndex >= 0 ? x(hoverIndex) : null;
  $: hoverYIn = hoverSample ? yIn(hoverSample.rate_in_mbps ?? 0) : null;
  $: hoverYOut = hoverSample ? yOut(hoverSample.rate_out_mbps ?? 0) : null;
</script>

<div class="traffic-graph">
  <svg
    width="100%"
    height="100%"
    viewBox="0 0 {width} {height}"
    preserveAspectRatio="xMidYMid meet"
    on:mousemove={handleMove}
    on:mouseleave={clearHover}
  >
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
      {#each yTickFractions as frac}
        <line
          x1={padding.left}
          y1={padding.top + chartHeight - frac * chartHeight}
          x2={width - padding.right}
          y2={padding.top + chartHeight - frac * chartHeight}
          class="grid-line"
        />
        <text x={padding.left - 8} y={padding.top + chartHeight - frac * chartHeight + 4} class="axis-label axis-label-y">
          {formatRate(maxRate * frac)}
        </text>
      {/each}

      <line x1={padding.left} y1={padding.top + chartHeight} x2={width - padding.right} y2={padding.top + chartHeight} class="axis-line" />
      <path d={areaIn} fill="url(#grad-in-{graphId})" />
      <path d={areaOut} fill="url(#grad-out-{graphId})" />
      <path d={pathIn} class="line-in" fill="none" stroke="var(--accent-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
      <path d={pathOut} class="line-out" fill="none" stroke="var(--success-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
      {#if hoverSample}
        <line x1={hoverX} y1={padding.top} x2={hoverX} y2={padding.top + chartHeight} class="hover-line" />
        <circle cx={hoverX} cy={hoverYIn} r="3.5" class="hover-dot-in" />
        <circle cx={hoverX} cy={hoverYOut} r="3.5" class="hover-dot-out" />
      {/if}
    </g>
    <text x={padding.left} y={height - 8} class="axis-label axis-label-x">{formatShortTime(firstSampledAt)}</text>
    <text x={width - padding.right} y={height - 8} text-anchor="end" class="axis-label axis-label-x">{formatShortTime(lastSampledAt)}</text>
  </svg>
  {#if hoverSample}
    <div class="tooltip">
      <div class="tooltip-time">{formatLongTime(hoverSample.sampled_at)}</div>
      <div class="tooltip-row"><span class="legend-dot legend-in"></span>In: {formatRate(hoverSample.rate_in_mbps ?? 0)}</div>
      <div class="tooltip-row"><span class="legend-dot legend-out"></span>Out: {formatRate(hoverSample.rate_out_mbps ?? 0)}</div>
    </div>
  {/if}
  {#if showLegend}
    <div class="legend">
      <span class="legend-item"><span class="legend-dot legend-in"></span> In</span>
      <span class="legend-item"><span class="legend-dot legend-out"></span> Out</span>
    </div>
  {/if}
</div>

<style>
  .traffic-graph {
    width: 100%;
    min-width: 0;
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  svg {
    width: 100%;
    min-height: 180px;
    display: block;
  }
  .chart-area .grid-line {
    stroke: var(--border-color);
    stroke-width: 1;
    stroke-dasharray: 3 4;
    opacity: 0.7;
  }
  .axis-line {
    stroke: var(--border-color);
    stroke-width: 1;
    opacity: 0.8;
  }
  .hover-line {
    stroke: var(--text-secondary);
    stroke-width: 1;
    stroke-dasharray: 2 3;
    opacity: 0.8;
  }
  .hover-dot-in {
    fill: var(--accent-color);
    stroke: var(--bg-primary);
    stroke-width: 1;
  }
  .hover-dot-out {
    fill: var(--success-color);
    stroke: var(--bg-primary);
    stroke-width: 1;
  }
  .axis-label {
    font-size: 10px;
    fill: var(--text-secondary);
  }
  .axis-label-y {
    text-anchor: end;
  }
  .axis-label-x {
    font-size: 11px;
  }
  .tooltip {
    align-self: flex-start;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
    line-height: 1.4;
    color: var(--text-primary);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.24);
  }
  .tooltip-time {
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text-secondary);
    font-size: 11px;
  }
  .tooltip-row {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .legend {
    display: flex;
    gap: 16px;
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
