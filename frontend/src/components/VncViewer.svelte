<script>
  import { onMount, onDestroy } from 'svelte';
  import RFB from '@novnc/novnc';
  import { readClipboardOrPrompt, typeIntoRfb } from '../lib/clipboardType.js';

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
  let rfb = null;
  let status = 'connecting'; // connecting | connected | disconnected | error
  let errorMessage = '';
  let reconnecting = false;
  let destroyed = false;
  let autoRetries = 0;
  let autoReconnectTimer = null;
  const MAX_AUTO_RETRIES = 5;

  function buildWsUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = new URL(`${scheme}://${window.location.host}${wsPath}`);
    url.searchParams.set('token', wsToken);
    return url.toString();
  }

  function isRfbConnected() {
    return rfb && rfb._rfbConnectionState === 'connected';
  }

  function safeDisconnect() {
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
      });
      instance.addEventListener('disconnect', (evt) => {
        if (rfb !== instance && rfb !== null) return;
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
    safeDisconnect();
  });
</script>

<div class="vnc-viewer">
  <div class="vnc-toolbar">
    <span class="vnc-status" class:ok={status === 'connected'} class:bad={status === 'error' || status === 'disconnected'}>
      {#if status === 'connecting'}
        Connecting…
      {:else if status === 'connected'}
        Connected
      {:else if status === 'disconnected'}
        Disconnected
      {:else}
        Error
      {/if}
    </span>
    <div class="vnc-toolbar-actions">
      <button type="button" class="vnc-btn" disabled={status !== 'connected'} on:click={sendCtrlAltDel}>
        Ctrl+Alt+Del
      </button>
      <button type="button" class="vnc-btn" disabled={status !== 'connected' || pasting} on:click={pasteClipboard}>
        {pasting ? 'Typing…' : 'Paste Clipboard'}
      </button>
      <button type="button" class="vnc-btn" on:click={() => reconnect()} disabled={reconnecting}>
        {reconnecting ? 'Reconnecting…' : 'Reconnect'}
      </button>
    </div>
  </div>
  {#if errorMessage}
    <p class="vnc-error">{errorMessage}</p>
  {/if}
  <div class="vnc-screen" bind:this={containerEl}></div>
</div>

<style>
  .vnc-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 480px;
    background: #101114;
    border-radius: 8px;
    overflow: hidden;
  }

  .vnc-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px;
    background: #1c1d22;
    color: #e6e6e6;
    font-size: 13px;
  }

  .vnc-status {
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-status.ok {
    color: #3ecf6a;
  }

  .vnc-status.bad {
    color: #ff6b61;
  }

  .vnc-toolbar-actions {
    display: flex;
    gap: 8px;
  }

  .vnc-btn {
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font-size: 12px;
  }

  .vnc-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .vnc-btn:not(:disabled):hover {
    background: rgba(255, 255, 255, 0.08);
  }

  .vnc-error {
    margin: 0;
    padding: 8px 12px;
    background: #3a1414;
    color: #ff8f87;
    font-size: 13px;
  }

  .vnc-screen {
    flex: 1;
    position: relative;
    overflow: hidden;
  }

  .vnc-screen :global(canvas) {
    display: block;
    margin: 0 auto;
  }
</style>
