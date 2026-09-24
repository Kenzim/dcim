/**
 * ATEN HERMON (encoding 0x59) decoder for SuperMicro X9 / WPCM450 iKVM.
 *
 * RGB555 16×16 subrects or full-frame raw lines. Adapted from the MPL-2.0
 * decoder in kelleyk/noVNC (bmc-support) and mkrasselt1/supermicro-kvm-html5.
 */

export class ByteQueue {
  constructor() {
    this._buf = new Uint8Array(0);
  }

  feed(chunk) {
    if (!chunk || !chunk.byteLength) return;
    const src = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    const next = new Uint8Array(this._buf.length + src.length);
    next.set(this._buf, 0);
    next.set(src, this._buf.length);
    this._buf = next;
  }

  get length() {
    return this._buf.length;
  }

  wait(n) {
    return this._buf.length < n;
  }

  peek8() {
    return this._buf[0];
  }

  shift(n) {
    const out = this._buf.subarray(0, n);
    this._buf = this._buf.subarray(n);
    return out;
  }

  shift8() {
    const value = this._buf[0];
    this._buf = this._buf.subarray(1);
    return value;
  }

  shift16() {
    const value = (this._buf[0] << 8) | this._buf[1];
    this._buf = this._buf.subarray(2);
    return value;
  }

  shift32() {
    const value =
      ((this._buf[0] << 24) | (this._buf[1] << 16) | (this._buf[2] << 8) | this._buf[3]) >>> 0;
    this._buf = this._buf.subarray(4);
    return value;
  }

  skip(n) {
    this._buf = this._buf.subarray(n);
  }
}

function rgb555ToRgba(pixelData, pixelCount) {
  const rgba = new Uint8Array(pixelCount * 4);
  for (let i = 0; i < pixelCount; i += 1) {
    const pixel = pixelData[i * 2] | (pixelData[i * 2 + 1] << 8);
    rgba[i * 4] = ((pixel >> 10) & 0x1f) * 255 / 31;
    rgba[i * 4 + 1] = ((pixel >> 5) & 0x1f) * 255 / 31;
    rgba[i * 4 + 2] = (pixel & 0x1f) * 255 / 31;
    rgba[i * 4 + 3] = 255;
  }
  return rgba;
}

export class HermonDecoder {
  constructor() {
    this._len = -1;
    this._type = -1;
  }

  reset() {
    this._len = -1;
    this._type = -1;
  }

  /**
   * Decode one HERMON rect. Returns false when more socket bytes are needed.
   * `onBlit(x, y, w, h, rgba)` receives RGBA tiles. `onResize(w, h)` on desktop change.
   */
  decode(sock, x, y, width, height, { onBlit, onResize }) {
    if (this._len === -1) {
      if (sock.wait(8)) return false;
      sock.shift32();
      this._len = sock.shift32();
      if (width === 64896 && height === 65056) {
        this._len = 0;
        this._type = -1;
        return true;
      }
      if (onResize) onResize(width, height);
    }

    if (this._len === 0) {
      this.reset();
      return true;
    }

    if (this._type === -1) {
      if (sock.wait(10)) return false;
      this._type = sock.shift8();
      sock.shift8();
      sock.shift32();
      sock.shift32();
      this._len -= 10;
    }

    while (this._len > 0) {
      if (this._type === 0) {
        const blockBytes = 6 + 16 * 16 * 2;
        if (sock.wait(blockBytes)) return false;
        sock.shift16();
        sock.shift16();
        const sy = sock.shift8();
        const sx = sock.shift8();
        const pixels = sock.shift(16 * 16 * 2);
        onBlit(sx * 16, sy * 16, 16, 16, rgb555ToRgba(pixels, 16 * 16));
        this._len -= blockBytes;
      } else if (this._type === 1) {
        const bytesPerLine = width * 2;
        if (sock.wait(bytesPerLine)) return false;
        const pixels = sock.shift(bytesPerLine);
        const curY = y + (height - Math.ceil(this._len / bytesPerLine));
        onBlit(x, curY, width, 1, rgb555ToRgba(pixels, width));
        this._len -= bytesPerLine;
      } else {
        if (sock.wait(this._len)) return false;
        sock.skip(this._len);
        this._len = 0;
      }
    }

    this.reset();
    return true;
  }
}
