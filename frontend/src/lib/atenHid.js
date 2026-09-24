/**
 * USB HID usage IDs for ATEN iKVM key events (18-byte type-4 frames).
 * Mapping follows kelleyk/noVNC bmc-support (MPL-2.0).
 */

const CODE_TO_HID = {
  Enter: 0x28,
  NumpadEnter: 0x58,
  Escape: 0x29,
  Backspace: 0x2a,
  Tab: 0x2b,
  Space: 0x2c,
  Minus: 0x2d,
  Equal: 0x2e,
  BracketLeft: 0x2f,
  BracketRight: 0x30,
  Backslash: 0x31,
  Semicolon: 0x33,
  Quote: 0x34,
  Backquote: 0x35,
  Comma: 0x36,
  Period: 0x37,
  Slash: 0x38,
  CapsLock: 0x39,
  PrintScreen: 0x46,
  ScrollLock: 0x47,
  Pause: 0x48,
  Insert: 0x49,
  Home: 0x4a,
  PageUp: 0x4b,
  Delete: 0x4c,
  End: 0x4d,
  PageDown: 0x4e,
  ArrowRight: 0x4f,
  ArrowLeft: 0x50,
  ArrowDown: 0x51,
  ArrowUp: 0x52,
  NumLock: 0x53,
  ControlLeft: 0xe0,
  ControlRight: 0xe4,
  ShiftLeft: 0xe1,
  ShiftRight: 0xe5,
  AltLeft: 0xe2,
  AltRight: 0xe6,
  MetaLeft: 0xe3,
  MetaRight: 0xe7,
};

for (let i = 0; i < 12; i += 1) {
  CODE_TO_HID[`F${i + 1}`] = 0x3a + i;
}
for (let i = 0; i < 26; i += 1) {
  CODE_TO_HID[`Key${String.fromCharCode(65 + i)}`] = 0x04 + i;
}
for (let i = 1; i <= 9; i += 1) {
  CODE_TO_HID[`Digit${i}`] = 0x1e + (i - 1);
}
CODE_TO_HID.Digit0 = 0x27;

const CHAR_TO_HID = {
  ' ': 0x2c,
  '\n': 0x28,
  '\r': 0x28,
  '\t': 0x2b,
};

export function hidFromCode(code) {
  return CODE_TO_HID[code] || 0;
}

export function hidFromChar(ch) {
  if (!ch) return 0;
  if (Object.prototype.hasOwnProperty.call(CHAR_TO_HID, ch)) return CHAR_TO_HID[ch];
  const lower = ch.toLowerCase();
  if (lower >= 'a' && lower <= 'z') return 0x04 + (lower.charCodeAt(0) - 97);
  if (ch >= '1' && ch <= '9') return 0x1e + (ch.charCodeAt(0) - 49);
  if (ch === '0') return 0x27;
  return 0;
}

export function atenKeyPacket(hidCode, down) {
  const pkt = new Uint8Array(18);
  pkt[0] = 4;
  pkt[2] = down ? 1 : 0;
  pkt[5] = (hidCode >>> 24) & 0xff;
  pkt[6] = (hidCode >>> 16) & 0xff;
  pkt[7] = (hidCode >>> 8) & 0xff;
  pkt[8] = hidCode & 0xff;
  return pkt;
}

export function atenPointerPacket(x, y, mask) {
  const pkt = new Uint8Array(18);
  pkt[0] = 5;
  pkt[2] = mask & 0xff;
  pkt[3] = (x >> 8) & 0xff;
  pkt[4] = x & 0xff;
  pkt[5] = (y >> 8) & 0xff;
  pkt[6] = y & 0xff;
  return pkt;
}

export function atenSetEncodingsPacket() {
  const encs = [0x59, 0x57, 0x58, 0, 1, -223];
  const pkt = new Uint8Array(4 + encs.length * 4);
  pkt[0] = 2;
  pkt[2] = (encs.length >> 8) & 0xff;
  pkt[3] = encs.length & 0xff;
  encs.forEach((enc, i) => {
    const v = enc >>> 0;
    const o = 4 + i * 4;
    pkt[o] = (v >>> 24) & 0xff;
    pkt[o + 1] = (v >>> 16) & 0xff;
    pkt[o + 2] = (v >>> 8) & 0xff;
    pkt[o + 3] = v & 0xff;
  });
  return pkt;
}

export function atenFbUpdateRequest(width, height, incremental = 0) {
  const pkt = new Uint8Array(10);
  pkt[0] = 3;
  pkt[1] = incremental & 0xff;
  pkt[6] = (width >> 8) & 0xff;
  pkt[7] = width & 0xff;
  pkt[8] = (height >> 8) & 0xff;
  pkt[9] = height & 0xff;
  return pkt;
}
