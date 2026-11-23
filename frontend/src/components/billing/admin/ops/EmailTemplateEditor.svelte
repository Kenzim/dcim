<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner, Tabs } from '../../../ui/index.js';
  import CodeEditor from '../../../CodeEditor.svelte';
  import { copyText } from '../../../../lib/copyText.js';
  import {
    getEmailTemplate,
    listEmailTemplateVersions,
    previewEmailTemplate,
    resetEmailTemplate,
    restoreEmailTemplateVersion,
    saveEmailTemplate,
    testSendEmailTemplate,
  } from '../../../../lib/billingOpsAdminApi.js';
  import { errorLines, label, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  export let templateId;

  const dispatch = createEventDispatcher();

  let template = null;
  let event = null;
  let loading = true;
  let loadError = '';

  let subject = '';
  let bodyText = '';
  let bodyHtml = '';
  let savedSubject = '';
  let savedText = '';
  let savedHtml = '';
  let editorKey = 0;
  let bodyTab = 'text';

  let message = null;
  let problems = [];
  let working = false;

  let preview = null;
  let previewing = false;
  let overridesText = '';
  let previewTab = 'text';

  let versions = null;
  let versionsLoading = false;
  let showHistory = false;

  let confirming = null;
  let testTo = '';
  let testing = false;
  let copied = '';

  onMount(() => load());

  function adopt(row) {
    template = row;
    subject = savedSubject = row.subject || '';
    bodyText = savedText = row.body_text || '';
    bodyHtml = savedHtml = row.body_html || '';
    editorKey += 1;
  }

  async function load() {
    loading = true;
    loadError = '';
    try {
      const row = await getEmailTemplate(templateId);
      event = row.event;
      adopt(row);
    } catch (err) {
      loadError = err.message;
    } finally {
      loading = false;
    }
  }

  $: dirty = template !== null && (subject !== savedSubject || bodyText !== savedText || bodyHtml !== savedHtml);
  $: variantName = template ? `${template.locale}${template.product_scope ? `, product ${template.product_scope}` : ''}` : '';

  function fail(err) {
    problems = errorLines(err);
    message = null;
  }

  function draftBody() {
    return { subject, body_text: bodyText, body_html: bodyHtml.trim() ? bodyHtml : null };
  }

  async function save() {
    working = true;
    problems = [];
    message = null;
    try {
      const row = await saveEmailTemplate(templateId, draftBody());
      adopt(row);
      message = `Saved as version ${row.version}.`;
      versions = null;
      if (showHistory) loadVersions();
    } catch (err) {
      fail(err);
    } finally {
      working = false;
    }
  }

  async function runPreview() {
    previewing = true;
    problems = [];
    message = null;
    let variables;
    if (overridesText.trim()) {
      try {
        variables = JSON.parse(overridesText);
        if (variables === null || typeof variables !== 'object' || Array.isArray(variables)) throw new Error('not an object');
      } catch {
        problems = ['The variable overrides must be a JSON object, for example {"invoice_number": "INV-1"}.'];
        previewing = false;
        return;
      }
    }
    try {
      preview = await previewEmailTemplate(templateId, {
        ...draftBody(),
        ...(variables ? { variables } : {}),
      });
    } catch (err) {
      preview = null;
      fail(err);
    } finally {
      previewing = false;
    }
  }

  async function toggleEnabled() {
    working = true;
    problems = [];
    message = null;
    try {
      const row = await saveEmailTemplate(templateId, { enabled: !template.enabled });
      template = { ...template, enabled: row.enabled, version: row.version, updated_at: row.updated_at };
      message = row.enabled ? 'The template is enabled; this email will be sent.' : 'The template is disabled; this email will not be sent.';
      confirming = null;
      versions = null;
    } catch (err) {
      fail(err);
      confirming = null;
    } finally {
      working = false;
    }
  }

  async function reset() {
    working = true;
    problems = [];
    message = null;
    try {
      const row = await resetEmailTemplate(templateId);
      adopt(row);
      preview = null;
      message = `The built-in text was saved as version ${row.version}.`;
      confirming = null;
      versions = null;
    } catch (err) {
      fail(err);
      confirming = null;
    } finally {
      working = false;
    }
  }

  async function loadVersions() {
    versionsLoading = true;
    try {
      versions = (await listEmailTemplateVersions(templateId)).versions;
    } catch (err) {
      fail(err);
    } finally {
      versionsLoading = false;
    }
  }

  function toggleHistory() {
    showHistory = !showHistory;
    if (showHistory && !versions) loadVersions();
  }

  async function restore() {
    working = true;
    problems = [];
    message = null;
    try {
      const row = await restoreEmailTemplateVersion(templateId, confirming.version);
      adopt(row);
      preview = null;
      message = `Version ${confirming.version} was restored as version ${row.version}.`;
      confirming = null;
      await loadVersions();
    } catch (err) {
      fail(err);
      confirming = null;
    } finally {
      working = false;
    }
  }

  async function testSend() {
    testing = true;
    problems = [];
    message = null;
    try {
      const body = testTo.trim() ? { to_address: testTo.trim() } : {};
      const result = await testSendEmailTemplate(templateId, body);
      message = `A test email was queued to ${result.to_address}. It uses the saved template with sample values.`;
    } catch (err) {
      fail(err);
    } finally {
      testing = false;
    }
  }

  async function copyVariable(name) {
    try {
      await copyText(`{{ ${name} }}`);
      copied = name;
      setTimeout(() => { if (copied === name) copied = ''; }, 1500);
    } catch {
      copied = '';
    }
  }

  function show(value) {
    return typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '');
  }
</script>

{#if loading}
  <div class="ba-state"><Spinner /></div>
{:else if loadError}
  <Alert type="danger">{loadError}</Alert>
  <Button variant="secondary" on:click={() => dispatch('close')}>Back to the list</Button>
{:else if template}
  <div class="ba-inline" style="justify-content: space-between; margin-bottom: 14px">
    <div class="ba-inline">
      <Button variant="secondary" size="small" on:click={() => dispatch('close')}>Back to the list</Button>
      <h3 style="margin:0">{template.event_title}</h3>
      <span class="ba-pill">{variantName}</span>
      <span class="ba-pill {template.enabled ? 'ok' : ''}">{template.enabled ? 'Enabled' : 'Disabled'}</span>
      <span class="ba-muted">Version {template.version}, saved {when(template.updated_at)}</span>
      {#if dirty}<span class="ba-pill warn">Unsaved changes</span>{/if}
    </div>
    <div class="ba-inline">
      <Button variant="secondary" size="small" disabled={working || dirty} on:click={() => (confirming = { kind: 'toggle' })}>
        {template.enabled ? 'Disable' : 'Enable'}
      </Button>
      <Button variant="secondary" size="small" disabled={working} on:click={() => (confirming = { kind: 'reset' })}>Reset to built-in text</Button>
      <Button variant="secondary" size="small" on:click={toggleHistory}>{showHistory ? 'Hide history' : 'Version history'}</Button>
    </div>
  </div>
  {#if event?.description}<p class="ba-muted ba-wide-note">{event.description}</p>{/if}
  {#if dirty}<p class="ba-hint">Enable and disable use the saved text. Save your changes first.</p>{/if}

  {#if message}<Alert type="success">{message}</Alert>{/if}
  {#if problems.length}
    <div class="ba-error" role="alert">
      {#if problems.length === 1}{problems[0]}{:else}
        The template has {problems.length} problems:
        <ul>{#each problems as line}<li>{line}</li>{/each}</ul>
      {/if}
    </div>
  {/if}

  <div class="editor-grid">
    <div class="ba-panel">
      <h3>Template</h3>
      <div class="ba-form">
        <label>Subject
          <input bind:value={subject} maxlength="500" />
        </label>
        <Tabs tabs={[{ id: 'text', label: 'Plain text' }, { id: 'html', label: 'HTML (optional)' }]} bind:active={bodyTab} />
        {#key editorKey}
          {#if bodyTab === 'text'}
            <CodeEditor bind:value={bodyText} />
          {:else}
            <CodeEditor bind:value={bodyHtml} />
            <p class="ba-hint">Leave the HTML empty to send plain text only.</p>
          {/if}
        {/key}
        <div class="ba-inline">
          <Button on:click={save} disabled={working || !dirty}>Save as a new version</Button>
          <Button variant="secondary" on:click={runPreview} disabled={previewing}>Preview this draft</Button>
        </div>
        <p class="ba-hint">Saving checks the template by rendering it with the sample values. Mail already queued is not changed.</p>
      </div>
    </div>

    <div class="ba-panel">
      <h3>Variables</h3>
      {#if event?.variables?.length}
        <p class="ba-hint">Use them as <span class="ba-mono">{'{{ name }}'}</span>. Select a name to copy it.</p>
        <ul class="vars">
          {#each event.variables as variable (variable.name)}
            <li>
              <button type="button" class="ba-link ba-mono" on:click={() => copyVariable(variable.name)}>
                {copied === variable.name ? 'copied' : variable.name}
              </button>
              <span class="ba-hint">{variable.type}{variable.required ? '' : ', optional'}: {variable.description}</span>
              <span class="ba-hint">Sample: <span class="ba-mono">{show(variable.sample)}</span></span>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="ba-empty">This event has no variables.</p>
      {/if}
    </div>
  </div>

  <div class="ba-panel">
    <h3>Preview</h3>
    <label class="ba-form">Variable overrides (optional JSON object)
      <textarea bind:value={overridesText} rows="2" placeholder={'{"invoice_number": "INV-1"}'}></textarea>
    </label>
    <div class="ba-actions">
      <Button variant="secondary" on:click={runPreview} disabled={previewing}>{previewing ? 'Rendering...' : 'Render preview'}</Button>
    </div>
    {#if preview}
      <p><strong>Subject:</strong> {preview.subject}</p>
      <div class="preview-pair">
        <div>
          <h4>Plain text</h4>
          <pre class="preview-text">{preview.text}</pre>
        </div>
        <div>
          <h4>HTML</h4>
          {#if preview.html}
            <iframe class="preview-html" title="HTML preview" sandbox="" srcdoc={preview.html}></iframe>
          {:else}
            <p class="ba-empty">No HTML body. Only the plain text is sent.</p>
          {/if}
        </div>
      </div>
    {:else}
      <p class="ba-hint">The preview renders the draft above with the sample values. Nothing is sent.</p>
    {/if}
  </div>

  <div class="ba-panel">
    <h3>Send a test</h3>
    <div class="ba-inline">
      <input bind:value={testTo} type="email" placeholder="Your own address if left empty" style="min-width:260px" />
      <Button variant="secondary" on:click={testSend} disabled={testing || dirty}>Send test email</Button>
    </div>
    <p class="ba-hint">Sends the saved template with sample values. At most 10 test emails per hour.{dirty ? ' Save your changes first.' : ''}</p>
  </div>

  {#if showHistory}
    <div class="ba-panel">
      <h3>Version history</h3>
      {#if versionsLoading}
        <div class="ba-state"><Spinner /></div>
      {:else if versions}
        <div class="ba-scroll">
          <table class="ba-table">
            <thead><tr><th>Version</th><th>Saved</th><th>By</th><th>Subject</th><th></th></tr></thead>
            <tbody>
              {#each versions as item (item.version)}
                <tr>
                  <td>{item.version}{item.version === template.version ? ' (current)' : ''}</td>
                  <td>{when(item.saved_at)}</td>
                  <td>{item.saved_by_user_id ? `User ${item.saved_by_user_id}` : 'System'}</td>
                  <td>{item.subject}</td>
                  <td>
                    {#if item.version !== template.version}
                      <button class="ba-link" disabled={working} on:click={() => (confirming = { kind: 'restore', version: item.version })}>Restore</button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}
{/if}

{#if confirming && template}
  <Modal
    title={confirming.kind === 'toggle' ? (template.enabled ? 'Disable this template' : 'Enable this template') : confirming.kind === 'reset' ? 'Reset to the built-in text' : `Restore version ${confirming.version}`}
    onClose={() => (confirming = null)}
  >
    {#if confirming.kind === 'toggle'}
      {#if template.enabled}
        <p>
          While <strong>{template.event_title}</strong> ({variantName}) is disabled, this email is not sent: the message is recorded as skipped.
        </p>
      {:else}
        <p>Once enabled, <strong>{template.event_title}</strong> ({variantName}) is sent to clients whenever the event happens.</p>
      {/if}
      <p class="ba-hint">This changes only whether the email is sent. The text and its version stay as they are.</p>
    {:else if confirming.kind === 'reset'}
      <p>
        The subject and bodies are replaced with the built-in text of this event, and the HTML body is cleared. It is saved as a new version, so the current text stays in the history.
        {#if dirty}Your unsaved changes will be lost.{/if}
      </p>
    {:else}
      <p>
        Version {confirming.version} is saved as a new version and becomes the current text.
        {#if dirty}Your unsaved changes will be lost.{/if}
      </p>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirming = null)}>Cancel</Button>
      {#if confirming.kind === 'toggle'}
        <Button variant={template.enabled ? 'danger' : 'primary'} disabled={working} on:click={toggleEnabled}>{template.enabled ? 'Disable template' : 'Enable template'}</Button>
      {:else if confirming.kind === 'reset'}
        <Button variant="danger" disabled={working} on:click={reset}>Reset to built-in text</Button>
      {:else}
        <Button disabled={working} on:click={restore}>Restore version</Button>
      {/if}
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .editor-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(240px, 1fr); gap: 18px; align-items: start; }
  .vars { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 10px; max-height: 520px; overflow: auto; }
  .vars li { display: grid; gap: 2px; }
  .preview-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .preview-pair h4 { margin: 0 0 6px; font-size: 13px; color: var(--text-secondary); }
  .preview-text { margin: 0; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-secondary); white-space: pre-wrap; word-break: break-word; font-size: 13px; min-height: 120px; }
  .preview-html { width: 100%; min-height: 260px; border: 1px solid var(--border-color); border-radius: 8px; background: #fff; }
  @media (max-width: 900px) { .editor-grid, .preview-pair { grid-template-columns: 1fr; } }
</style>
