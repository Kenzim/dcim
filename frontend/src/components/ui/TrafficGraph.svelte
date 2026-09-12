<script>
  /**
   * Responsive SVG traffic graph for rate_in_mbps / rate_out_mbps over time.
   * samples: [{ sampled_at, rate_in_mbps, rate_out_mbps }]
   * Fills its parent; width/height props are fallbacks before the first measure.
   */
  export let samples = [];
  export let width = 420;
  export let height = 210;
  export let showLegend = true;
  export let graphId = 'default';

  let measuredWidth = 0;
  let measuredHeight = 0;

  $: chartW = measuredWidth > 0 ? measuredWidth : width;
  $: chartH = measuredHeight > 0 ? measuredHeight : height;

  $: chartSamples = (samples || [])
    .filter((s) => s && (s.rate_in_mbps != null || s.rate_out_mbps != null))
    .slice(-120);

  $: rawMaxRate = Math.max(
    1,
    ...chartSamples.flatMap((s) => [
      Number(s.rate_in_mbps) || 0,
      Number(s.rate_out_mbps) || 0,
    ])
  );
  $: maxRate = niceCeiling(rawMaxRate * 1.05);
  $: padding = { top: 12, right: 12, bottom: 24, left: 56 };
  $: innerW = Math.max(1, chartW - padding.left - padding.right);
  $: innerH = Math.max(1, chartH - padding.top - padding.bottom);
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
    const chosen = candidates.find((c) => normalized <= c) ?? 10;
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

  function xAt(i) {
    if (chartSamples.length <= 1) return padding.left;
    return padding.left + (i / Math.max(1, chartSamples.length - 1)) * innerW;
  }

  function yAt(v) {
    const n = Number(v) || 0;
    return padding.top + innerH - (n / maxRate) * innerH;
  }

  function buildLine(samples, key, w, h, yMax, pad) {
    if (!samples || samples.length < 2) return '';
    const plotW = Math.max(1, w - pad.left - pad.right);
    const plotH = Math.max(1, h - pad.top - pad.bottom);
    const x = (i) => pad.left + (i / Math.max(1, samples.length - 1)) * plotW;
    const y = (v) => pad.top + plotH - ((Number(v) || 0) / yMax) * plotH;
    return samples.map((s, i) => `${i === 0 ? 'M' : 'L'} ${x(i)} ${y(s[key])}`).join(' ');
  }

  function buildArea(line, w, h, pad) {
    if (!line) return '';
    const plotW = Math.max(1, w - pad.left - pad.right);
    const plotH = Math.max(1, h - pad.top - pad.bottom);
    const lastX = pad.left + plotW;
    return `${line} L ${lastX} ${pad.top + plotH} L ${pad.left} ${pad.top + plotH} Z`;
  }

  // Size args must be in this expression so paths recompute after bind:clientWidth/Height.
  $: pathIn = buildLine(chartSamples, 'rate_in_mbps', chartW, chartH, maxRate, padding);
  $: pathOut = buildLine(chartSamples, 'rate_out_mbps', chartW, chartH, maxRate, padding);
  $: areaIn = buildArea(pathIn, chartW, chartH, padding);
  $: areaOut = buildArea(pathOut, chartW, chartH, padding);

  function handleMove(event) {
    if (chartSamples.length < 1) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const svgX = ((event.clientX - rect.left) / rect.width) * chartW;
    const clampedX = Math.max(padding.left, Math.min(chartW - padding.right, svgX));
    const relative = (clampedX - padding.left) / Math.max(innerW, 1);
    hoverIndex = Math.max(
      0,
      Math.min(chartSamples.length - 1, Math.round(relative * (chartSamples.length - 1)))
    );
  }

  function clearHover() {
    hoverIndex = -1;
  }

  $: hoverSample = hoverIndex >= 0 ? chartSamples[hoverIndex] : null;
  $: hoverX = hoverIndex >= 0 ? xAt(hoverIndex) : null;
  $: hoverYIn = hoverSample ? yAt(hoverSample.rate_in_mbps) : null;
  $: hoverYOut = hoverSample ? yAt(hoverSample.rate_out_mbps) : null;
</script>

<div class="traffic-graph">
  <div class="chart-stage" bind:clientWidth={measuredWidth} bind:clientHeight={measuredHeight}>
    <svg
      width={chartW}
      height={chartH}
      viewBox="0 0 {chartW} {chartH}"
      preserveAspectRatio="none"
      role="img"
      aria-label="Traffic rate chart"
      on:mousemove={handleMove}
      on:mouseleave={clearHover}
    >
      <defs>
        <linearGradient id="grad-in-{graphId}" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="var(--accent-color)" stop-opacity="0.22" />
          <stop offset="100%" stop-color="var(--accent-color)" stop-opacity="0" />
        </linearGradient>
        <linearGradient id="grad-out-{graphId}" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="var(--success-color)" stop-opacity="0.18" />
          <stop offset="100%" stop-color="var(--success-color)" stop-opacity="0" />
        </linearGradient>
      </defs>

      {#each yTickFractions as frac}
        <line
          x1={padding.left}
          y1={padding.top + innerH - frac * innerH}
          x2={chartW - padding.right}
          y2={padding.top + innerH - frac * innerH}
          class="grid-line"
        />
        <text
          x={padding.left - 8}
          y={padding.top + innerH - frac * innerH + 3}
          class="axis-label axis-label-y"
        >
          {formatRate(maxRate * frac)}
        </text>
      {/each}

      <line
        x1={padding.left}
        y1={padding.top + innerH}
        x2={chartW - padding.right}
        y2={padding.top + innerH}
        class="axis-line"
      />

      {#key `${chartW}x${chartH}:${maxRate}`}
        {#if areaIn}
          <path d={areaIn} fill="url(#grad-in-{graphId})" />
        {/if}
        {#if areaOut}
          <path d={areaOut} fill="url(#grad-out-{graphId})" />
        {/if}
        {#if pathIn}
          <path
            d={pathIn}
            class="line-in"
            fill="none"
            stroke="var(--accent-color)"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            vector-effect="non-scaling-stroke"
          />
        {/if}
        {#if pathOut}
          <path
            d={pathOut}
            class="line-out"
            fill="none"
            stroke="var(--success-color)"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            vector-effect="non-scaling-stroke"
          />
        {/if}
      {/key}

      {#if hoverSample}
        <line
          x1={hoverX}
          y1={padding.top}
          x2={hoverX}
          y2={padding.top + innerH}
          class="hover-line"
        />
        <circle cx={hoverX} cy={hoverYIn} r="3.5" class="hover-dot-in" />
        <circle cx={hoverX} cy={hoverYOut} r="3.5" class="hover-dot-out" />
      {/if}

      <text x={padding.left} y={chartH - 6} class="axis-label axis-label-x">
        {formatShortTime(firstSampledAt)}
      </text>
      <text
        x={chartW - padding.right}
        y={chartH - 6}
        text-anchor="end"
        class="axis-label axis-label-x"
      >
        {formatShortTime(lastSampledAt)}
      </text>
    </svg>

    {#if hoverSample}
      <div
        class="tooltip"
        style="left: {Math.min(Math.max((hoverX / chartW) * 100, 10), 90)}%"
      >
        <div class="tooltip-time">{formatLongTime(hoverSample.sampled_at)}</div>
        <div class="tooltip-row">
          <span class="legend-dot legend-in"></span>In: {formatRate(hoverSample.rate_in_mbps ?? 0)}
        </div>
        <div class="tooltip-row">
          <span class="legend-dot legend-out"></span>Out: {formatRate(hoverSample.rate_out_mbps ?? 0)}
        </div>
      </div>
    {/if}
  </div>

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
    height: 100%;
    min-width: 0;
    min-height: 0;
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .chart-stage {
    position: relative;
    flex: 1;
    min-width: 0;
    min-height: 140px;
    width: 100%;
  }

  svg {
    display: block;
    width: 100%;
    height: 100%;
  }

  .grid-line {
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
    stroke-width: 1.5;
  }

  .hover-dot-out {
    fill: var(--success-color);
    stroke: var(--bg-primary);
    stroke-width: 1.5;
  }

  .axis-label {
    font-size: 10px;
    fill: var(--text-secondary);
  }

  .axis-label-y {
    text-anchor: end;
  }

  .tooltip {
    position: absolute;
    top: 8px;
    transform: translateX(-50%);
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 12px;
    line-height: 1.4;
    color: var(--text-primary);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.24);
    pointer-events: none;
    white-space: nowrap;
    z-index: 2;
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
    flex-shrink: 0;
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
