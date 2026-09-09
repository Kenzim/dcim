<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import {
    adminStoreCreateCategory,
    adminStoreDeleteCategory,
    adminStoreListCategories,
    adminStoreUpdateCategory,
  } from '../../lib/api.js';

  let categories = [];
  let loading = true;
  let saving = false;
  let error = '';
  let success = '';
  let editing = null;
  let confirmDelete = null;
  let form = blankForm();

  function blankForm() {
    return { name: '', slug: '', description: '', sort_order: 0, enabled: true };
  }

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      categories = await adminStoreListCategories();
    } catch (err) {
      error = err.message || 'Failed to load categories';
      categories = [];
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    editing = 'new';
    form = blankForm();
  }

  function openEdit(row) {
    editing = row.id;
    form = {
      name: row.name,
      slug: row.slug,
      description: row.description || '',
      sort_order: row.sort_order ?? 0,
      enabled: row.enabled,
    };
  }

  async function save() {
    saving = true;
    error = '';
    success = '';
    const payload = {
      ...form,
      name: form.name.trim(),
      slug: form.slug.trim(),
      description: form.description.trim() || null,
      sort_order: Number(form.sort_order) || 0,
    };
    try {
      if (editing === 'new') {
        await adminStoreCreateCategory(payload);
        success = 'Category created.';
      } else {
        await adminStoreUpdateCategory(editing, payload);
        success = 'Category updated.';
      }
      editing = null;
      await load();
    } catch (err) {
      error = err.message || 'Save failed';
    } finally {
      saving = false;
    }
  }

  async function doDelete() {
    if (!confirmDelete) return;
    saving = true;
    error = '';
    try {
      await adminStoreDeleteCategory(confirmDelete.id);
      success = 'Category deleted.';
      confirmDelete = null;
      await load();
    } catch (err) {
      error = err.message || 'Delete failed';
    } finally {
      saving = false;
    }
  }
</script>

<PageHeader title="Store Categories">
  <svelte:fragment slot="actions">
    <Button on:click={openCreate}>Add category</Button>
  </svelte:fragment>
</PageHeader>

<div class="page">
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if categories.length === 0}
    <div class="empty">
      <p>No categories yet.</p>
      <Button on:click={openCreate}>Create your first category</Button>
    </div>
  {:else}
    <div class="panel">
      <table>
        <thead>
          <tr><th>Name</th><th>Slug</th><th>Order</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {#each categories as row (row.id)}
            <tr>
              <td><strong>{row.name}</strong></td>
              <td><code>{row.slug}</code></td>
              <td>{row.sort_order}</td>
              <td>{row.enabled ? 'Enabled' : 'Disabled'}</td>
              <td class="actions">
                <Button variant="secondary" on:click={() => openEdit(row)}>Edit</Button>
                <Button variant="danger" on:click={() => (confirmDelete = row)}>Delete</Button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if editing}
  <Modal title={editing === 'new' ? 'New category' : 'Edit category'} onClose={() => (editing = null)}>
    <label>Name <input bind:value={form.name} required /></label>
    <label>Slug <input bind:value={form.slug} required /></label>
    <label>Description <textarea bind:value={form.description} rows="3"></textarea></label>
    <label>Sort order <input type="number" bind:value={form.sort_order} /></label>
    <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editing = null)}>Cancel</Button>
      <Button on:click={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if confirmDelete}
  <Modal title="Delete category?" onClose={() => (confirmDelete = null)}>
    <p>Delete <strong>{confirmDelete.name}</strong>? This cannot be undone.</p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirmDelete = null)}>Cancel</Button>
      <Button variant="danger" on:click={doDelete} disabled={saving}>Delete</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 28px 32px 36px; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 12px; text-transform: uppercase; color: var(--text-tertiary); }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .empty { text-align: center; padding: 48px 24px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); }
  .empty p { color: var(--text-secondary); margin-bottom: 16px; }
  label { display: grid; gap: 6px; margin-bottom: 14px; font-size: 13px; font-weight: 600; }
  input, textarea { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
</style>
