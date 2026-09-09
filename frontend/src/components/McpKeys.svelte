<script>
  import PageHeader from './PageHeader.svelte';
  import { Button, Modal, FormGroup, FormError } from './ui/index.js';
  import {
    getMcpKeys,
    getMcpKey,
    createMcpKey,
    updateMcpKey,
    deleteMcpKey,
    rotateMcpKey,
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  const SCOPE_ORDER = ['read', 'write', 'destructive'];
  const SCOPE_HELP = {
    read: 'Inventory, status, and search',
    write: 'Mutations such as provision and power on',
    destructive: 'Terminate, reinstall, delete, console',
  };

  let keys = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingKey = null;
  let formData = emptyForm();
  let formError = null;
  let cidrDraft = '';
  let rotatingKey = {};
  let togglingKey = {};
  let newKeyModal = emptyRevealModal();
  let snippetCopied = false;

  $: mcpOriginUrl = mcpEndpointUrl();
  $: exampleSnippet = mcpRemoteSnippet();

  onMount(async () => {
    await loadKeys();
  });

  function emptyForm() {
    return {
      name: '',
      description: '',
      enabled: true,
      scopes: ['read'],
      ip_allowlist: [],
      expires_at: '',
    };
  }

  function emptyRevealModal() {
    return { open: false, name: '', apiKey: '', copied: false, copiedSnippet: false };
  }

  function mcpEndpointUrl() {
    if (typeof window === 'undefined') return '/mcp';
    return `${window.location.origin}/mcp`;
  }

  function mcpRemoteSnippet(apiKey) {
    return JSON.stringify(
      {
        mcpServers: {
          rackflow: {
            url: mcpEndpointUrl(),
            headers: { Authorization: `Bearer ${apiKey || 'rfmcp_…'}` },
          },
        },
      },
      null,
      2,
    );
  }

  function normalizeAllowlist(value) {
    if (!value) return [];
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
    return String(value)
      .split(/[\n,]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function normalizeScopes(value) {
    const set = new Set(Array.isArray(value) ? value : []);
    return SCOPE_ORDER.filter((scope) => set.has(scope));
  }

  function toDatetimeLocal(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fromDatetimeLocal(value) {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return date.toISOString();
  }

  function formatWhen(iso) {
    if (!iso) return 'Never';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
  }

  function payloadFromForm() {
    return {
      name: formData.name.trim(),
      description: formData.description.trim() || null,
      enabled: !!formData.enabled,
      scopes: normalizeScopes(formData.scopes),
      ip_allowlist: formData.ip_allowlist.length ? formData.ip_allowlist : null,
      expires_at: fromDatetimeLocal(formData.expires_at),
    };
  }

  function plaintextFromResponse(row) {
    if (row?.plaintext_api_key) return row.plaintext_api_key;
    const key = row?.api_key || '';
    // Masked list/get values are prefix + ellipsis; create/rotate may put
    // the full secret in api_key (billing-integrations style).
    if (
      typeof key === 'string' &&
      key.startsWith('rfmcp_') &&
      key.length > 20 &&
      !key.includes('…') &&
      !key.includes('...')
    ) {
      return key;
    }
    return null;
  }

  async function loadKeys() {
    try {
      loading = true;
      error = null;
      keys = await getMcpKeys();
    } catch (err) {
      error = err.message;
      console.error('Failed to load MCP keys:', err);
    } finally {
      loading = false;
    }
  }

  function openAddModal() {
    editingKey = null;
    formData = emptyForm();
    cidrDraft = '';
    formError = null;
    showModal = true;
  }

  async function openEditModal(key) {
    let row = key;
    try {
      row = await getMcpKey(key.id);
    } catch (err) {
      console.error('Failed to refresh MCP key:', err);
    }
    editingKey = row;
    formData = {
      name: row.name,
      description: row.description || '',
      enabled: row.enabled,
      scopes: normalizeScopes(row.scopes),
      ip_allowlist: normalizeAllowlist(row.ip_allowlist),
      expires_at: toDatetimeLocal(row.expires_at),
    };
    cidrDraft = '';
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingKey = null;
    formData = emptyForm();
    cidrDraft = '';
    formError = null;
  }

  function setScope(scope, checked) {
    const idx = SCOPE_ORDER.indexOf(scope);
    if (idx < 0) return;
    const next = checked ? SCOPE_ORDER.slice(0, idx + 1) : SCOPE_ORDER.slice(0, idx);
    formData = { ...formData, scopes: next };
  }

  function addCidr() {
    const value = cidrDraft.trim();
    if (!value) return;
    if (!value.includes('/')) {
      formError = 'CIDR must include a prefix length, e.g. 192.0.2.0/24';
      return;
    }
    if (formData.ip_allowlist.includes(value)) {
      cidrDraft = '';
      return;
    }
    formError = null;
    formData = { ...formData, ip_allowlist: [...formData.ip_allowlist, value] };
    cidrDraft = '';
  }

  function removeCidr(cidr) {
    formData = { ...formData, ip_allowlist: formData.ip_allowlist.filter((item) => item !== cidr) };
  }

  function onCidrKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      addCidr();
    }
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.left = '-999999px';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      try {
        document.execCommand('copy');
        return true;
      } finally {
        document.body.removeChild(textarea);
      }
    } catch (err) {
      console.error('Copy failed', err);
      return false;
    }
  }

  function showNewKey(name, apiKey) {
    newKeyModal = { open: true, name, apiKey, copied: false, copiedSnippet: false };
  }

  function closeNewKeyModal() {
    newKeyModal = emptyRevealModal();
  }

  async function copyNewKey() {
    const ok = await copyText(newKeyModal.apiKey);
    if (ok) newKeyModal = { ...newKeyModal, copied: true };
  }

  async function copyNewKeySnippet() {
    const ok = await copyText(mcpRemoteSnippet(newKeyModal.apiKey));
    if (ok) newKeyModal = { ...newKeyModal, copiedSnippet: true };
  }

  async function copyExampleSnippet() {
    const ok = await copyText(exampleSnippet);
    snippetCopied = ok;
    if (ok) setTimeout(() => { if (snippetCopied) snippetCopied = false; }, 2000);
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }
    if (!formData.scopes.length) {
      formError = 'Select at least one scope';
      return;
    }

    try {
      formError = null;
      const payload = payloadFromForm();
      if (editingKey) {
        await updateMcpKey(editingKey.id, payload);
        closeModal();
        await loadKeys();
      } else {
        const created = await createMcpKey(payload);
        closeModal();
        await loadKeys();
        const plaintext = plaintextFromResponse(created);
        if (plaintext) showNewKey(created.name || payload.name, plaintext);
      }
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(key) {
    if (!confirm(`Are you sure you want to delete MCP key "${key.name}"?`)) {
      return;
    }

    try {
      await deleteMcpKey(key.id);
      await loadKeys();
    } catch (err) {
      alert('Failed to delete MCP key: ' + err.message);
    }
  }

  async function handleToggleEnabled(key) {
    const next = !key.enabled;
    const label = next ? 'enable' : 'disable';
    if (!confirm(`${next ? 'Enable' : 'Disable'} MCP key "${key.name}"?`)) {
      return;
    }
    try {
      togglingKey[key.id] = true;
      togglingKey = togglingKey;
      await updateMcpKey(key.id, { enabled: next });
      await loadKeys();
    } catch (err) {
      alert(`Failed to ${label} MCP key: ` + err.message);
    } finally {
      togglingKey[key.id] = false;
      togglingKey = togglingKey;
    }
  }

  async function handleRotateKey(key) {
    if (!confirm(`Are you sure you want to rotate the API key for "${key.name}"? The old key will stop working immediately.`)) {
      return;
    }

    try {
      rotatingKey[key.id] = true;
      rotatingKey = rotatingKey;
      const updated = await rotateMcpKey(key.id);
      await loadKeys();
      const plaintext = plaintextFromResponse(updated);
      if (plaintext) showNewKey(key.name, plaintext);
      else alert('Key rotated, but the new secret was not returned. Try creating a new key.');
    } catch (err) {
      alert('Failed to rotate API key: ' + err.message);
    } finally {
      rotatingKey[key.id] = false;
      rotatingKey = rotatingKey;
    }
  }
</script>

<PageHeader title="MCP Keys">
  <svelte:fragment slot="actions">
    <Button variant="primary" on:click={openAddModal}>
      <svelte:fragment slot="icon">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
      </svelte:fragment>
      Add MCP Key
    </Button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page integrations-container">
  <p class="admin-page-lead">
    Dedicated keys for remote admin AI over MCP at <code>{mcpOriginUrl}</code>.
    These are not billing or reseller keys. Scopes are a ladder:
    <strong>read</strong> ⊂ <strong>write</strong> ⊂ <strong>destructive</strong>.
  </p>

  <div class="snippet-panel">
    <div class="snippet-header">
      <span>Remote MCP snippet</span>
      <Button variant="secondary" size="small" on:click={copyExampleSnippet}>
        {snippetCopied ? 'Copied!' : 'Copy snippet'}
      </Button>
    </div>
    <pre class="snippet-code">{exampleSnippet}</pre>
    <p class="field-hint">Replace <code>rfmcp_…</code> with the plaintext key shown once on create or rotate.</p>
  </div>

  {#if loading}
    <div class="loading">Loading MCP keys...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if keys.length === 0}
    <div class="empty-state">
      <p>No MCP keys found. Click "Add MCP Key" to create one.</p>
    </div>
  {:else}
    <div class="integrations-grid">
      {#each keys as key (key.id)}
        {@const allowlist = normalizeAllowlist(key.ip_allowlist)}
        {@const scopes = normalizeScopes(key.scopes)}
        <div class="integration-card">
          <div class="integration-header">
            <div>
              <h3>{key.name}</h3>
              <span class="integration-type">rfmcp</span>
            </div>
            <div class="integration-status">
              {#if key.enabled}
                <span class="status-badge status-active">Active</span>
              {:else}
                <span class="status-badge status-inactive">Disabled</span>
              {/if}
            </div>
          </div>

          {#if key.description}
            <p class="integration-description">{key.description}</p>
          {/if}

          <div class="scope-row">
            {#each SCOPE_ORDER as scope (scope)}
              <span class="scope-chip" class:scope-on={scopes.includes(scope)}>{scope}</span>
            {/each}
          </div>

          <div class="integration-details">
            <div class="detail-item">
              <span class="detail-label">API Key:</span>
              <div class="api-key-container">
                <span class="api-key-hidden" title="Full key is shown only once, when created or rotated">{key.api_key || 'rfmcp_…'}</span>
              </div>
            </div>

            <div class="detail-item">
              <span class="detail-label">Last Used:</span>
              <span>{formatWhen(key.last_used_at)}</span>
            </div>

            <div class="detail-item">
              <span class="detail-label">Last IP:</span>
              <span>{key.last_used_ip || '—'}</span>
            </div>

            <div class="detail-item">
              <span class="detail-label">Allowlist:</span>
              <span class="allowlist-value">
                {#if allowlist.length}
                  {allowlist.join(', ')}
                {:else}
                  Any IP
                {/if}
              </span>
            </div>

            <div class="detail-item">
              <span class="detail-label">Expires:</span>
              <span>{key.expires_at ? formatWhen(key.expires_at) : 'Never'}</span>
            </div>
          </div>

          <div class="integration-actions">
            <Button
              variant="secondary"
              size="small"
              on:click={() => handleToggleEnabled(key)}
              disabled={togglingKey[key.id]}
            >
              {togglingKey[key.id] ? 'Saving...' : (key.enabled ? 'Disable' : 'Enable')}
            </Button>
            <Button variant="secondary" size="small" on:click={() => handleRotateKey(key)} disabled={rotatingKey[key.id]}>
              {rotatingKey[key.id] ? 'Rotating...' : 'Rotate Key'}
            </Button>
            <Button iconOnly on:click={() => openEditModal(key)} title="Edit" ariaLabel="Edit">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </Button>
            <Button variant="danger" iconOnly on:click={() => handleDelete(key)} title="Delete" ariaLabel="Delete">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </Button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <Modal
    title={editingKey ? 'Edit MCP Key' : 'Add MCP Key'}
    onClose={closeModal}
  >
    {#if formError}
      <FormError>{formError}</FormError>
    {/if}
    <FormGroup label="Name *" forId="mcp-key-name" required>
      <input
        id="mcp-key-name"
        type="text"
        bind:value={formData.name}
        placeholder="e.g., Cursor ops"
        required
      />
    </FormGroup>
    <FormGroup label="Description" forId="mcp-key-description">
      <textarea
        id="mcp-key-description"
        bind:value={formData.description}
        placeholder="Optional description"
        rows="3"
      ></textarea>
    </FormGroup>
    <FormGroup label="Scopes" forId="mcp-key-scope-read">
      <div class="scope-checks">
        {#each SCOPE_ORDER as scope}
          <label class="scope-check">
            <input
              id={scope === 'read' ? 'mcp-key-scope-read' : undefined}
              type="checkbox"
              checked={formData.scopes.includes(scope)}
              on:change={(e) => setScope(scope, e.currentTarget.checked)}
            />
            <span>
              <strong>{scope}</strong>
              <small>{SCOPE_HELP[scope]}</small>
            </span>
          </label>
        {/each}
      </div>
    </FormGroup>
    <FormGroup label="IP allowlist (CIDR)" forId="mcp-key-cidr" help="Leave empty to allow any IP. One CIDR per entry.">
      <div class="cidr-editor">
        {#if formData.ip_allowlist.length}
          <ul class="cidr-list">
            {#each formData.ip_allowlist as cidr}
              <li>
                <code>{cidr}</code>
                <button type="button" class="cidr-remove" on:click={() => removeCidr(cidr)} aria-label="Remove {cidr}">Remove</button>
              </li>
            {/each}
          </ul>
        {/if}
        <div class="cidr-add">
          <input
            id="mcp-key-cidr"
            type="text"
            bind:value={cidrDraft}
            placeholder="192.0.2.0/24"
            on:keydown={onCidrKeydown}
          />
          <Button variant="secondary" size="small" on:click={addCidr}>Add</Button>
        </div>
      </div>
    </FormGroup>
    <FormGroup label="Expires" forId="mcp-key-expires" help="Leave empty for no expiry.">
      <input
        id="mcp-key-expires"
        type="datetime-local"
        bind:value={formData.expires_at}
      />
    </FormGroup>
    <FormGroup>
      <label class="enabled-check">
        <input type="checkbox" bind:checked={formData.enabled} />
        Enabled
      </label>
    </FormGroup>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={closeModal}>Cancel</Button>
      <Button variant="primary" on:click={handleSubmit}>
        {editingKey ? 'Update' : 'Create'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if newKeyModal.open}
  <Modal title="Copy your API key now" size="large" onClose={closeNewKeyModal}>
    <p class="new-key-warning">
      This is the only time the full API key for <strong>{newKeyModal.name}</strong>
      will be shown. Store it securely — it cannot be retrieved later. If you lose
      it, rotate the key to generate a new one.
    </p>
    <div class="new-key-box">
      <code class="new-key-value">{newKeyModal.apiKey}</code>
    </div>
    <p class="snippet-label">Remote MCP snippet</p>
    <pre class="snippet-code">{mcpRemoteSnippet(newKeyModal.apiKey)}</pre>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={copyNewKey}>
        {newKeyModal.copied ? 'Copied!' : 'Copy key'}
      </Button>
      <Button variant="secondary" on:click={copyNewKeySnippet}>
        {newKeyModal.copiedSnippet ? 'Copied!' : 'Copy snippet'}
      </Button>
      <Button variant="primary" on:click={closeNewKeyModal}>Done</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }

  .snippet-panel {
    margin: 0 0 20px;
    padding: 14px 16px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
  }

  .snippet-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .snippet-code {
    margin: 0;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 8px;
    overflow-x: auto;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.45;
    color: var(--text-primary);
    white-space: pre;
  }

  .snippet-label {
    margin: 16px 0 8px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .integrations-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 400px), 1fr));
    gap: 20px;
  }

  .integration-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .integration-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .integration-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .integration-header h3 {
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .integration-type {
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .integration-status {
    display: flex;
    align-items: center;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
  }

  .status-active {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-inactive {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .integration-description {
    margin: 12px 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
  }

  .scope-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 0 0 12px;
  }

  .scope-chip {
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    background: var(--bg-tertiary);
    color: var(--text-tertiary);
  }

  .scope-chip.scope-on {
    background: var(--info-bg, var(--bg-tertiary));
    color: var(--info-text, var(--text-primary));
  }

  .integration-details {
    margin: 16px 0;
    padding: 16px;
    background: var(--bg-tertiary);
    border-radius: 8px;
  }

  .detail-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
    font-size: 14px;
  }

  .detail-item:last-child {
    margin-bottom: 0;
  }

  .detail-label {
    font-weight: 600;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .allowlist-value {
    text-align: right;
    word-break: break-all;
  }

  .api-key-container {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .api-key-hidden {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .field-hint {
    margin: 8px 0 0;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .scope-checks {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .scope-check,
  .enabled-check {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    cursor: pointer;
  }

  .scope-check span {
    display: flex;
    flex-direction: column;
  }

  .scope-check small {
    color: var(--text-secondary);
    font-size: 12px;
  }

  .cidr-editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .cidr-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .cidr-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 6px;
  }

  .cidr-remove {
    border: none;
    background: none;
    color: var(--danger-color);
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
  }

  .cidr-add {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .new-key-warning {
    margin: 0 0 16px 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
  }

  .new-key-box {
    background: var(--bg-tertiary);
    border-radius: 8px;
    padding: 12px;
    word-break: break-all;
  }

  .new-key-value {
    font-family: 'Courier New', monospace;
    font-size: 13px;
    color: var(--text-primary);
  }

  .integration-actions {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border-color);
  }
</style>
