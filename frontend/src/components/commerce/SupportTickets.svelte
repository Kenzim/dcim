<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import {
    adminSupportGetTicket,
    adminSupportListTickets,
    adminSupportPatchTicket,
    adminSupportReplyTicket,
  } from '../../lib/api.js';

  let tickets = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let statusFilter = '';
  let replyText = '';
  let staffNote = false;

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      tickets = await adminSupportListTickets(statusFilter ? { status: statusFilter } : {});
    } catch (err) {
      error = err.message || 'Failed to load tickets';
      tickets = [];
    } finally {
      loading = false;
    }
  }

  async function openTicket(id) {
    working = true;
    replyText = '';
    staffNote = false;
    try {
      selected = await adminSupportGetTicket(id);
    } catch (err) {
      error = err.message || 'Failed to load ticket';
    } finally {
      working = false;
    }
  }

  async function sendReply() {
    if (!selected || !replyText.trim()) return;
    working = true;
    error = '';
    success = '';
    try {
      await adminSupportReplyTicket(selected.id, {
        body_text: replyText.trim(),
        is_staff_note: staffNote,
      });
      replyText = '';
      selected = await adminSupportGetTicket(selected.id);
      success = staffNote ? 'Staff note added.' : 'Reply sent.';
      await load();
    } catch (err) {
      error = err.message || 'Reply failed';
    } finally {
      working = false;
    }
  }

  async function setStatus(status) {
    if (!selected) return;
    working = true;
    try {
      selected = await adminSupportPatchTicket(selected.id, { status });
      success = `Ticket marked ${status}.`;
      await load();
    } catch (err) {
      error = err.message || 'Update failed';
    } finally {
      working = false;
    }
  }
</script>

<PageHeader title="Support Tickets" />

<div class="page">
  <div class="filters">
    <label>Status
      <select bind:value={statusFilter} on:change={load}>
        <option value="">All</option>
        <option value="open">Open</option>
        <option value="answered">Answered</option>
        <option value="customer_reply">Customer reply</option>
        <option value="closed">Closed</option>
      </select>
    </label>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    <div class="layout">
      <div class="panel list">
        {#if tickets.length === 0}
          <p class="empty">No tickets in this queue.</p>
        {:else}
          {#each tickets as row (row.id)}
            <button class:selected={selected?.id === row.id} on:click={() => openTicket(row.id)}>
              <span><strong>#{row.ticket_number}</strong><small>{row.subject}</small></span>
              <span class="meta">{row.status}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="panel detail">
        {#if working && !selected}<Spinner />{:else if !selected}
          <p class="empty">Select a ticket to view the thread.</p>
        {:else}
          <div class="head">
            <div>
              <h2>#{selected.ticket_number} · {selected.subject}</h2>
              <p>{selected.department_name || 'Support'} · {selected.username || `User #${selected.user_id}`}</p>
            </div>
            <span class="status">{selected.status}</span>
          </div>

          <div class="thread">
            {#each selected.messages || [] as msg (msg.id)}
              <div class="msg" class:staff={msg.is_staff_note}>
                <header>
                  <span>{msg.is_staff_note ? 'Staff note' : 'Message'}</span>
                  <time>{new Date(msg.created_at).toLocaleString()}</time>
                </header>
                <p>{msg.body_text}</p>
              </div>
            {/each}
          </div>

          <div class="reply">
            <textarea bind:value={replyText} rows="4" placeholder="Write a reply or staff note…"></textarea>
            <label class="check"><input type="checkbox" bind:checked={staffNote} /> Staff note (hidden from client)</label>
            <div class="actions">
              <Button on:click={sendReply} disabled={working || !replyText.trim()}>Send</Button>
              {#if selected.status !== 'closed'}
                <Button variant="secondary" on:click={() => setStatus('closed')} disabled={working}>Close</Button>
              {:else}
                <Button variant="secondary" on:click={() => setStatus('open')} disabled={working}>Reopen</Button>
              {/if}
            </div>
          </div>
        {/if}
      </article>
    </div>
  {/if}
</div>

<style>
  .page { padding: 28px 32px 36px; }
  .filters { margin-bottom: 16px; }
  .filters label { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
  select { margin-left: 8px; padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .state { min-height: 220px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(280px, .85fr) minmax(0, 1.15fr); gap: 18px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
  .list > button.selected, .list > button:hover { background: var(--portal-accent-soft); }
  .list small { display: block; margin-top: 3px; color: var(--text-tertiary); }
  .meta { font-size: 12px; text-transform: capitalize; color: var(--text-tertiary); }
  .detail { padding: 20px; }
  .head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .head h2 { margin: 0 0 4px; font-size: 18px; }
  .head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .status { padding: 5px 10px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); font-size: 12px; font-weight: 700; text-transform: capitalize; }
  .thread { display: grid; gap: 10px; margin-bottom: 16px; max-height: 360px; overflow: auto; }
  .msg { padding: 12px; border-radius: 8px; background: var(--bg-secondary); }
  .msg.staff { border-left: 3px solid var(--warning-color); }
  .msg header { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--text-tertiary); margin-bottom: 6px; }
  .msg p { margin: 0; white-space: pre-wrap; font-size: 14px; line-height: 1.45; }
  .reply textarea { width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); margin-bottom: 8px; }
  .check { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 10px; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
</style>
