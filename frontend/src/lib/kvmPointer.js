/**
 * Pointer mapping for HTML5 KVM canvases that are CSS-scaled in the tab.
 *
 * The painted bitmap is letterboxed (uniform scale) inside the canvas box.
 * Mapping clientX/Y through the full element (or ATEN offsetX / CSS scale)
 * makes the OS cursor and the remote cursor drift as soon as the tab is
 * larger than the framebuffer.
 */

export function mapPointerToFramebuffer(clientX, clientY, rect, fw, fh) {
  const width = Number(fw) || 0;
  const height = Number(fh) || 0;
  if (!rect?.width || !rect?.height || !width || !height) return null;
  const scale = Math.min(rect.width / width, rect.height / height);
  if (!scale) return null;
  const padX = (rect.width - width * scale) / 2;
  const padY = (rect.height - height * scale) / 2;
  const x = (clientX - rect.left - padX) / scale;
  const y = (clientY - rect.top - padY) / scale;
  return {
    x: Math.min(Math.max(x, 0), width),
    y: Math.min(Math.max(y, 0), height),
  };
}

export function eventToFramebuffer(ev, canvas) {
  if (!ev || !canvas) return null;
  return mapPointerToFramebuffer(
    ev.clientX,
    ev.clientY,
    canvas.getBoundingClientRect(),
    canvas.width,
    canvas.height,
  );
}

/** Size the canvas CSS box to the bitmap aspect so the hit target is the image. */
export function letterboxCanvas(canvas, stage) {
  if (!canvas || !stage) return 0;
  const fw = canvas.width || 1;
  const fh = canvas.height || 1;
  const availW = stage.clientWidth;
  const availH = stage.clientHeight;
  if (!availW || !availH) return 0;
  const scale = Math.min(availW / fw, availH / fh);
  canvas.style.width = `${fw * scale}px`;
  canvas.style.height = `${fh * scale}px`;
  return scale;
}

export function watchCanvasFit(stage, fit) {
  const onFit = () => fit();
  const ro = stage ? new ResizeObserver(onFit) : null;
  if (ro && stage) ro.observe(stage);
  window.addEventListener('resize', onFit);
  const viewport = window.visualViewport;
  if (viewport) {
    viewport.addEventListener('resize', onFit);
    viewport.addEventListener('scroll', onFit);
  }
  onFit();
  return () => {
    ro?.disconnect();
    window.removeEventListener('resize', onFit);
    viewport?.removeEventListener('resize', onFit);
    viewport?.removeEventListener('scroll', onFit);
  };
}

/**
 * ATEN's mouse uses offsetX / set_scale, which diverges from the painted
 * box under CSS scale and browser zoom. Force framebuffer coords from the
 * viewport rect instead.
 */
export function patchAtenPointer(rfb, canvas) {
  if (!rfb || !canvas) return;
  const toFb = (e) => eventToFramebuffer(e, canvas) || { x: 0, y: 0 };
  const mouse = rfb._mouse;
  let patched = false;
  if (mouse) {
    ['getEventPosition', '_event_pos', '_pos', '_getMousePos'].forEach((name) => {
      if (typeof mouse[name] === 'function') {
        mouse[name] = toFb;
        patched = true;
      }
    });
    if (patched && typeof mouse.set_scale === 'function') {
      mouse.set_scale(1);
      mouse._rackflowFbCoords = true;
    }
  }
  const util = window.Util;
  if (util && typeof util.getEventPosition === 'function' && !util._rackflowKvmPointer) {
    const orig = util.getEventPosition.bind(util);
    util.getEventPosition = function getEventPosition(e, obj, scale) {
      const el = obj?.tagName ? obj : e?.target;
      if (el?.tagName === 'CANVAS') {
        return eventToFramebuffer(e, el) || { x: 0, y: 0 };
      }
      return orig(e, obj, scale);
    };
    util._rackflowKvmPointer = true;
  }
}
