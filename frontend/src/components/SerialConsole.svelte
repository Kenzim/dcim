<script>
  import { onMount, onDestroy } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import { FitAddon } from '@xterm/addon-fit';
  import '@xterm/xterm/css/xterm.css';
  import { readClipboardOrPrompt, typeIntoSerialWs } from '../lib/clipboardType.js';

  // Session credentials minted by the admin/client "vnc-session" endpoints
  // or by POST /api/vnc/redeem, for a VM whose console_type is "serial"
  // (Proxmox `vga: serialN`, e.g. cloud-init images with no virtual GPU).
  // Unlike the noVNC viewer, no password/ticket is sent from here -- the
  // backend WS bridge authenticates the Proxmox term stream itself.
  export let wsToken;
  export let wsPath = '/api/vnc/ws';
  // Optional: async () => session. Called before reconnect so we can mint a
  // fresh Proxmox proxy (old tickets die when the upstream WS closes).
  export let refreshSession = null;

  let containerEl;
  let term = null;
  let fitAddon = null;
  let ws = null;
  let resizeObserver = null;
  let status = 'connecting'; // connecting | connected | disconnected | error
  let errorMessage = '';
  let reconnecting = false;

  function buildWsUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = new URL(`${scheme}://${window.location.host}${wsPath}`);
    url.searchParams.set('token', wsToken);
    return url.toString();
  }

  function sendResize() {
    if (!ws || ws.readyState !== WebSocket.OPEN || !term) return;
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }

  function connect() {
    status = 'connecting';
    errorMessage = '';

    if (!term) {
      term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        theme: { background: '#101114' },
      });
      fitAddon = new FitAddon();
      term.loadAddon(fitAddon);
      term.open(containerEl);
      fitAddon.fit();
      term.onData((data) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(data));
        }
      });
      term.onResize(sendResize);
    }

    try {
      ws = new WebSocket(buildWsUrl(), 'binary');
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => {
        status = 'connected';
        fitAddon.fit();
        sendResize();
        term.focus();
      };
      ws.onmessage = (evt) => {
        if (typeof evt.data === 'string') {
          term.write(evt.data);
        } else {
          term.write(new Uint8Array(evt.data));
        }
      };
      ws.onclose = (evt) => {
        status = 'disconnected';
        if (!evt.wasClean) {
          errorMessage = 'Console disconnected unexpectedly.';
        }
      };
      ws.onerror = () => {
        status = 'error';
        errorMessage = 'Console connection failed.';
      };
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
    }
  }

  async function reconnect() {
    if (reconnecting) return;
    reconnecting = true;
    errorMessage = '';
    try {
      if (refreshSession) {
        const fresh = await refreshSession();
        if (fresh?.ws_token) wsToken = fresh.ws_token;
        if (fresh?.ws_path) wsPath = fresh.ws_path;
      }
      try {
        ws?.close();
      } catch (_) {
        /* ignore */
      }
      connect();
    } catch (e) {
      status = 'error';
      errorMessage = e.message || String(e);
    } finally {
      reconnecting = false;
    }
  }

  // Raw serial streams have no clipboard concept of their own -- "pasting"
  // means sending the text as UTF-8 bytes over the same WebSocket path as
  // keystrokes. We do NOT use `term.paste()`: that wraps text in bracketed
  // paste sequences many serial gettys mishandle, and it also fails when
  // the Clipboard API is blocked (common on HTTP / without permission).
  let pasting = false;
  async function pasteClipboard() {
    if (pasting || status !== 'connected') return;
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

  /** Type arbitrary text into the serial console (used by Paste Password). */
  export async function typeText(text) {
    if (status !== 'connected' || !text) return;
    await typeIntoSerialWs(ws, text);
    term?.focus();
  }

  onMount(() => {
    connect();
    resizeObserver = new ResizeObserver(() => fitAddon?.fit());
    resizeObserver.observe(containerEl);
  });

  onDestroy(() => {
    resizeObserver?.disconnect();
    try {
      ws?.close();
    } catch (_) {
      /* ignore */
    }
    ws = null;
    term?.dispose();
    term = null;
  });
</script>

<div class="serial-viewer">
  <div class="serial-toolbar">
    <span class="serial-status" class:ok={status === 'connected'} class:bad={status === 'error' || status === 'disconnected'}>
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
    <div class="serial-toolbar-actions">
      <button type="button" class="serial-btn" on:click={pasteClipboard} disabled={status !== 'connected' || pasting}>
        {pasting ? 'Typing…' : 'Paste Clipboard'}
      </button>
      <button type="button" class="serial-btn" on:click={reconnect} disabled={reconnecting}>
        {reconnecting ? 'Reconnecting…' : 'Reconnect'}
      </button>
    </div>
  </div>
  {#if errorMessage}
    <p class="serial-error">{errorMessage}</p>
  {/if}
  <div class="serial-screen" bind:this={containerEl}></div>
</div>

<style>
  .serial-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 480px;
    background: #101114;
    border-radius: 8px;
    overflow: hidden;
  }

  .serial-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px;
    background: #1c1d22;
    color: #e6e6e6;
    font-size: 13px;
  }

  .serial-status {
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.08);
  }

  .serial-status.ok {
    color: #3ecf6a;
  }

  .serial-status.bad {
    color: #ff6b61;
  }

  .serial-toolbar-actions {
    display: flex;
    gap: 8px;
  }

  .serial-btn {
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font-size: 12px;
  }

  .serial-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }

  .serial-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .serial-error {
    margin: 0;
    padding: 8px 12px;
    background: #3a1414;
    color: #ff8f87;
    font-size: 13px;
  }

  .serial-screen {
    flex: 1;
    position: relative;
    overflow: hidden;
    padding: 4px 8px;
  }

  .serial-screen :global(.xterm) {
    height: 100%;
  }
</style>
