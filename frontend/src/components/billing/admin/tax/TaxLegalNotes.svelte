<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { listTaxLegalNotes, saveTaxLegalNote } from '../../../../lib/billingTaxAdminApi.js';

  let notes = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let editor = null;
  let form = { key: '', locale: 'en', text: '' };
  let formError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      notes = await listTaxLegalNotes();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function openNew() {
    editor = { isNew: true };
    form = { key: '', locale: 'en', text: '' };
    formError = '';
  }

  function openEdit(note) {
    editor = { isNew: false };
    form = { key: note.key, locale: note.locale, text: note.text };
    formError = '';
  }

  async function save() {
    if (!form.key.trim() || !form.locale.trim() || !form.text.trim()) {
      formError = 'The key, locale and text are all required.';
      return;
    }
    working = true;
    try {
      await saveTaxLegalNote({ key: form.key.trim(), locale: form.locale.trim(), text: form.text.trim() });
      success = `Legal note ${form.key.trim()} (${form.locale.trim()}) saved.`;
      editor = null;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      working = false;
    }
  }
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Legal notes</h3>
    <Button on:click={openNew}>New note</Button>
  </div>
  <p class="ba-muted">Wording printed on invoices when a rule names the note, for example for a reverse charge. A published invoice keeps the text it was published with. A pack update never overwrites a note you have edited.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if notes.length === 0}
    <p class="ba-empty">No legal notes.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Key</th><th>Locale</th><th>Text</th><th></th><th></th></tr></thead>
        <tbody>
          {#each notes as note (note.id)}
            <tr>
              <td><span class="ba-mono">{note.key}</span></td>
              <td>{note.locale}</td>
              <td style="max-width:520px">{note.text}</td>
              <td>{#if note.edited_by_admin}<span class="ba-pill info">edited</span>{/if}</td>
              <td><button class="ba-link" on:click={() => openEdit(note)}>Edit</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if editor}
  <Modal title={editor.isNew ? 'New legal note' : `Edit ${form.key} (${form.locale})`} onClose={() => (editor = null)}>
    <form class="ba-form" on:submit|preventDefault={save}>
      <div class="ba-two">
        <label>Key<input bind:value={form.key} maxlength="64" disabled={!editor.isNew} /></label>
        <label>Locale<input bind:value={form.locale} maxlength="16" disabled={!editor.isNew} /></label>
      </div>
      <label>Text<textarea bind:value={form.text} rows="5" maxlength="1000"></textarea></label>
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editor = null)}>Cancel</Button>
      <Button on:click={save} disabled={working}>Save</Button>
    </svelte:fragment>
  </Modal>
{/if}
