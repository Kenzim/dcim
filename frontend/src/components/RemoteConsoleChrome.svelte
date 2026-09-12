<script>
  import { onDestroy, onMount } from 'svelte';
  import { getKvmPower, setKvmPower } from '../lib/api.js';
  import {
    assessLatencyHealth,
    computeJitter,
    latencyHealthLabel,
    latencySparkline,
  } from '../lib/consoleLatency.js';

  /** connecting | connected | disconnected | error */
  export let status = 'connecting';
  export let errorMessage = '';
  export let pasting = false;
  export let reconnecting = false;
  export let showCad = true;
  export let extraLabel = '';
  /** Host VGA is blank (BMC still connected). Toolbar + overlay say No signal. */
  export let noSignal = false;
  export let latencyMs = null;
  export let latencySamples = [];
  export let onCad = () => {};
  export let onPaste = () => {};
  export let onReconnect = () => {};
  /** KVM ws_token — chassis power menu. Empty on VM VNC. */
  export let powerToken = '';

  let showLatencyGraph = false;
  let powerRoot;
  let powerOpen = false;
  let powerBusy = false;
  let powerState = '';
  let powerActions = [];
  let powerError = '';
  let powerPoll = null;
  let watchedToken = '';

  function stopPowerPoll() {
    if (powerPoll) {
      clearInterval(powerPoll);
      powerPoll = null;
    }
  }

  async function refreshPower() {
    if (!powerToken) return;
    try {
      const data = await getKvmPower(powerToken);
      powerState = data.power_state || '';
      powerActions = data.actions || [];
      powerError = data.success === false ? data.message || 'Power status unavailable' : '';
    } catch (e) {
      powerError = e.message || String(e);
    }
  }

  function startPowerWatch(token) {
    if (token === watchedToken) return;
    watchedToken = token;
    stopPowerPoll();
    powerState = '';
    powerActions = [];
    powerError = '';
    powerOpen = false;
    if (!token) return;
    refreshPower();
    powerPoll = setInterval(refreshPower, 8000);
  }

  $: startPowerWatch(powerToken);

  function togglePowerMenu() {
    if (powerBusy) return;
    powerOpen = !powerOpen;
  }

  function onDocPointer(event) {
    if (!powerOpen) return;
    if (powerRoot && !powerRoot.contains(event.target)) powerOpen = false;
  }

  function onDocKey(event) {
    if (event.key === 'Escape') powerOpen = false;
  }

  async function runPower(action) {
    if (!powerToken || powerBusy || !action?.id || action.enabled === false) return;
    if (action.confirm && !window.confirm(action.confirm)) return;
    powerBusy = true;
    powerOpen = false;
    powerError = '';
    try {
      const data = await setKvmPower(powerToken, action.id);
      powerState = data.power_state || powerState;
      powerActions = data.actions || powerActions;
      if (data.success === false && data.message) powerError = data.message;
    } catch (e) {
      powerError = e.message || String(e);
      refreshPower();
    } finally {
      powerBusy = false;
    }
  }

  $: powerStateLabel =
    powerState === 'on' ? 'On' : powerState === 'off' ? 'Off' : powerState ? 'Unknown' : '';

  onMount(() => {
    document.addEventListener('pointerdown', onDocPointer);
    document.addEventListener('keydown', onDocKey);
    return () => {
      document.removeEventListener('pointerdown', onDocPointer);
      document.removeEventListener('keydown', onDocKey);
    };
  });

  onDestroy(() => {
    stopPowerPoll();
  });

  $: latencyHealth = assessLatencyHealth(latencySamples);
  $: latencyAvg = latencySamples.length
    ? Math.round(latencySamples.reduce((a, b) => a + b, 0) / latencySamples.length)
    : null;
  $: latencyJitter = computeJitter(latencySamples);
  $: latencyPath = latencySparkline(latencySamples);
  $: connected = status === 'connected';
  $: showNoSignal = connected && noSignal;

  function toggleLatencyGraph() {
    if (latencyMs == null && latencySamples.length === 0) return;
    showLatencyGraph = !showLatencyGraph;
  }

  export function closeLatencyGraph() {
    showLatencyGraph = false;
  }
</script>

<div class="vnc-viewer">
  <div class="vnc-toolbar">
    <div class="vnc-toolbar-left">
      <span
        class="vnc-status"
        class:ok={connected && !noSignal}
        class:warn={showNoSignal}
        class:bad={status === 'error' || status === 'disconnected'}
      >
        {#if status === 'connecting'}
          Connecting…
        {:else if showNoSignal}
          No signal
        {:else if status === 'connected'}
          Connected
        {:else if status === 'disconnected'}
          Disconnected
        {:else}
          Error
        {/if}
      </span>
      {#if extraLabel && !showNoSignal}
        <span class="vnc-extra">{extraLabel}</span>
      {/if}
      {#if connected && latencyMs != null}
        <button
          type="button"
          class="vnc-latency"
          class:healthy={latencyHealth === 'healthy'}
          class:unstable={latencyHealth === 'unstable'}
          class:poor={latencyHealth === 'poor'}
          class:open={showLatencyGraph}
          title="Console latency — click for graph"
          on:click={toggleLatencyGraph}
        >
          <span class="vnc-latency-ms">{latencyMs}ms</span>
          <span class="vnc-latency-health">{latencyHealthLabel(latencyHealth)}</span>
        </button>
      {/if}
    </div>
    <div class="vnc-toolbar-actions">
      {#if powerToken}
        <div class="vnc-power" bind:this={powerRoot}>
          <button
            type="button"
            class="vnc-btn vnc-power-btn"
            class:open={powerOpen}
            disabled={powerBusy}
            aria-haspopup="menu"
            aria-expanded={powerOpen}
            title={powerError || 'Chassis power'}
            on:click={togglePowerMenu}
          >
            {powerBusy ? 'Power…' : powerStateLabel ? `Power · ${powerStateLabel}` : 'Power'}
          </button>
          {#if powerOpen}
            <div class="vnc-power-menu" role="menu">
              {#if powerError}
                <div class="vnc-power-error">{powerError}</div>
              {/if}
              {#each powerActions as action (action.id)}
                <button
                  type="button"
                  class="vnc-power-item"
                  class:danger={action.id === 'off' || action.id === 'cycle'}
                  role="menuitem"
                  disabled={powerBusy || action.enabled === false}
                  on:click={() => runPower(action)}
                >
                  {action.label}
                </button>
              {:else}
                <div class="vnc-power-empty">Loading power options…</div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
      {#if showCad}
        <button type="button" class="vnc-btn" disabled={!connected} on:click={onCad}>
          Ctrl+Alt+Del
        </button>
      {/if}
      <button type="button" class="vnc-btn" disabled={!connected || pasting} on:click={onPaste}>
        {pasting ? 'Typing…' : 'Paste Clipboard'}
      </button>
      <button type="button" class="vnc-btn" on:click={onReconnect} disabled={reconnecting}>
        {reconnecting ? 'Reconnecting…' : 'Reconnect'}
      </button>
    </div>
  </div>
  {#if errorMessage}
    <p class="vnc-error">{errorMessage}</p>
  {/if}
  <div class="vnc-screen-wrap">
    <slot />
    {#if showNoSignal}
      <div class="no-signal-overlay" aria-live="polite">No Signal</div>
    {/if}
    {#if showLatencyGraph}
      <div class="latency-overlay" role="dialog" aria-label="Latency graph">
        <div class="latency-overlay-head">
          <div>
            <div class="latency-overlay-title">
              {latencyMs != null ? `${latencyMs}ms` : '—'}
              <span class="latency-overlay-health" class:healthy={latencyHealth === 'healthy'} class:unstable={latencyHealth === 'unstable'} class:poor={latencyHealth === 'poor'}>
                {latencyHealthLabel(latencyHealth)}
              </span>
            </div>
            <div class="latency-overlay-sub">
              {#if latencyAvg != null && latencyJitter != null}
                avg {latencyAvg}ms · jitter {latencyJitter}ms
              {:else}
                Collecting samples…
              {/if}
            </div>
          </div>
          <button type="button" class="latency-close" on:click={() => (showLatencyGraph = false)} aria-label="Close">×</button>
        </div>
        <svg class="latency-chart" viewBox="0 0 220 56" preserveAspectRatio="none" aria-hidden="true">
          {#if latencyPath}
            <path d={latencyPath} fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke" />
          {/if}
        </svg>
        <p class="latency-hint">Health follows stability more than absolute RTT — steady ~80ms is fine; spikes are not.</p>
      </div>
    {/if}
  </div>
</div>

<style>
  .vnc-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 480px;
    background: #101114;
    border-radius: 8px;
    overflow: hidden;
    color: #e6e6e6;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }

  .vnc-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px;
    background: #1c1d22;
    color: #e6e6e6;
    font-size: 13px;
    position: relative;
    z-index: 10;
  }

  .vnc-toolbar-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .vnc-status {
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-status.ok {
    color: #3ecf6a;
  }

  .vnc-status.warn {
    color: #e6c35c;
  }

  .vnc-status.bad {
    color: #ff6b61;
  }

  .no-signal-overlay {
    position: absolute;
    inset: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    background: #000;
    color: #f0f0f0;
    font-size: 28px;
    font-weight: 500;
    letter-spacing: 0.08em;
  }

  .vnc-extra {
    font-size: 12px;
    color: #9a9a9a;
    white-space: nowrap;
  }

  .vnc-latency {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    padding: 2px 8px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.18);
    background: rgba(255, 255, 255, 0.04);
    color: #c8c8c8;
    cursor: pointer;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  .vnc-latency:hover,
  .vnc-latency.open {
    background: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.3);
  }

  .vnc-latency-ms {
    font-weight: 600;
    color: #e6e6e6;
  }

  .vnc-latency-health {
    color: #9a9a9a;
  }

  .vnc-latency.healthy .vnc-latency-health,
  .latency-overlay-health.healthy {
    color: #3ecf6a;
  }

  .vnc-latency.unstable .vnc-latency-health,
  .latency-overlay-health.unstable {
    color: #e6c35c;
  }

  .vnc-latency.poor .vnc-latency-health,
  .latency-overlay-health.poor {
    color: #ff6b61;
  }

  .vnc-toolbar-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
    align-items: center;
  }

  .vnc-power {
    position: relative;
  }

  .vnc-power-btn.open {
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-power-menu {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 8;
    min-width: 240px;
    padding: 6px;
    border-radius: 8px;
    background: #1c1d22;
    border: 1px solid rgba(255, 255, 255, 0.16);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  }

  .vnc-power-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 7px 10px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: #e6e6e6;
    cursor: pointer;
    font-size: 12px;
  }

  .vnc-power-item:not(:disabled):hover {
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-power-item.danger:not(:disabled):hover {
    background: rgba(255, 80, 70, 0.16);
  }

  .vnc-power-item:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .vnc-power-error,
  .vnc-power-empty {
    padding: 6px 10px 8px;
    font-size: 11px;
    color: #ff8f87;
    line-height: 1.35;
  }

  .vnc-power-empty {
    color: #9a9a9a;
  }

  .vnc-btn {
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font-size: 12px;
  }

  .vnc-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .vnc-btn:not(:disabled):hover {
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-error {
    margin: 0;
    padding: 8px 12px;
    background: #3a1414;
    color: #ff8f87;
    font-size: 13px;
  }

  .vnc-screen-wrap {
    flex: 1;
    position: relative;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .latency-overlay {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 5;
    width: min(280px, calc(100% - 24px));
    padding: 10px 12px;
    border-radius: 8px;
    background: rgba(16, 17, 20, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: #e6e6e6;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
    backdrop-filter: blur(6px);
  }

  .latency-overlay-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
  }

  .latency-overlay-title {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 16px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .latency-overlay-health {
    font-size: 12px;
    font-weight: 600;
  }

  .latency-overlay-sub {
    margin-top: 2px;
    font-size: 11px;
    color: #9a9a9a;
    font-variant-numeric: tabular-nums;
  }

  .latency-close {
    border: none;
    background: transparent;
    color: #9a9a9a;
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
    padding: 0 2px;
  }

  .latency-close:hover {
    color: #e6e6e6;
  }

  .latency-chart {
    display: block;
    width: 100%;
    height: 56px;
    color: #6cb6ff;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 4px;
  }

  .latency-hint {
    margin: 8px 0 0;
    font-size: 11px;
    line-height: 1.35;
    color: #7a7a7a;
  }
</style>
