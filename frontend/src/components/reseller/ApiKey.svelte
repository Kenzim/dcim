<script>
  import { onMount } from 'svelte';
  import {
    getResellerApiKey,
    rotateOwnResellerApiKey,
  } from '../../lib/api.js';
  import { Alert, Spinner } from '../ui/index.js';

  let keyInfo;
  let currentPassword = '';
  let revealedKey = '';
  let loading = true;
  let rotating = false;
  let error = '';
  let copied = false;

  onMount(async () => {
    try {
      keyInfo = await getResellerApiKey();
    } catch (err) {
      error = err.message || 'API key details could not be loaded.';
    } finally {
      loading = false;
    }
  });

  async function rotate() {
    if (!window.confirm('Rotate the reseller API key? Existing integrations will stop working immediately.')) return;
    rotating = true;
    error = '';
    revealedKey = '';
    try {
      const result = await rotateOwnResellerApiKey(currentPassword);
      revealedKey = result.api_key;
      keyInfo = { configured: true, prefix: result.prefix, last_used_at: null };
      currentPassword = '';
    } catch (err) {
      error = err.message || 'API key could not be rotated.';
    } finally {
      rotating = false;
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(revealedKey);
      copied = true;
    } catch (_) {
      error = 'Clipboard access was denied.';
    }
  }
</script>

<section aria-labelledby="api-key-title">
  <div class="heading"><h1 id="api-key-title">Reseller API</h1><p>Authenticate external ordering and lifecycle integrations.</p></div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading API key status…</span></div>
  {:else}
    <article class="panel">
      <h2>API key</h2>
      <dl>
        <div><dt>Status</dt><dd>{keyInfo.configured ? 'Configured' : 'Not configured'}</dd></div>
        <div><dt>Prefix</dt><dd><code>{keyInfo.prefix ? `${keyInfo.prefix}••••••••` : '—'}</code></dd></div>
        <div><dt>Last used</dt><dd>{keyInfo.last_used_at ? new Date(keyInfo.last_used_at).toLocaleString() : 'Never'}</dd></div>
      </dl>
      <Alert type="warning">Rotating invalidates the previous key immediately. Rackflow stores only a hash and cannot recover it.</Alert>
      <form on:submit|preventDefault={rotate}>
        <label for="api-password">Current account password</label>
        <input id="api-password" type="password" bind:value={currentPassword} autocomplete="current-password" required placeholder="Confirm your password" />
        <button class="danger" type="submit" disabled={rotating || !currentPassword}>{rotating ? 'Rotating…' : 'Rotate API key'}</button>
      </form>
    </article>
    {#if revealedKey}
      <article class="reveal" aria-live="assertive">
        <h2>Copy this key now</h2>
        <p>This is the only time the plaintext key will be shown. Store it in your secret manager before leaving this page.</p>
        <code>{revealedKey}</code>
        <button on:click={copyKey}>{copied ? 'Copied' : 'Copy key'}</button>
      </article>
    {/if}
  {/if}
</section>

<style>
  .heading { margin-bottom: 22px; }.heading h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }
  .panel, .reveal { padding: 22px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }.panel { max-width: 720px; }.panel h2, .reveal h2 { margin: 0 0 16px; }.panel dl { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }.panel dl div { padding: 12px; border-radius: 8px; background: var(--bg-secondary); }dt { color: var(--text-tertiary); font-size: 11px; }dd { margin: 4px 0 0; font-weight: 700; overflow-wrap: anywhere; }form { display: grid; gap: 9px; max-width: 420px; }label { font-weight: 700; font-size: 13px; }input { padding: 11px 12px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .danger { justify-self: start; padding: 10px 15px; border: 0; border-radius: 8px; background: var(--danger-color); color: white; font-weight: 750; cursor: pointer; }.danger:disabled { opacity: .55; }.reveal { max-width: 720px; margin-top: 18px; border-color: var(--warning-color); background: var(--warning-bg); color: var(--warning-text); }.reveal p { line-height: 1.5; }.reveal code { display: block; padding: 13px; overflow-wrap: anywhere; border-radius: 8px; background: var(--code-bg); color: var(--code-text); }.reveal button { margin-top: 12px; padding: 9px 13px; border: 1px solid currentColor; border-radius: 8px; background: transparent; color: inherit; font-weight: 750; cursor: pointer; }
  @media (max-width: 600px) { .panel dl { grid-template-columns: 1fr; } }
</style>
