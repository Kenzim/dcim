<script>
  /**
   * Multi-series SVG line chart for aggregate throughput (mbps) over time.
   * series: [{ id, label, color, visible, samples: [{ t: epochMs, value: mbps }] }]
   * width/height are measured pixel sizes from the parent so the chart fills
   * whatever space it's given without distortion.
   */
  export let series = [];
  export let width = 960;
  export let height = 280;

  $: visibleSeries = series.filter((s) => s.visible !== false);
  $: allSamples = visibleSeries.flatMap((s) => s.samples || []);
  $: domainStart = allSamples.length ? Math.min(...allSamples.map((s) => s.t)) : 0;
  $: domainEnd = allSamples.length ? Math.max(...allSamples.map((s) => s.t)) : 1;
  $: domainSpan = Math.max(1, domainEnd - domainStart);
  $: rawMax = Math.max(1, ...allSamples.map((s) => s.value || 0));
  $: maxValue = niceCeiling(rawMax * 1.1);
  $: padding = { top: 16, right: 16, bottom: 28, left: 64 };
  $: chartWidth = Math.max(1, width - padding.left - padding.right);
  $: chartHeight = Math.max(1, height - padding.top - padding.bottom);
  $: yTickFractions = [1, 0.75, 0.5, 0.25, 0];

  let hoverX = null;
  let hoverT = null;

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

  function formatShortTime(t) {
    if (!t) return '—';
    const d = new Date(t);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function formatLongTime(t) {
    if (!t) return '—';
    const d = new Date(t);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString();
  }

  function x(t) {
    return padding.left + ((t - domainStart) / domainSpan) * chartWidth;
  }

  function y(v) {
    return padding.top + chartHeight - (v / maxValue) * chartHeight;
  }

  function pathFor(samples) {
    if (!samples || samples.length < 2) return '';
    return samples.map((s, i) => `${i === 0 ? 'M' : 'L'} ${x(s.t)} ${y(s.value || 0)}`).join(' ');
  }

  function nearestSample(samples, t) {
    if (!samples || !samples.length) return null;
    let best = samples[0];
    let bestDiff = Math.abs(samples[0].t - t);
    for (const s of samples) {
      const diff = Math.abs(s.t - t);
      if (diff < bestDiff) {
        best = s;
        bestDiff = diff;
      }
    }
    return best;
  }

  function handleMove(event) {
    if (!allSamples.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const svgX = ((event.clientX - rect.left) / rect.width) * width;
    const clampedX = Math.max(padding.left, Math.min(width - padding.right, svgX));
    hoverX = clampedX;
    hoverT = domainStart + ((clampedX - padding.left) / chartWidth) * domainSpan;
  }

  function clearHover() {
    hoverX = null;
    hoverT = null;
  }

  $: hoverEntries =
    hoverT != null
      ? visibleSeries
          .map((s) => ({ series: s, sample: nearestSample(s.samples, hoverT) }))
          .filter((e) => e.sample)
      : [];
</script>

<div class="agg-chart">
  {#if allSamples.length === 0}
    <div class="agg-chart-empty">No traffic data in this window.</div>
  {:else}
    <svg
      width="100%"
      height="100%"
      viewBox="0 0 {width} {height}"
      preserveAspectRatio="none"
      role="img"
      aria-label="Aggregate traffic chart"
      on:mousemove={handleMove}
      on:mouseleave={clearHover}
    >
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
            {formatRate(maxValue * frac)}
          </text>
        {/each}

        <line x1={padding.left} y1={padding.top + chartHeight} x2={width - padding.right} y2={padding.top + chartHeight} class="axis-line" />

        {#each visibleSeries as s (s.id)}
          <path d={pathFor(s.samples)} fill="none" stroke={s.color} stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
        {/each}

        {#if hoverT != null}
          <line x1={hoverX} y1={padding.top} x2={hoverX} y2={padding.top + chartHeight} class="hover-line" />
          {#each hoverEntries as entry (entry.series.id)}
            <circle cx={x(entry.sample.t)} cy={y(entry.sample.value || 0)} r="3.5" fill={entry.series.color} stroke="var(--bg-primary)" stroke-width="1.5" />
          {/each}
        {/if}
      </g>
      <text x={padding.left} y={height - 8} class="axis-label axis-label-x">{formatShortTime(domainStart)}</text>
      <text x={width - padding.right} y={height - 8} text-anchor="end" class="axis-label axis-label-x">{formatShortTime(domainEnd)}</text>
    </svg>
    {#if hoverEntries.length > 0}
      <div class="tooltip" style="left: {Math.min(Math.max((hoverX / width) * 100, 8), 92)}%">
        <div class="tooltip-time">{formatLongTime(hoverT)}</div>
        {#each hoverEntries as entry (entry.series.id)}
          <div class="tooltip-row">
            <span class="legend-dot" style="background: {entry.series.color}" />
            {entry.series.label}: {formatRate(entry.sample.value)}
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .agg-chart {
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    position: relative;
  }

  .agg-chart-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-tertiary);
    font-size: 14px;
  }

  svg {
    width: 100%;
    height: 100%;
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
    position: absolute;
    top: 8px;
    transform: translateX(-50%);
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-primary);
    box-shadow: var(--shadow-lg);
    pointer-events: none;
    white-space: nowrap;
    z-index: 10;
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
    gap: 6px;
  }

  .legend-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
</style>
