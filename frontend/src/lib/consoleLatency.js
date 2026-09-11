/**
 * Shared console RTT probe: ping/pong on the same WebSocket as the video
 * stream. The Rackflow WS bridge answers ``{"type":"ping"}`` with
 * ``{"type":"pong"}`` and never forwards those frames upstream.
 *
 * VM VNC uses text frames. Bare-metal KVM is a binary subprotocol — text
 * pings are dropped (every sample times out at 3000ms). Use
 * ``encoding: 'binary'`` there.
 */

export const LATENCY_HISTORY = 60;
export const LATENCY_INTERVAL_MS = 1000;
export const LATENCY_TIMEOUT_MS = 3000;

export function computeJitter(samples) {
  if (!samples || samples.length < 2) return null;
  let sum = 0;
  for (let i = 1; i < samples.length; i += 1) {
    sum += Math.abs(samples[i] - samples[i - 1]);
  }
  return Math.round(sum / (samples.length - 1));
}

/** Prefer stability over absolute RTT: ~80ms steady is fine; spiky is not. */
export function assessLatencyHealth(samples) {
  if (!samples || samples.length < 4) return 'unknown';
  const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
  const jitter = computeJitter(samples);
  if (mean >= 400 || jitter >= 45) return 'poor';
  if (jitter >= 20 || mean >= 220) return 'unstable';
  return 'healthy';
}

export function latencyHealthLabel(health) {
  if (health === 'healthy') return 'Healthy';
  if (health === 'unstable') return 'Unstable';
  if (health === 'poor') return 'Poor';
  return 'Measuring…';
}

export function latencySparkline(samples) {
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

let pingSeq = 0;

function pingId() {
  pingSeq += 1;
  return `${Date.now()}-${pingSeq}`;
}

function asUint8(data) {
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  return null;
}

/** Parse a ping/pong control payload from a text or binary WS frame. */
export function parseControlMessage(data) {
  if (typeof data === 'string') {
    if (!data.startsWith('{')) return null;
    try {
      return JSON.parse(data);
    } catch (_) {
      return null;
    }
  }
  const u8 = asUint8(data);
  if (!u8 || u8.length < 2 || u8[0] !== 0x7b) return null;
  try {
    return JSON.parse(new TextDecoder().decode(u8));
  } catch (_) {
    return null;
  }
}

/**
 * Wrap ``ws.onmessage`` so control frames are consumed here and video
 * frames still reach the original handler (noVNC / AMI / ATEN).
 *
 * ``onSample({ ms, samples })`` is called on each RTT (and on timeout spikes
 * after the first successful pong).
 */
export function createWsLatencyProbe({
  onSample,
  intervalMs = LATENCY_INTERVAL_MS,
  timeoutMs = LATENCY_TIMEOUT_MS,
  history = LATENCY_HISTORY,
  encoding = 'text',
} = {}) {
  let ws = null;
  let timer = null;
  let pending = null;
  let samples = [];
  let originalOnMessage = null;
  let messageHandler = null;
  let everPonged = false;

  function clearPending() {
    if (pending?.timeoutId != null) clearTimeout(pending.timeoutId);
    pending = null;
  }

  function record(ms) {
    const value = Math.max(0, Math.round(ms));
    samples = [...samples.slice(-(history - 1)), value];
    onSample?.({ ms: value, samples });
  }

  function sendPing() {
    if (ws?.readyState !== WebSocket.OPEN || pending) return;
    const t = pingId();
    const sentAt = performance.now();
    const timeoutId = setTimeout(() => {
      if (pending?.t === t) {
        pending = null;
        // Don't paint the timeout value as RTT before we have ever heard
        // a pong (KVM used to sit at "3000ms Poor" forever).
        if (everPonged) record(timeoutMs);
      }
    }, timeoutMs);
    pending = { t, sentAt, timeoutId };
    try {
      const payload = JSON.stringify({ type: 'ping', t });
      if (encoding === 'binary') {
        ws.send(new TextEncoder().encode(payload));
      } else {
        ws.send(payload);
      }
    } catch (_) {
      // Socket already closing; drop this probe rather than leave it pending.
      clearPending();
    }
  }

  function onMessage(event) {
    const msg = parseControlMessage(event.data);
    if (msg) {
      if (msg.type === 'pong' && pending && msg.t === pending.t) {
        const rtt = performance.now() - pending.sentAt;
        clearPending();
        everPonged = true;
        record(rtt);
      }
      return;
    }
    if (typeof originalOnMessage === 'function') {
      originalOnMessage(event);
    }
  }

  function detach() {
    clearPending();
    if (timer != null) {
      clearInterval(timer);
      timer = null;
    }
    if (ws && messageHandler && ws.onmessage === messageHandler) {
      try {
        ws.onmessage = originalOnMessage;
      } catch (_) {
        /* ignore */
      }
    }
    ws = null;
    originalOnMessage = null;
    messageHandler = null;
    everPonged = false;
  }

  function attach(socket) {
    detach();
    if (!socket) return;
    ws = socket;
    originalOnMessage = socket.onmessage;
    messageHandler = onMessage;
    socket.onmessage = onMessage;
    samples = [];
    everPonged = false;
    onSample?.({ ms: null, samples });
    sendPing();
    timer = setInterval(sendPing, intervalMs);
  }

  return { attach, detach };
}
