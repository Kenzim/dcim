<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import {
    createEmailTemplate,
    listEmailEvents,
    listEmailTemplates,
    saveEmailTemplate,
  } from '../../../../lib/billingOpsAdminApi.js';
  import EmailTemplateEditor from './EmailTemplateEditor.svelte';
  import { errorLines, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  let events = [];
  let templates = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let editingId = null;
  let eventFilter = '';

  let toggling = null;
  let toggleError = [];

  let creating = null;
  let createError = [];

  onMount(load);

  async function load() {
    loading = true;
    try {
      const [eventData, templateData] = await Promise.all([listEmailEvents(), listEmailTemplates()]);
      events = eventData.events || [];
      templates = templateData.templates || [];
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function closeEditor() {
    editingId = null;
    load();
  }

  $: visibleEvents = events.filter((event) => !eventFilter || event.key === eventFilter);
  const rowsFor = (key, all) => all.filter((row) => row.event_key === key);
  const scope = (row) => (row.product_scope ? `Product ${row.product_scope}` : 'All products');

  function askToggle(row) {
    toggling = row;
    toggleError = [];
  }

  async function confirmToggle() {
    working = true;
    toggleError = [];
    try {
      await saveEmailTemplate(toggling.id, { enabled: !toggling.enabled });
      success = `${toggling.event_title} (${toggling.locale}) is now ${toggling.enabled ? 'disabled' : 'enabled'}.`;
      toggling = null;
      await load();
    } catch (err) {
      toggleError = errorLines(err);
    } finally {
      working = false;
    }
  }

  function openCreate(event) {
    creating = {
      event_key: event?.key || '',
      locale: 'en',
      product_scope: '',
    };
    createError = [];
  }

  $: creatingEvent = creating ? events.find((event) => event.key === creating.event_key) : null;

  async function submitCreate() {
    if (!creatingEvent) {
      createError = ['Choose an event.'];
      return;
    }
    working = true;
    createError = [];
    try {
      const body = {
        event_key: creatingEvent.key,
        locale: creating.locale.trim() || 'en',
        subject: creatingEvent.default_subject,
        body_text: creatingEvent.default_body,
        enabled: false,
      };
      if (creatingEvent.accepts_product && String(creating.product_scope).trim()) {
        body.product_scope = Number(creating.product_scope);
      }
      const row = await createEmailTemplate(body);
      creating = null;
      success = 'The template was created from the built-in text and is disabled until you enable it.';
      await load();
      editingId = row.id;
    } catch (err) {
      createError = errorLines(err);
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Email Templates">
  <svelte:fragment slot="actions">
    {#if editingId === null}
      <Button on:click={() => openCreate(null)}>Add a variant</Button>
    {/if}
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  {#if editingId !== null}
    <EmailTemplateEditor templateId={editingId} on:close={closeEditor} />
  {:else}
    <p class="ba-muted ba-wide-note">
      The text of each email the system sends. A disabled template means that email is not sent. A template can use the variables of its event; saving checks it by rendering with sample values.
      When a template fails to render while sending, the built-in text is used instead so money emails are never dropped.
    </p>
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    {#if success}<Alert type="success">{success}</Alert>{/if}

    {#if loading && events.length === 0}
      <div class="ba-state"><Spinner /></div>
    {:else}
      <div class="ba-toolbar" style="margin-top:18px">
        <label>Event
          <select bind:value={eventFilter}>
            <option value="">All events</option>
            {#each events as event (event.key)}<option value={event.key}>{event.title}</option>{/each}
          </select>
        </label>
      </div>
      {#each visibleEvents as event (event.key)}
        {@const rows = rowsFor(event.key, templates)}
        <div class="ba-panel">
          <div class="ba-inline" style="justify-content: space-between">
            <h3 style="margin:0">{event.title} <small class="ba-mono">{event.key}</small></h3>
            <Button variant="secondary" size="small" on:click={() => openCreate(event)}>Add a variant</Button>
          </div>
          <p class="ba-muted" style="margin:6px 0 10px">{event.description}</p>
          {#if rows.length === 0}
            <p class="ba-hint">No template yet. The built-in text is used.</p>
          {:else}
            <div class="ba-scroll">
              <table class="ba-table">
                <thead><tr><th>Locale</th><th>Applies to</th><th>Subject</th><th>State</th><th>Version</th><th>Saved</th><th></th></tr></thead>
                <tbody>
                  {#each rows as row (row.id)}
                    <tr>
                      <td>{row.locale}</td>
                      <td>{scope(row)}</td>
                      <td>{row.subject}</td>
                      <td><span class="ba-pill {row.enabled ? 'ok' : ''}">{row.enabled ? 'Enabled' : 'Disabled'}</span></td>
                      <td>{row.version}</td>
                      <td>{when(row.updated_at)}</td>
                      <td>
                        <div class="ba-inline">
                          <button class="ba-link" on:click={() => (editingId = row.id)}>Edit</button>
                          <button class="ba-link" on:click={() => askToggle(row)}>{row.enabled ? 'Disable' : 'Enable'}</button>
                        </div>
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </div>
      {/each}
    {/if}
  {/if}
</div>

{#if toggling}
  <Modal title={toggling.enabled ? 'Disable this template' : 'Enable this template'} onClose={() => (toggling = null)}>
    {#if toggling.enabled}
      <p>While <strong>{toggling.event_title}</strong> ({toggling.locale}, {scope(toggling).toLowerCase()}) is disabled, this email is not sent: the message is recorded as skipped.</p>
    {:else}
      <p>Once enabled, <strong>{toggling.event_title}</strong> ({toggling.locale}, {scope(toggling).toLowerCase()}) is sent whenever the event happens.</p>
    {/if}
    <p class="ba-hint">This changes only whether the email is sent. The text and its version stay as they are.</p>
    {#if toggleError.length}
      <div class="ba-error"><ul>{#each toggleError as line}<li>{line}</li>{/each}</ul></div>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (toggling = null)}>Cancel</Button>
      <Button variant={toggling.enabled ? 'danger' : 'primary'} disabled={working} on:click={confirmToggle}>
        {toggling.enabled ? 'Disable template' : 'Enable template'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if creating}
  <Modal title="Add a template variant" onClose={() => (creating = null)}>
    <form class="ba-form" on:submit|preventDefault={submitCreate}>
      <label>Event
        <select bind:value={creating.event_key}>
          <option value="">Choose an event</option>
          {#each events as event (event.key)}<option value={event.key}>{event.title}</option>{/each}
        </select>
      </label>
      <label>Locale
        <input bind:value={creating.locale} placeholder="en" maxlength="16" />
      </label>
      {#if creatingEvent?.accepts_product}
        <label>Product ID (empty for all products)
          <input type="number" min="1" bind:value={creating.product_scope} />
        </label>
      {/if}
      <p class="ba-hint">The variant starts from the built-in text and is disabled until you enable it.</p>
      {#if createError.length}
        <div class="ba-error"><ul>{#each createError as line}<li>{line}</li>{/each}</ul></div>
      {/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (creating = null)}>Cancel</Button>
      <Button disabled={working} on:click={submitCreate}>Create variant</Button>
    </svelte:fragment>
  </Modal>
{/if}
