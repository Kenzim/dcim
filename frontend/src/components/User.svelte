<script>
  import { onMount } from 'svelte';
  import { user, logout } from '../stores/auth.js';
  import { getCurrentUser, getSessions, deleteSession, changePassword } from '../lib/api.js';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';

  let userData = null;
  let sessions = [];
  let loading = true;
  let sessionsLoading = false;
  let error = null;

  let selectedTokenIds = [];
  let lastClickedIndex = null;
  let shiftKeyHeld = false;
  let bulkRevoking = false;

  let currentPassword = '';
  let newPassword = '';
  let confirmPassword = '';
  let passwordChanging = false;
  let passwordError = '';
  let passwordSuccess = false;

  $: revocableSessions = sessions.filter((s) => !s.is_current);
  $: selectedCount = selectedTokenIds.length;
  $: allRevocableSelected =
    revocableSessions.length > 0 &&
    revocableSessions.every((s) => selectedTokenIds.includes(s.token_id));
  $: someRevocableSelected =
    revocableSessions.some((s) => selectedTokenIds.includes(s.token_id)) && !allRevocableSelected;

  onMount(async () => {
    await loadUserData();
    await loadSessions();
  });

  async function loadUserData() {
    loading = true;
    error = null;
    try {
      userData = await getCurrentUser();
      if (userData) {
        user.set(userData);
      }
    } catch (err) {
      error = err.message || 'Failed to load user data';
      console.error('Error loading user data:', err);
    } finally {
      loading = false;
    }
  }

  async function loadSessions() {
    sessionsLoading = true;
    try {
      sessions = await getSessions() || [];
      const valid = new Set(sessions.filter((s) => !s.is_current).map((s) => s.token_id));
      selectedTokenIds = selectedTokenIds.filter((id) => valid.has(id));
      lastClickedIndex = null;
    } catch (err) {
      console.error('Error loading sessions:', err);
      sessions = [];
      selectedTokenIds = [];
      lastClickedIndex = null;
    } finally {
      sessionsLoading = false;
    }
  }

  function setRangeSelected(fromIndex, toIndex, select) {
    const start = Math.min(fromIndex, toIndex);
    const end = Math.max(fromIndex, toIndex);
    const rangeIds = sessions
      .slice(start, end + 1)
      .filter((s) => !s.is_current)
      .map((s) => s.token_id);
    if (select) {
      const next = new Set(selectedTokenIds);
      rangeIds.forEach((id) => next.add(id));
      selectedTokenIds = [...next];
    } else {
      const remove = new Set(rangeIds);
      selectedTokenIds = selectedTokenIds.filter((id) => !remove.has(id));
    }
  }

  function onSessionCheckboxMouseDown(event) {
    shiftKeyHeld = event.shiftKey;
  }

  function onSessionCheckboxChange(event, session, index) {
    if (session.is_current) {
      event.currentTarget.checked = false;
      return;
    }

    const select = event.currentTarget.checked;
    if (shiftKeyHeld && lastClickedIndex != null) {
      setRangeSelected(lastClickedIndex, index, select);
    } else if (select) {
      if (!selectedTokenIds.includes(session.token_id)) {
        selectedTokenIds = [...selectedTokenIds, session.token_id];
      }
    } else {
      selectedTokenIds = selectedTokenIds.filter((id) => id !== session.token_id);
    }

    lastClickedIndex = index;
    shiftKeyHeld = false;
  }

  function toggleSelectAllRevocable() {
    selectedTokenIds = allRevocableSelected ? [] : revocableSessions.map((s) => s.token_id);
    lastClickedIndex = null;
  }

  async function handleDeleteSession(tokenId) {
    if (!confirm('Are you sure you want to delete this session?')) {
      return;
    }

    try {
      await deleteSession(tokenId);
      selectedTokenIds = selectedTokenIds.filter((id) => id !== tokenId);
      await loadSessions();
    } catch (err) {
      alert(err.message || 'Failed to delete session');
      console.error('Error deleting session:', err);
    }
  }

  async function handleBulkRevoke() {
    const ids = selectedTokenIds.filter((id) => {
      const session = sessions.find((s) => s.token_id === id);
      return session && !session.is_current;
    });
    if (!ids.length) return;
    if (!confirm(`Revoke ${ids.length} selected session${ids.length === 1 ? '' : 's'}?`)) {
      return;
    }

    bulkRevoking = true;
    try {
      const results = await Promise.allSettled(ids.map((id) => deleteSession(id)));
      const failed = results.filter((r) => r.status === 'rejected').length;
      selectedTokenIds = [];
      await loadSessions();
      if (failed) {
        alert(`Revoked ${ids.length - failed} session(s); ${failed} failed.`);
      }
    } catch (err) {
      alert(err.message || 'Failed to revoke sessions');
      console.error('Error bulk-revoking sessions:', err);
    } finally {
      bulkRevoking = false;
    }
  }

  async function handleLogoutCurrentSession() {
    if (!confirm('Are you sure you want to logout? This will end your current session.')) {
      return;
    }

    try {
      await logout();
      navigate('/');
    } catch (err) {
      alert(err.message || 'Failed to logout');
      console.error('Error logging out:', err);
    }
  }

  function formatDate(dateString) {
    if (!dateString || dateString === 'unknown') return 'Unknown';
    try {
      const date = new Date(dateString);
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  }

  /** Keep the header checkbox in mixed state when only some rows are selected. */
  function checkboxIndeterminate(node, value) {
    node.indeterminate = !!value;
    return {
      update(next) {
        node.indeterminate = !!next;
      },
    };
  }

  async function handleChangePassword() {
    passwordError = '';
    passwordSuccess = false;
    if (!currentPassword || !newPassword || !confirmPassword) {
      passwordError = 'Please fill in all fields';
      return;
    }
    if (newPassword !== confirmPassword) {
      passwordError = 'New password and confirmation do not match';
      return;
    }
    if (newPassword.length < 8) {
      passwordError = 'New password must be at least 8 characters';
      return;
    }
    passwordChanging = true;
    try {
      await changePassword(currentPassword, newPassword);
      passwordSuccess = true;
      currentPassword = '';
      newPassword = '';
      confirmPassword = '';
    } catch (err) {
      passwordError = err.message || 'Failed to change password';
    } finally {
      passwordChanging = false;
    }
  }
</script>

<PageHeader title="User Profile" />

<div class="admin-page">
  {#if loading}
    <div class="loading-container">
      <div class="spinner"></div>
      <p>Loading user data...</p>
    </div>
  {:else if error}
    <div class="alert alert-error">
      <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      {error}
    </div>
  {:else if userData}
    <div class="profile-meta">
      <div class="meta-item"><span>ID</span><strong>#{userData.id}</strong></div>
      <div class="meta-item"><span>Username</span><strong>{userData.username}</strong></div>
      <div class="meta-item"><span>Email</span><strong>{userData.email}</strong></div>
      <div class="meta-item">
        <span>Role</span>
        <strong>
          {#if userData.is_admin}
            <span class="badge badge-success">Administrator</span>
          {:else}
            <span class="badge badge-secondary">User</span>
          {/if}
        </strong>
      </div>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>Change password</h2>
      </div>
      {#if passwordSuccess}
        <div class="alert alert-success">
          <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          Password changed successfully.
        </div>
      {:else}
        {#if passwordError}
          <div class="alert alert-error">
            <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {passwordError}
          </div>
        {/if}
        <form class="password-form" on:submit|preventDefault={handleChangePassword}>
          <div class="form-group">
            <label for="current-password" class="form-label">Current password</label>
            <input
              id="current-password"
              type="password"
              class="form-control"
              bind:value={currentPassword}
              placeholder="Enter current password"
              autocomplete="current-password"
              disabled={passwordChanging}
            />
          </div>
          <div class="form-group">
            <label for="new-password" class="form-label">New password</label>
            <input
              id="new-password"
              type="password"
              class="form-control"
              bind:value={newPassword}
              placeholder="Enter new password (min 8 characters)"
              autocomplete="new-password"
              disabled={passwordChanging}
            />
          </div>
          <div class="form-group">
            <label for="confirm-password" class="form-label">Confirm new password</label>
            <input
              id="confirm-password"
              type="password"
              class="form-control"
              bind:value={confirmPassword}
              placeholder="Confirm new password"
              autocomplete="new-password"
              disabled={passwordChanging}
            />
          </div>
          <button type="submit" class="btn-primary" disabled={passwordChanging}>
            {passwordChanging ? 'Changing…' : 'Change password'}
          </button>
        </form>
      {/if}
    </section>

    <section class="panel panel-sessions">
      <div class="panel-head">
        <h2>Active sessions</h2>
        <div class="panel-head-actions">
          {#if selectedCount > 0}
            <button
              type="button"
              class="btn-danger btn-small"
              disabled={bulkRevoking}
              on:click={handleBulkRevoke}
            >
              {bulkRevoking ? 'Revoking…' : `Revoke selected (${selectedCount})`}
            </button>
          {/if}
          <span class="session-count">{sessions.length}</span>
        </div>
      </div>
      {#if sessionsLoading}
        <div class="sessions-loading">
          <div class="spinner-small"></div>
          <span>Loading sessions…</span>
        </div>
      {:else if sessions.length === 0}
        <div class="sessions-empty">No active sessions</div>
      {:else}
        <div class="admin-data-table sessions-table-wrap">
          <table class="sessions-table">
            <colgroup>
              <col class="col-check" />
              <col class="col-token" />
              <col class="col-ip" />
              <col class="col-seen" />
              <col class="col-created" />
              <col class="col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th class="check-col">
                  <input
                    type="checkbox"
                    aria-label="Select all revocable sessions"
                    disabled={revocableSessions.length === 0 || bulkRevoking}
                    checked={allRevocableSelected}
                    use:checkboxIndeterminate={someRevocableSelected}
                    on:change={toggleSelectAllRevocable}
                  />
                </th>
                <th>Token</th>
                <th>IP</th>
                <th>Last seen</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {#each sessions as session, index (session.token_id)}
                <tr
                  class:current={session.is_current}
                  class:selected={selectedTokenIds.includes(session.token_id)}
                >
                  <td class="check-col">
                    <input
                      type="checkbox"
                      aria-label={session.is_current ? 'Current session (not revocable)' : `Select session ${session.token}`}
                      disabled={session.is_current || bulkRevoking}
                      checked={selectedTokenIds.includes(session.token_id)}
                      on:mousedown={onSessionCheckboxMouseDown}
                      on:change={(e) => onSessionCheckboxChange(e, session, index)}
                    />
                  </td>
                  <td>
                    <code class="session-token">{session.token}</code>
                    {#if session.is_current}
                      <span class="session-badge">Current</span>
                    {/if}
                  </td>
                  <td>{session.last_seen_ip || '—'}</td>
                  <td>{formatDate(session.last_seen_at)}</td>
                  <td>{formatDate(session.created_at)}</td>
                  <td class="actions-cell">
                    {#if session.is_current}
                      <button
                        type="button"
                        class="btn-secondary btn-small"
                        on:click={handleLogoutCurrentSession}
                      >Logout</button>
                    {:else}
                      <button
                        type="button"
                        class="btn-danger btn-small"
                        disabled={bulkRevoking}
                        on:click={() => handleDeleteSession(session.token_id)}
                      >Revoke</button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .loading-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 48px 20px;
    color: var(--text-secondary);
  }

  .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-color);
    border-top-color: var(--accent-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 12px;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .alert {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    margin-bottom: 12px;
  }

  .alert-error {
    background: var(--danger-bg);
    color: var(--danger-text);
    border: 1px solid var(--danger-color);
  }

  .alert-success {
    background: var(--success-bg);
    color: var(--success-text);
    border: 1px solid var(--success-color);
  }

  .alert-icon {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
  }

  .profile-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 20px;
    margin-bottom: 14px;
    padding: 10px 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
  }

  .meta-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 120px;
  }

  .meta-item span {
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }

  .meta-item strong {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: 600;
  }

  .badge-success {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .badge-secondary {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .panel {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    margin-bottom: 14px;
  }

  .panel-sessions {
    padding: 0;
    overflow: hidden;
  }

  .panel-sessions .panel-head {
    padding: 12px 14px;
    margin-bottom: 0;
  }

  .panel-sessions .sessions-loading,
  .panel-sessions .sessions-empty {
    padding: 20px 14px;
  }

  .sessions-table-wrap {
    width: 100%;
    border: none;
    border-top: 1px solid var(--border-color);
    border-radius: 0;
  }

  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 10px;
  }

  .panel-head h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--text-primary);
  }

  .panel-head-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .session-count {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-tertiary);
    background: var(--bg-tertiary);
    padding: 2px 8px;
    border-radius: 3px;
  }

  .check-col {
    width: 36px;
    text-align: center;
    vertical-align: middle;
  }

  .check-col input {
    width: 15px;
    height: 15px;
    margin: 0;
    accent-color: var(--accent-color);
    cursor: pointer;
    vertical-align: middle;
  }

  .check-col input:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .sessions-table tr.selected td {
    background: color-mix(in srgb, var(--accent-color) 10%, var(--bg-primary));
  }

  .sessions-table {
    width: 100%;
    table-layout: fixed;
  }

  .sessions-table .col-check {
    width: 36px;
  }

  .sessions-table .col-token {
    width: auto;
  }

  .sessions-table .col-ip {
    width: 140px;
  }

  .sessions-table .col-seen,
  .sessions-table .col-created {
    width: 170px;
  }

  .sessions-table .col-actions {
    width: 96px;
  }

  .sessions-table th,
  .sessions-table td {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .password-form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px 12px;
    align-items: end;
    max-width: 720px;
  }

  .password-form .form-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 0;
  }

  .password-form .form-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .password-form .form-control {
    height: 32px;
    padding: 0 10px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    font-size: 13px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .password-form .form-control:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }

  .password-form .btn-primary {
    align-self: end;
    width: fit-content;
  }

  .sessions-loading,
  .sessions-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 20px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .spinner-small {
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-color);
    border-top-color: var(--accent-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  .sessions-table tr.current td {
    background: color-mix(in srgb, var(--accent-color) 7%, var(--bg-primary));
  }

  .session-token {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-primary);
    background: none;
    padding: 0;
    margin-right: 6px;
  }

  .session-badge {
    display: inline-block;
    padding: 1px 6px;
    background: var(--accent-color);
    color: var(--accent-contrast, #fff);
    border-radius: 3px;
    font-size: 10.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    vertical-align: middle;
  }

  .actions-cell {
    text-align: right;
    white-space: nowrap;
  }
</style>
