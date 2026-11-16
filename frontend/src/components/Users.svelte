<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import { listClients, createClient, inviteUser, resendInvite, revokeInvite } from '../lib/api.js';

  let clients = [];
  let loading = true;
  let error = null;

  let q = '';
  let passwordFilter = 'all';
  let searchDebounce;

  let showCreateForm = false;
  let createBusy = false;
  let createError = null;
  let createForm = { username: '', email: '', password: '' };

  let showInviteForm = false;
  let inviteBusy = false;
  let inviteError = null;
  let inviteNotice = null;
  let inviteForm = { email: '', username: '' };

  onMount(load);

  function onSearchInput() {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(load, 300);
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const params = {};
      if (q.trim()) params.q = q.trim();
      if (passwordFilter !== 'all') params.has_password = passwordFilter === 'set';
      clients = await listClients(params);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  function openClient(c) {
    navigate(`/admin/users/${c.user_id}`);
  }

  async function submitInvite() {
    inviteError = null;
    inviteNotice = null;
    inviteBusy = true;
    try {
      if (!inviteForm.email.trim()) throw new Error('Email is required');
      const created = await inviteUser({
        email: inviteForm.email.trim(),
        kind: 'client',
        username: inviteForm.username.trim() || undefined,
      });
      inviteNotice = `Invitation sent to ${created.email}.`;
      showInviteForm = false;
      inviteForm = { email: '', username: '' };
      await load();
    } catch (e) {
      inviteError = e.message || String(e);
    } finally {
      inviteBusy = false;
    }
  }

  async function inviteAction(c, action) {
    error = null;
    inviteNotice = null;
    try {
      if (action === 'revoke') {
        if (!confirm(`Revoke the invitation for ${c.email}? The account is kept.`)) return;
        await revokeInvite(c.user_id);
        inviteNotice = `Invitation for ${c.email} revoked.`;
      } else {
        await resendInvite(c.user_id);
        inviteNotice = `Invitation resent to ${c.email}.`;
      }
      await load();
    } catch (e) {
      error = e.message || String(e);
    }
  }

  async function submitCreate() {
    createError = null;
    createBusy = true;
    try {
      if (!createForm.username.trim() || !createForm.email.trim()) {
        throw new Error('Username and email are required');
      }
      const user = await createClient({
        username: createForm.username.trim(),
        email: createForm.email.trim(),
        password: createForm.password || undefined,
      });
      showCreateForm = false;
      createForm = { username: '', email: '', password: '' };
      navigate(`/admin/users/${user.user_id}`);
    } catch (e) {
      createError = e.message || String(e);
    } finally {
      createBusy = false;
    }
  }
</script>

<PageHeader title="Users">
  <svelte:fragment slot="actions">
    <button type="button" class="btn-secondary" on:click={() => (showInviteForm = !showInviteForm)}>
      {showInviteForm ? 'Cancel' : 'Invite client'}
    </button>
    <button type="button" class="btn-primary" on:click={() => (showCreateForm = !showCreateForm)}>
      {showCreateForm ? 'Cancel' : '+ New client'}
    </button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page container">
  <p class="admin-page-lead">Every client always has full client portal access (via admin impersonation and billing SSO). Password login is a separate, optional setting per client.</p>
  <div class="section-header">
    <div class="filters">
      <input
        class="search-input"
        type="search"
        placeholder="Search username, email…"
        bind:value={q}
        on:input={onSearchInput}
      />
      <label for="password-filter">Password login:</label>
      <select id="password-filter" bind:value={passwordFilter} on:change={load}>
        <option value="all">All</option>
        <option value="set">Password set</option>
        <option value="unset">No password (SSO/impersonation only)</option>
      </select>
    </div>
  </div>

  {#if inviteNotice}<div class="notice">{inviteNotice}</div>{/if}
  {#if showInviteForm}
    <div class="panel">
      <h4>Invite client</h4>
      {#if inviteError}<div class="error">{inviteError}</div>{/if}
      <div class="form-row">
        <label>Email <input type="email" bind:value={inviteForm.email} /></label>
        <label>Username (optional) <input bind:value={inviteForm.username} /></label>
        <button type="button" class="btn-primary" disabled={inviteBusy} on:click={submitInvite}>
          {inviteBusy ? 'Sending…' : 'Send invitation'}
        </button>
      </div>
      <p class="hint">The account is created now so you can assign services before they respond. They choose their username and password from the emailed link, which expires in 7 days.</p>
    </div>
  {/if}

  {#if showCreateForm}
    <div class="panel">
      <h4>Create client user</h4>
      {#if createError}<div class="error">{createError}</div>{/if}
      <div class="form-row">
        <label>Username <input bind:value={createForm.username} /></label>
        <label>Email <input type="email" bind:value={createForm.email} /></label>
        <label>Password (optional) <input type="password" bind:value={createForm.password} /></label>
        <button type="button" class="btn-primary" disabled={createBusy} on:click={submitCreate}>
          {createBusy ? 'Creating…' : 'Create'}
        </button>
      </div>
      <p class="hint">Leave password blank to create the client with portal access via impersonation/SSO only; set one now or later to also allow direct password login.</p>
    </div>
  {/if}

  {#if loading}
    <div class="loading">Loading users...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if clients.length === 0}
    <div class="empty-state"><p>No clients match these filters.</p></div>
  {:else}
    <div class="admin-data-table">
      <table class="data-table">
        <thead>
          <tr>
            <th>Username</th>
            <th>Email</th>
            <th>Password login</th>
            <th>Integration</th>
            <th>Services</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each clients as c}
            <tr class="clickable-row" on:click={() => openClient(c)}>
              <td class="name-cell">{c.username}</td>
              <td>{c.email}</td>
              <td>
                {#if c.invited}
                  <span class="status-badge status-invited">Invited</span>
                {:else}
                  <span class="status-badge" class:status-active={c.has_password}>
                    {c.has_password ? 'Password set' : 'Disabled'}
                  </span>
                {/if}
              </td>
              <td>{c.integration_name || '—'}</td>
              <td>{c.service_count}</td>
              <td class="table-actions" on:click|stopPropagation>
                {#if c.invited}
                  <button type="button" class="btn-secondary btn-small" on:click={() => inviteAction(c, 'resend')}>Resend</button>
                  <button type="button" class="btn-danger btn-small" on:click={() => inviteAction(c, 'revoke')}>Revoke</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .section-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .filters { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
  .filters label { font-weight: 600; color: var(--text-primary); }
  .search-input {
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    min-width: 220px;
  }
  .filters select {
    padding: 8px 32px 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  .loading, .error, .empty-state { text-align: center; padding: 48px; color: var(--text-secondary); }
  .error { color: var(--danger-color); }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); margin-bottom: 20px; }
  .panel h4 { margin: 0 0 12px; }
  .panel .hint { margin: 12px 0 0; font-size: 13px; color: var(--text-secondary); }
  .form-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  .form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  .form-row input {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-primary);
    color: var(--text-primary);
  }
  .data-table { min-width: 700px; }
  .clickable-row { cursor: pointer; }
  .name-cell { font-weight: 600; color: var(--text-primary); }
  .status-badge { background: var(--bg-tertiary); color: var(--text-secondary); }
  .status-badge.status-invited { background: var(--info-bg); color: var(--info-text); }
  .notice { margin-bottom: 16px; padding: 10px 14px; border-radius: 8px; background: var(--success-bg); color: var(--success-text); }
  .status-badge.status-active { background: var(--success-bg); color: var(--success-text); }
</style>
