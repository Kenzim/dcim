<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Alert, Badge, Button, FormError, Spinner } from './ui/index.js';
  import {
    getAutomationSettings,
    updateAutomationSetting,
    resetAutomationSetting,
  } from '../lib/api.js';

  let groups = [];
  let items = [];
  let drafts = {};
  let errors = {};
  let busy = {};
  let saved = {};
  let loading = true;
  let loadError = null;

  onMount(loadSettings);

  async function loadSettings() {
    loading = true;
    loadError = null;
    try {
      const data = await getAutomationSettings();
      groups = data.groups || [];
      items = data.items || [];
      drafts = Object.fromEntries(items.map((item) => [item.key, item.value]));
    } catch (err) {
      loadError = err.message;
    } finally {
      loading = false;
    }
  }

  function groupItems(allItems, groupKey) {
    return allItems.filter((item) => item.group === groupKey);
  }

  function formatValue(value) {
    if (value === true) return 'On';
    if (value === false) return 'Off';
    return String(value);
  }

  function describeRange(validation) {
    if (!validation) return '';
    if (validation.min != null && validation.max != null) {
      return `Between ${validation.min} and ${validation.max}.`;
    }
    if (validation.min != null) return `At least ${validation.min}.`;
    if (validation.max != null) return `At most ${validation.max}.`;
    return '';
  }

  function isDirty(item, currentDrafts) {
    return currentDrafts[item.key] !== item.value;
  }

  function replaceItem(updated) {
    items = items.map((item) => (item.key === updated.key ? updated : item));
    drafts = { ...drafts, [updated.key]: updated.value };
  }

  function setBusy(key, value) {
    busy = { ...busy, [key]: value };
  }

  function setError(key, message) {
    errors = { ...errors, [key]: message };
  }

  function flashSaved(key, message) {
    saved = { ...saved, [key]: message };
    setTimeout(() => {
      saved = { ...saved, [key]: null };
    }, 3000);
  }

  async function save(item) {
    setBusy(item.key, true);
    setError(item.key, null);
    try {
      const value = drafts[item.key];
      replaceItem(await updateAutomationSetting(item.key, value === '' ? null : value));
      flashSaved(item.key, 'Saved');
    } catch (err) {
      setError(item.key, err.message);
    } finally {
      setBusy(item.key, false);
    }
  }

  async function reset(item) {
    setBusy(item.key, true);
    setError(item.key, null);
    try {
      replaceItem(await resetAutomationSetting(item.key));
      flashSaved(item.key, 'Reset to default');
    } catch (err) {
      setError(item.key, err.message);
    } finally {
      setBusy(item.key, false);
    }
  }
</script>

<PageHeader title="Automation and billing settings" />

<div class="admin-page automation-settings">
  <p class="admin-page-lead">
    These settings are stored in the database and take effect without a deploy.
    Until a value is saved here, the value from the server's environment is used.
    Resetting a setting removes the saved value and returns to that default.
  </p>

  {#if loading}
    <div class="loading"><Spinner size="small" /> Loading settings...</div>
  {:else if loadError}
    <Alert type="error">{loadError}</Alert>
    <Button variant="secondary" on:click={loadSettings}>Try again</Button>
  {:else}
    {#each groups as group (group.key)}
      {@const groupSettings = groupItems(items, group.key)}
      {#if groupSettings.length}
        <section class="settings-group">
          <h2>{group.label}</h2>
          {#if group.description}<p class="group-description">{group.description}</p>{/if}

          {#each groupSettings as item (item.key)}
            {@const inputId = `setting-${item.key}`}
            <div class="setting-row">
              <div class="setting-info">
                <label for={inputId} class="setting-name"><code>{item.key}</code></label>
                <p class="setting-description">{item.description}</p>
                <div class="setting-meta">
                  <Badge variant={item.source === 'stored' ? 'accent' : 'muted'}>
                    {item.source === 'stored' ? 'Saved value' : 'Default'}
                  </Badge>
                  <span class="meta-text">Default: <code>{formatValue(item.default)}</code></span>
                  {#if describeRange(item.validation)}
                    <span class="meta-text">{describeRange(item.validation)}</span>
                  {/if}
                  {#if item.requires_restart}
                    <Badge variant="warning" caps={false}>Takes effect after restart</Badge>
                  {/if}
                </div>
              </div>

              <div class="setting-control">
                {#if item.value_type === 'bool'}
                  <label class="toggle">
                    <input id={inputId} type="checkbox" bind:checked={drafts[item.key]} disabled={busy[item.key]} />
                    <span>{drafts[item.key] ? 'On' : 'Off'}</span>
                  </label>
                {:else if item.validation.choices}
                  <select id={inputId} bind:value={drafts[item.key]} disabled={busy[item.key]}>
                    {#each item.validation.choices as choice (choice)}
                      <option value={choice}>{choice}</option>
                    {/each}
                  </select>
                {:else if item.value_type === 'int'}
                  <input
                    id={inputId}
                    type="number"
                    step="1"
                    min={item.validation.min ?? undefined}
                    max={item.validation.max ?? undefined}
                    bind:value={drafts[item.key]}
                    disabled={busy[item.key]}
                  />
                {:else}
                  <input
                    id={inputId}
                    type="text"
                    maxlength={item.validation.max_length ?? undefined}
                    bind:value={drafts[item.key]}
                    disabled={busy[item.key]}
                  />
                {/if}

                <div class="setting-actions">
                  <Button
                    size="small"
                    disabled={busy[item.key] || !isDirty(item, drafts)}
                    on:click={() => save(item)}
                  >Save</Button>
                  <Button
                    variant="secondary"
                    size="small"
                    disabled={busy[item.key] || item.source !== 'stored'}
                    on:click={() => reset(item)}
                  >Reset to default</Button>
                  {#if saved[item.key]}<span class="saved-note" role="status">{saved[item.key]}</span>{/if}
                </div>
                {#if errors[item.key]}
                  <FormError>{errors[item.key]}</FormError>
                {/if}
              </div>
            </div>
          {/each}
        </section>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .automation-settings {
    max-width: 960px;
  }

  .loading {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-secondary);
  }

  .settings-group {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: 16px 20px;
    margin-bottom: 20px;
  }

  h2 {
    margin: 0 0 4px;
    font-size: 16px;
    color: var(--text-primary);
  }

  .group-description {
    margin: 0 0 8px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .setting-row {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
    gap: 20px;
    padding: 14px 0;
    border-top: 1px solid var(--border-color);
  }

  .setting-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  .setting-description {
    margin: 4px 0 8px;
    font-size: 13px;
    line-height: 1.45;
    color: var(--text-secondary);
  }

  .setting-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
  }

  .meta-text {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .setting-control input[type='number'],
  .setting-control input[type='text'],
  .setting-control select {
    width: 100%;
    padding: 8px 10px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background-color: var(--bg-primary);
    color: var(--text-primary);
  }

  .setting-control input:focus,
  .setting-control select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }

  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-primary);
  }

  .setting-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 10px 0 0;
  }

  .saved-note {
    font-size: 12px;
    color: var(--success-text);
  }

  .setting-control :global(.form-error) {
    margin: 10px 0 0;
    font-size: 13px;
  }

  @media (max-width: 768px) {
    .setting-row {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
