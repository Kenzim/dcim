<script>
  import { onMount } from 'svelte';
  import SerialConsole from '../components/SerialConsole.svelte';
  import { redeemSolLaunchTicket } from '../lib/api.js';

  let session = null;
  let error = null;
  let loading = true;

  onMount(async () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('t');
    const inlineError = params.get('e');
    window.history.replaceState({}, '', '/sol');

    if (inlineError) {
      error = inlineError;
      loading = false;
      return;
    }

    if (!token) {
      error = 'Missing console link token.';
      loading = false;
      return;
    }

    try {
      session = await redeemSolLaunchTicket(token);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div class="sol-launch-page">
  {#if loading}
    <p class="status-msg">Opening serial console…</p>
  {:else if error}
    <div class="status-msg error">
      <p>{error}</p>
      <p class="hint">This link may have expired or already been used. Close this window and reopen the console.</p>
    </div>
  {:else if session}
    <SerialConsole
      wsToken={session.ws_token}
      wsPath={session.ws_path || '/api/sol/ws'}
      localEcho={true}
      ipmiSolEscape={true}
      statusHint="Keep Local echo on (host does not echo keys). Enter, then Ctrl-] then ? for ipmitool help."
    />
  {/if}
</div>

<style>
  .sol-launch-page {
    position: fixed;
    inset: 0;
    background: #101114;
    display: flex;
    flex-direction: column;
  }

  .sol-launch-page :global(.serial-viewer) {
    flex: 1;
    min-height: 0;
    border-radius: 0;
  }

  .status-msg {
    margin: auto;
    color: #e6e6e6;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    text-align: center;
    padding: 0 24px;
  }

  .status-msg.error {
    color: #ff8f87;
  }

  .status-msg .hint {
    color: #9a9a9a;
    font-size: 13px;
  }
</style>
