<script>
  import { onMount, onDestroy } from 'svelte';
  import RFB from '@novnc/novnc';
  import { readClipboardOrPrompt, typeIntoRfb } from '../lib/clipboardType.js';

  // Session credentials minted by the admin/client "vnc-session" endpoints
  // or by POST /api/vnc/redeem. `vncPassword` is a Proxmox-issued,
  // single-session VNC/RFB ticket -- not a Proxmox account password.
  export let wsToken;
  export let wsPath = '/api/vnc/ws';
  export let vncPassword;
  // Optional: async () => session. Called before reconnect so we can mint a
  // fresh Proxmox proxy (old tickets die when the upstream WS closes).
  export let refreshSession = null;

  let containerEl;
  let rfb = null;
  let status = 'connecting'; // connecting | connected | disconnected | error
  let errorMessage = '';
  let reconnecting = false;
  let destroyed = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  const MAX_AUTO_RETRIES = 5;

  // Console RTT via text-frame ping/pong on the same WS (see vm_vnc proxy).
  const LATENCY_HISTORY = 60;
  const LATENCY_INTERVAL_MS = 1000;
  const LATENCY_TIMEOUT_MS = 3000;
  let latencyMs = null;
  let latencySamples = []; // recent RTTs for graph / health
  let showLatencyGraph = false;
  let latencyTimer = null;
  let pendingPing = null; // { t, timeoutId }
  let latencyWs = null;
  let latencyOnMessage = null;

  $: latencyHealth = assessLatencyHealth(latencySamples);
  $: latencyAvg = latencySamples.length
    ? Math.round(latencySamples.reduce((a, b) => a + b, 0) / latencySamples.length)
    : null;
  $: latencyJitter = computeJitter(latencySamples);
  $: latencyPath = latencySparkline(latencySamples);

  function buildWsUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = new URL(`${scheme}://${window.location.host}${wsPath}`);
    url.searchParams.set('token', wsToken);
    return url.toString();
  }

  function isRfbConnected() {
    return rfb && rfb._rfbConnectionState === 'connected';
  }

  function computeJitter(samples) {
    if (!samples || samples.length < 2) return null;
    let sum = 0;
    for (let i = 1; i < samples.length; i += 1) {
      sum += Math.abs(samples[i] - samples[i - 1]);
    }
    return Math.round(sum / (samples.length - 1));
  }

  // Prefer stability over absolute RTT: ~80ms steady is fine; spiky is not.
  function assessLatencyHealth(samples) {
    if (!samples || samples.length < 4) return 'unknown';
    const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
    const jitter = computeJitter(samples);
    if (mean >= 400 || jitter >= 45) return 'poor';
    if (jitter >= 20 || mean >= 220) return 'unstable';
    return 'healthy';
  }

  function latencyHealthLabel(health) {
    if (health === 'healthy') return 'Healthy';
    if (health === 'unstable') return 'Unstable';
    if (health === 'poor') return 'Poor';
    return 'Measuring…';
  }

  function latencySparkline(samples) {
    if (!samples || samples.length < 2) return '';
    const w = 220;
    const h = 56;
    const pad = 2;
    const max = Math.max(...samples, 1);
    const min = Math.min(...samples);
    const span = Math.max(max - min, 8);
    const n = samples.length;
    return samples
      .map((v, i) => {
        const x = pad + (i / (n - 1)) * (w - pad * 2);
        const y = h - pad - ((v - min) / span) * (h - pad * 2);
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }

  function clearPendingPing() {
    if (pendingPing?.timeoutId != null) clearTimeout(pendingPing.timeoutId);
    pendingPing = null;
  }

  function stopLatencyProbe() {
    clearPendingPing();
    if (latencyTimer != null) {
      clearInterval(latencyTimer);
      latencyTimer = null;
    }
    if (latencyWs && latencyOnMessage) {
      try {
        // Restore noVNC handler if we still own this socket.
        if (latencyWs.onmessage === latencyMessageHandler) {
          latencyWs.onmessage = latencyOnMessage;
        }
      } catch (_) {
        /* ignore */
      }
    }
    latencyWs = null;
    latencyOnMessage = null;
  }

  function recordSample(ms) {
    const value = Math.max(0, Math.round(ms));
    latencyMs = value;
    latencySamples = [...latencySamples.slice(-(LATENCY_HISTORY - 1)), value];
  }

  function latencyMessageHandler(event) {
    if (typeof event.data === 'string') {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === 'pong' && pendingPing && msg.t === pendingPing.t) {
          const rtt = performance.now() - pendingPing.sentAt;
          clearPendingPing();
          recordSample(rtt);
        }
      } catch (_) {
        /* ignore non-JSON control text */
      }
      return;
    }
    if (typeof latencyOnMessage === 'function') {
      latencyOnMessage(event);
    }
  }

  function sendLatencyPing() {
    if (!latencyWs || latencyWs.readyState !== WebSocket.OPEN || pendingPing) return;
    const t = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const sentAt = performance.now();
    const timeoutId = setTimeout(() => {
      if (pendingPing?.t === t) {
        pendingPing = null;
        // Timed-out probes count as a spike so variance reflects loss.
        recordSample(LATENCY_TIMEOUT_MS);
      }
    }, LATENCY_TIMEOUT_MS);
    pendingPing = { t, sentAt, timeoutId };
    try {
      latencyWs.send(JSON.stringify({ type: 'ping', t }));
    } catch (_) {
      clearPendingPing();
    }
  }

  function startLatencyProbe(instance) {
    stopLatencyProbe();
    const sock = instance?._sock?._websocket;
    if (!sock) return;
    latencyWs = sock;
    latencyOnMessage = sock.onmessage;
    sock.onmessage = latencyMessageHandler;
    latencySamples = [];
    latencyMs = null;
    sendLatencyPing();
    latencyTimer = setInterval(sendLatencyPing, LATENCY_INTERVAL_MS);
  }

  function toggleLatencyGraph() {
    if (latencyMs == null && latencySamples.length === 0) return;
    showLatencyGraph = !showLatencyGraph;
  }

  function safeDisconnect() {
    stopLatencyProbe();
    showLatencyGraph = false;
    const old = rfb;
    rfb = null;
    if (!old) return;
    try {
      // Calling disconnect() after noVNC already closed throws
      // "Tried changing state of a disconnected RFB object".
      if (old._rfbConnectionState && old._rfbConnectionState !== 'disconnected') {
        old.disconnect();
      }
    } catch (_) {
      /* ignore */
    }
  }

  function clearAutoReconnect() {
    if (autoReconnectTimer != null) {
      clearTimeout(autoReconnectTimer);
      autoReconnectTimer = null;
    }
  }

  function scheduleAutoReconnect() {
    if (destroyed || reconnecting || !refreshSession) return;
    if (autoRetries >= MAX_AUTO_RETRIES) {
      errorMessage = 'Console disconnected. Click Reconnect to try again.';
      return;
    }
    clearAutoReconnect();
    const delayMs = Math.min(1000 * 2 ** autoRetries, 15000);
    autoRetries += 1;
    status = 'connecting';
    errorMessage = `Reconnecting… (attempt ${autoRetries}/${MAX_AUTO_RETRIES})`;
    autoReconnectTimer = setTimeout(() => {
      autoReconnectTimer = null;
      reconnect({ auto: true });
    }, delayMs);
  }

  function connect() {
    if (destroyed) return;
    status = 'connecting';
    errorMessage = '';
    try {
      // Clear any leftover canvas from a previous RFB instance before
      // creating a new one (reconnect path).
      if (containerEl) containerEl.innerHTML = '';
      safeDisconnect();
      const instance = new RFB(containerEl, buildWsUrl(), {
        credentials: { password: vncPassword },
        wsProtocols: ['binary'],
      });
      rfb = instance;
      instance.scaleViewport = true;
      instance.clipViewport = false;
      instance.addEventListener('connect', () => {
        if (rfb !== instance || destroyed) return;
        status = 'connected';
        errorMessage = '';
        autoRetries = 0;
        startLatencyProbe(instance);
      });
      instance.addEventListener('disconnect', (evt) => {
        if (rfb !== instance && rfb !== null) return;
        stopLatencyProbe();
        rfb = null;
        if (destroyed || reconnecting) return;
        status = 'disconnected';
        const unclean = evt?.detail && evt.detail.clean === false;
        if (unclean) {
          errorMessage = 'Console disconnected unexpectedly.';
          scheduleAutoReconnect();
        }
      });
      instance.addEventListener('securityfailure', (evt) => {
        if (rfb !== instance) return;
        status = 'error';
        errorMessage = evt?.detail?.reason || 'VNC authentication failed.';
      });
      instance.addEventListener('credentialsrequired', () => {
        // Should not happen since we supply credentials upfront, but surface
        // it clearly rather than hanging silently.
        if (rfb !== instance) return;
        status = 'error';
        errorMessage = 'The console requires credentials that were not provided.';
      });
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
    }
  }

  async function reconnect({ auto = false } = {}) {
    if (reconnecting || destroyed) return;
    reconnecting = true;
    clearAutoReconnect();
    if (!auto) {
      autoRetries = 0;
      errorMessage = '';
    }
    try {
      if (refreshSession) {
        const fresh = await refreshSession();
        if (fresh?.ws_token) wsToken = fresh.ws_token;
        if (fresh?.ws_path) wsPath = fresh.ws_path;
        if (fresh?.vnc_password) vncPassword = fresh.vnc_password;
      }
      safeDisconnect();
      connect();
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
      if (auto) scheduleAutoReconnect();
    } finally {
      reconnecting = false;
    }
  }

  function sendCtrlAltDel() {
    if (!isRfbConnected()) return;
    try {
      rfb.sendCtrlAltDel();
    } catch (_) {
      /* ignore */
    }
  }

  // Type clipboard text as keystrokes (down+up per character). The VNC
  // clipboard extension (`clipboardPasteFrom`) only updates the guest
  // clipboard and still needs Ctrl+V / guest agent support -- that's not
  // what "Paste Clipboard" means here, and it silently no-ops on most
  // Proxmox VMs. Fall back to a prompt when the Clipboard API is blocked.
  let pasting = false;
  async function pasteClipboard() {
    if (pasting || !isRfbConnected()) return;
    errorMessage = '';
    pasting = true;
    try {
      const text = await readClipboardOrPrompt();
      if (!text) return;
      await typeText(text);
    } catch (e) {
      errorMessage = `Paste failed: ${e.message || e}`;
    } finally {
      pasting = false;
    }
  }

  /** Type arbitrary text into the guest (used by Paste Password). */
  export async function typeText(text, opts = {}) {
    if (!isRfbConnected() || !text) return;
    await typeIntoRfb(rfb, text, opts);
  }

  onMount(connect);
  onDestroy(() => {
    destroyed = true;
    clearAutoReconnect();
    stopLatencyProbe();
    safeDisconnect();
  });
</script>

<div class="vnc-viewer">
  <div class="vnc-toolbar">
    <div class="vnc-toolbar-left">
      <span class="vnc-status" class:ok={status === 'connected'} class:bad={status === 'error' || status === 'disconnected'}>
        {#if status === 'connecting'}
          Connecting…
        {:else if status === 'connected'}
          Connected
        {:else if status === 'disconnected'}
          Disconnected
        {:else}
          Error
        {/if}
      </span>
      {#if status === 'connected' && latencyMs != null}
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
      <button type="button" class="vnc-btn" disabled={status !== 'connected'} on:click={sendCtrlAltDel}>
        Ctrl+Alt+Del
      </button>
      <button type="button" class="vnc-btn" disabled={status !== 'connected' || pasting} on:click={pasteClipboard}>
        {pasting ? 'Typing…' : 'Paste Clipboard'}
      </button>
      <button type="button" class="vnc-btn" on:click={() => reconnect()} disabled={reconnecting}>
        {reconnecting ? 'Reconnecting…' : 'Reconnect'}
      </button>
    </div>
  </div>
  {#if errorMessage}
    <p class="vnc-error">{errorMessage}</p>
  {/if}
  <div class="vnc-screen-wrap">
    <!-- RFB owns this node (clears/rewrites children); keep overlays outside. -->
    <div class="vnc-screen" bind:this={containerEl}></div>
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

  .vnc-status.bad {
    color: #ff6b61;
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
  }

  .vnc-screen {
    height: 100%;
    overflow: hidden;
  }

  .vnc-screen :global(canvas) {
    display: block;
    margin: 0 auto;
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
