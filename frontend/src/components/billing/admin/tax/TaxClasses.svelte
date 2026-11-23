<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { createTaxClass, deleteTaxClass, listTaxClasses, updateTaxClass } from '../../../../lib/billingTaxAdminApi.js';

  let classes = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let editing = null;
  let form = { code: '', name: '', description: '' };
  let formError = '';
  let deleting = null;
  let deleteError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      classes = await listTaxClasses();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    editing = { id: null };
    form = { code: '', name: '', description: '' };
    formError = '';
  }

  function openEdit(row) {
    editing = row;
    form = { code: row.code, name: row.name, description: row.description || '' };
    formError = '';
  }

  async function save() {
    if (!form.name.trim()) {
      formError = 'A name is required.';
      return;
    }
    working = true;
    formError = '';
    try {
      if (editing.id === null) {
        if (!/^[a-z][a-z0-9_]*$/.test(form.code)) {
          formError = 'The code starts with a lower-case letter and may hold lower-case letters, digits and underscores.';
          working = false;
          return;
        }
        await createTaxClass({ code: form.code, name: form.name.trim(), description: form.description.trim() || null });
        success = `Tax class ${form.code} created.`;
      } else {
        await updateTaxClass(editing.id, { name: form.name.trim(), description: form.description.trim() || null });
        success = `Tax class ${editing.code} updated.`;
      }
      editing = null;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      working = false;
    }
  }

  async function confirmDelete() {
    working = true;
    deleteError = '';
    try {
      await deleteTaxClass(deleting.id);
      success = `Tax class ${deleting.code} deleted.`;
      deleting = null;
      await load();
    } catch (err) {
      deleteError = err.message;
    } finally {
      working = false;
    }
  }
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Tax classes</h3>
    <Button on:click={openCreate}>New class</Button>
  </div>
  <p class="ba-muted">A class groups products that are taxed alike. A product with no class uses <span class="ba-mono">standard</span>. System classes cannot be deleted; a class used by a rule or a product cannot be deleted either.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if classes.length === 0}
    <p class="ba-empty">No tax classes.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Code</th><th>Name</th><th>Description</th><th>Kind</th><th></th></tr></thead>
        <tbody>
          {#each classes as row (row.id)}
            <tr>
              <td><span class="ba-mono">{row.code}</span></td>
              <td>{row.name}</td>
              <td>{row.description || '-'}</td>
              <td><span class="ba-pill {row.system ? 'info' : ''}">{row.system ? 'system' : 'custom'}</span></td>
              <td class="ba-inline">
                <button class="ba-link" on:click={() => openEdit(row)}>Edit</button>
                <button class="ba-link" on:click={() => { deleting = row; deleteError = ''; }}>Delete</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if editing}
  <Modal title={editing.id === null ? 'New tax class' : `Edit tax class ${editing.code}`} onClose={() => (editing = null)}>
    <form class="ba-form" on:submit|preventDefault={save}>
      <label>Code
        <input bind:value={form.code} disabled={editing.id !== null} maxlength="32" placeholder="e.g. hosting" />
        <span class="ba-hint">Fixed once created.</span>
      </label>
      <label>Name<input bind:value={form.name} maxlength="128" /></label>
      <label>Description<input bind:value={form.description} maxlength="512" /></label>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editing = null)}>Cancel</Button>
      <Button on:click={save} disabled={working}>Save</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if deleting}
  <Modal title="Delete tax class {deleting.code}?" onClose={() => (deleting = null)}>
    <p>Delete the class <strong>{deleting.name}</strong>? This is recorded in the audit log and cannot be undone.</p>
    {#if deleteError}<div class="ba-error">{deleteError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (deleting = null)}>Cancel</Button>
      <Button variant="danger" on:click={confirmDelete} disabled={working}>Delete</Button>
    </svelte:fragment>
  </Modal>
{/if}
