/**
 * Same-origin wrapper around AMI MegaRAC decode_worker.js.
 *
 * The BMC decoder writes RGB into imageBuffer.data. Structured clones of
 * ImageData can arrive in the worker with a null .data (seen in Thorium),
 * which surfaces as "Cannot set properties of null (setting 'N')" in
 * convertYuvToRgb. Allocate the ImageData inside this worker instead.
 *
 * CSP: script-src 'self' — this file is served from /kvm-decode-bridge.js;
 * importScripts of /api/kvm/assets/... is also same-origin.
 */
(function () {
  var params = new URL(self.location.href).searchParams;
  var token = params.get('token') || '';
  var src = params.get('src') || '/api/kvm/assets/libs/kvm/ast/decode_worker.js';
  var assetUrl = new URL(src, self.location.origin);
  assetUrl.searchParams.set('token', token);

  var origAdd = self.addEventListener.bind(self);
  self.addEventListener = function (type, listener, options) {
    if (type !== 'message' || typeof listener !== 'function') {
      return origAdd(type, listener, options);
    }
    return origAdd(
      type,
      function (e) {
        var d = e.data;
        if (d && (d.cmd === 'imageBuffer' || d.cmd === 'resolution_changed')) {
          var w = Number(d.w) || (d.imageBuffer && d.imageBuffer.width) || 0;
          var h = Number(d.h) || (d.imageBuffer && d.imageBuffer.height) || 0;
          var buf = d.imageBuffer;
          var hasPixels = buf && buf.data && buf.data.length;
          if (w && h && !hasPixels) {
            d = {
              cmd: d.cmd,
              imageBuffer: new ImageData(w, h),
              w: w,
              h: h,
            };
            e = { data: d };
          }
        }
        return listener.call(this, e);
      },
      options
    );
  };

  importScripts(assetUrl.href);
})();
