<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { applyTaxPack, listTaxPacks, previewTaxPack } from '../../../../lib/billingTaxAdminApi.js';
  import { fromInput, inMinutes, label, show, when } from './taxUi.js';

  const ACTION_TONE = { add: 'ok', change: 'warn', remove: 'bad', unchanged: '', kept_edited: 'info' };
  const SHOWN_FIELDS = ['name', 'country', 'tax_class', 'customer_type', 'rate_percent', 'jurisdiction', 'reverse_charge', 'valid_from', 'valid_to'];

  let packs = [];
  let loading = true;
  let error = '';

  let active = null; // pack being previewed
  let version = '';
  let effectiveFrom = '';
  let previewing = false;
  let previewError = null;
  let diff = null;
  let previewed = null; // the version and effective_from the diff was made for
  let showUnchanged = false;

  let confirming = false;
  let applying = false;
  let applyError = null;
  let result = null;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      packs = await listTaxPacks();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function appliedText(pack) {
    return pack.applied_versions?.length ? pack.applied_versions.join(', ') : 'never applied';
  }

  function openPack(pack) {
    active = pack;
    version = pack.version;
    effectiveFrom = inMinutes(10);
    diff = null;
    previewed = null;
    previewError = null;
    applyError = null;
    result = null;
  }

  function describe(err) {
    const codes = {
      pack_changed: 'The rules or the pack changed since you previewed it, so the diff you saw is stale. Nothing was applied. Preview again and check the new diff before applying.',
      tax_rules_busy: 'Another change was holding the tax rules, so nothing was applied. Preview again, then apply.',
      pack_not_found: 'This pack no longer exists. Reload the list.',
      pack_version_not_found: 'The pack does not offer that version.',
      effective_from_in_past: 'The start time is in the past. Choose a time from now on.',
      effective_from_invalid: 'The start time is not valid.',
      pack_unavailable: 'The pack could not be loaded.',
    };
    const stale = err.status === 409;
    return {
      stale,
      status: err.status,
      text: codes[err.code] ? `${codes[err.code]}${err.status === 409 ? '' : ` (${err.message})`}` : err.message,
    };
  }

  async function preview() {
    if (!version.trim()) {
      previewError = { text: 'Enter the pack version.' };
      return;
    }
    const at = fromInput(effectiveFrom);
    if (!at) {
      previewError = { text: 'Choose when the pack takes effect.' };
      return;
    }
    previewing = true;
    previewError = null;
    applyError = null;
    result = null;
    try {
      diff = await previewTaxPack(active.pack_id, { version: version.trim(), effective_from: at });
      previewed = { version: version.trim(), effective_from: at };
    } catch (err) {
      diff = null;
      previewed = null;
      previewError = describe(err);
    } finally {
      previewing = false;
    }
  }

  async function apply() {
    applying = true;
    applyError = null;
    try {
      result = await applyTaxPack(active.pack_id, { ...previewed, diff_hash: diff.diff_hash });
      confirming = false;
      diff = null;
      previewed = null;
      await load();
    } catch (err) {
      applyError = describe(err);
      if (err.status === 409 || err.status === 404 || err.status === 422) {
        // The diff can no longer be applied as shown.
        diff = null;
        previewed = null;
        confirming = false;
      }
    } finally {
      applying = false;
    }
  }

  function changedFields(entry) {
    if (!entry.old || !entry.new) return [];
    return SHOWN_FIELDS.filter((f) => String(entry.old[f] ?? '') !== String(entry.new[f] ?? ''));
  }

  $: current = diff && previewed && previewed.version === version.trim() && previewed.effective_from === fromInput(effectiveFrom);
  $: entries = (diff?.entries || []).filter((e) => showUnchanged || e.action !== 'unchanged');
  $: changeCount = (diff?.entries || []).filter((e) => e.action !== 'unchanged' && e.action !== 'kept_edited').length;
  $: addedClass = (diff?.entries || []).some((e) => e.type === 'tax_class' && e.action === 'add');
</script>

<div class="ba-panel">
  <h3>Rule packs</h3>
  <p class="ba-muted">A pack is a versioned set of tax rules, classes and legal notes. Preview shows exactly what applying it would add, change and close from the chosen time; nothing changes until you apply the diff you saw.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if result}
    <Alert type="success">
      Applied {result.pack_id} {result.version} from {when(result.effective_from)}:
      {result.entries.filter((e) => e.action === 'add').length} added,
      {result.entries.filter((e) => e.action === 'change').length} changed,
      {result.entries.filter((e) => e.action === 'remove').length} closed.
      {#if result.entries.some((e) => e.type === 'tax_class' && e.action === 'add')}
        A new tax class was created. Products are not moved to it automatically: set the class on each product in the store product editor.
      {/if}
    </Alert>
  {/if}
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if packs.length === 0}
    <p class="ba-empty">No packs are available.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Pack</th><th>Source</th><th>Newest version</th><th>Applied version</th><th></th></tr></thead>
        <tbody>
          {#each packs as pack (pack.pack_id)}
            <tr>
              <td><strong>{pack.title || pack.pack_id}</strong><div class="ba-hint ba-mono">{pack.pack_id}</div></td>
              <td>{pack.source}</td>
              <td>
                {#if pack.available}{pack.version}{:else}<span class="ba-pill bad">unavailable</span><div class="ba-hint">{pack.error}</div>{/if}
              </td>
              <td>{appliedText(pack)}{#if pack.applied_rule_count}<div class="ba-hint">{pack.applied_rule_count} rule row(s)</div>{/if}</td>
              <td><button class="ba-link" disabled={!pack.available} on:click={() => openPack(pack)}>Preview and apply</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if active}
  <div class="ba-panel">
    <h3>{active.title || active.pack_id} <small>preview and apply</small></h3>
    <div class="ba-toolbar">
      <label>Version
        <select bind:value={version}>
          {#each [...(active.versions?.length ? active.versions : [active.version])].reverse() as v (v)}
            <option value={v}>{v}{v === active.version ? ' (newest)' : ''}</option>
          {/each}
        </select>
      </label>
      <label>Takes effect (not in the past)<input type="datetime-local" bind:value={effectiveFrom} /></label>
      <Button on:click={preview} disabled={previewing}>{diff ? 'Preview again' : 'Preview'}</Button>
      <Button variant="secondary" on:click={() => { active = null; diff = null; previewError = null; applyError = null; }}>Close</Button>
    </div>

    {#if previewError}<Alert type="danger">{previewError.text}</Alert>{/if}
    {#if applyError}
      <Alert type={applyError.stale ? 'warning' : 'danger'}>
        <strong>{applyError.status ? `Not applied (error ${applyError.status}). ` : ''}</strong>{applyError.text}
        {#if applyError.stale} The diff was cleared: use Preview again before applying.{/if}
      </Alert>
    {/if}

    {#if previewing}
      <div class="ba-state"><Spinner /></div>
    {:else if diff}
      {#if !current}
        <Alert type="warning">The version or time was changed after this preview. Preview again before applying.</Alert>
      {/if}
      <p class="ba-muted">
        {diff.entries.filter((e) => e.action === 'add').length} to add,
        {diff.entries.filter((e) => e.action === 'change').length} to change,
        {diff.entries.filter((e) => e.action === 'remove').length} to close,
        {diff.entries.filter((e) => e.action === 'unchanged').length} unchanged,
        {diff.entries.filter((e) => e.action === 'kept_edited').length} legal notes kept because you edited them.
        <span class="ba-mono">diff {diff.diff_hash.slice(0, 12)}</span>
      </p>
      <label class="ba-inline" style="gap:8px;font-weight:500"><input type="checkbox" bind:checked={showUnchanged} /> Show unchanged entries</label>
      {#if entries.length === 0}
        <p class="ba-empty">Nothing to show.</p>
      {:else}
        <div class="ba-scroll">
          <table class="ba-table">
            <thead><tr><th>Type</th><th>Key</th><th>Action</th><th>Detail</th></tr></thead>
            <tbody>
              {#each entries as entry (`${entry.type}:${entry.key}:${entry.locale || ''}`)}
                <tr>
                  <td>{label(entry.type)}</td>
                  <td><span class="ba-mono">{entry.key}{entry.locale ? ` (${entry.locale})` : ''}</span></td>
                  <td><span class="ba-pill {ACTION_TONE[entry.action]}">{label(entry.action)}</span></td>
                  <td>
                    {#if entry.type === 'rule' && entry.action === 'add'}
                      {entry.new.name}: {entry.new.rate_percent}% {entry.new.country || 'any country'}, {entry.new.tax_class}, from {when(entry.new.valid_from)}
                    {:else if entry.type === 'rule' && entry.action === 'change'}
                      {#each changedFields(entry) as f}
                        <div>{label(f)}: <span class="ba-mono">{show(entry.old[f])}</span> to <span class="ba-mono">{show(entry.new[f])}</span></div>
                      {/each}
                      {#if entry.warning}<div class="ba-hint">{entry.warning}</div>{/if}
                    {:else if entry.type === 'rule' && entry.action === 'remove'}
                      {entry.old.name} ends at the effective time
                    {:else if entry.type === 'legal_note' && entry.action !== 'unchanged'}
                      <div class="ba-hint">Now: {show(entry.old)}</div><div>New: {entry.new}</div>
                    {:else if entry.type === 'tax_class'}
                      The class is created
                    {:else}
                      -
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      {#if addedClass}<p class="ba-hint">Applying creates a tax class. Products are not moved to it; set the class in the store product editor afterwards.</p>{/if}
      <div class="ba-actions">
        <Button variant="danger" disabled={!current || changeCount === 0} on:click={() => { confirming = true; applyError = null; }}>Apply this diff</Button>
        {#if changeCount === 0}<span class="ba-hint">Nothing would change, so there is nothing to apply.</span>{/if}
      </div>
    {/if}
  </div>
{/if}

{#if confirming && diff}
  <Modal title="Apply {active.pack_id} {previewed.version}?" onClose={() => (confirming = false)}>
    <p>
      This changes live tax rules from <strong>{when(previewed.effective_from)}</strong>:
      {diff.entries.filter((e) => e.action === 'add').length} rules added,
      {diff.entries.filter((e) => e.action === 'change').length} superseded,
      {diff.entries.filter((e) => e.action === 'remove').length} closed. It is recorded in the audit log and cannot be undone as one step.
    </p>
    <p class="ba-muted">If the rules or the pack changed since you previewed, the server refuses and nothing is applied; you will need to preview again.</p>
    {#if applyError}<div class="ba-error">{applyError.text}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirming = false)}>Cancel</Button>
      <Button variant="danger" on:click={apply} disabled={applying}>Apply</Button>
    </svelte:fragment>
  </Modal>
{/if}
