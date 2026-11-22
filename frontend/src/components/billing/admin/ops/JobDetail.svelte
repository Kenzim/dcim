<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button, Spinner, Tabs } from '../../../ui/index.js';
  import { getJobForecast, getJobRun, listJobRuns } from '../../../../lib/billingOpsAdminApi.js';
  import { detailPairs, label, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  export let job;
  /** Bumped by the parent after it starts a run, so the run list reloads. */
  export let refreshKey = 0;

  const dispatch = createEventDispatcher();

  const RUN_TONE = { complete: 'ok', running: 'info', failed: 'bad', aborted: 'bad', skipped: 'warn' };
  const OUTCOME_TONE = { done: 'ok', planned: 'info', already_done: '', skipped: 'warn', failed: 'bad' };
  const OUTCOMES = ['planned', 'done', 'already_done', 'skipped', 'failed'];
  const ITEM_PAGE = 100;

  let tab = 'runs';
  let error = '';

  let runs = [];
  let runsLoading = false;
  let runsOffset = 0;
  const runsLimit = 25;

  let openRun = null;
  let runLoading = false;
  let outcome = '';
  let itemOffset = 0;

  let forecast = null;
  let forecastDays = 7;
  let forecastLoading = false;

  let loadedJob = null;

  $: if (job && job !== loadedJob) {
    loadedJob = job;
    tab = 'runs';
    openRun = null;
    runsOffset = 0;
    forecast = null;
    error = '';
    loadRuns();
  }

  let lastRefresh = refreshKey;
  $: if (refreshKey !== lastRefresh) {
    lastRefresh = refreshKey;
    loadRuns();
    if (tab === 'forecast') loadForecast();
  }

  async function loadRuns() {
    runsLoading = true;
    try {
      const data = await listJobRuns(job, { limit: runsLimit, offset: runsOffset });
      runs = data.items || [];
    } catch (err) {
      error = err.message;
    } finally {
      runsLoading = false;
    }
  }

  async function showRun(id, resetFilters = true) {
    runLoading = true;
    error = '';
    if (resetFilters) {
      outcome = '';
      itemOffset = 0;
    }
    try {
      openRun = await getJobRun(id, { outcome, limit: ITEM_PAGE, offset: itemOffset });
    } catch (err) {
      error = err.message;
    } finally {
      runLoading = false;
    }
  }

  function changeOutcome() {
    itemOffset = 0;
    showRun(openRun.id, false);
  }

  function page(delta) {
    itemOffset = Math.max(0, itemOffset + delta * ITEM_PAGE);
    showRun(openRun.id, false);
  }

  async function loadForecast() {
    forecastLoading = true;
    error = '';
    try {
      forecast = await getJobForecast(job, forecastDays);
    } catch (err) {
      error = err.message;
    } finally {
      forecastLoading = false;
    }
  }

  function onTab(event) {
    tab = event.detail.id;
    error = '';
    if (tab === 'forecast' && !forecast) loadForecast();
  }

  function runCounts(counts) {
    return Object.entries(counts || {}).filter(([key, value]) => key !== 'by_action' && typeof value === 'number');
  }

  /** An item the job left for staff: a rate conflict recorded on a finished action. */
  function itemNeedsReview(item) {
    const detail = item.detail || {};
    return detail.reason === 'conflict' || (Array.isArray(detail.conflicts) && detail.conflicts.length > 0);
  }

  $: reviewItems = openRun ? openRun.items.filter(itemNeedsReview) : [];
  $: failedItems = openRun ? openRun.items.filter((item) => item.outcome === 'failed') : [];
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">{label(job)}</h3>
    <Button variant="secondary" size="small" on:click={() => dispatch('close')}>Close</Button>
  </div>
  <div style="margin-top:12px">
    <Tabs
      tabs={[{ id: 'runs', label: 'Runs' }, { id: 'forecast', label: 'Forecast' }]}
      active={tab}
      on:change={onTab}
    />
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}

  {#if tab === 'runs'}
    {#if openRun}
      <div class="ba-inline" style="margin:14px 0 8px">
        <Button variant="secondary" size="small" on:click={() => (openRun = null)}>Back to runs</Button>
        <strong>Run #{openRun.id}</strong>
        <span class="ba-pill {RUN_TONE[openRun.status] || ''}">{label(openRun.status)}</span>
        <span class="ba-pill {openRun.mode === 'live' ? 'bad' : 'info'}">{label(openRun.mode)}</span>
        <span class="ba-muted">{label(openRun.trigger)} · started {when(openRun.started_at)} · watermark {when(openRun.watermark_at)}</span>
      </div>
      {#if openRun.error}<div class="ba-error">{openRun.error}</div>{/if}

      {#if reviewItems.length}
        <div class="ba-review" role="alert">
          <strong>{reviewItems.length} item{reviewItems.length === 1 ? '' : 's'} in this page need review.</strong>
          The job recorded a conflict and left the action for staff; it will not retry it.
        </div>
      {/if}
      {#if failedItems.length}
        <div class="ba-error">{failedItems.length} item{failedItems.length === 1 ? '' : 's'} in this page failed. The reason is in the detail column.</div>
      {/if}

      <div class="ba-toolbar" style="margin-top:12px">
        <label>Outcome
          <select bind:value={outcome} on:change={changeOutcome}>
            <option value="">All</option>
            {#each OUTCOMES as value}<option {value}>{label(value)}</option>{/each}
          </select>
        </label>
        <span class="ba-muted">Showing {openRun.items.length} item{openRun.items.length === 1 ? '' : 's'} from {itemOffset + 1}</span>
      </div>
      {#if runLoading}
        <div class="ba-state"><Spinner /></div>
      {:else if openRun.items.length === 0}
        <p class="ba-empty">No items{outcome ? ` with the outcome ${label(outcome)}` : ''}.</p>
      {:else}
        <div class="ba-scroll">
          <table class="ba-table">
            <thead><tr><th>Subject</th><th>Action</th><th>Outcome</th><th>Detail</th></tr></thead>
            <tbody>
              {#each openRun.items as item (item.id)}
                <tr class:ba-review-row={itemNeedsReview(item)}>
                  <td><span class="ba-mono">{item.subject_key}</span></td>
                  <td>{label(item.action)}</td>
                  <td>
                    <span class="ba-pill {OUTCOME_TONE[item.outcome] || ''}">{label(item.outcome)}</span>
                    {#if itemNeedsReview(item)}<span class="ba-pill warn">Needs review</span>{/if}
                  </td>
                  <td>
                    {#each detailPairs(item.detail) as [key, text]}
                      <div class="ba-hint"><strong>{key}:</strong> {text}</div>
                    {/each}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <div class="ba-inline" style="margin-top:12px">
          <Button variant="secondary" size="small" disabled={itemOffset === 0} on:click={() => page(-1)}>Previous</Button>
          <Button variant="secondary" size="small" disabled={openRun.items.length < ITEM_PAGE} on:click={() => page(1)}>Next</Button>
        </div>
      {/if}
    {:else if runsLoading}
      <div class="ba-state"><Spinner /></div>
    {:else if runs.length === 0}
      <p class="ba-empty">This job has not run yet.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Run</th><th>Mode</th><th>Trigger</th><th>Status</th><th>Started</th><th>Finished</th><th>Items</th><th></th></tr></thead>
          <tbody>
            {#each runs as run (run.id)}
              <tr>
                <td>#{run.id}</td>
                <td><span class="ba-pill {run.mode === 'live' ? 'bad' : 'info'}">{label(run.mode)}</span></td>
                <td>{label(run.trigger)}</td>
                <td>
                  <span class="ba-pill {RUN_TONE[run.status] || ''}">{label(run.status)}</span>
                  {#if run.error}<div class="ba-hint" style="margin-top:4px">{run.error}</div>{/if}
                </td>
                <td>{when(run.started_at)}</td>
                <td>{when(run.finished_at)}</td>
                <td>
                  {#each runCounts(run.counts) as [key, value]}
                    <span class="ba-pill {OUTCOME_TONE[key] || ''}" style="margin-right:4px">{label(key)} {value}</span>
                  {:else}-{/each}
                </td>
                <td><button class="ba-link" on:click={() => showRun(run.id)}>Items</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="ba-inline" style="margin-top:12px">
        <Button variant="secondary" size="small" disabled={runsOffset === 0} on:click={() => { runsOffset = Math.max(0, runsOffset - runsLimit); loadRuns(); }}>Previous</Button>
        <Button variant="secondary" size="small" disabled={runs.length < runsLimit} on:click={() => { runsOffset += runsLimit; loadRuns(); }}>Next</Button>
      </div>
    {/if}
  {:else}
    <div class="ba-toolbar" style="margin-top:14px">
      <label>Look ahead
        <select bind:value={forecastDays} on:change={loadForecast}>
          {#each [1, 7, 14, 30, 60] as days}<option value={days}>{days} day{days === 1 ? '' : 's'}</option>{/each}
        </select>
      </label>
    </div>
    <p class="ba-muted">What the job would act on in this period if it were live. Read only; each subject appears once, with its next step.</p>
    {#if forecastLoading}
      <div class="ba-state"><Spinner /></div>
    {:else if forecast}
      {#if forecast.items.length === 0}
        <p class="ba-empty">Nothing is due in the next {forecast.days} day{forecast.days === 1 ? '' : 's'}.</p>
      {:else}
        <p><strong>{forecast.items.length}</strong> action{forecast.items.length === 1 ? '' : 's'} in the next {forecast.days} day{forecast.days === 1 ? '' : 's'}.</p>
        <div class="ba-scroll">
          <table class="ba-table">
            <thead><tr><th>Due</th><th>Subject</th><th>Action</th><th>Detail</th></tr></thead>
            <tbody>
              {#each forecast.items as item}
                <tr>
                  <td>{when(item.trigger_at)}</td>
                  <td><span class="ba-mono">{item.subject_key}</span></td>
                  <td>{label(item.action)}</td>
                  <td>
                    {#each detailPairs(item.detail, ['trigger_at']) as [key, text]}
                      <div class="ba-hint"><strong>{key}:</strong> {text}</div>
                    {/each}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  {/if}
</div>
