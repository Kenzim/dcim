<script>
  import { onDestroy, onMount } from 'svelte';
  import { readClipboardOrPrompt } from '../lib/clipboardType.js';
  import { eventToFramebuffer, letterboxCanvas, watchCanvasFit } from '../lib/kvmPointer.js';
  import RemoteConsoleChrome from './RemoteConsoleChrome.svelte';

  /** Session from POST /api/kvm/redeem. The NanoKVM token stays on the server. */
  export let session;

  const MOD = {
    ControlLeft: ['ctrl', 1],
    ShiftLeft: ['shift', 2],
    AltLeft: ['alt', 4],
    MetaLeft: ['meta', 8],
    ControlRight: ['ctrl', 16],
    ShiftRight: ['shift', 32],
    AltRight: ['alt', 64],
    MetaRight: ['meta', 128],
  };

  let canvas;
  let stage;
  let ctx;
  let status = 'connecting';
  let errorMessage = '';
  let resText = '';
  let versionText = '';
  let noSignal = false;
  let pasting = false;
  let reconnecting = false;
  let ws = null;
  let destroyed = false;
  let stopFitWatch = null;
  let pingTimer = null;
  let pasteTimer = null;
  let drawing = false;
  let pendingFrame = null;
  let lastMouse = 0;
  let mod = { ctrl: 0, shift: 0, alt: 0, meta: 0 };
  const pressed = new Set();

  function send(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(payload));
  }

  function sendKey(code, down) {
    const mapped = MOD[code];
    if (down && mapped) mod[mapped[0]] = mapped[1];
    if (!down && mapped) mod[mapped[0]] = 0;
    send({
      type: 'key',
      code,
      down,
      ctrl: mod.ctrl,
      shift: mod.shift,
      alt: mod.alt,
      meta: mod.meta,
    });
  }

  function releaseKeys() {
    for (const code of pressed) sendKey(code, false);
    pressed.clear();
    mod = { ctrl: 0, shift: 0, alt: 0, meta: 0 };
    send({ type: 'key', code: '', down: false, ctrl: 0, shift: 0, alt: 0, meta: 0 });
  }

  function sendCtrlAltDel() {
    send({ type: 'key', code: 'Delete', down: true, ctrl: 1, shift: 0, alt: 4, meta: 0 });
    send({ type: 'key', code: 'Delete', down: false, ctrl: 0, shift: 0, alt: 0, meta: 0 });
  }

  function sendMouse(ev, event, button) {
    if (!canvas || !canvas.width || !canvas.height) return;
    const pt = eventToFramebuffer(ev, canvas);
    if (!pt) return;
    const payload = {
      type: 'mouse',
      event,
      x: pt.x / canvas.width,
      y: pt.y / canvas.height,
    };
    if (button !== undefined) payload.button = button;
    send(payload);
  }

  async function pasteClipboard() {
    const text = await readClipboardOrPrompt();
    if (!text || destroyed) return;
    pasting = true;
    send({ type: 'paste', text: text.slice(0, 1024) });
    clearTimeout(pasteTimer);
    pasteTimer = setTimeout(() => {
      pasting = false;
    }, Math.min(8000, 200 + text.length * 60));
  }

  function handleControl(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch (_) {
      return;
    }
    if (!message || typeof message !== 'object') return;
    if (message.type === 'ready') {
      status = 'connected';
      const version = message.application ? `NanoKVM ${message.application}` : 'NanoKVM';
      versionText = message.input ? version : `${version} (keyboard unavailable)`;
      if (!resText) resText = versionText;
      return;
    }
    if (message.type === 'error') {
      status = 'error';
      errorMessage = message.message || 'NanoKVM console failed';
    }
  }

  async function paintFrames() {
    if (drawing) return;
    drawing = true;
    try {
      while (pendingFrame && !destroyed && ctx && canvas) {
        const frame = pendingFrame;
        pendingFrame = null;
        try {
          const bitmap = await createImageBitmap(new Blob([frame], { type: 'image/jpeg' }));
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            letterboxCanvas(canvas, stage);
          }
          ctx.drawImage(bitmap, 0, 0);
          bitmap.close();
          resText = versionText
            ? `${canvas.width}×${canvas.height} · ${versionText}`
            : `${canvas.width}×${canvas.height}`;
          status = 'connected';
          noSignal = false;
        } catch (_) {
          /* a partial JPEG is replaced by the next frame */
        }
      }
    } finally {
      drawing = false;
      if (pendingFrame && !destroyed) paintFrames();
    }
  }

  function connect() {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const path = session.ws_path || '/api/kvm/ws';
    const socket = new WebSocket(
      `${proto}://${window.location.host}${path}?token=${encodeURIComponent(session.ws_token)}`,
      ['binary'],
    );
    socket.binaryType = 'arraybuffer';
    ws = socket;
    status = 'connecting';
    socket.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        handleControl(ev.data);
        return;
      }
      pendingFrame = new Uint8Array(ev.data);
      paintFrames();
    };
    socket.onerror = () => {
      if (!errorMessage) errorMessage = 'Console connection failed';
      status = 'error';
    };
    socket.onclose = (ev) => {
      if (destroyed) return;
      if (ev.reason) errorMessage = ev.reason;
      status = errorMessage ? 'error' : 'disconnected';
    };
    clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ type: 'ping', t: Date.now() }), 15000);
  }

  function teardown() {
    clearInterval(pingTimer);
    pingTimer = null;
    releaseKeys();
    const old = ws;
    ws = null;
    if (old) {
      try {
        old.close();
      } catch (_) {
        /* ignore */
      }
    }
  }

  async function reconnect() {
    if (reconnecting || destroyed) return;
    reconnecting = true;
    errorMessage = '';
    try {
      teardown();
      connect();
    } finally {
      reconnecting = false;
    }
  }

  function onKeyDown(ev) {
    if (ev.repeat) return;
    ev.preventDefault();
    pressed.add(ev.code);
    sendKey(ev.code, true);
  }

  function onKeyUp(ev) {
    ev.preventDefault();
    pressed.delete(ev.code);
    sendKey(ev.code, false);
  }

  onMount(() => {
    ctx = canvas.getContext('2d');
    stopFitWatch = watchCanvasFit(stage, () => letterboxCanvas(canvas, stage));
    window.addEventListener('blur', releaseKeys);
    connect();
  });

  onDestroy(() => {
    destroyed = true;
    clearTimeout(pasteTimer);
    window.removeEventListener('blur', releaseKeys);
    stopFitWatch?.();
    teardown();
  });
</script>

<RemoteConsoleChrome
  {status}
  {errorMessage}
  {pasting}
  {reconnecting}
  extraLabel={resText}
  {noSignal}
  powerToken={session.ws_token}
  onCad={sendCtrlAltDel}
  onPaste={pasteClipboard}
  onReconnect={reconnect}
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
        sendMouse(e, 'move');
      }}
      on:mousedown|preventDefault={(e) => {
        if (canvas) canvas.focus();
        sendMouse(e, 'down', e.button);
      }}
      on:mouseup|preventDefault={(e) => sendMouse(e, 'up', e.button)}
      on:wheel|preventDefault={(e) => send({ type: 'mouse', event: 'wheel', delta: e.deltaY })}
      on:keydown={onKeyDown}
      on:keyup={onKeyUp}
      on:blur={releaseKeys}
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
