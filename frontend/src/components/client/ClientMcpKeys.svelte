<script>
  import { onMount } from 'svelte';
  import { Alert, Badge, Button, Spinner } from '../ui/index.js';
  import {
    clientMcpCreateKey,
    clientMcpDeleteKey,
    clientMcpListKeys,
    clientMcpUpdateKey,
  } from '../../lib/api.js';

  export let impersonating = false;

  const SCOPE_HELP = {
    read: 'View services, invoices and tickets',
    write: 'Power control, console links, backups, ticket replies',
    destructive: 'Reinstall, restore or delete backups, run service actions',
  };

  let info = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let name = '';
  let scopes = { read: true, write: false, destructive: false };
  let expiresInDays = '';
  let newKey = null;
  let copied = false;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      info = await clientMcpListKeys();
    } catch (err) {
      error = err.message || 'Failed to load MCP keys';
    } finally {
      loading = false;
    }
  }

  function selectedScopes() {
    // Higher scopes include the lower ones on the server, but send them explicitly.
    const out = ['read'];
    if (scopes.write || scopes.destructive) out.push('write');
    if (scopes.destructive) out.push('destructive');
    return out;
  }

  async function createKey() {
    if (!name.trim()) {
      error = 'Give the key a name.';
      return;
    }
    working = true;
    error = '';
    success = '';
    newKey = null;
    try {
      const payload = { name: name.trim(), scopes: selectedScopes() };
      if (expiresInDays) payload.expires_in_days = Number(expiresInDays);
      newKey = await clientMcpCreateKey(payload);
      name = '';
      expiresInDays = '';
      copied = false;
      await load();
    } catch (err) {
      error = err.message || 'Failed to create key';
    } finally {
      working = false;
    }
  }

  async function toggleKey(key) {
    working = true;
    error = '';
    success = '';
    try {
      await clientMcpUpdateKey(key.id, { enabled: !key.enabled });
      success = key.enabled ? 'Key disabled.' : 'Key enabled.';
      await load();
    } catch (err) {
      error = err.message || 'Failed to update key';
    } finally {
      working = false;
    }
  }

  async function removeKey(key) {
    if (!confirm(`Delete the key "${key.name}"? Assistants using it will stop working immediately.`)) return;
    working = true;
    error = '';
    success = '';
    try {
      await clientMcpDeleteKey(key.id);
      success = 'Key deleted.';
      await load();
    } catch (err) {
      error = err.message || 'Failed to delete key';
    } finally {
      working = false;
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(newKey.api_key);
      copied = true;
    } catch (_) {
      copied = false;
    }
  }

  function fmt(value) {
    return value ? new Date(value).toLocaleString() : 'Never';
  }

  $: canCreate = info && info.server_enabled && info.allowed && info.can_manage && !impersonating;
  $: endpoint = info?.endpoint || '/client-mcp';
  $: example = JSON.stringify(
    { mcpServers: { rackflow: { type: 'http', url: endpoint, headers: { Authorization: 'Bearer <your key>' } } } },
    null,
    2,
  );
</script>

<div class="panel">
  <h2>MCP access for AI assistants</h2>
  <p class="muted">
    Connect an AI assistant to your account over MCP. A key only sees and acts on your own services,
    with the same permissions you have in this portal. Keys are shown once, so store them somewhere safe.
  </p>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if info}
    {#if !info.server_enabled}
      <Alert type="warning">MCP access is not switched on for this platform.</Alert>
    {:else if !info.allowed}
      <Alert type="warning">MCP access is not enabled for your account. Contact support if you need it.</Alert>
    {/if}
    {#if impersonating || !info.can_manage}
      <p class="muted">Keys cannot be created or changed while signed in on the client's behalf.</p>
    {/if}

    {#if newKey}
      <div class="new-key">
        <strong>Your new key (shown once)</strong>
        <code class="secret">{newKey.api_key}</code>
        <div class="actions">
          <Button on:click={copyKey}>{copied ? 'Copied' : 'Copy key'}</Button>
          <Button variant="secondary" on:click={() => (newKey = null)}>I have saved it</Button>
        </div>
      </div>
    {/if}

    {#if canCreate}
      <h3>Create a key</h3>
      <div class="grid">
        <label>Name <input bind:value={name} maxlength="255" placeholder="e.g. Claude on my laptop" /></label>
        <label>
          Expires after (days, optional)
          <input type="number" min="1" max="3650" bind:value={expiresInDays} />
        </label>
      </div>
      <fieldset>
        <legend>Permissions</legend>
        <label class="check"><input type="checkbox" checked disabled /> <span><b>read</b>: {SCOPE_HELP.read}</span></label>
        <label class="check"><input type="checkbox" bind:checked={scopes.write} /> <span><b>write</b>: {SCOPE_HELP.write}</span></label>
        <label class="check"><input type="checkbox" bind:checked={scopes.destructive} /> <span><b>destructive</b>: {SCOPE_HELP.destructive}</span></label>
      </fieldset>
      <Button on:click={createKey} disabled={working}>Create key</Button>
    {/if}

    <h3>Your keys ({info.keys.length} of {info.max_keys})</h3>
    {#if info.keys.length === 0}
      <p class="muted">No keys yet.</p>
    {:else}
      <ul class="simple-list">
        {#each info.keys as key (key.id)}
          <li>
            <div class="key-main">
              <span>
                <b>{key.name}</b>
                <Badge variant={key.enabled ? 'success' : 'muted'}>{key.enabled ? 'Enabled' : 'Disabled'}</Badge>
              </span>
              <small>
                <code>{key.key_prefix}</code> · {key.scopes.join(', ')} · last used {fmt(key.last_used_at)}
                {#if key.last_used_ip}from {key.last_used_ip}{/if}
                · expires {key.expires_at ? fmt(key.expires_at) : 'never'}
              </small>
            </div>
            {#if info.can_manage && !impersonating}
              <div class="actions">
                <Button variant="secondary" on:click={() => toggleKey(key)} disabled={working}>
                  {key.enabled ? 'Disable' : 'Enable'}
                </Button>
                <Button variant="secondary" on:click={() => removeKey(key)} disabled={working}>Delete</Button>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    <h3>Connect an assistant</h3>
    <p class="muted">Endpoint: <code>{endpoint}</code> (Streamable HTTP, send the key as a Bearer token).</p>
    <pre class="example">{example}</pre>
    <p class="muted">
      Power actions, backup restores and reinstalls ask the assistant for explicit confirmation, and they are
      logged in your activity.
    </p>
  {/if}
</div>

<style>
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 20px; box-shadow: var(--shadow-sm); }
  h2 { margin: 0 0 12px; font-size: 17px; }
  h3 { margin: 24px 0 10px; font-size: 15px; }
  .state { min-height: 120px; display: grid; place-items: center; gap: 12px; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 16px; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input:not([type='checkbox']) { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  fieldset { border: 1px solid var(--border-color); border-radius: 8px; margin: 0 0 16px; padding: 10px 14px; display: grid; gap: 8px; }
  legend { font-size: 13px; font-weight: 600; padding: 0 6px; }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 400; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .simple-list { list-style: none; margin: 0; padding: 0; }
  .simple-list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; flex-wrap: wrap; }
  .key-main { display: grid; gap: 4px; }
  .key-main small { color: var(--text-tertiary); }
  .muted { color: var(--text-secondary); font-size: 14px; }
  .new-key { border: 1px solid var(--accent-color); border-radius: 8px; padding: 14px; margin: 12px 0; display: grid; gap: 10px; }
  .secret { word-break: break-all; font-size: 13px; user-select: all; }
  .example { background: var(--bg-tertiary); padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 12px; }
  code { font-size: 12px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
