<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import { getClientProxyCredentials, rotateClientProxyCredentials } from '../../lib/api.js';
  import { copyText } from '../../lib/copyText.js';

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
  let copyFastMsg = '';

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

  // Fast path: the same login as the public path, with the private address of the IP's subnet as host.
  $: fastAssignments = assignments.filter((a) => a.fast_path);
  $: fastLines = fastAssignments
    .filter((a) => a.fast_path.active && a.fast_path.endpoint)
    .map((a) => a.fast_path.endpoint)
    .join('\n');
  $: fastOffLocations = [
    ...new Set(fastAssignments.filter((a) => !a.fast_path.active).map((a) => a.fast_path.location_name)),
  ];

  async function copyLines(text, setMsg) {
    if (!text) return;
    setMsg('');
    try {
      await copyText(text);
      setMsg('Copied');
      setTimeout(() => setMsg(''), 2000);
    } catch (e) {
      error = e.message || 'Failed to copy';
    }
  }

  const copyAllEndpoints = () => copyLines(allEndpointLines(), (m) => { copyAllMsg = m; });
  const copyAllFastPath = () => copyLines(fastLines, (m) => { copyFastMsg = m; });

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
      <div class="head-actions">
        <Button variant="secondary" disabled={busy} on:click={copyAllEndpoints}>
          {copyAllMsg || 'Copy all (ip:port:user:pass)'}
        </Button>
        {#if fastLines}
          <Button variant="secondary" disabled={busy} on:click={copyAllFastPath}>
            {copyFastMsg || 'Copy all fast path (ip:port:user:pass)'}
          </Button>
        {/if}
      </div>
    {/if}
  </header>

  {#if loading}
    <div class="loading"><Spinner size="small" /> Loading credentials…</div>
  {:else if assignments.length === 0}
    <p class="muted">{unavailable ? 'Credentials unavailable.' : 'No IP assigned yet — contact support.'}</p>
  {:else}
    {#if error}<Alert type="error">{error}</Alert>{/if}
    {#if fastOffLocations.length > 0}
      <p class="fast-note">
        Private fast path addresses are available in {fastOffLocations.join(', ')}.
        <a href="/client/networks">Turn the fast path on</a> on the Networks page to use them.
      </p>
    {/if}
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
          {#if a.fast_path?.active}
            <div class="row">
              <span class="label">Fast path</span>
              <code>{a.fast_path.ip}:{a.fast_path.port}</code>
            </div>
            {#if a.fast_path.http_url}<div class="row"><span class="label">Fast HTTP</span><code class="small">{a.fast_path.http_url}</code></div>{/if}
            {#if a.fast_path.socks5_url}<div class="row"><span class="label">Fast SOCKS5</span><code class="small">{a.fast_path.socks5_url}</code></div>{/if}
          {/if}
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
  .head-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .fast-note {
    margin: 0 0 12px;
    font-size: 13px;
    color: var(--text-secondary);
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
    width: 96px;
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
