<script>
  import { onDestroy, onMount } from 'svelte';
  import { readClipboardOrPrompt } from '../lib/clipboardType.js';
  import { createWsLatencyProbe } from '../lib/consoleLatency.js';
  import { letterboxCanvas, watchCanvasFit, eventToFramebuffer } from '../lib/kvmPointer.js';
  import { ByteQueue, HermonDecoder } from '../lib/atenHermon.js';
  import {
    atenFbUpdateRequest,
    atenKeyPacket,
    atenPointerPacket,
    atenSetEncodingsPacket,
    hidFromChar,
    hidFromCode,
  } from '../lib/atenHid.js';
  import RemoteConsoleChrome from './RemoteConsoleChrome.svelte';

  export let session;

  const ATEN_SKIP = {
    4: 20,
    22: 1,
    51: 4,
    55: 2,
    57: 264,
    60: 8,
  };
  const MAX_AUTO_RETRIES = 5;
  const HERMON = 0x59;

  let canvas;
  let chrome;
  let stage;
  let ctx;
  let status = 'connecting';
  let errorMessage = '';
  let extraLabel = '';
  let noSignal = false;
  let destroyed = false;
  let ws = null;
  let pasting = false;
  let reconnecting = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  let stopFitWatch = null;
  let sock = new ByteQueue();
  let hermon = new HermonDecoder();
  let fbu = null;
  let fbWidth = 1024;
  let fbHeight = 768;
  let imageData = null;

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

  function rackflowWsUrl() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const path = session.ws_path || '/api/kvm/ws';
    return `${proto}://${location.host}${path}?token=${encodeURIComponent(session.ws_token)}`;
  }

  function sendBytes(data) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(data);
  }

  function resizeFb(width, height) {
    if (!width || !height || (width === fbWidth && height === fbHeight && canvas?.width === width)) {
      return;
    }
    fbWidth = width;
    fbHeight = height;
    if (canvas) {
      canvas.width = width;
      canvas.height = height;
      ctx = canvas.getContext('2d');
      imageData = ctx.createImageData(width, height);
      imageData.data.fill(0);
      ctx.putImageData(imageData, 0, 0);
    }
    extraLabel = `${width}×${height}`;
    requestAnimationFrame(fitCanvas);
  }

  function blit(x, y, w, h, rgba) {
    if (!ctx || !canvas) return;
    noSignal = false;
    const tile = ctx.createImageData(w, h);
    tile.data.set(rgba);
    ctx.putImageData(tile, x, y);
  }

  function fitCanvas() {
    if (!canvas || !stage) return;
    letterboxCanvas(canvas, stage);
  }

  function parseMessages() {
    while (true) {
      if (fbu) {
        if (!parseRect()) return;
        continue;
      }
      if (sock.wait(1)) return;
      const type = sock.peek8();
      if (type === 0) {
        if (sock.wait(4)) return;
        sock.shift8();
        sock.shift8();
        fbu = { nrects: sock.shift16(), i: 0 };
        continue;
      }
      const skip = ATEN_SKIP[type];
      if (skip != null) {
        if (sock.wait(1 + skip)) return;
        sock.shift8();
        sock.skip(skip);
        continue;
      }
      // Unknown: drop the type byte so we cannot stall forever.
      sock.shift8();
    }
  }

  function parseRect() {
    while (fbu && fbu.i < fbu.nrects) {
      if (!fbu.rect) {
        if (sock.wait(12)) return false;
        fbu.rect = {
          x: sock.shift16(),
          y: sock.shift16(),
          w: sock.shift16(),
          h: sock.shift16(),
          enc: sock.shift32() | 0,
        };
      }
      const rect = fbu.rect;
      if (rect.enc === HERMON || rect.enc === 0) {
        const ok = hermon.decode(sock, rect.x, rect.y, rect.w, rect.h, {
          onBlit: blit,
          onResize: resizeFb,
        });
        if (!ok) return false;
      }
      fbu.rect = null;
      fbu.i += 1;
    }
    if (fbu && fbu.i >= fbu.nrects) {
      fbu = null;
      sendBytes(atenFbUpdateRequest(fbWidth, fbHeight, 1));
    }
    return true;
  }

  function pointerMask(ev) {
    let mask = 0;
    if (ev.buttons & 1) mask |= 1;
    if (ev.buttons & 4) mask |= 2;
    if (ev.buttons & 2) mask |= 4;
    return mask;
  }

  function onPointer(ev) {
    if (!isLive() || !canvas) return;
    const pt = eventToFramebuffer(ev, canvas);
    if (!pt) return;
    sendBytes(atenPointerPacket(Math.round(pt.x), Math.round(pt.y), pointerMask(ev)));
  }

  function onKey(ev, down) {
    if (!isLive()) return;
    ev.preventDefault();
    const hid = hidFromCode(ev.code);
    if (!hid) return;
    sendBytes(atenKeyPacket(hid, down));
  }

  function sendCtrlAltDel() {
    if (!isLive()) return;
    const keys = [0xe0, 0xe2, 0x4c];
    keys.forEach((hid) => sendBytes(atenKeyPacket(hid, true)));
    keys.slice().reverse().forEach((hid) => sendBytes(atenKeyPacket(hid, false)));
  }

  async function typeText(text) {
    if (!isLive() || !text) return;
    for (const ch of String(text)) {
      const hid = hidFromChar(ch);
      if (!hid) continue;
      sendBytes(atenKeyPacket(hid, true));
      sendBytes(atenKeyPacket(hid, false));
      await new Promise((r) => setTimeout(r, 8));
    }
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

  function openHubSocket() {
    const socket = new WebSocket(rackflowWsUrl(), ['binary']);
    ws = socket;
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => {
      if (destroyed || ws !== socket) return;
      status = 'connected';
      errorMessage = '';
      autoRetries = 0;
      sendBytes(atenSetEncodingsPacket());
      sendBytes(atenFbUpdateRequest(fbWidth, fbHeight, 0));
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
      sock.feed(ev.data);
      try {
        parseMessages();
      } catch (err) {
        errorMessage = `parse error: ${err.message || err}`;
      }
    };
    latencyProbe.attach(socket);
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
    sock = new ByteQueue();
    hermon = new HermonDecoder();
    fbu = null;
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
      noSignal = true;
      openHubSocket();
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
      if (auto) scheduleAutoReconnect();
    } finally {
      reconnecting = false;
    }
  }

  onMount(() => {
    if (canvas) {
      ctx = canvas.getContext('2d');
      canvas.width = fbWidth;
      canvas.height = fbHeight;
    }
    noSignal = true;
    stopFitWatch = watchCanvasFit(stage, fitCanvas);
    openHubSocket();
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
  {noSignal}
  {latencyMs}
  {latencySamples}
  powerToken={session.ws_token}
  onCad={sendCtrlAltDel}
  onPaste={pasteClipboard}
  onReconnect={() => reconnect()}
>
  <div class="kvm-stage" bind:this={stage}>
    <canvas
      bind:this={canvas}
      class="kvm-canvas"
      tabindex="0"
      width="1024"
      height="768"
      on:click={() => canvas && canvas.focus()}
      on:pointerdown={onPointer}
      on:pointermove={onPointer}
      on:pointerup={onPointer}
      on:contextmenu|preventDefault
      on:keydown={(e) => onKey(e, true)}
      on:keyup={(e) => onKey(e, false)}
    ></canvas>
  </div>
</RemoteConsoleChrome>

<style>
  .kvm-stage {
    flex: 1;
    width: 100%;
    height: 100%;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #000;
  }
  .kvm-canvas {
    flex: none;
    max-width: 100%;
    max-height: 100%;
    background: #000;
    outline: none;
    cursor: crosshair;
  }
</style>
