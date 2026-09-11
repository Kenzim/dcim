<script>
  import { onMount, onDestroy } from 'svelte';
  import RFB from '@novnc/novnc';
  import { readClipboardOrPrompt, typeIntoRfb } from '../lib/clipboardType.js';
  import { createWsLatencyProbe } from '../lib/consoleLatency.js';
  import RemoteConsoleChrome from './RemoteConsoleChrome.svelte';

  // Session credentials minted by the admin/client "vnc-session" endpoints
  // or by POST /api/vnc/redeem. `vncPassword` is a Proxmox-issued,
  // single-session VNC/RFB ticket -- not a Proxmox account password.
  export let wsToken;
  export let wsPath = '/api/vnc/ws';
  export let vncPassword;
  // Optional: async () => session. Called before reconnect so we can mint a
  // fresh Proxmox proxy (old tickets die when the upstream WS closes).
  export let refreshSession = null;

  let containerEl;
  let chrome;
  let rfb = null;
  let status = 'connecting'; // connecting | connected | disconnected | error
  let errorMessage = '';
  let reconnecting = false;
  let destroyed = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  const MAX_AUTO_RETRIES = 5;

  let latencyMs = null;
  let latencySamples = [];
  const latencyProbe = createWsLatencyProbe({
    onSample({ ms, samples }) {
      latencyMs = ms;
      latencySamples = samples;
    },
  });

  function buildWsUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = new URL(`${scheme}://${window.location.host}${wsPath}`);
    url.searchParams.set('token', wsToken);
    return url.toString();
  }

  function isRfbConnected() {
    return rfb && rfb._rfbConnectionState === 'connected';
  }

  function stopLatencyProbe() {
    latencyProbe.detach();
    chrome?.closeLatencyGraph?.();
  }

  function safeDisconnect() {
    stopLatencyProbe();
    const old = rfb;
    rfb = null;
    if (!old) return;
    try {
      // Calling disconnect() after noVNC already closed throws
      // "Tried changing state of a disconnected RFB object".
      if (old._rfbConnectionState && old._rfbConnectionState !== 'disconnected') {
        old.disconnect();
      }
    } catch (_) {
      /* ignore */
    }
  }

  function clearAutoReconnect() {
    if (autoReconnectTimer != null) {
      clearTimeout(autoReconnectTimer);
      autoReconnectTimer = null;
    }
  }

  function scheduleAutoReconnect() {
    if (destroyed || reconnecting || !refreshSession) return;
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

  function connect() {
    if (destroyed) return;
    status = 'connecting';
    errorMessage = '';
    try {
      // Clear any leftover canvas from a previous RFB instance before
      // creating a new one (reconnect path).
      if (containerEl) containerEl.innerHTML = '';
      safeDisconnect();
      const instance = new RFB(containerEl, buildWsUrl(), {
        credentials: { password: vncPassword },
        wsProtocols: ['binary'],
      });
      rfb = instance;
      instance.scaleViewport = true;
      instance.clipViewport = false;
      instance.addEventListener('connect', () => {
        if (rfb !== instance || destroyed) return;
        status = 'connected';
        errorMessage = '';
        autoRetries = 0;
        latencyProbe.attach(instance._sock?._websocket);
      });
      instance.addEventListener('disconnect', (evt) => {
        if (rfb !== instance && rfb !== null) return;
        stopLatencyProbe();
        rfb = null;
        if (destroyed || reconnecting) return;
        status = 'disconnected';
        const unclean = evt?.detail && evt.detail.clean === false;
        if (unclean) {
          errorMessage = 'Console disconnected unexpectedly.';
          scheduleAutoReconnect();
        }
      });
      instance.addEventListener('securityfailure', (evt) => {
        if (rfb !== instance) return;
        status = 'error';
        errorMessage = evt?.detail?.reason || 'VNC authentication failed.';
      });
      instance.addEventListener('credentialsrequired', () => {
        // Should not happen since we supply credentials upfront, but surface
        // it clearly rather than hanging silently.
        if (rfb !== instance) return;
        status = 'error';
        errorMessage = 'The console requires credentials that were not provided.';
      });
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
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
      if (refreshSession) {
        const fresh = await refreshSession();
        if (fresh?.ws_token) wsToken = fresh.ws_token;
        if (fresh?.ws_path) wsPath = fresh.ws_path;
        if (fresh?.vnc_password) vncPassword = fresh.vnc_password;
      }
      safeDisconnect();
      connect();
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
      if (auto) scheduleAutoReconnect();
    } finally {
      reconnecting = false;
    }
  }

  function sendCtrlAltDel() {
    if (!isRfbConnected()) return;
    try {
      rfb.sendCtrlAltDel();
    } catch (_) {
      /* ignore */
    }
  }

  // Type clipboard text as keystrokes (down+up per character). The VNC
  // clipboard extension (`clipboardPasteFrom`) only updates the guest
  // clipboard and still needs Ctrl+V / guest agent support -- that's not
  // what "Paste Clipboard" means here, and it silently no-ops on most
  // Proxmox VMs. Fall back to a prompt when the Clipboard API is blocked.
  let pasting = false;
  async function pasteClipboard() {
    if (pasting || !isRfbConnected()) return;
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

  /** Type arbitrary text into the guest (used by Paste Password). */
  export async function typeText(text, opts = {}) {
    if (!isRfbConnected() || !text) return;
    await typeIntoRfb(rfb, text, opts);
  }

  onMount(connect);
  onDestroy(() => {
    destroyed = true;
    clearAutoReconnect();
    stopLatencyProbe();
    safeDisconnect();
  });
</script>

<RemoteConsoleChrome
  bind:this={chrome}
  {status}
  {errorMessage}
  {pasting}
  {reconnecting}
  {latencyMs}
  {latencySamples}
  onCad={sendCtrlAltDel}
  onPaste={pasteClipboard}
  onReconnect={() => reconnect()}
>
  <!-- RFB owns this node (clears/rewrites children); keep overlays outside. -->
  <div class="vnc-screen" bind:this={containerEl}></div>
</RemoteConsoleChrome>

<style>
  .vnc-screen {
    height: 100%;
    overflow: hidden;
  }

  .vnc-screen :global(canvas) {
    display: block;
    margin: 0 auto;
  }
</style>
