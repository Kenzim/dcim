<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { user as currentUser } from '../stores/auth.js';
  import {
    listAdmins,
    createAdminAccount,
    updateAdminAccount,
    deleteAdminAccount,
  } from '../lib/api.js';

  let admins = [];
  let loading = true;
  let error = null;

  let showCreateForm = false;
  let createBusy = false;
  let createError = null;
  let createForm = { username: '', email: '', password: '' };

  let editingId = null;
  let editForm = { email: '', password: '' };
  let editBusy = false;
  let editError = null;

  onMount(load);

  async function load() {
    loading = true;
    error = null;
    try {
      admins = await listAdmins();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  async function submitCreate() {
    createError = null;
    createBusy = true;
    try {
      if (!createForm.username.trim() || !createForm.email.trim() || !createForm.password) {
        throw new Error('Username, email, and password are required');
      }
      await createAdminAccount({
        username: createForm.username.trim(),
        email: createForm.email.trim(),
        password: createForm.password,
      });
      showCreateForm = false;
      createForm = { username: '', email: '', password: '' };
      await load();
    } catch (e) {
      createError = e.message || String(e);
    } finally {
      createBusy = false;
    }
  }

  function startEdit(admin) {
    editingId = admin.id;
    editForm = { email: admin.email, password: '' };
    editError = null;
  }

  function cancelEdit() {
    editingId = null;
    editError = null;
  }

  async function submitEdit(admin) {
    editError = null;
    editBusy = true;
    try {
      const body = {};
      if (editForm.email && editForm.email !== admin.email) body.email = editForm.email;
      if (editForm.password) body.password = editForm.password;
      await updateAdminAccount(admin.id, body);
      editingId = null;
      await load();
    } catch (e) {
      editError = e.message || String(e);
    } finally {
      editBusy = false;
    }
  }

  async function removeAdmin(admin) {
    if (!confirm(`Delete admin "${admin.username}"? This cannot be undone.`)) return;
    error = null;
    try {
      await deleteAdminAccount(admin.id);
      await load();
    } catch (e) {
      error = e.message || String(e);
    }
  }
</script>

<PageHeader title="Admins">
  <svelte:fragment slot="actions">
    <button type="button" class="btn-primary" on:click={() => (showCreateForm = !showCreateForm)}>
      {showCreateForm ? 'Cancel' : '+ New admin'}
    </button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page container">
  <p class="admin-page-lead">Staff accounts with full admin access. Admins have no client portal.</p>

  {#if showCreateForm}
    <div class="panel">
      <h4>Create admin</h4>
      {#if createError}<div class="error">{createError}</div>{/if}
      <div class="form-row">
        <label>Username <input bind:value={createForm.username} /></label>
        <label>Email <input type="email" bind:value={createForm.email} /></label>
        <label>Password <input type="password" bind:value={createForm.password} /></label>
        <button type="button" class="btn-primary" disabled={createBusy} on:click={submitCreate}>
          {createBusy ? 'Creating…' : 'Create'}
        </button>
      </div>
    </div>
  {/if}

  {#if loading}
    <div class="loading">Loading admins...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else}
    <div class="admin-data-table">
      <table class="data-table">
        <thead>
          <tr>
            <th>Username</th>
            <th>Email</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each admins as admin}
            <tr>
              <td class="name-cell">
                {admin.username}
                {#if $currentUser?.id === admin.id}<span class="you-badge">you</span>{/if}
              </td>
              <td>
                {#if editingId === admin.id}
                  <input class="inline-input" type="email" bind:value={editForm.email} />
                {:else}
                  {admin.email}
                {/if}
              </td>
              <td class="actions-cell table-actions">
                {#if editingId === admin.id}
                  <input class="inline-input" type="password" placeholder="New password (optional)" bind:value={editForm.password} />
                  {#if editError}<div class="error inline-error">{editError}</div>{/if}
                  <button class="btn-secondary btn-small" disabled={editBusy} on:click={() => submitEdit(admin)}>Save</button>
                  <button class="btn-link" disabled={editBusy} on:click={cancelEdit}>Cancel</button>
                {:else}
                  <button class="btn-secondary btn-small" on:click={() => startEdit(admin)}>Edit</button>
                  <button class="btn-danger btn-small" on:click={() => removeAdmin(admin)}>Delete</button>
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
  .loading, .error { text-align: center; padding: 48px; color: var(--text-secondary); }
  .error { color: var(--danger-color); }
  .inline-error { padding: 0; text-align: left; }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); margin-bottom: 20px; }
  .panel h4 { margin: 0 0 12px; }
  .form-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  .form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  .form-row input, .inline-input {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-primary);
    color: var(--text-primary);
  }
  .data-table { min-width: 600px; }
  .name-cell { font-weight: 600; color: var(--text-primary); }
  .you-badge {
    margin-left: 6px;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 10.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    background: var(--info-bg);
    color: var(--info-text);
    vertical-align: middle;
  }
  .actions-cell { justify-content: flex-end; }
  .btn-link {
    background: none;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    font-family: inherit;
    height: 28px;
    padding: 0 8px;
  }
</style>
