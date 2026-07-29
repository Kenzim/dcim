<script>
  import { onMount } from 'svelte';
  import ConsoleViewer from '../components/ConsoleViewer.svelte';
  import { redeemVmVncLaunchTicket } from '../lib/api.js';

  let session = null;
  let error = null;
  let loading = true;

  onMount(async () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('t');
    // A launcher (admin/client popup, WHMCS) that hit a permission/
    // validation failure before it could mint a ticket redirects here with
    // `e=<message>` instead, so the popup still shows a friendly error.
    const inlineError = params.get('e');
    // Strip the ticket/error from the URL immediately so it can't be
    // re-used or bookmarked (the ticket is single-use server-side anyway,
    // but this avoids a confusing "invalid link" on refresh).
    window.history.replaceState({}, '', '/vnc');

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
      session = await redeemVmVncLaunchTicket(token);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div class="vnc-launch-page">
  {#if loading}
    <p class="status-msg">Opening console…</p>
  {:else if error}
    <div class="status-msg error">
      <p>{error}</p>
      <p class="hint">This link may have expired or already been used. Close this window and reopen the console.</p>
    </div>
  {:else if session}
    <ConsoleViewer {session} />
  {/if}
</div>

<style>
  .vnc-launch-page {
    position: fixed;
    inset: 0;
    background: #101114;
    display: flex;
    flex-direction: column;
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
