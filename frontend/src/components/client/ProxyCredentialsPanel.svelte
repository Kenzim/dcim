<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import { getClientProxyCredentials, rotateClientProxyCredentials } from '../../lib/api.js';

  /** http_proxy service id. */
  export let serviceId;
  /** Whether the rotate action is granted (detail.proxy_rotate_available). */
  export let canRotate = false;

  const PROXY_PORT = 8080;

  let loading = true;
  let assignments = [];
  let unavailable = false;
  let busy = false;
  let message = '';
  let error = '';
  let copyAllMsg = '';

  onMount(async () => {
    try {
      const res = await getClientProxyCredentials(serviceId);
      assignments = res.assignments || [];
    } catch (_) {
      // 403 just means the client lacks proxy.view_credentials — treat as
      // "nothing to show" rather than an alarming error.
      assignments = [];
      unavailable = true;
    } finally {
      loading = false;
    }
  });

  function endpointLine(a) {
    if (a?.endpoint) return a.endpoint;
    if (!a?.ip_address || !a.username || !a.password) return '';
    const port = a.port || PROXY_PORT;
    return `${a.ip_address}:${port}:${a.username}:${a.password}`;
  }

  function allEndpointLines() {
    return assignments.map(endpointLine).filter(Boolean).join('\n');
  }

  async function copyAllEndpoints() {
    const text = allEndpointLines();
    if (!text) return;
    copyAllMsg = '';
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      copyAllMsg = 'Copied';
      setTimeout(() => { copyAllMsg = ''; }, 2000);
    } catch (e) {
      error = e.message || 'Failed to copy';
    }
  }

  async function rotate() {
    if (busy) return;
    if (!confirm('Rotate credentials for this proxy? The old username/password stop working immediately.')) return;
    busy = true;
    message = '';
    error = '';
    try {
      const res = await rotateClientProxyCredentials(serviceId);
      assignments = res.assignments || [];
      message = 'Credentials rotated';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

<section class="panel">
  <header class="panel-head">
    <div>
      <h3>Proxy access</h3>
      <p class="muted">HTTP and SOCKS5 use the same IP/port with these credentials.</p>
    </div>
    {#if !loading && assignments.length > 0}
      <Button variant="secondary" disabled={busy} on:click={copyAllEndpoints}>
        {copyAllMsg || 'Copy all (ip:port:user:pass)'}
      </Button>
    {/if}
  </header>

  {#if loading}
    <div class="loading"><Spinner size="small" /> Loading credentials…</div>
  {:else if assignments.length === 0}
    <p class="muted">{unavailable ? 'Credentials unavailable.' : 'No IP assigned yet — contact support.'}</p>
  {:else}
    {#if error}<Alert type="error">{error}</Alert>{/if}
    <ul class="assignments">
      {#each assignments as a}
        <li class="assignment">
          <div class="row">
            <span class="label">IP</span>
            <code>{a.ip_address}</code>
          </div>
          <div class="row">
            <span class="label">Username</span>
            <code>{a.username || '—'}</code>
          </div>
          <div class="row">
            <span class="label">Password</span>
            <code>{a.password || '—'}</code>
          </div>
          {#if a.http_url}<div class="row"><span class="label">HTTP</span><code class="small">{a.http_url}</code></div>{/if}
          {#if a.socks5_url}<div class="row"><span class="label">SOCKS5</span><code class="small">{a.socks5_url}</code></div>{/if}
        </li>
      {/each}
    </ul>
    {#if canRotate}
      <div class="actions">
        <Button variant="secondary" disabled={busy} on:click={rotate}>Rotate credentials</Button>
        {#if message}<span class="ok">{message}</span>{/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  .panel-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .panel-head h3 {
    margin: 0 0 4px;
    font-size: 16px;
    font-weight: 700;
  }
  .panel-head p {
    margin: 0;
  }
  .muted {
    color: var(--text-tertiary);
    font-size: 13px;
  }
  .loading {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-secondary);
    font-size: 14px;
  }
  .assignments {
    list-style: none;
    margin: 0 0 14px;
    padding: 0;
    display: grid;
    gap: 10px;
  }
  .assignment {
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    display: grid;
    gap: 6px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
  }
  .label {
    width: 76px;
    flex-shrink: 0;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }
  code {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-primary);
    word-break: break-all;
  }
  code.small {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .ok {
    font-size: 13px;
    color: var(--success-color);
  }
</style>
