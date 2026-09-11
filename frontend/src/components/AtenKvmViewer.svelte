<script>
  import { onDestroy, onMount } from 'svelte';
  import { readClipboardOrPrompt, typeIntoAtenRfb } from '../lib/clipboardType.js';
  import { createWsLatencyProbe } from '../lib/consoleLatency.js';
  import { letterboxCanvas, patchAtenPointer, watchCanvasFit, eventToFramebuffer } from '../lib/kvmPointer.js';
  import RemoteConsoleChrome from './RemoteConsoleChrome.svelte';

  /** Session from POST /api/kvm/redeem. BMC cookies/tokens stay on the server. */
  export let session;

  const ATEN_SCRIPTS = [
    'util.js',
    'webutil.js',
    'base64.js',
    'websock.js',
    'des.js',
    'keysymdef.js',
    'keyboard.js',
    'input.js',
    'display.js',
    'jsunzip.js',
    'ast2100.js',
    'rfb.js',
    'keysym.js',
  ];

  const MAX_AUTO_RETRIES = 5;

  let canvas;
  let chrome;
  let stage;
  let status = 'connecting';
  let errorMessage = '';
  let extraLabel = '';
  let destroyed = false;
  let ws = null;
  let rfb = null;
  let pendingFrames = [];
  let scriptsReady = false;
  let reconnecting = false;
  let pasting = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  let stopFitWatch = null;

  let latencyMs = null;
  let latencySamples = [];
  const latencyProbe = createWsLatencyProbe({
    encoding: 'binary',
    onSample({ ms, samples }) {
      latencyMs = ms;
      latencySamples = samples;
    },
  });

  function isLive() {
    return !destroyed && ws && ws.readyState === WebSocket.OPEN && status === 'connected';
  }

  function assetUrl(name) {
    const token = session.ws_token || '';
    return `/api/kvm/assets/novnc/include/${name}?token=${encodeURIComponent(token)}`;
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[data-aten-kvm="${src}"]`);
      if (existing) {
        resolve();
        return;
      }
      const el = document.createElement('script');
      el.src = src;
      el.async = false;
      el.dataset.atenKvm = src;
      el.onload = () => resolve();
      el.onerror = () => reject(new Error(`Failed to load ${src}`));
      document.head.appendChild(el);
    });
  }

  function rackflowWsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const path = session.ws_path || '/api/kvm/ws';
    return `${proto}://${location.host}${path}?token=${encodeURIComponent(session.ws_token)}`;
  }

  function stubAtenDomHelpers() {
    const chain = {
      parent() { return chain; },
      addClass() { return chain; },
      removeClass() { return chain; },
      find() { return chain; },
      removeAttr() { return chain; },
      unbind() { return chain; },
      remove() { return chain; },
      prop() { return chain; },
    };
    const jq = () => chain;
    if (!window.jQuery) window.jQuery = jq;
    if (!window.$jq) window.$jq = jq;
    if (!window.$) window.$ = jq;
  }

  function feedRfb(data) {
    if (!rfb || !rfb._sock) return;
    rfb._sock._decode_message(data);
    rfb._handle_message();
  }

  /** Letterbox the bitmap so CSS size matches aspect; pointer mapping uses the painted box. */
  function fitCanvas() {
    if (!canvas || !stage) return;
    letterboxCanvas(canvas, stage);
    if (!rfb?._mouse || typeof rfb._mouse.set_scale !== 'function') return;
    if (rfb._mouse._rackflowFbCoords) {
      rfb._mouse.set_scale(1);
      return;
    }
    const fw = canvas.width || 1;
    const rect = canvas.getBoundingClientRect();
    rfb._mouse.set_scale(rect.width / fw);
  }

  function mousePoint(x, y, ev) {
    if (ev && canvas) {
      const pt = eventToFramebuffer(ev, canvas);
      if (pt) return pt;
    }
    return { x: x, y: y };
  }

  function attachRfb(RFB) {
    rfb = new RFB({
      target: canvas,
      focusContainer: canvas,
      encrypt: false,
      shared: true,
      local_cursor: true,
      true_color: true,
      onMouseMove: mousePoint,
      onMouseMode: (crypto, mode) => {
        // Mode 3 is pointer-lock relative; force absolute so the OS cursor maps 1:1.
        if (rfb && rfb._SMC_SetMouseMode && Number(mode) === 3) {
          rfb._SMC_SetMouseMode(crypto, 1);
        }
      },
      onUpdateState: (_rfb, state, _old, msg) => {
        if (destroyed) return;
        if (state === 'failed' || state === 'fatal') {
          status = 'error';
          errorMessage = msg || 'KVM failed';
        } else if (state === 'normal') {
          status = 'connected';
          errorMessage = '';
          autoRetries = 0;
        }
      },
      onUpdateFps: (fps, res) => {
        if (!destroyed && res) extraLabel = `${res}  ${fps} fps`;
      },
      onFBResize: (_rfb, w, h) => {
        if (destroyed) return;
        extraLabel = `${w}×${h}`;
        requestAnimationFrame(fitCanvas);
      },
    });
    rfb._rfb_insydevnc = true;
    rfb._rfb_version = 55.8;
    rfb._rfb_state = 'normal';
    rfb._fb_width = 640;
    rfb._fb_height = 480;
    if (rfb._AST) {
      rfb._AST.VideoEnable = 1;
      rfb._AST.KbMsEnable = 1;
    }
    if (rfb._display) rfb._display.resize(640, 480);
    if (rfb._keyboard) rfb._keyboard.grab();
    if (rfb._mouse) rfb._mouse.grab();

    rfb._sock._websocket = ws;
    rfb._sock._mode = 'binary';
    rfb._sock.flush = function flushAten() {
      if (!ws || ws.readyState !== WebSocket.OPEN || this._sQ.length === 0) return true;
      ws.send(new Uint8Array(this._sQ).buffer);
      this._sQ = [];
      return true;
    };

    const queued = pendingFrames;
    pendingFrames = [];
    for (const frame of queued) {
      try {
        feedRfb(frame);
      } catch (err) {
        status = 'error';
        errorMessage = `parse error: ${err.message || err}`;
        return;
      }
    }
    try {
      if (rfb._insyde_FBUReq) rfb._insyde_FBUReq();
      if (rfb._SMC_GetMouseMode) rfb._SMC_GetMouseMode();
      if (rfb._SMC_MouseSync) rfb._SMC_MouseSync();
    } catch (_) {
      /* ignore */
    }
    patchAtenPointer(rfb, canvas);
    requestAnimationFrame(fitCanvas);
    if (!destroyed) {
      status = 'connected';
      errorMessage = '';
      autoRetries = 0;
    }
  }

  function openHubSocket() {
    const socket = new WebSocket(rackflowWsUrl(), ['binary']);
    ws = socket;
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => {
      if (destroyed || ws !== socket) return;
      status = 'connecting';
    };
    socket.onerror = () => {
      if (destroyed || reconnecting || ws !== socket) return;
      status = 'error';
      errorMessage = 'websocket error';
    };
    socket.onclose = (e) => {
      if (ws !== socket) return;
      latencyProbe.detach();
      chrome?.closeLatencyGraph?.();
      if (destroyed || reconnecting) return;
      status = 'disconnected';
      if (e.code !== 1000) {
        errorMessage = `disconnected (${e.code} ${e.reason || ''})`.trim();
        scheduleAutoReconnect();
      }
    };
    socket.onmessage = (ev) => {
      if (destroyed || ws !== socket) return;
      if (!rfb) {
        pendingFrames.push(ev.data);
        return;
      }
      try {
        feedRfb(ev.data);
      } catch (err) {
        errorMessage = `parse error: ${err.message || err}`;
      }
    };
    latencyProbe.attach(socket);
  }

  function sendCtrlAltDel() {
    if (!isLive()) return;
    if (canvas) canvas.focus();
    if (rfb && rfb.sendCtrlAltDel) rfb.sendCtrlAltDel();
  }

  async function pasteClipboard() {
    if (pasting || !isLive()) return;
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

  export async function typeText(text, opts = {}) {
    if (!isLive() || !text) return;
    if (canvas) canvas.focus();
    await typeIntoAtenRfb(rfb, text, { ...opts, stillConnected: isLive });
  }

  function clearAutoReconnect() {
    if (autoReconnectTimer != null) {
      clearTimeout(autoReconnectTimer);
      autoReconnectTimer = null;
    }
  }

  function scheduleAutoReconnect() {
    if (destroyed || reconnecting) return;
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

  function teardownSocket() {
    clearAutoReconnect();
    latencyProbe.detach();
    chrome?.closeLatencyGraph?.();
    try {
      if (rfb && rfb.disconnect) rfb.disconnect();
    } catch (_) {
      /* ignore */
    }
    rfb = null;
    pendingFrames = [];
    if (ws) {
      const old = ws;
      ws = null;
      try {
        old.close();
      } catch (_) {
        /* ignore */
      }
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
      teardownSocket();
      status = 'connecting';
      openHubSocket();
      if (scriptsReady && window.RFB) attachRfb(window.RFB);
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
      if (auto) scheduleAutoReconnect();
    } finally {
      reconnecting = false;
    }
  }

  onMount(async () => {
    if (!canvas) return;
    stubAtenDomHelpers();
    stopFitWatch = watchCanvasFit(stage, fitCanvas);
    openHubSocket();
    try {
      for (const name of ATEN_SCRIPTS) {
        await loadScript(assetUrl(name));
      }
      const RFB = window.RFB;
      if (!RFB) throw new Error('ATEN RFB client did not load');
      scriptsReady = true;
      if (!destroyed && ws) attachRfb(RFB);
    } catch (err) {
      status = 'error';
      errorMessage = err.message || String(err);
    }
  });

  onDestroy(() => {
    destroyed = true;
    stopFitWatch?.();
    teardownSocket();
  });
</script>

<RemoteConsoleChrome
  bind:this={chrome}
  {status}
  {errorMessage}
  {pasting}
  {reconnecting}
  {extraLabel}
  {latencyMs}
  {latencySamples}
  onCad={sendCtrlAltDel}
  onPaste={pasteClipboard}
  onReconnect={() => reconnect()}
>
  <div class="kvm-stage" bind:this={stage}>
    <canvas
      bind:this={canvas}
      class="kvm-canvas"
      tabindex="0"
      width="640"
      height="480"
      on:click={() => canvas && canvas.focus()}
      on:contextmenu|preventDefault
    ></canvas>
  </div>
</RemoteConsoleChrome>

<style>
  .kvm-stage {
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #000;
  }
  .kvm-canvas {
    flex: none;
    background: #000;
    outline: none;
    cursor: crosshair;
  }
</style>
