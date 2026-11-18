<script>
  import { createEventDispatcher } from 'svelte';
  import Button from '../ui/Button.svelte';
  import { createStorageSftpUser, updateStorageSftpUser, deleteStorageSftpUser } from '../../lib/api.js';
  import { SFTP_STATE_LABELS } from '../../lib/storage.js';

  // A storage object from /api/admin/storage or /api/client/storage.
  export let storage;
  // true: use the client routes; false: the staff routes.
  export let client = false;

  const dispatch = createEventDispatcher();
  let working = false;
  let error = '';
  let revealed = null;
  let copied = '';
  let showCreate = false;
  let form = emptyForm();
  let editingId = null;
  let editForm = {};

  function emptyForm() {
    return { name: '', public_keys: '', password_mode: 'none', password: '', allowed_ips: '' };
  }

  $: sftp = storage?.sftp || { available: false, state: 'unavailable', users: [], shares: [] };
  $: writable = storage && ['ready', 'provisioning', 'error', 'unlinked'].includes(storage.status);

  const lines = (text) => text.split(/\n+/).map((s) => s.trim()).filter(Boolean);
  const addresses = (text) => text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

  async function run(action) {
    working = true;
    error = '';
    try {
      const updated = await action();
      if (updated?.generated_password) revealed = updated.generated_password;
      dispatch('change', updated);
      return true;
    } catch (err) {
      error = err.message || 'Request failed';
      return false;
    } finally {
      working = false;
    }
  }

  async function submitCreate() {
    const body = {
      name: form.name.trim(),
      public_keys: lines(form.public_keys),
      allowed_ips: addresses(form.allowed_ips),
      generate_password: form.password_mode === 'generate',
    };
    if (form.password_mode === 'set') body.password = form.password;
    revealed = null;
    if (await run(() => createStorageSftpUser(storage.service_id, body, client))) {
      showCreate = false;
      form = emptyForm();
    }
  }

  function startEdit(user) {
    editingId = user.id;
    editForm = {
      public_keys: user.public_keys.map((k) => k.key).join('\n'),
      allowed_ips: user.allowed_ips.join('\n'),
      password_mode: 'keep',
      password: '',
    };
  }

  async function submitEdit(user) {
    const body = { public_keys: lines(editForm.public_keys), allowed_ips: addresses(editForm.allowed_ips) };
    if (editForm.password_mode === 'generate') body.generate_password = true;
    if (editForm.password_mode === 'set') body.password = editForm.password;
    if (editForm.password_mode === 'clear') body.clear_password = true;
    revealed = null;
    if (await run(() => updateStorageSftpUser(storage.service_id, user.id, body, client))) editingId = null;
  }

  function toggle(user) {
    run(() => updateStorageSftpUser(storage.service_id, user.id, { enabled: !user.enabled }, client));
  }

  function remove(user) {
    if (!confirm(`Delete SFTP login ${user.login}? Its open sessions are closed.`)) return;
    run(() => deleteStorageSftpUser(storage.service_id, user.id, client));
  }

  async function copy(text, key) {
    try {
      await navigator.clipboard.writeText(text);
      copied = key;
      setTimeout(() => {
        if (copied === key) copied = '';
      }, 1500);
    } catch {
      error = 'Clipboard unavailable';
    }
  }
</script>

<div class="sftp">
  <div class="head">
    <h3>SFTP access <span class="badge" class:ok={sftp.state === 'ready'} class:bad={sftp.state === 'error'}>{SFTP_STATE_LABELS[sftp.state] || sftp.state}</span></h3>
    {#if writable && !showCreate && sftp.users.length < 16}
      <Button size="small" on:click={() => (showCreate = true)}>New login</Button>
    {/if}
  </div>

  {#if sftp.available}
    <p class="muted">
      Connect from anywhere to <span class="mono">{sftp.host}</span> port <span class="mono">{sftp.port}</span>.
      {#if sftp.host_key_fingerprint}Host key <span class="mono">{sftp.host_key_fingerprint}</span>.{/if}
      Each share with SFTP turned on appears as a folder.
    </p>
    {#if sftp.shares.length === 0}
      <p class="muted">No share has SFTP turned on yet. Edit a share and choose its SFTP access.</p>
    {/if}
  {:else}
    <p class="muted">SFTP becomes available once the storage is linked to a network and placed on a storage node.</p>
  {/if}
  {#if !client && sftp.runner_error}<p class="err">Runner: {sftp.runner_error}</p>{/if}

  {#if error}<div class="banner error">{error}</div>{/if}
  {#if revealed}
    <div class="banner ok">
      Generated password, shown only once:
      <div class="copyline"><code>{revealed}</code><button class="tiny" on:click={() => copy(revealed, 'pw')}>{copied === 'pw' ? 'Copied' : 'Copy'}</button></div>
    </div>
  {/if}

  {#if showCreate}
    <div class="form">
      <label>Name <input bind:value={form.name} maxlength="32" placeholder="backup" /></label>
      <small>The login is the name followed by a dot and the storage number, for example backup.{storage.service_id}.</small>
      <label>SSH public keys
        <textarea rows="3" bind:value={form.public_keys} placeholder="ssh-ed25519 AAAA... laptop"></textarea>
      </label>
      <small>One per line: ed25519, ECDSA, or RSA of at least 2048 bits.</small>
      <label>Password
        <select bind:value={form.password_mode}>
          <option value="none">No password (keys only)</option>
          <option value="generate">Generate one for me</option>
          <option value="set">Set a password</option>
        </select>
      </label>
      {#if form.password_mode === 'set'}
        <input type="password" bind:value={form.password} minlength="12" autocomplete="new-password" aria-label="Password" />
        <small>At least 12 characters.</small>
      {/if}
      <label>Allowed source addresses
        <textarea rows="2" bind:value={form.allowed_ips} placeholder="203.0.113.0/24"></textarea>
      </label>
      <small>Optional. Addresses or CIDRs allowed to log in; leave empty to allow any address.</small>
      <div class="actions">
        <Button size="small" on:click={submitCreate} disabled={working || !form.name.trim()}>Create login</Button>
        <Button size="small" variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
      </div>
    </div>
  {/if}

  {#each sftp.users as user (user.id)}
    <div class="user">
      <div class="user-head">
        <div>
          <strong class="mono">{user.login}</strong>
          {#if !user.enabled}<span class="badge">Disabled</span>{/if}
          {#if user.has_password}<span class="badge">Password</span>{/if}
          <span class="badge">{user.public_keys.length} key{user.public_keys.length === 1 ? '' : 's'}</span>
        </div>
        {#if writable}
          <div class="actions">
            <button class="tiny" disabled={working} on:click={() => startEdit(user)}>Edit</button>
            <button class="tiny" disabled={working} on:click={() => toggle(user)}>{user.enabled ? 'Disable' : 'Enable'}</button>
            <button class="tiny danger" disabled={working} on:click={() => remove(user)}>Delete</button>
          </div>
        {/if}
      </div>
      {#each user.public_keys as key (key.fingerprint)}
        <p class="muted mono">{key.type} {key.fingerprint}{key.comment ? ` (${key.comment})` : ''}</p>
      {/each}
      <p class="muted">
        From: {#if user.allowed_ips.length}<span class="mono">{user.allowed_ips.join(', ')}</span>{:else}any address{/if}
        · Last login: {user.last_login_at ? `${new Date(user.last_login_at).toLocaleString()} from ${user.last_login_ip || 'unknown'}` : 'never'}
      </p>
      {#if user.sftp_command}
        <div class="copyline">
          <code>{user.sftp_command}</code>
          <button class="tiny" on:click={() => copy(user.sftp_command, `c${user.id}`)}>{copied === `c${user.id}` ? 'Copied' : 'Copy'}</button>
        </div>
      {/if}
      {#if editingId === user.id}
        <div class="form">
          <label>SSH public keys <textarea rows="3" bind:value={editForm.public_keys}></textarea></label>
          <label>Password
            <select bind:value={editForm.password_mode}>
              <option value="keep">Keep as it is</option>
              <option value="generate">Generate a new one</option>
              <option value="set">Set a new one</option>
              {#if user.has_password}<option value="clear">Remove the password</option>{/if}
            </select>
          </label>
          {#if editForm.password_mode === 'set'}
            <input type="password" bind:value={editForm.password} minlength="12" autocomplete="new-password" aria-label="New password" />
          {/if}
          <label>Allowed source addresses <textarea rows="2" bind:value={editForm.allowed_ips}></textarea></label>
          <div class="actions">
            <Button size="small" on:click={() => submitEdit(user)} disabled={working}>Save</Button>
            <Button size="small" variant="secondary" on:click={() => (editingId = null)}>Cancel</Button>
          </div>
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .sftp { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
  .head, .user-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  h3 { margin: 6px 0 0; font-size: 15px; }
  .user { border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
  .badge { display: inline-block; margin-left: 6px; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--border-color); color: var(--text-secondary); }
  .badge.ok { color: var(--success-text, #2e7d32); border-color: currentColor; }
  .badge.bad { color: var(--danger-color, #c62828); border-color: currentColor; }
  .muted { margin: 0; font-size: 13px; color: var(--text-secondary); word-break: break-word; }
  .err { margin: 0; font-size: 13px; color: var(--danger-color); }
  .copyline { display: flex; gap: 8px; align-items: center; background: var(--bg-secondary); border-radius: 6px; padding: 6px 8px; }
  .copyline code { flex: 1; overflow-x: auto; white-space: nowrap; }
  .form { display: flex; flex-direction: column; gap: 6px; border: 1px dashed var(--border-color); border-radius: 8px; padding: 12px; }
  .form label { display: grid; gap: 4px; font-size: 13px; font-weight: 600; }
  .form small { color: var(--text-tertiary); font-size: 12px; margin-top: -4px; }
  input, textarea, select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); }
  .actions { display: flex; gap: 6px; }
  .tiny { padding: 3px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); cursor: pointer; font-size: 12px; }
  .tiny.danger { color: var(--danger-color); border-color: var(--danger-color); }
  .tiny:disabled { opacity: 0.5; cursor: default; }
  .banner { padding: 8px 10px; border-radius: 6px; }
  .banner.error { background: var(--danger-bg); color: var(--danger-text); }
  .banner.ok { background: var(--success-bg, #e7f5ef); color: var(--success-text, #0b6b3a); }
</style>
