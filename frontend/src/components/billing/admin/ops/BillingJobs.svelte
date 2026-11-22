<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { changeJobMode, getJobForecast, listJobs, runJob } from '../../../../lib/billingOpsAdminApi.js';
  import JobDetail from './JobDetail.svelte';
  import { label, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  const MODE_TONE = { off: '', dry_run: 'info', live: 'ok' };
  const MODE_NAME = { off: 'Off', dry_run: 'Dry run', live: 'Live' };
  const RUN_TONE = { complete: 'ok', running: 'info', failed: 'bad', aborted: 'bad', skipped: 'warn' };
  const JOB_TEXT = {
    generate_invoices_ahead: 'Creates cycle invoices ahead of their due date, by the lead time.',
    invoice_reminders: 'Sends upcoming and overdue notices for invoices with a balance.',
    late_fees: 'Adds one late fee to an overdue invoice as a debit note.',
    nonpayment_suspension: 'Suspends retail services whose cycle invoice stays unpaid.',
    nonpayment_termination: 'Warns, then terminates, services suspended for non-payment.',
    cancellations: 'Completes cancellation requests when they take effect.',
    tax_id_recheck: 'Re-checks stale or unavailable tax ID validations.',
    tax_exchange_rates_fetch: 'Fetches the monthly exchange rates used for sterling VAT.',
  };

  let jobs = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let selected = null;
  let refreshKey = 0;

  let change = null;
  let changeError = '';
  let forecastCount = null;
  let forecastFailed = false;
  let forecastDays = 7;
  let typed = '';

  let running = null;
  let runError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      jobs = (await listJobs()).items || [];
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  $: needsReviewTotal = jobs.reduce((sum, job) => sum + (job.needs_review || 0), 0);

  async function openChange(job, to) {
    changeError = '';
    typed = '';
    forecastCount = null;
    forecastFailed = false;
    change = { job, to };
    if (to === 'live') {
      forecastDays = job.job === 'nonpayment_termination' ? 30 : 7;
      try {
        const data = await getJobForecast(job.job, forecastDays);
        forecastCount = data.items.length;
      } catch {
        forecastFailed = true;
      }
    }
  }

  async function applyMode(job, to) {
    working = true;
    changeError = '';
    success = '';
    try {
      await changeJobMode(job.job, to);
      success = `${label(job.job)} is now ${MODE_NAME[to].toLowerCase()}.`;
      change = null;
      await load();
      refreshKey += 1;
    } catch (err) {
      changeError = err.message;
      if (!change) error = err.message;
    } finally {
      working = false;
    }
  }

  function requestMode(job, to) {
    if (to === 'dry_run') {
      applyMode(job, to);
    } else {
      openChange(job, to);
    }
  }

  function askRun(job, dryRun) {
    runError = '';
    running = { job, dryRun };
  }

  async function confirmRun() {
    working = true;
    runError = '';
    try {
      const result = await runJob(running.job.job, running.dryRun);
      success = `${running.dryRun ? 'Dry run' : 'Run'} #${result.run_id} of ${label(running.job.job)} finished: ${label(result.status)}.${result.error ? ` ${result.error}` : ''}`;
      running = null;
      await load();
      refreshKey += 1;
    } catch (err) {
      runError = err.message;
    } finally {
      working = false;
    }
  }

  function runCounts(counts) {
    return Object.entries(counts || {}).filter(([key, value]) => key !== 'by_action' && typeof value === 'number');
  }

  $: selectedJob = jobs.find((job) => job.job === selected) || null;
</script>

<PageHeader title="Billing Jobs">
  <svelte:fragment slot="actions">
    <Button variant="secondary" on:click={load} disabled={loading}>Refresh</Button>
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  <p class="ba-muted ba-wide-note">
    Each job is off, in dry run (it records what it would do and changes nothing) or live. A job goes live only from dry run, after at least one completed
    scheduled dry run and with its preconditions met. A live job acts only on triggers that fall after it went live, so switching it on never reaches back in time.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if needsReviewTotal > 0}
    <div class="ba-review" role="alert">
      <strong>{needsReviewTotal} job action{needsReviewTotal === 1 ? '' : 's'} need{needsReviewTotal === 1 ? 's' : ''} review.</strong>
      A job left them for staff and will not retry them. They are marked in the table below; open the job's runs to see the failed or conflicting items.
    </div>
  {/if}

  {#if loading && jobs.length === 0}
    <div class="ba-state"><Spinner /></div>
  {:else if jobs.length === 0}
    <p class="ba-empty">No billing jobs are registered.</p>
  {:else}
    <div class="ba-panel" style="margin-top:18px">
      <div class="ba-scroll">
        <table class="ba-table">
          <thead>
            <tr><th>Job</th><th>Mode</th><th>Last run</th><th>Next run</th><th>Needs review</th><th>Change mode</th></tr>
          </thead>
          <tbody>
            {#each jobs as job (job.job)}
              <tr class:ba-selected={selected === job.job} class:ba-review-row={job.needs_review > 0}>
                <td>
                  <button class="ba-link" style="font-weight:700;font-size:14px" on:click={() => (selected = job.job)}>{label(job.job)}</button>
                  <div class="ba-sub">{JOB_TEXT[job.job] || ''}</div>
                  <div class="ba-sub">{job.cadence ? `Runs ${job.cadence}` : ''}</div>
                </td>
                <td>
                  <span class="ba-pill {MODE_TONE[job.mode] || ''}">{MODE_NAME[job.mode] || label(job.mode)}</span>
                  {#if job.mode === 'dry_run'}
                    <div class="ba-sub">Since {when(job.dry_run_since)}</div>
                    <div class="ba-sub">{job.completed_scheduled_dry_runs} completed scheduled dry run{job.completed_scheduled_dry_runs === 1 ? '' : 's'}</div>
                  {:else if job.mode === 'live'}
                    <div class="ba-sub">Live since {when(job.live_since)}</div>
                  {/if}
                  {#if job.config_since}<div class="ba-sub">Settings changed {when(job.config_since)}</div>{/if}
                  {#if job.mode !== 'live' && job.preconditions_unmet?.length}
                    <div class="ba-sub" title="These must be met before the job can go live">
                      Not ready to go live: {job.preconditions_unmet.join('; ')}
                    </div>
                  {/if}
                </td>
                <td>
                  {#if job.last_run}
                    <div class="ba-stack">
                      <span>
                        <span class="ba-pill {RUN_TONE[job.last_run.status] || ''}">{label(job.last_run.status)}</span>
                        <span class="ba-pill {job.last_run.mode === 'live' ? 'bad' : 'info'}">{label(job.last_run.mode)}</span>
                      </span>
                      <span class="ba-sub">{when(job.last_run.started_at)}</span>
                      <span class="ba-sub">
                        {#each runCounts(job.last_run.counts) as [key, value], index}{index ? ', ' : ''}{label(key)} {value}{:else}no items{/each}
                      </span>
                      {#if job.last_run.error}<span class="ba-sub">{job.last_run.error}</span>{/if}
                    </div>
                  {:else}
                    <span class="ba-muted">Never run</span>
                  {/if}
                </td>
                <td>{job.next_run_at ? when(job.next_run_at) : '-'}</td>
                <td>
                  {#if job.needs_review > 0}
                    <span class="ba-pill bad">{job.needs_review} need review</span>
                  {:else}
                    <span class="ba-muted">None</span>
                  {/if}
                </td>
                <td>
                  <div class="ba-inline">
                    {#if job.mode === 'off'}
                      <Button size="small" variant="secondary" disabled={working} on:click={() => requestMode(job, 'dry_run')}>Start dry run</Button>
                    {:else}
                      {#if job.mode === 'dry_run'}
                        <Button size="small" disabled={working} on:click={() => requestMode(job, 'live')}>Go live</Button>
                      {/if}
                      <Button size="small" variant="secondary" disabled={working} on:click={() => requestMode(job, 'off')}>Turn off</Button>
                    {/if}
                    <Button size="small" variant="secondary" on:click={() => (selected = job.job)}>Runs</Button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}

  {#if selectedJob}
    <div class="ba-actions" style="margin-top:0">
      <Button variant="secondary" size="small" disabled={working} on:click={() => askRun(selectedJob, true)}>Dry run now</Button>
      {#if selectedJob.mode === 'live'}
        <Button variant="secondary" size="small" disabled={working} on:click={() => askRun(selectedJob, false)}>Run live now</Button>
      {/if}
    </div>
    <JobDetail job={selectedJob.job} {refreshKey} on:close={() => (selected = null)} />
  {/if}
</div>

{#if change}
  <Modal title={change.to === 'live' ? `Take ${label(change.job.job)} live` : `Turn ${label(change.job.job)} off`} onClose={() => (change = null)}>
    {#if change.to === 'live'}
      <p>
        <strong>{label(change.job.job)}</strong> will start acting for real: sending email, changing invoices and services as its rules say. It acts only on triggers that fall after this moment.
        The change is recorded in the audit log.
      </p>
      {#if change.job.preconditions_unmet?.length}
        <Alert type="warning">Preconditions not met: {change.job.preconditions_unmet.join('; ')}. The server will refuse until they are.</Alert>
      {/if}
      <p class="ba-muted">
        {change.job.completed_scheduled_dry_runs} completed scheduled dry run{change.job.completed_scheduled_dry_runs === 1 ? '' : 's'} so far.
        {#if forecastFailed}
          The forecast could not be loaded.
        {:else if forecastCount === null}
          Loading the forecast...
        {:else}
          The forecast shows <strong>{forecastCount}</strong> action{forecastCount === 1 ? '' : 's'} in the next {forecastDays} days.
        {/if}
      </p>
      <label class="ba-form"><span>Type <strong>live</strong> to confirm</span>
        <input bind:value={typed} autocomplete="off" />
      </label>
    {:else}
      <p>
        Turning <strong>{label(change.job.job)}</strong> off stops it acting.
        {#if change.job.mode === 'live'}
          Time spent off is never caught up: if you switch it on again it starts from that moment.
        {/if}
      </p>
    {/if}
    {#if changeError}<div class="ba-error">{changeError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (change = null)}>Cancel</Button>
      <Button
        variant={change.to === 'live' ? 'danger' : 'primary'}
        disabled={working || (change.to === 'live' && typed.trim() !== 'live')}
        on:click={() => applyMode(change.job, change.to)}
      >{change.to === 'live' ? 'Go live' : 'Turn off'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if running}
  <Modal title={running.dryRun ? 'Dry run now' : 'Run now'} onClose={() => (running = null)}>
    {#if running.dryRun}
      <p>Start a dry run of <strong>{label(running.job.job)}</strong>. It records what the job would do and changes nothing.</p>
    {:else}
      <Alert type="warning">This is a live run of {label(running.job.job)}. It acts for real on everything due now.</Alert>
    {/if}
    {#if runError}<div class="ba-error">{runError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (running = null)}>Cancel</Button>
      <Button variant={running.dryRun ? 'primary' : 'danger'} disabled={working} on:click={confirmRun}>
        {running.dryRun ? 'Start dry run' : 'Run live now'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}
