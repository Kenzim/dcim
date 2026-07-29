<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Button, Modal, FormGroup, FormError } from './ui/index.js';
  import { createReseller, listResellerGroups, listResellers } from '../lib/api.js';

  let resellers = [];
  let groups = [];
  let loading = true;
  let error = '';
  let filters = { q: '', status: '', group_id: '' };
  let showCreate = false;
  let saving = false;
  let formError = '';
  let form = {
    username: '',
    email: '',
    password: '',
    group_id: '',
    status: 'active',
    charge_preference: 'credit_first',
  };
  let keyReveal = { open: false, username: '', apiKey: '', copied: false };

  onMount(async () => {
    await Promise.all([loadGroups(), loadResellers()]);
  });

  async function loadGroups() {
    try {
      groups = await listResellerGroups();
    } catch (err) {
      error = err.message || String(err);
    }
  }

  async function loadResellers() {
    loading = true;
    error = '';
    try {
      resellers = await listResellers(filters);
    } catch (err) {
      error = err.message || String(err);
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    form = {
      username: '',
      email: '',
      password: '',
      group_id: '',
      status: 'active',
      charge_preference: 'credit_first',
    };
    formError = '';
    showCreate = true;
  }

  async function submitCreate() {
    saving = true;
    formError = '';
    try {
      const created = await createReseller({
        ...form,
        username: form.username.trim(),
        email: form.email.trim(),
        group_id: form.group_id ? Number(form.group_id) : null,
      });
      showCreate = false;
      keyReveal = {
        open: true,
        username: created.user.username,
        apiKey: created.api_key,
        copied: false,
      };
      await loadResellers();
    } catch (err) {
      formError = err.message || String(err);
    } finally {
      saving = false;
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(keyReveal.apiKey);
      keyReveal = { ...keyReveal, copied: true };
    } catch (_) {
      keyReveal = { ...keyReveal, copied: false };
    }
  }

  function money(cents) {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
    }).format((cents || 0) / 100);
  }
</script>

<PageHeader title="Resellers">
  <svelte:fragment slot="actions">
    <Button on:click={openCreate}>New reseller</Button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page page">
  <div class="toolbar">
    <form class="filters" on:submit|preventDefault={loadResellers}>
      <input aria-label="Search resellers" placeholder="Search username, email, or key prefix" bind:value={filters.q} />
      <select aria-label="Filter by status" bind:value={filters.status}>
        <option value="">All statuses</option>
        <option value="active">Active</option>
        <option value="suspended">Suspended</option>
        <option value="disabled">Disabled</option>
      </select>
      <select aria-label="Filter by reseller group" bind:value={filters.group_id}>
        <option value="">All groups</option>
        {#each groups as group}
          <option value={group.id}>{group.name}</option>
        {/each}
      </select>
      <Button type="submit" variant="secondary">Apply</Button>
    </form>
  </div>

  {#if error}
    <div class="alert alert-danger">{error}</div>
  {/if}

  {#if loading}
    <div class="state">Loading resellers…</div>
  {:else if resellers.length === 0}
    <div class="state">No resellers match these filters.</div>
  {:else}
    <div class="admin-data-table">
      <table class="resellers-table">
        <thead>
          <tr>
            <th>Reseller</th>
            <th>Status</th>
            <th>Group</th>
            <th>Balance</th>
            <th>Services</th>
            <th>Invoices</th>
            <th>API key</th>
          </tr>
        </thead>
        <tbody>
          {#each resellers as reseller}
            <tr>
              <td>
                <a class="name" href={`/admin/resellers/${reseller.id}`}>{reseller.user.username}</a>
                <div class="muted">{reseller.user.email}</div>
              </td>
              <td><span class={`status-badge status status-${reseller.status}`}>{reseller.status}</span></td>
              <td>{reseller.group?.name || 'Ungrouped'}</td>
              <td class:negative={reseller.balance_cents < 0}>{money(reseller.balance_cents)}</td>
              <td>{reseller.service_summary?.total || 0}</td>
              <td>{reseller.invoice_summary?.total || 0}</td>
              <td><code>{reseller.api_key_prefix || 'Not issued'}</code></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if showCreate}
  <Modal title="Create reseller" onClose={() => (showCreate = false)}>
    {#if formError}<FormError>{formError}</FormError>{/if}
    <form id="create-reseller-form" on:submit|preventDefault={submitCreate}>
      <FormGroup label="Username" forId="reseller-username" required>
        <input id="reseller-username" bind:value={form.username} required autocomplete="off" />
      </FormGroup>
      <FormGroup label="Email" forId="reseller-email" required>
        <input id="reseller-email" type="email" bind:value={form.email} required />
      </FormGroup>
      <FormGroup label="Initial password" forId="reseller-password" required>
        <input id="reseller-password" type="password" minlength="8" maxlength="72" bind:value={form.password} required autocomplete="new-password" />
      </FormGroup>
      <FormGroup label="Group" forId="reseller-group">
        <select id="reseller-group" bind:value={form.group_id}>
          <option value="">Ungrouped</option>
          {#each groups.filter((group) => group.enabled) as group}
            <option value={group.id}>{group.name}</option>
          {/each}
        </select>
      </FormGroup>
      <div class="form-grid">
        <FormGroup label="Status" forId="reseller-status">
          <select id="reseller-status" bind:value={form.status}>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="disabled">Disabled</option>
          </select>
        </FormGroup>
        <FormGroup label="Charge preference" forId="reseller-charge">
          <select id="reseller-charge" bind:value={form.charge_preference}>
            <option value="credit_first">Credit first</option>
            <option value="payment_first">Payment first</option>
          </select>
        </FormGroup>
      </div>
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
      <Button type="submit" form="create-reseller-form" disabled={saving}>
        {saving ? 'Creating…' : 'Create reseller'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if keyReveal.open}
  <Modal title="Copy the reseller API key now" onClose={() => (keyReveal.open = false)}>
    <div class="key-warning">
      This is the only time the full API key for <strong>{keyReveal.username}</strong> will be shown.
      Copy it into a secure secret store before closing this panel. It cannot be retrieved later.
    </div>
    <code class="key-value">{keyReveal.apiKey}</code>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={copyKey}>{keyReveal.copied ? 'Copied' : 'Copy key'}</Button>
      <Button on:click={() => (keyReveal.open = false)}>I stored it</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { color: var(--text-primary); }
  .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .filters { display: flex; gap: 10px; flex: 1; flex-wrap: wrap; }
  .filters input { min-width: min(320px, 100%); flex: 1; }
  input, select {
    padding: 9px 11px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-primary);
    background-color: var(--bg-primary);
  }
  .resellers-table { min-width: 850px; }
  .name { color: var(--accent-color); font-weight: 600; text-decoration: none; }
  .muted { color: var(--text-secondary); font-size: 12px; margin-top: 2px; }
  .status { text-transform: capitalize; }
  .status-active { background: var(--success-bg); color: var(--success-text); }
  .status-suspended { background: var(--warning-bg); color: var(--warning-text); }
  .status-disabled { background: var(--danger-bg); color: var(--danger-text); }
  .negative { color: var(--danger-color); font-weight: 700; }
  .state { padding: 48px; text-align: center; color: var(--text-secondary); }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .key-warning { padding: 12px; margin-bottom: 16px; border-radius: 8px; color: var(--warning-text); background: var(--warning-bg); }
  .key-value { display: block; padding: 14px; border-radius: 8px; background: var(--bg-tertiary); color: var(--text-primary); overflow-wrap: anywhere; user-select: all; }
  @media (max-width: 768px) {
    .filters { width: 100%; }
    .filters input, .filters select { width: 100%; }
    .form-grid { grid-template-columns: 1fr; }
  }
</style>
