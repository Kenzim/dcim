<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { listJobActions, resolveJobAction } from '../../../../lib/billingOpsAdminApi.js';
  import { detailPairs, label, when } from './opsFormat.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  /** Limit the list to one job; empty lists every job. */
  export let job = '';
  /** Bumped by the parent after anything that can change the list. */
  export let refreshKey = 0;

  const dispatch = createEventDispatcher();
  const limit = 25;

  let items = [];
  let total = 0;
  let offset = 0;
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let resolving = null;
  let outcome = 'done';
  let note = '';
  let understood = false;
  let resolveError = '';

  let loadedJob = job;
  $: if (job !== loadedJob) {
    loadedJob = job;
    offset = 0;
    load();
  }

  let lastRefresh = refreshKey;
  $: if (refreshKey !== lastRefresh) {
    lastRefresh = refreshKey;
    load();
  }

  onMount(load);

  async function load() {
    loading = true;
    try {
      const data = await listJobActions({ status: 'needs_review', job, limit, offset });
      items = data.items || [];
      total = data.total ?? items.length;
      if (items.length === 0 && offset > 0) {
        offset = Math.max(0, offset - limit);
        await load();
        return;
      }
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function openResolve(item) {
    resolving = item;
    outcome = 'done';
    note = '';
    understood = false;
    resolveError = '';
  }

  async function confirmResolve() {
    working = true;
    resolveError = '';
    try {
      await resolveJobAction(resolving.id, outcome, note.trim());
      success = `${label(resolving.job)} ${resolving.subject_key} was recorded as ${outcome}. Nothing was done to the service.`;
      resolving = null;
      await load();
      dispatch('resolved');
    } catch (err) {
      resolveError = err.message;
    } finally {
      working = false;
    }
  }

  $: canResolve = note.trim().length > 0 && understood && !working;
</script>

<div class="needs-review">
  <p class="ba-muted ba-wide-note">
    A job claimed these actions and did not finish them, so it will not retry them. Check the service yourself, then record what you found. Recording
    a decision only marks the action done or failed and clears the alert; it does not run, retry or undo anything.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading && items.length === 0}
    <div class="ba-state"><Spinner /></div>
  {:else if items.length === 0}
    <p class="ba-empty">No actions need review.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead>
          <tr>{#if !job}<th>Job</th>{/if}<th>Subject</th><th>Action</th><th>Claimed</th><th>Detail</th><th></th></tr>
        </thead>
        <tbody>
          {#each items as item (item.id)}
            <tr class="ba-review-row">
              {#if !job}<td>{label(item.job)}</td>{/if}
              <td><span class="ba-mono">{item.subject_key}</span></td>
              <td>{item.action ? label(item.action) : '-'}</td>
              <td>{when(item.claimed_at)}</td>
              <td>
                {#each detailPairs(item.detail) as [key, text]}
                  <div class="ba-hint"><strong>{key}:</strong> {text}</div>
                {:else}-{/each}
              </td>
              <td><Button size="small" on:click={() => openResolve(item)}>Resolve</Button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="ba-inline" style="margin-top:12px">
      <Button variant="secondary" size="small" disabled={offset === 0 || loading} on:click={() => { offset = Math.max(0, offset - limit); load(); }}>Previous</Button>
      <span class="ba-muted">Showing {offset + 1} to {offset + items.length} of {total}</span>
      <Button variant="secondary" size="small" disabled={offset + items.length >= total || loading} on:click={() => { offset += limit; load(); }}>Next</Button>
    </div>
  {/if}
</div>

{#if resolving}
  <Modal title="Resolve action needing review" onClose={() => (resolving = null)}>
    <p>
      <strong>{label(resolving.job)}</strong>: <span class="ba-mono">{resolving.subject_key}</span>
      {#if resolving.action}({label(resolving.action)}){/if}
    </p>
    {#each detailPairs(resolving.detail) as [key, text]}
      <div class="ba-hint"><strong>{key}:</strong> {text}</div>
    {/each}
    <Alert type="warning">
      This records your decision and does nothing else. It does not run, retry or undo the action, and it does not change the service, its invoices or the job.
      If the service still needs terminating, suspending or fixing, do that yourself first.
    </Alert>
    <div class="ba-form" style="margin-top:12px">
      <fieldset class="ba-radios">
        <legend>What did you find?</legend>
        <label class="ba-check"><input type="radio" bind:group={outcome} value="done" /> The action took effect (mark it done)</label>
        <label class="ba-check"><input type="radio" bind:group={outcome} value="failed" /> The action did not take effect (mark it failed)</label>
      </fieldset>
      <label>Note (required)
        <textarea bind:value={note} rows="3" maxlength="1000" placeholder="What you checked and what you found"></textarea>
      </label>
      <label class="ba-check">
        <input type="checkbox" bind:checked={understood} />
        I understand this only records that I checked the action and found it {outcome}. It does nothing to the service.
      </label>
    </div>
    {#if resolveError}<div class="ba-error">{resolveError}</div>{/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (resolving = null)}>Cancel</Button>
      <Button disabled={!canResolve} on:click={confirmResolve}>Record as {outcome}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .ba-radios { border: 0; padding: 0; margin: 0 0 10px; display: grid; gap: 6px; }
  .ba-radios legend { font-weight: 600; margin-bottom: 4px; padding: 0; }
</style>
