<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import { getClientProfile, setClientPassword, impersonateClient, listPermissionSets, updateClientPermissionSet } from '../lib/api.js';

  /** Route param: numeric user id */
  export let userId;

  let profile = null;
  let loading = true;
  let error = null;
  let busy = false;
  let actionError = null;
  let actionMessage = null;
  let permissionSets = [];
  let selectedPermissionSetId = '';
  let permissionBusy = false;

  let passwordForm = { password: '' };

  async function load() {
    loading = true;
    error = null;
    actionError = null;
    actionMessage = null;
    try {
      const [profileData, permissionSetRows] = await Promise.all([
        getClientProfile(userId),
        listPermissionSets(),
      ]);
      profile = profileData;
      permissionSets = permissionSetRows;
      selectedPermissionSetId = profile.permission_set_id ? String(profile.permission_set_id) : '';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  async function savePermissionSet() {
    actionError = null;
    actionMessage = null;
    permissionBusy = true;
    try {
      const id = selectedPermissionSetId ? Number(selectedPermissionSetId) : null;
      await updateClientPermissionSet(profile.user_id, id);
      actionMessage = 'Client permission preset updated.';
      await load();
    } catch (e) {
      actionError = e.message || String(e);
    } finally {
      permissionBusy = false;
    }
  }

  onMount(load);
  $: userId, load();

  function formatServiceStatus(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  async function submitSetPassword() {
    actionError = null;
    actionMessage = null;
    busy = true;
    try {
      if (!passwordForm.password) throw new Error('Enter a new password');
      await setClientPassword(profile.user_id, passwordForm.password);
      passwordForm.password = '';
      actionMessage = 'Password updated.';
      await load();
    } catch (e) {
      actionError = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function clearPassword() {
    if (!confirm('Clear this client\'s password? They will only be able to sign in via impersonation or billing SSO.')) return;
    actionError = null;
    actionMessage = null;
    busy = true;
    try {
      await setClientPassword(profile.user_id, '');
      actionMessage = 'Password cleared — password login disabled.';
      await load();
    } catch (e) {
      actionError = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function signInAsClient() {
    actionError = null;
    busy = true;
    try {
      const { token } = await impersonateClient(profile.user_id);
      window.open(`/client?impersonate=${encodeURIComponent(token)}`, '_blank', 'noopener');
    } catch (e) {
      actionError = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

<PageHeader title="User Profile" />
<div class="container">
  <button class="btn-secondary" on:click={() => navigate('/admin/users')}>← Back to Users</button>

  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if profile}
    <section class="panel">
      <h3>{profile.username}</h3>
      <table class="kv-table">
        <tbody>
          <tr><th>Username</th><td>{profile.username}</td></tr>
          <tr><th>Email</th><td>{profile.email}</td></tr>
          <tr>
            <th>Password login</th>
            <td>
              <span class="status-badge" class:status-active={profile.has_password}>
                {profile.has_password ? 'Password set' : 'Disabled (impersonation / SSO only)'}
              </span>
            </td>
          </tr>
          {#if profile.integration_name}
            <tr><th>Billing integration</th><td>{profile.integration_name}</td></tr>
          {/if}
          {#if profile.external_username}
            <tr><th>External username</th><td>{profile.external_username}</td></tr>
          {/if}
          {#if profile.external_email}
            <tr><th>External email</th><td>{profile.external_email}</td></tr>
          {/if}
        </tbody>
      </table>

      {#if profile.linked_externals?.length}
        <h4>Linked billing identities</h4>
        <ul class="linked-list">
          {#each profile.linked_externals as link}
            <li>
              {link.external_username || `external id ${link.external_user_id}`}
              <span class="muted">({link.integration_name})</span>
            </li>
          {/each}
        </ul>
      {/if}

      {#if actionError}<div class="error">{actionError}</div>{/if}
      {#if actionMessage}<div class="success">{actionMessage}</div>{/if}

      <div class="actions">
        <button type="button" class="btn-primary" disabled={busy} on:click={signInAsClient}>
          {busy ? 'Working…' : 'Sign in as this user →'}
        </button>
        <span class="hint">Opens the client portal in a new tab, signed in as this user. Your admin session is unaffected. Every client always has portal access this way, regardless of password status.</span>
      </div>

      <div class="subpanel">
        <h4>Client permission preset</h4>
        <p class="hint">
          Controls which actions this client can take on their services (power, IPMI, reinstall, etc). Falls back to
          the product's preset, then system defaults, when unset. Individual services can still override this.
        </p>
        <div class="form-row">
          <label>Preset
            <select bind:value={selectedPermissionSetId}>
              <option value="">No preset (use product default)</option>
              {#each permissionSets as ps}
                <option value={String(ps.id)}>{ps.name}</option>
              {/each}
            </select>
          </label>
          <button type="button" class="btn-secondary" disabled={permissionBusy} on:click={savePermissionSet}>
            {permissionBusy ? 'Saving…' : 'Save preset'}
          </button>
        </div>
      </div>

      <div class="subpanel">
        <h4>Password login</h4>
        <p class="hint">
          Clients always have full client portal access via impersonation / billing SSO. Set a password here to
          also allow this client to sign in directly with a username and password.
        </p>
        <div class="form-row">
          <label>New password <input type="password" bind:value={passwordForm.password} /></label>
          <button type="button" class="btn-secondary" disabled={busy} on:click={submitSetPassword}>Set password</button>
          {#if profile.has_password}
            <button type="button" class="btn-link-danger" disabled={busy} on:click={clearPassword}>Clear password</button>
          {/if}
        </div>
      </div>
    </section>

    <section class="panel">
      <h3>Services ({profile.services?.length || 0})</h3>
      {#if !profile.services || profile.services.length === 0}
        <p class="muted">No services owned by this client.</p>
      {:else}
        <table class="kv-table services-table">
          <thead>
            <tr><th>Name</th><th>Type</th><th>Status</th></tr>
          </thead>
          <tbody>
            {#each profile.services as s}
              <tr class="clickable-row" on:click={() => navigate(`/admin/services/${s.id}`)}>
                <td>{s.name}</td>
                <td>{s.service_type || '—'}</td>
                <td>{formatServiceStatus(s.status)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
  {/if}
</div>

<style>
  .container { padding: 32px; display: grid; gap: 16px; }
  @media (max-width: 768px) { .container { padding: 16px; } }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); }
  .kv-table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
  .kv-table th { text-align: left; padding: 6px 10px; color: var(--text-secondary); font-weight: 600; width: 200px; }
  .kv-table td { padding: 6px 10px; color: var(--text-primary); }
  .services-table th, .services-table td { width: auto; }
  .muted { color: var(--text-secondary); }
  .linked-list { margin: 0 0 12px; padding-left: 20px; }
  .status-badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; background: var(--info-bg); color: var(--info-text); }
  .status-badge.status-active { background: var(--success-bg); color: var(--success-text); }
  .actions { display: flex; align-items: center; gap: 12px; margin: 12px 0; flex-wrap: wrap; }
  .hint { font-size: 13px; color: var(--text-secondary); }
  .subpanel { border-top: 1px solid var(--border-color); margin-top: 16px; padding-top: 16px; }
  .subpanel h4 { margin: 0 0 8px; }
  .form-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  .form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  .form-row input, .form-row select {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-primary);
    color: var(--text-primary);
  }
  .clickable-row { cursor: pointer; }
  .clickable-row:hover { background: var(--bg-tertiary); }
  .error { color: var(--danger-color); }
  .success { color: var(--success-text); }
  .btn-primary {
    padding: 10px 20px;
    background: var(--accent-color);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-secondary {
    padding: 10px 16px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-link-danger {
    background: none;
    border: none;
    color: var(--danger-color);
    font-weight: 600;
    cursor: pointer;
    text-decoration: underline;
  }
  .btn-primary:disabled, .btn-secondary:disabled, .btn-link-danger:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
