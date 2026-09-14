<script>
  import { onDestroy, onMount } from 'svelte';
  import { readClipboardOrPrompt, typeIntoHid } from '../lib/clipboardType.js';
  import { createWsLatencyProbe } from '../lib/consoleLatency.js';
  import { eventToFramebuffer, letterboxCanvas, watchCanvasFit } from '../lib/kvmPointer.js';
  import RemoteConsoleChrome from './RemoteConsoleChrome.svelte';

  /** Session from POST /api/kvm/redeem. BMC cookies/tokens stay on the server. */
  export let session;

  const IVTP = {
    HDR: 8,
    CMD_HID: 0x01,
    CMD_RESUME: 0x06,
    CMD_STOP: 0x08,
    CMD_BLANK: 0x09,
    CMD_FULL: 0x0b,
    CMD_VALIDATED: 0x13,
    CMD_MAX_SESSION: 0x16,
    CMD_ALLOWED: 0x17,
    CMD_VIDEO: 0x19,
    CMD_ACTIVE: 0x27,
    CMD_KEEPALIVE: 0x39,
  };

  const IUSB = {
    HID_HDR: 34,
    HDR: 32,
    KEYBD: 48,
    MOUSE: 49,
    PROTO_KEYBD: 16,
    PROTO_MOUSE: 32,
    FROM_REMOTE: 128,
    KEYBD_DEV: 2,
    KEYBD_IF: 0,
    MOUSE_DEV: 2,
    MOUSE_IF: 1,
    MAJOR: 1,
    MINOR: 0,
  };

  const HID_KEY = {
    8: 42, 9: 43, 13: 40, 27: 41, 32: 44,
    33: 75, 34: 78, 35: 77, 36: 74, 37: 80, 38: 82, 39: 79, 40: 81,
    45: 73, 46: 76, 48: 39,
    49: 30, 50: 31, 51: 32, 52: 33, 53: 34, 54: 35, 55: 36, 56: 37, 57: 38,
    65: 4, 66: 5, 67: 6, 68: 7, 69: 8, 70: 9, 71: 10, 72: 11, 73: 12, 74: 13,
    75: 14, 76: 15, 77: 16, 78: 17, 79: 18, 80: 19, 81: 20, 82: 21, 83: 22,
    84: 23, 85: 24, 86: 25, 87: 26, 88: 27, 89: 28, 90: 29,
    91: 227, 93: 101, 112: 58, 113: 59, 114: 60, 115: 61, 116: 62, 117: 63,
    118: 64, 119: 65, 120: 66, 121: 67, 122: 68, 123: 69,
    186: 51, 187: 46, 188: 54, 189: 45, 190: 55, 191: 56, 192: 53,
    219: 47, 220: 49, 221: 48, 222: 52,
  };

  const MAX_AUTO_RETRIES = 5;

  let canvas;
  let stage;
  let chrome;
  let stopFitWatch = null;
  let ctx;
  let status = 'connecting';
  let errorMessage = '';
  let resText = '';
  let noSignal = false;
  let ws = null;
  let worker = null;
  let keepAlive = null;
  let imageBuffer = null;
  let fatal = null;
  let seqKbd = 0;
  let seqMouse = 0;
  let modifiers = 0;
  let buttons = 0;
  let lastMouse = 0;
  let prevComplete = true;
  let frameChunks = [];
  let frameGot = 0;
  let compressSize = 0;
  let header = null;
  let videoFrames = 0;
  let destroyed = false;
  let reconnecting = false;
  let pasting = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  const buf = { u8: new Uint8Array(0) };

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

  function ivtp(type, pktStatus, payload) {
    const n = payload ? payload.byteLength : 0;
    const out = new ArrayBuffer(IVTP.HDR + n);
    const dv = new DataView(out);
    const u8 = new Uint8Array(out);
    dv.setUint16(0, type, true);
    dv.setUint32(2, n, true);
    dv.setUint16(6, pktStatus, true);
    if (payload) u8.set(new Uint8Array(payload), 8);
    return out;
  }

  function iusbHid(dev, proto, devNum, ifNum, seq, report) {
    const pktSize = IUSB.HID_HDR - 1 + report.length;
    const out = new ArrayBuffer(IVTP.HDR + pktSize);
    const dv = new DataView(out);
    const u8 = new Uint8Array(out);
    dv.setUint16(0, IVTP.CMD_HID, true);
    dv.setUint32(2, pktSize, true);
    dv.setUint16(6, 0, true);
    const o = 8;
    u8.set(new TextEncoder().encode('IUSB    '), o);
    u8[o + 8] = IUSB.MAJOR;
    u8[o + 9] = IUSB.MINOR;
    u8[o + 10] = IUSB.HDR;
    u8[o + 11] = 0;
    dv.setInt32(o + 12, pktSize - IUSB.HDR, true);
    u8[o + 16] = 0;
    u8[o + 17] = dev;
    u8[o + 18] = proto;
    u8[o + 19] = IUSB.FROM_REMOTE;
    u8[o + 20] = devNum;
    u8[o + 21] = ifNum;
    u8[o + 22] = 0;
    u8[o + 23] = 0;
    dv.setInt32(o + 24, seq, true);
    u8[o + 28] = 0;
    u8[o + 29] = 0;
    u8[o + 30] = 0;
    u8[o + 31] = 0;
    u8[o + 32] = report.length & 0xff;
    u8.set(report, o + 33);
    let sum = 0;
    for (let i = 8; i < 8 + IUSB.HDR; i++) sum = (sum + u8[i]) & 0xff;
    u8[19] = (-sum) & 0xff;
    return out;
  }

  function appendBuf(chunk) {
    const n = new Uint8Array(buf.u8.length + chunk.length);
    n.set(buf.u8);
    n.set(chunk, buf.u8.length);
    buf.u8 = n;
  }

  function takeBuf(n) {
    const copy = new Uint8Array(buf.u8.subarray(0, n));
    buf.u8 = buf.u8.subarray(n);
    return copy;
  }

  function send(out) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(out);
  }

  function startStreaming() {
    if (keepAlive) return;
    status = 'connected';
    errorMessage = '';
    autoRetries = 0;
    if (!videoFrames) noSignal = true;
    keepAlive = setInterval(() => send(ivtp(IVTP.CMD_KEEPALIVE, 0, null)), 3000);
    send(ivtp(IVTP.CMD_FULL, 1, null));
    nudgeMouse();
    latencyProbe.attach(ws);
  }

  function showNoSignal({ nudge = true } = {}) {
    noSignal = true;
    resText = '';
    if (ctx && canvas) {
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    if (nudge) nudgeMouse();
  }

  function markHasVideo(w, h) {
    noSignal = false;
    if (w && h) resText = `${w}×${h}`;
  }

  function postDecodeBuffer(w, h) {
    if (!worker || !w || !h) return;
    // Always send dimensions. The same-origin decode bridge allocates
    // ImageData inside the worker when a cloned buffer arrives with null .data.
    const msg = { cmd: 'imageBuffer', imageBuffer, w, h };
    worker.postMessage(msg);
    worker.postMessage({ cmd: 'resolution_changed', imageBuffer, w, h });
  }

  function resetImage(w, h, { advertise = true } = {}) {
    if (!canvas || !ctx) return;
    canvas.width = w;
    canvas.height = h;
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, w, h);
    imageBuffer = new ImageData(w, h);
    ctx.putImageData(imageBuffer, 0, 0);
    postDecodeBuffer(w, h);
    if (advertise) resText = `${w}×${h}`;
    letterboxCanvas(canvas, stage);
  }

  function onVideo(payload) {
    if (payload.length < 2) return;
    if (prevComplete) {
      if (payload.length < 88) return;
      const hdr = payload.subarray(2, 2 + 86);
      const x = hdr[4] | (hdr[5] << 8);
      const y = hdr[6] | (hdr[7] << 8);
      const csize = hdr[69] | (hdr[70] << 8) | (hdr[71] << 16) | (hdr[72] << 24);
      header = {
        SourceModeInfo: { X: x, Y: y },
        DestinationModeInfo: {
          X: hdr[13] | (hdr[14] << 8),
          Y: hdr[15] | (hdr[16] << 8),
        },
        FrameHeader: {
          JPEGTableSelector: hdr[44],
          JPEGYUVTableMapping: hdr[45],
          AdvanceTableSelector: hdr[47],
          RC4Enable: hdr[53],
        },
        Mode420: hdr[55],
        CompressData: { CompressSize: csize },
      };
      compressSize = csize;
      if (!x || !y || !csize) {
        showNoSignal({ nudge: false });
        prevComplete = true;
        frameChunks = [];
        frameGot = 0;
        return;
      }
      markHasVideo(x, y);
      if (canvas && (x !== canvas.width || y !== canvas.height)) {
        resetImage(x, y);
      }
      frameChunks = [payload.subarray(88)];
      frameGot = payload.length - 88;
    } else {
      frameChunks.push(payload.subarray(2));
      frameGot += payload.length - 2;
    }
    if (frameGot < compressSize) {
      prevComplete = false;
      return;
    }
    prevComplete = true;
    const raw = new Uint8Array(compressSize);
    let off = 0;
    for (const c of frameChunks) {
      const n = Math.min(c.length, compressSize - off);
      raw.set(c.subarray(0, n), off);
      off += n;
      if (off >= compressSize) break;
    }
    const words = (compressSize / 4) | 0;
    const aligned = new ArrayBuffer(words * 4);
    new Uint8Array(aligned).set(raw.subarray(0, words * 4));
    if (worker) worker.postMessage({ header, buffer: new Int32Array(aligned) });
    videoFrames += 1;
  }

  function onPkt(type, pktStatus, payload) {
    switch (type) {
      case IVTP.CMD_ALLOWED:
        // Handshake already happened server-side; ignore if a leftover 0x17 arrives.
        break;
      case IVTP.CMD_MAX_SESSION:
        errorMessage = 'BMC reports KVM max sessions — wait for timeout or kick the other viewer';
        break;
      case IVTP.CMD_VALIDATED: {
        const ok = payload.length ? payload[0] : 0;
        if (ok !== 1) {
          fatal = `KVM token rejected (${ok})`;
          status = 'error';
          errorMessage = fatal;
          send(ivtp(IVTP.CMD_STOP, 0, null));
          if (ws) ws.close();
          return;
        }
        startStreaming();
        break;
      }
      case IVTP.CMD_ACTIVE:
        send(ivtp(IVTP.CMD_FULL, 1, null));
        break;
      case IVTP.CMD_KEEPALIVE:
        send(ivtp(IVTP.CMD_KEEPALIVE, 0, null));
        break;
      case IVTP.CMD_BLANK:
        showNoSignal();
        break;
      case IVTP.CMD_STOP:
        errorMessage = `BMC stopped KVM session (${pktStatus})`;
        break;
      case IVTP.CMD_VIDEO:
        onVideo(payload);
        break;
      default:
        break;
    }
  }

  function drain() {
    while (buf.u8.length >= IVTP.HDR) {
      const dv = new DataView(buf.u8.buffer, buf.u8.byteOffset, buf.u8.byteLength);
      const type = dv.getUint16(0, true);
      const size = dv.getUint32(2, true);
      const pktStatus = dv.getUint16(6, true);
      if (buf.u8.length < IVTP.HDR + size) return;
      takeBuf(IVTP.HDR);
      const payload = takeBuf(size);
      onPkt(type, pktStatus, payload);
    }
  }

  function sendKey(keyCode, location, down) {
    let hid = HID_KEY[keyCode];
    if (keyCode === 17) hid = location === 2 ? 228 : 224;
    if (keyCode === 16) hid = location === 2 ? 229 : 225;
    if (keyCode === 18) hid = location === 2 ? 230 : 226;
    if (keyCode === 91 || keyCode === 92) hid = location === 2 ? 231 : 227;
    const bits = { 17: [0x01, 0x10], 16: [0x02, 0x20], 18: [0x04, 0x40], 91: [0x08, 0x80], 92: [0x08, 0x80] };
    if (bits[keyCode]) {
      const [l, r] = bits[keyCode];
      const bit = location === 2 ? r : l;
      modifiers = down ? (modifiers | bit) : (modifiers & ~bit);
    }
    const keys = new Uint8Array(6);
    let autoBreak = 0;
    if (down && hid && ![16, 17, 18, 91, 92].includes(keyCode)) {
      keys[0] = hid;
      autoBreak = 1;
    }
    const report = new Uint8Array(8);
    report[0] = modifiers;
    report[1] = autoBreak;
    report.set(keys, 2);
    send(iusbHid(IUSB.KEYBD, IUSB.PROTO_KEYBD, IUSB.KEYBD_DEV, IUSB.KEYBD_IF, seqKbd++, report));
  }

  function nudgeMouse() {
    const report = new Uint8Array(6);
    const dv = new DataView(report.buffer);
    dv.setInt16(1, 16383, true);
    dv.setInt16(3, 16383, true);
    send(iusbHid(IUSB.MOUSE, IUSB.PROTO_MOUSE, IUSB.MOUSE_DEV, IUSB.MOUSE_IF, seqMouse++, report));
  }

  function sendMouse(ev) {
    if (!canvas) return;
    const pt = eventToFramebuffer(ev, canvas);
    if (!pt) return;
    const sx = ((pt.x * 32767) / canvas.width) + 0.5;
    const sy = ((pt.y * 32767) / canvas.height) + 0.5;
    const report = new Uint8Array(6);
    const dv = new DataView(report.buffer);
    report[0] = buttons;
    dv.setInt16(1, sx, true);
    dv.setInt16(3, sy, true);
    report[5] = 0;
    send(iusbHid(IUSB.MOUSE, IUSB.PROTO_MOUSE, IUSB.MOUSE_DEV, IUSB.MOUSE_IF, seqMouse++, report));
  }

  function sendCtrlAltDel() {
    if (!isLive()) return;
    if (canvas) canvas.focus();
    modifiers = 0x01 | 0x04;
    sendKey(17, 1, true);
    sendKey(18, 1, true);
    sendKey(46, 0, true);
    setTimeout(() => {
      modifiers = 0;
      sendKey(17, 1, false);
      sendKey(18, 1, false);
      sendKey(46, 0, false);
    }, 80);
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
    await typeIntoHid(sendKey, text, { ...opts, stillConnected: isLive });
    modifiers = 0;
  }

  function resetDecodeState() {
    buf.u8 = new Uint8Array(0);
    prevComplete = true;
    frameChunks = [];
    frameGot = 0;
    compressSize = 0;
    header = null;
    videoFrames = 0;
    seqKbd = 0;
    seqMouse = 0;
    modifiers = 0;
    buttons = 0;
    imageBuffer = null;
    noSignal = false;
    resText = '';
  }

  function startWorker() {
    const workerPath = session.decode_worker_path || '/api/kvm/assets/libs/kvm/ast/decode_worker.js';
    const workerQs = new URLSearchParams({
      token: session.ws_token,
      src: workerPath,
    });
    worker = new Worker(`/kvm-decode-bridge.js?${workerQs}`);
    worker.onerror = (e) => {
      status = 'error';
      errorMessage = `worker error: ${e.message || 'failed to load decoder'}`;
    };
    worker.onmessage = (e) => {
      if (e.data.cmd === 'draw') {
        if (noSignal) return;
        imageBuffer = e.data.ibuf;
        if (
          imageBuffer &&
          canvas &&
          imageBuffer.width &&
          imageBuffer.height &&
          (imageBuffer.width !== canvas.width || imageBuffer.height !== canvas.height)
        ) {
          canvas.width = imageBuffer.width;
          canvas.height = imageBuffer.height;
          resText = `${imageBuffer.width}×${imageBuffer.height}`;
          letterboxCanvas(canvas, stage);
        }
        if (imageBuffer && ctx) ctx.putImageData(imageBuffer, 0, 0);
      } else if (e.data.cmd === 'exception') {
        errorMessage = 'decode error';
        console.error(e.data.ex);
      }
    };
  }

  function openSocket() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const path = session.ws_path || '/api/kvm/ws';
    const socket = new WebSocket(`${proto}://${location.host}${path}?token=${encodeURIComponent(session.ws_token)}`, ['binary']);
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
      if (keepAlive) {
        clearInterval(keepAlive);
        keepAlive = null;
      }
      latencyProbe.detach();
      chrome?.closeLatencyGraph?.();
      if (destroyed || reconnecting) return;
      if (fatal) {
        status = 'error';
        errorMessage = fatal;
        return;
      }
      status = 'disconnected';
      if (e.code !== 1000) {
        errorMessage = `disconnected (${e.code} ${e.reason || ''})`.trim();
        scheduleAutoReconnect();
      }
    };
    socket.onmessage = (ev) => {
      if (ws !== socket) return;
      try {
        appendBuf(new Uint8Array(ev.data));
        drain();
      } catch (err) {
        errorMessage = `parse error: ${err.message}`;
      }
    };
  }

  function connect() {
    if (destroyed) return;
    status = 'connecting';
    errorMessage = '';
    fatal = null;
    resetDecodeState();
    startWorker();
    resetImage(640, 480, { advertise: false });
    openSocket();
  }

  function clearAutoReconnect() {
    if (autoReconnectTimer != null) {
      clearTimeout(autoReconnectTimer);
      autoReconnectTimer = null;
    }
  }

  function scheduleAutoReconnect() {
    if (destroyed || reconnecting || fatal) return;
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

  function teardown() {
    clearAutoReconnect();
    latencyProbe.detach();
    chrome?.closeLatencyGraph?.();
    if (keepAlive) {
      clearInterval(keepAlive);
      keepAlive = null;
    }
    if (ws) {
      const old = ws;
      ws = null;
      try {
        if (old.readyState === WebSocket.OPEN) old.send(ivtp(IVTP.CMD_STOP, 0, null));
        old.close();
      } catch (_) {
        /* ignore */
      }
    }
    if (worker) {
      worker.terminate();
      worker = null;
    }
  }

  async function reconnect({ auto = false } = {}) {
    if (reconnecting || destroyed) return;
    reconnecting = true;
    clearAutoReconnect();
    if (!auto) {
      autoRetries = 0;
      fatal = null;
      errorMessage = '';
    }
    try {
      teardown();
      connect();
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
      if (auto) scheduleAutoReconnect();
    } finally {
      reconnecting = false;
    }
  }

  onMount(() => {
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    stopFitWatch = watchCanvasFit(stage, () => letterboxCanvas(canvas, stage));
    connect();
  });

  onDestroy(() => {
    destroyed = true;
    stopFitWatch?.();
    teardown();
  });
</script>

<RemoteConsoleChrome
  bind:this={chrome}
  {status}
  {errorMessage}
  {pasting}
  {reconnecting}
  extraLabel={resText}
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
      on:click={() => canvas && canvas.focus()}
      on:contextmenu|preventDefault
      on:mousemove={(e) => {
        const now = performance.now();
        if (now - lastMouse < 16) return;
        lastMouse = now;
        sendMouse(e);
      }}
      on:mousedown|preventDefault={(e) => {
        if (canvas) canvas.focus();
        buttons |= [1, 4, 2][e.button] || 0;
        sendMouse(e);
      }}
      on:mouseup|preventDefault={(e) => {
        buttons &= ~([1, 4, 2][e.button] || 0);
        sendMouse(e);
      }}
      on:keydown|preventDefault={(e) => sendKey(e.keyCode, e.location, true)}
      on:keyup|preventDefault={(e) => sendKey(e.keyCode, e.location, false)}
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
