/**
 * Helpers for the console "Paste Clipboard" buttons: read text from the
 * system clipboard (with a prompt fallback when the Clipboard API is
 * blocked), and map characters to RFB/X11 key events for noVNC typing.
 *
 * Guests behind QEMU (especially macOS) often ignore bare Latin-1 keysyms
 * for Shift characters like ``!`` / ``@``. Typing must send the physical
 * US-QWERTY key ``code`` (scancode) plus Shift when needed, matching what
 * a real keyboard produces.
 */

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Read clipboard text. Falls back to a prompt when `navigator.clipboard`
 * is unavailable (HTTP, denied permission, older browsers) so the user
 * can still paste manually into a dialog.
 */
export async function readClipboardOrPrompt() {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.readText === 'function') {
      const text = await navigator.clipboard.readText();
      if (text) return text;
    }
  } catch (_) {
    /* fall through to prompt */
  }
  return window.prompt('Paste text to type into the console:') ?? '';
}

/** Normalize newlines for serial TTYs (prefer CR). */
export function normalizeSerialText(text) {
  return String(text).replace(/\r\n/g, '\n').replace(/\n/g, '\r');
}

/** Normalize newlines for VNC typing (prefer LF → Return once). */
export function normalizeVncText(text) {
  return String(text).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

// X11 keysyms used by RFB (matches @novnc/novnc core/input/keysym.js).
const XK_Return = 0xff0d;
const XK_Tab = 0xff09;
const XK_BackSpace = 0xff08;
const XK_Escape = 0xff1b;
const XK_Shift_L = 0xffe1;

/**
 * US-QWERTY unshifted → shifted punctuation (and digit row).
 * Values are HTML KeyboardEvent.code names from noVNC's XtScancode map.
 */
const US_PUNCT = {
  ' ': { code: 'Space', shift: false },
  '`': { code: 'Backquote', shift: false },
  '~': { code: 'Backquote', shift: true },
  '1': { code: 'Digit1', shift: false },
  '!': { code: 'Digit1', shift: true },
  '2': { code: 'Digit2', shift: false },
  '@': { code: 'Digit2', shift: true },
  '3': { code: 'Digit3', shift: false },
  '#': { code: 'Digit3', shift: true },
  '4': { code: 'Digit4', shift: false },
  $: { code: 'Digit4', shift: true },
  '5': { code: 'Digit5', shift: false },
  '%': { code: 'Digit5', shift: true },
  '6': { code: 'Digit6', shift: false },
  '^': { code: 'Digit6', shift: true },
  '7': { code: 'Digit7', shift: false },
  '&': { code: 'Digit7', shift: true },
  '8': { code: 'Digit8', shift: false },
  '*': { code: 'Digit8', shift: true },
  '9': { code: 'Digit9', shift: false },
  '(': { code: 'Digit9', shift: true },
  '0': { code: 'Digit0', shift: false },
  ')': { code: 'Digit0', shift: true },
  '-': { code: 'Minus', shift: false },
  _: { code: 'Minus', shift: true },
  '=': { code: 'Equal', shift: false },
  '+': { code: 'Equal', shift: true },
  '[': { code: 'BracketLeft', shift: false },
  '{': { code: 'BracketLeft', shift: true },
  ']': { code: 'BracketRight', shift: false },
  '}': { code: 'BracketRight', shift: true },
  '\\': { code: 'Backslash', shift: false },
  '|': { code: 'Backslash', shift: true },
  ';': { code: 'Semicolon', shift: false },
  ':': { code: 'Semicolon', shift: true },
  "'": { code: 'Quote', shift: false },
  '"': { code: 'Quote', shift: true },
  ',': { code: 'Comma', shift: false },
  '<': { code: 'Comma', shift: true },
  '.': { code: 'Period', shift: false },
  '>': { code: 'Period', shift: true },
  '/': { code: 'Slash', shift: false },
  '?': { code: 'Slash', shift: true },
};

/**
 * Map a single Unicode code point to an RFB/X11 keysym, mirroring
 * noVNC's keysymdef.lookup (Latin-1 1:1, then 0x01000000 | codepoint).
 */
export function charToKeysym(ch) {
  if (!ch) return null;
  const u = ch.codePointAt(0);
  if (u === 0x09) return XK_Tab;
  if (u === 0x0a || u === 0x0d) return XK_Return;
  if (u === 0x08) return XK_BackSpace;
  if (u === 0x1b) return XK_Escape;
  if (u >= 0x20 && u <= 0xff) return u;
  return 0x01000000 | u;
}

/**
 * Map a character to a physical keystroke for QEMU/noVNC.
 * @returns {{ keysym: number, code: string|null, shift: boolean }|null}
 */
export function charToKeyStroke(ch) {
  if (!ch) return null;
  const keysym = charToKeysym(ch);
  if (keysym == null) return null;

  if (ch === '\n' || ch === '\r') {
    return { keysym: XK_Return, code: 'Enter', shift: false };
  }
  if (ch === '\t') {
    return { keysym: XK_Tab, code: 'Tab', shift: false };
  }
  if (ch === '\b') {
    return { keysym: XK_BackSpace, code: 'Backspace', shift: false };
  }
  if (ch === '\u001b') {
    return { keysym: XK_Escape, code: 'Escape', shift: false };
  }

  if (/^[a-z]$/.test(ch)) {
    return { keysym, code: `Key${ch.toUpperCase()}`, shift: false };
  }
  if (/^[A-Z]$/.test(ch)) {
    return { keysym, code: `Key${ch}`, shift: true };
  }

  const punct = US_PUNCT[ch];
  if (punct) {
    return { keysym, code: punct.code, shift: punct.shift };
  }

  // Fallback: keysym only (non-US glyphs). Better than dropping the char.
  return { keysym, code: null, shift: false };
}

function sendKeyStroke(rfb, { keysym, code, shift }) {
  if (shift) {
    rfb.sendKey(XK_Shift_L, 'ShiftLeft', true);
  }
  rfb.sendKey(keysym, code, true);
  rfb.sendKey(keysym, code, false);
  if (shift) {
    rfb.sendKey(XK_Shift_L, 'ShiftLeft', false);
  }
}

/**
 * Type ``text`` into a connected noVNC RFB client via key events
 * (down+up per character), with a small delay so guests don't drop keys.
 */
export async function typeIntoRfb(rfb, text, { delayMs = 20, submit = false } = {}) {
  if (!rfb || !text) return;
  const stillConnected = () => rfb._rfbConnectionState === 'connected';
  if (!stillConnected()) return;
  const normalized = normalizeVncText(text);
  try {
    rfb.focus?.();
  } catch (_) {
    /* ignore */
  }
  for (const ch of normalized) {
    if (!stillConnected()) return;
    const stroke = charToKeyStroke(ch);
    if (!stroke) continue;
    // Down+up explicitly — some guests (macOS loginwindow) drop the
    // combined sendKey(keysym, null) form under load. Shift chars need
    // a real ShiftLeft + scancode pair for QEMU Extended Key Event.
    try {
      sendKeyStroke(rfb, stroke);
    } catch (_) {
      return;
    }
    if (delayMs > 0) await sleep(delayMs);
  }
  if (submit && stillConnected()) {
    await sleep(Math.max(delayMs, 30));
    try {
      sendKeyStroke(rfb, { keysym: XK_Return, code: 'Enter', shift: false });
    } catch (_) {
      /* ignore */
    }
  }
}

/**
 * Send ``text`` over an open serial WebSocket as raw UTF-8 bytes (same path
 * as xterm onData keystrokes), chunked so large pastes don't flood the bridge.
 */
export async function typeIntoSerialWs(ws, text, { chunkSize = 64, delayMs = 10 } = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !text) return;
  const normalized = normalizeSerialText(text);
  const encoder = new TextEncoder();
  for (let i = 0; i < normalized.length; i += chunkSize) {
    ws.send(encoder.encode(normalized.slice(i, i + chunkSize)));
    if (i + chunkSize < normalized.length && delayMs > 0) await sleep(delayMs);
  }
}

/**
 * HTML KeyboardEvent.code → legacy keyCode, matching AMI MegaRAC HID maps
 * (see KvmViewer HID_KEY).
 */
const EVENT_CODE_TO_KEYCODE = {
  Space: 32,
  Enter: 13,
  Tab: 9,
  Backspace: 8,
  Escape: 27,
  Backquote: 192,
  Digit0: 48,
  Digit1: 49,
  Digit2: 50,
  Digit3: 51,
  Digit4: 52,
  Digit5: 53,
  Digit6: 54,
  Digit7: 55,
  Digit8: 56,
  Digit9: 57,
  Minus: 189,
  Equal: 187,
  BracketLeft: 219,
  BracketRight: 221,
  Backslash: 220,
  Semicolon: 186,
  Quote: 222,
  Comma: 188,
  Period: 190,
  Slash: 191,
  ShiftLeft: 16,
};

export function eventCodeToKeyCode(code) {
  if (!code) return null;
  if (EVENT_CODE_TO_KEYCODE[code] != null) return EVENT_CODE_TO_KEYCODE[code];
  if (/^Key[A-Z]$/.test(code)) return code.codePointAt(3);
  return null;
}

/** Map a character to AMI HID ``sendKey(keyCode, location, down)`` args. */
export function charToHidStroke(ch) {
  const stroke = charToKeyStroke(ch);
  if (!stroke?.code) return null;
  const keyCode = eventCodeToKeyCode(stroke.code);
  if (keyCode == null) return null;
  return { keyCode, shift: !!stroke.shift };
}

async function typeNormalized(text, sendStroke, { delayMs = 20, submit = false, stillConnected = () => true, submitStroke } = {}) {
  if (!text || !stillConnected()) return;
  const normalized = normalizeVncText(text);
  for (const ch of normalized) {
    if (!stillConnected()) return;
    try {
      if (sendStroke(ch) === false) return;
    } catch (_) {
      // BMC/guest rejected a key; abort the rest of the paste.
      return;
    }
    if (delayMs > 0) await sleep(delayMs);
  }
  if (submit && stillConnected() && submitStroke) {
    await sleep(Math.max(delayMs, 30));
    try {
      submitStroke();
    } catch (_) {
      // Enter after paste is best-effort; the text already went out.
    }
  }
}

function sendHidChar(sendKey, ch) {
  const stroke = charToHidStroke(ch);
  if (!stroke) return;
  if (stroke.shift) sendKey(16, 1, true);
  sendKey(stroke.keyCode, 0, true);
  sendKey(stroke.keyCode, 0, false);
  if (stroke.shift) sendKey(16, 1, false);
}

/**
 * Type ``text`` as USB HID reports through an AMI MegaRAC ``sendKey``
 * callback ``(keyCode, location, down)``.
 */
export async function typeIntoHid(sendKey, text, opts = {}) {
  if (!sendKey) return;
  await typeNormalized(text, (ch) => sendHidChar(sendKey, ch), {
    ...opts,
    submitStroke: () => {
      sendKey(13, 0, true);
      sendKey(13, 0, false);
    },
  });
}

function sendAtenKey(rfb, keysym, down) {
  if (!rfb || typeof rfb.sendKey !== 'function') return false;
  try {
    // SuperMicro ATEN is old noVNC: sendKey(keysym, down). Passing a
    // boolean as the second arg is the down flag, not a scancode.
    rfb.sendKey(keysym, down);
    return true;
  } catch {
    // ATEN sendKey throws if the socket is already gone.
    return false;
  }
}

function sendAtenChar(rfb, ch) {
  const stroke = charToKeyStroke(ch);
  if (!stroke) return true;
  if (stroke.shift && !sendAtenKey(rfb, XK_Shift_L, true)) return false;
  if (!sendAtenKey(rfb, stroke.keysym, true)) return false;
  if (!sendAtenKey(rfb, stroke.keysym, false)) return false;
  if (stroke.shift && !sendAtenKey(rfb, XK_Shift_L, false)) return false;
  return true;
}

/**
 * Type ``text`` into SuperMicro ATEN InsydeVNC (legacy noVNC sendKey).
 */
export async function typeIntoAtenRfb(rfb, text, opts = {}) {
  if (!rfb) return;
  await typeNormalized(text, (ch) => sendAtenChar(rfb, ch), {
    ...opts,
    submitStroke: () => {
      sendAtenKey(rfb, XK_Return, true);
      sendAtenKey(rfb, XK_Return, false);
    },
  });
}
