<script>
  import { onMount } from 'svelte';
  import AtenKvmViewer from '../components/AtenKvmViewer.svelte';
  import KvmViewer from '../components/KvmViewer.svelte';
  import { redeemIpmiKvmLaunchTicket } from '../lib/api.js';

  let session = null;
  let error = null;
  let loading = true;

  onMount(async () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('t');
    const inlineError = params.get('e');
    window.history.replaceState({}, '', '/kvm');

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
      session = await redeemIpmiKvmLaunchTicket(token);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div class="kvm-launch-page">
  {#if loading}
    <p class="status-msg">Opening KVM…</p>
  {:else if error}
    <div class="status-msg error">
      <p>{error}</p>
      <p class="hint">This link may have expired or already been used. Close this window and reopen the console.</p>
    </div>
  {:else if session}
    {#if session.profile === 'supermicro'}
      <AtenKvmViewer {session} />
    {:else}
      <KvmViewer {session} />
    {/if}
  {/if}
</div>

<style>
  .kvm-launch-page {
    position: fixed;
    inset: 0;
    background: #101114;
    display: flex;
    flex-direction: column;
  }

  .kvm-launch-page :global(.vnc-viewer) {
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
