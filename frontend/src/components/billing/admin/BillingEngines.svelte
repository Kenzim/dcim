<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import {
    getEngines,
    listMismatches,
    resolveMismatch,
    runScan,
    switchEngine,
  } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  const MODE_TONE = { v2: 'ok', shadow: 'info', legacy: '' };
  const MODES = ['legacy', 'shadow', 'v2'];
  const ENGINE_TEXT = {
    invoice: 'Invoice engine: balances, statuses, credit and debit notes, partial refunds.',
    credit: 'Credit engine: the credit balance ledger for every account.',
  };

  let data = null;
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let mismatches = { total: 0, items: [] };
  let filters = { engine: '', check_key: '', open_only: true };
  let offset = 0;
  const limit = 25;
  let mismatchLoading = false;
  let resolving = null;
  let resolveNote = '';
  let resolveError = '';

  let switching = null;
  let typed = '';
  let confirmOverpay = false;
  let switchError = null;

  let scanResult = null;

  onMount(async () => {
    await Promise.all([loadEngines(), loadMismatches()]);
  });

  async function loadEngines() {
    loading = true;
    try {
      data = await getEngines();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function loadMismatches() {
    mismatchLoading = true;
    try {
      mismatches = await listMismatches({
        engine: filters.engine,
        check_key: filters.check_key.trim(),
        open_only: filters.open_only,
        offset,
        limit,
      });
    } catch (err) {
      error = err.message;
    } finally {
      mismatchLoading = false;
    }
  }

  function applyFilters() {
    offset = 0;
    loadMismatches();
  }

  async function scan(dry) {
    working = true;
    error = '';
    success = '';
    try {
      scanResult = await runScan(dry);
      success = dry ? 'Dry run finished; nothing was recorded.' : 'Scan finished.';
      await Promise.all([loadEngines(), loadMismatches()]);
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  function openSwitch(engine, to) {
    switching = { engine, to };
    typed = '';
    confirmOverpay = false;
    switchError = null;
  }

  async function confirmSwitch() {
    working = true;
    switchError = null;
    try {
      const result = await switchEngine(switching.engine.engine, switching.to, confirmOverpay);
      success = `The ${switching.engine.engine} engine is now ${result.mode}.`;
      if (result.rollback?.reseller_caches_differing) {
        success += ` ${result.rollback.reseller_caches_differing} reseller cache(s) differ and need resynchronising.`;
      }
      switching = null;
      await Promise.all([loadEngines(), loadMismatches()]);
    } catch (err) {
      switchError = {
        message: err.message,
        failing: err.detail?.failing || [],
        criteria: (err.detail?.criteria || []).filter((item) => !item.ok),
      };
    } finally {
      working = false;
    }
  }

  async function submitResolve() {
    if (resolveNote.trim().length < 3) {
      resolveError = 'Write a note of at least 3 characters.';
      return;
    }
    working = true;
    try {
      await resolveMismatch(resolving.id, resolveNote.trim());
      resolving = null;
      success = 'Mismatch resolved. The quiet period is not shortened by resolving.';
      await Promise.all([loadEngines(), loadMismatches()]);
    } catch (err) {
      resolveError = err.message;
    } finally {
      working = false;
    }
  }

  function label(value) {
    return String(value ?? '').replaceAll('_', ' ');
  }

  function when(value) {
    return value ? new Date(value).toLocaleString() : '-';
  }

  function show(value) {
    if (value === null || value === undefined) return '-';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function details(item) {
    return Object.entries(item)
      .filter(([key]) => key !== 'item' && key !== 'ok')
      .map(([key, value]) => `${label(key)}: ${show(value)}`)
      .join(' · ');
  }

  function openCounts(engine) {
    const counts = engine.open_mismatches || {};
    const text = Object.entries(counts).map(([key, value]) => `${key} ${value}`).join(', ');
    return text || 'none';
  }

  $: switchNeedsConfirm = switching?.engine.engine === 'invoice' && switching?.to === 'v2' && data?.engines?.find((e) => e.engine === 'credit')?.mode !== 'v2';
  $: pages = Math.max(1, Math.ceil(mismatches.total / limit));
  $: page = Math.floor(offset / limit) + 1;
</script>

<PageHeader title="Billing Engines">
  <svelte:fragment slot="actions">
    <Button variant="secondary" on:click={() => scan(true)} disabled={working}>Dry-run scan</Button>
    <Button on:click={() => scan(false)} disabled={working}>Run scan now</Button>
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  <p class="ba-muted">
    Each engine runs as legacy (old rules only), shadow (old rules decide, the new rules are compared and every difference is recorded) or v2 (new rules decide).
    An engine may only move to v2 once the switch criterion holds. On-demand scans never count towards the criterion.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if data}
    <p class="ba-hint">Daily scan: {data.scan_enabled ? 'on' : 'off'}{data.scan_dry_run ? ' (dry run)' : ''} · quiet period needed: {data.min_days} days</p>
    {#each data.engines as engine (engine.engine)}
      <div class="ba-panel">
        <div class="ba-inline" style="justify-content: space-between">
          <h3 style="margin:0">{engine.engine} engine <span class="ba-pill {MODE_TONE[engine.mode]}">{engine.mode}</span></h3>
          <div class="ba-inline">
            {#each MODES.filter((m) => m !== engine.mode) as target}
              <Button variant={target === 'v2' ? 'primary' : 'secondary'} size="small" on:click={() => openSwitch(engine, target)}>Switch to {target}</Button>
            {/each}
          </div>
        </div>
        <p class="ba-muted">{ENGINE_TEXT[engine.engine]}</p>
        <dl class="ba-grid">
          <div><dt>Shadow since</dt><dd>{when(engine.shadow_since)}</dd></div>
          <div><dt>Last scan finished</dt><dd>{when(engine.last_scan?.finished_at)}</dd></div>
          <div><dt>Subjects checked</dt><dd>{engine.last_scan?.subjects_checked ?? '-'}</dd></div>
          <div><dt>Mismatches in last scan</dt><dd>{engine.last_scan?.mismatches_found ?? '-'}</dd></div>
          <div><dt>Open mismatches</dt><dd>{openCounts(engine)}</dd></div>
          <div><dt>Switched to v2</dt><dd>{engine.switch_record?.switched_at ? when(engine.switch_record.switched_at) : 'never'}</dd></div>
        </dl>
        <h3 style="margin-top:16px">Switch criterion to v2
          <span class="ba-pill {engine.switch_criterion.ok ? 'ok' : 'bad'}">{engine.switch_criterion.ok ? 'met' : 'not met'}</span>
        </h3>
        <ul class="checklist">
          {#each engine.switch_criterion.items as item (item.item)}
            <li>
              <span class="mark {item.ok ? 'ok' : 'bad'}" aria-hidden="true">{item.ok ? '✓' : '✗'}</span>
              <div>
                <strong>{label(item.item)}</strong> <span class="ba-hint">{item.ok ? 'ok' : 'failing'}</span>
                <div class="ba-hint">{details(item)}</div>
              </div>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  {/if}

  {#if scanResult}
    <div class="ba-panel">
      <h3>Last scan result {scanResult.dry_run ? '(dry run)' : ''}</h3>
      {#each Object.entries(scanResult.engines || {}) as [name, summary]}
        <p class="ba-muted"><strong>{name}</strong>:
          {#if summary.skipped}skipped ({label(summary.skipped)})
          {:else}{summary.subjects_checked} checked, {summary.mismatches_found} mismatches{/if}
        </p>
        {#if summary.findings?.length}
          <ul class="ba-hint">{#each summary.findings as finding}<li>{show(finding)}</li>{/each}</ul>
        {/if}
      {/each}
    </div>
  {/if}

  <div class="ba-panel">
    <h3>Mismatches <small>{mismatches.total} found</small></h3>
    <div class="ba-toolbar">
      <label>Engine
        <select bind:value={filters.engine} on:change={applyFilters}>
          <option value="">Both</option><option value="invoice">Invoice</option><option value="credit">Credit</option>
        </select>
      </label>
      <label>Check
        <input bind:value={filters.check_key} placeholder="e.g. i1_decision" on:change={applyFilters} />
      </label>
      <label class="ba-inline" style="font-weight:500"><input type="checkbox" bind:checked={filters.open_only} on:change={applyFilters} /> Open only</label>
    </div>
    {#if mismatchLoading}
      <div class="ba-state"><Spinner /></div>
    {:else if mismatches.items.length === 0}
      <p class="ba-empty">No mismatches match.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Engine</th><th>Check</th><th>Subject</th><th>Legacy</th><th>v2</th><th class="ba-num">Seen</th><th>Last seen</th><th></th></tr></thead>
          <tbody>
            {#each mismatches.items as row (row.id)}
              <tr>
                <td>{row.engine}</td>
                <td><span class="ba-mono">{row.check_key}</span></td>
                <td>{row.subject_type} {row.subject_id}</td>
                <td><span class="ba-mono">{show(row.legacy_value)}</span></td>
                <td><span class="ba-mono">{show(row.v2_value)}</span></td>
                <td class="ba-num">{row.occurrences}</td>
                <td>{when(row.last_seen_at)}</td>
                <td>
                  {#if row.resolved_at}
                    <span class="ba-pill ok" title={row.resolution_note}>resolved</span>
                  {:else}
                    <button class="ba-link" on:click={() => { resolving = row; resolveNote = ''; resolveError = ''; }}>Resolve</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="ba-inline" style="margin-top:12px">
        <Button variant="secondary" size="small" disabled={offset === 0} on:click={() => { offset = Math.max(0, offset - limit); loadMismatches(); }}>Previous</Button>
        <span class="ba-muted">Page {page} of {pages}</span>
        <Button variant="secondary" size="small" disabled={page >= pages} on:click={() => { offset += limit; loadMismatches(); }}>Next</Button>
      </div>
    {/if}
  </div>
</div>

{#if resolving}
  <Modal title="Resolve mismatch #{resolving.id}" onClose={() => (resolving = null)}>
    <p class="ba-muted">{resolving.engine} / {resolving.check_key} on {resolving.subject_type} {resolving.subject_id}. Resolving never edits money and does not shorten the quiet period; a repeat of the difference reopens it.</p>
    <label class="ba-form">Note
      <textarea bind:value={resolveNote} rows="3" maxlength="2000" placeholder="What was found and why it is acceptable"></textarea>
    </label>
    {#if resolveError}<div class="ba-error">{resolveError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (resolving = null)}>Cancel</Button>
      <Button on:click={submitResolve} disabled={working}>Resolve</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if switching}
  <Modal title="Switch the {switching.engine.engine} engine to {switching.to}" onClose={() => (switching = null)}>
    <p>
      The {switching.engine.engine} engine is now <strong>{switching.engine.mode}</strong>. You are about to change it to <strong>{switching.to}</strong>.
      This changes how live money is decided and is recorded in the audit log.
    </p>
    {#if switching.to === 'v2' && !switching.engine.switch_criterion.ok}
      <Alert type="warning">The switch criterion is not met right now. The server will refuse the switch and name what is failing.</Alert>
    {/if}
    {#if switching.to !== 'v2' && switching.engine.mode === 'v2'}
      <Alert type="warning">
        {#if switching.engine.engine === 'invoice'}
          The invoice engine can only leave v2 while no credit note, debit note or partial refund exists.
        {:else}
          The reseller cache becomes authoritative again; any accounts whose caches differ will be listed.
        {/if}
      </Alert>
    {/if}
    {#if switchNeedsConfirm}
      <label class="ba-inline" style="align-items:flex-start"><input type="checkbox" bind:checked={confirmOverpay} style="margin-top:4px" />
        <span>I confirm that overpayments on reseller invoices will keep going to the reseller credit cache, because the credit engine is not v2 yet.</span>
      </label>
    {/if}
    <label class="ba-form" style="margin-top:10px"><span>Type <strong>{switching.to}</strong> to confirm</span>
      <input bind:value={typed} autocomplete="off" />
    </label>
    {#if switchError}
      <div class="ba-error">
        {switchError.message}
        {#if switchError.criteria.length}
          <ul>{#each switchError.criteria as item}<li><strong>{label(item.item)}</strong>: {details(item)}</li>{/each}</ul>
        {:else if switchError.failing.length}
          <ul>{#each switchError.failing as name}<li>{label(name)}</li>{/each}</ul>
        {/if}
      </div>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (switching = null)}>Cancel</Button>
      <Button variant={switching.to === 'v2' ? 'danger' : 'primary'} on:click={confirmSwitch} disabled={working || typed.trim() !== switching.to}>Switch to {switching.to}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .checklist { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .checklist li { display: flex; gap: 10px; align-items: flex-start; padding: 8px 10px; border-radius: 8px; background: var(--bg-secondary); }
  .mark { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; font-size: 12px; font-weight: 700; flex-shrink: 0; margin-top: 1px; }
  .mark.ok { background: var(--success-bg); color: var(--success-text); }
  .mark.bad { background: var(--danger-bg); color: var(--danger-text); }
</style>
