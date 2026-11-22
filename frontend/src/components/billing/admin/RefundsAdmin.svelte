<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import {
    dispatchRefund,
    listRefundReviews,
    listRefunds,
    newIdempotencyKey,
    parseCents,
    resolveRefund,
    resolveRefundReview,
  } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  const STATUSES = ['pending', 'review', 'succeeded', 'failed'];

  let tab = 'refunds';
  let refunds = [];
  let reviews = [];
  let statusFilter = '';
  let invoiceFilter = '';
  let openOnly = true;
  let loading = true;
  let error = '';
  let success = '';
  let working = false;

  let resolving = null;
  let resolveForm = { outcome: 'succeeded', ref: '', note: '' };
  let resolveError = '';

  let reviewing = null;
  let reviewForm = { action: 'refund_original_method', amount: '', note: '' };
  let reviewError = '';
  let reviewKey = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      const [refundData, reviewData] = await Promise.all([
        listRefunds({ status: statusFilter, invoice_id: invoiceFilter.trim(), limit: 200 }),
        listRefundReviews(openOnly),
      ]);
      refunds = refundData.items;
      reviews = reviewData.items;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function dispatchNow(refund) {
    working = true;
    error = '';
    success = '';
    try {
      const result = await dispatchRefund(refund.id);
      success = `Refund #${refund.id} is now ${result.status}${result.failure_message ? `: ${result.failure_message}` : ''}.`;
      await load();
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  function openResolve(refund) {
    resolving = refund;
    resolveForm = { outcome: 'succeeded', ref: refund.gateway_refund_ref || '', note: '' };
    resolveError = '';
  }

  async function submitResolve() {
    if (!resolveForm.note.trim()) {
      resolveError = 'A note is required.';
      return;
    }
    working = true;
    try {
      const result = await resolveRefund(resolving.id, {
        outcome: resolveForm.outcome,
        gateway_refund_ref: resolveForm.ref.trim() || undefined,
        note: resolveForm.note.trim(),
      });
      success = `Refund #${resolving.id} marked ${result.status}.`;
      resolving = null;
      await load();
    } catch (err) {
      resolveError = err.message;
    } finally {
      working = false;
    }
  }

  function openReview(item) {
    reviewing = item;
    reviewKey = newIdempotencyKey('refund-review');
    reviewForm = {
      action: item.payment_id ? 'refund_original_method' : 'dismiss',
      amount: item.amount_cents ? (item.amount_cents / 100).toFixed(2) : '',
      note: '',
    };
    reviewError = '';
  }

  async function submitReview() {
    const action = reviewForm.action;
    const body = { invoice_id: reviewing.invoice_id, reason: reviewing.reason, action };
    if (reviewing.payment_id) body.payment_id = reviewing.payment_id;
    if (action === 'dismiss' && !reviewForm.note.trim()) {
      reviewError = 'Write a note to dismiss a review.';
      return;
    }
    if (action !== 'dismiss') {
      if (!reviewing.payment_id) {
        reviewError = 'This review names no payment, so it can only be dismissed.';
        return;
      }
      if (reviewForm.amount.trim()) {
        const cents = parseCents(reviewForm.amount);
        if (cents === null) {
          reviewError = 'Enter the amount as for example 12.50, or leave it empty for the full amount.';
          return;
        }
        body.amount_cents = cents;
      }
    }
    if (reviewForm.note.trim()) body.note = reviewForm.note.trim();
    working = true;
    try {
      const result = await resolveRefundReview(body, reviewKey);
      success =
        result.status === 'refund_failed'
          ? `The gateway declined the refund (refund #${result.refund_id}); the review stays open.`
          : result.status === 'already_resolved'
            ? 'This review was already resolved.'
            : 'Review resolved.';
      reviewing = null;
      await load();
    } catch (err) {
      reviewError = err.message;
      reviewKey = newIdempotencyKey('refund-review');
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

  function tone(status) {
    return status === 'succeeded' ? 'ok' : status === 'failed' ? 'bad' : 'warn';
  }

  $: openReviews = reviews.filter((item) => !item.resolved).length;
</script>

<PageHeader title="Refunds" />

<div class="ba-page">
  <div class="ba-actions" style="margin-top:0">
    <Button variant={tab === 'refunds' ? 'primary' : 'secondary'} on:click={() => (tab = 'refunds')}>Refunds ({refunds.length})</Button>
    <Button variant={tab === 'reviews' ? 'primary' : 'secondary'} on:click={() => (tab = 'reviews')}>Review queue ({openReviews} open)</Button>
  </div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if tab === 'refunds'}
    <div class="ba-toolbar">
      <label>Status
        <select bind:value={statusFilter} on:change={load}>
          <option value="">All</option>
          {#each STATUSES as status}<option value={status}>{status}</option>{/each}
        </select>
      </label>
      <label>Invoice id
        <input bind:value={invoiceFilter} inputmode="numeric" on:change={load} placeholder="Any" />
      </label>
    </div>
    <p class="ba-muted">A pending refund has been reserved but not confirmed by the gateway: send it again, or resolve it by hand once you know what happened. A refund in review needs a person to decide.</p>
    {#if refunds.length === 0}
      <p class="ba-empty">No refunds match.</p>
    {:else}
      <div class="ba-panel ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Invoice</th><th>Payment</th><th>To</th><th>Reason</th><th>Status</th><th class="ba-num">Amount</th><th>Requested</th><th></th></tr></thead>
          <tbody>
            {#each refunds as refund (refund.id)}
              <tr>
                <td>{refund.id}</td>
                <td>#{refund.invoice_id}</td>
                <td>#{refund.payment_id} <span class="ba-hint">{refund.gateway}</span></td>
                <td>{refund.destination}</td>
                <td>{label(refund.reason)}{#if refund.reason_text}<div class="ba-hint">{refund.reason_text}</div>{/if}</td>
                <td>
                  <span class="ba-pill {tone(refund.status)}">{refund.status}</span>
                  {#if refund.failure_message}<div class="ba-hint">{refund.failure_message}</div>{/if}
                  {#if refund.status === 'pending' && refund.attempts}<div class="ba-hint">{refund.attempts} attempt(s)</div>{/if}
                </td>
                <td class="ba-num">{formatMoney(refund.amount_cents, refund.currency)}</td>
                <td>{when(refund.requested_at)}</td>
                <td>
                  {#if refund.status === 'pending' || refund.status === 'review'}
                    <div class="ba-inline">
                      {#if refund.status === 'pending' && refund.destination === 'gateway'}
                        <button class="ba-link" disabled={working} on:click={() => dispatchNow(refund)}>Send now</button>
                      {/if}
                      <button class="ba-link" on:click={() => openResolve(refund)}>Resolve</button>
                    </div>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else}
    <div class="ba-toolbar">
      <label class="ba-inline" style="font-weight:500"><input type="checkbox" bind:checked={openOnly} on:change={load} /> Open reviews only</label>
    </div>
    <p class="ba-muted">A review is raised when money was paid on an order that was then cancelled or could not be fulfilled. Refund it to the original method or to account credit, or dismiss it with a note.</p>
    {#if reviews.length === 0}
      <p class="ba-empty">The review queue is empty.</p>
    {:else}
      <div class="ba-panel ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Invoice</th><th>Order</th><th>Payment</th><th>Reason</th><th class="ba-num">Amount</th><th>Recorded</th><th>State</th><th></th></tr></thead>
          <tbody>
            {#each reviews as item (`${item.invoice_id}-${item.payment_id}-${item.reason}`)}
              <tr>
                <td>#{item.invoice_number}</td>
                <td>{item.order_id ? `#${item.order_id}` : '-'}</td>
                <td>{item.payment_id ? `#${item.payment_id}` : 'whole invoice'}</td>
                <td>{label(item.reason)}{#if item.policy}<div class="ba-hint">policy {label(item.policy)}</div>{/if}</td>
                <td class="ba-num">{item.amount_cents ? formatMoney(item.amount_cents, item.currency) : '-'}</td>
                <td>{when(item.recorded_at)}</td>
                <td>
                  {#if item.resolved}
                    <span class="ba-pill ok">resolved</span>
                    <div class="ba-hint">{label(item.resolution?.action)}{item.resolution?.note ? `: ${item.resolution.note}` : ''}</div>
                  {:else}
                    <span class="ba-pill warn">open</span>
                  {/if}
                </td>
                <td>{#if !item.resolved}<button class="ba-link" on:click={() => openReview(item)}>Resolve</button>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

{#if resolving}
  <Modal title="Resolve refund #{resolving.id}" onClose={() => (resolving = null)}>
    <p class="ba-muted">Use this when the outcome is known outside Rackflow. Marking it succeeded records it as done; marking it failed releases the reserved amount.</p>
    <form class="ba-form" on:submit|preventDefault={submitResolve}>
      <label>Outcome
        <select bind:value={resolveForm.outcome}>
          <option value="succeeded">Succeeded (the money was returned)</option>
          <option value="failed">Failed (nothing was returned)</option>
        </select>
      </label>
      {#if resolveForm.outcome === 'succeeded' && resolving.destination === 'gateway'}
        <label>Gateway refund reference
          <input bind:value={resolveForm.ref} maxlength="255" />
          <span class="ba-hint">Required for a refund to the original method.</span>
        </label>
      {/if}
      <label>Note<textarea bind:value={resolveForm.note} rows="3" maxlength="1000"></textarea></label>
      {#if resolveError}<div class="ba-error">{resolveError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (resolving = null)}>Cancel</Button>
      <Button on:click={submitResolve} disabled={working}>Resolve</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if reviewing}
  <Modal title="Resolve review for invoice #{reviewing.invoice_number}" onClose={() => (reviewing = null)}>
    <p class="ba-muted">{label(reviewing.reason)}{reviewing.payment_id ? `, payment #${reviewing.payment_id}` : ', whole invoice'}.</p>
    <form class="ba-form" on:submit|preventDefault={submitReview}>
      <label>Action
        <select bind:value={reviewForm.action}>
          <option value="refund_original_method" disabled={!reviewing.payment_id}>Refund to the original payment method</option>
          <option value="credit" disabled={!reviewing.payment_id}>Refund to account credit</option>
          <option value="dismiss">Dismiss (no money moves)</option>
        </select>
      </label>
      {#if reviewForm.action !== 'dismiss'}
        <label>Amount (leave empty for the full amount)
          <input bind:value={reviewForm.amount} inputmode="decimal" />
        </label>
      {/if}
      <label>Note {reviewForm.action === 'dismiss' ? '' : '(optional)'}
        <textarea bind:value={reviewForm.note} rows="3" maxlength="1000"></textarea>
      </label>
      {#if reviewError}<div class="ba-error">{reviewError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (reviewing = null)}>Cancel</Button>
      <Button on:click={submitReview} disabled={working}>{working ? 'Working…' : 'Resolve'}</Button>
    </svelte:fragment>
  </Modal>
{/if}
