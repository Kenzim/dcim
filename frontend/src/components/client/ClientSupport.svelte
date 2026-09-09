<script>
  import { onMount } from 'svelte';
  import { navigate } from '../../lib/router.js';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import {
    clientCommerceCreateTicket,
    clientCommerceGetTicket,
    clientCommerceListTickets,
    clientCommerceReplyTicket,
  } from '../../lib/api.js';

  /** Ticket ID from route, if any. */
  export let ticketId = null;

  const DEPARTMENTS = [
    { id: 1, name: 'Sales' },
    { id: 2, name: 'Billing' },
    { id: 3, name: 'Technical' },
  ];

  let tickets = [];
  let selected = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let showCreate = false;
  let replyText = '';
  let createForm = { department_id: '3', subject: '', body_text: '', priority: 'medium' };

  onMount(async () => {
    await load();
    if (ticketId) await openTicket(Number(ticketId));
  });

  async function load() {
    loading = true;
    error = '';
    try {
      tickets = await clientCommerceListTickets();
    } catch (err) {
      if (err.status === 503) tickets = [];
      else error = err.message || 'Failed to load tickets';
    } finally {
      loading = false;
    }
  }

  async function openTicket(id) {
    working = true;
    showCreate = false;
    replyText = '';
    try {
      selected = await clientCommerceGetTicket(id);
      navigate(`/client/support/${id}`);
    } catch (err) {
      error = err.message || 'Failed to load ticket';
    } finally {
      working = false;
    }
  }

  async function createTicket() {
    working = true;
    error = '';
    success = '';
    try {
      const ticket = await clientCommerceCreateTicket({
        department_id: Number(createForm.department_id),
        subject: createForm.subject.trim(),
        body_text: createForm.body_text.trim(),
        priority: createForm.priority,
      });
      showCreate = false;
      createForm = { department_id: '3', subject: '', body_text: '', priority: 'medium' };
      success = 'Ticket created.';
      await load();
      await openTicket(ticket.id);
    } catch (err) {
      error = err.message || 'Failed to create ticket';
    } finally {
      working = false;
    }
  }

  async function sendReply() {
    if (!selected || !replyText.trim()) return;
    working = true;
    error = '';
    try {
      await clientCommerceReplyTicket(selected.id, replyText.trim());
      replyText = '';
      selected = await clientCommerceGetTicket(selected.id);
      success = 'Message sent.';
      await load();
    } catch (err) {
      error = err.message || 'Failed to send message';
    } finally {
      working = false;
    }
  }

  function backToList() {
    selected = null;
    navigate('/client/support');
  }
</script>

<section aria-labelledby="support-title">
  <div class="page-head">
    <div>
      <h1 id="support-title">Support</h1>
      <p class="sub">Open tickets and follow up with our team.</p>
    </div>
    <Button on:click={() => { showCreate = true; selected = null; }}>New ticket</Button>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if showCreate}
    <div class="panel">
      <h2>Create ticket</h2>
      <label>Department
        <select bind:value={createForm.department_id}>
          {#each DEPARTMENTS as dept (dept.id)}<option value={String(dept.id)}>{dept.name}</option>{/each}
        </select>
      </label>
      <label>Subject <input bind:value={createForm.subject} required /></label>
      <label>Message <textarea bind:value={createForm.body_text} rows="6" required></textarea></label>
      <label>Priority
        <select bind:value={createForm.priority}>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </label>
      <div class="actions">
        <Button variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
        <Button on:click={createTicket} disabled={working || !createForm.subject.trim() || !createForm.body_text.trim()}>Submit</Button>
      </div>
    </div>
  {:else}
    <div class="layout">
      <div class="panel list">
        {#if tickets.length === 0}
          <p class="empty">No tickets yet. Need help? Open a new ticket.</p>
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
          <p class="empty">Select a ticket to view the conversation.</p>
        {:else}
          <div class="head">
            <div><h2>#{selected.ticket_number} · {selected.subject}</h2><p>{selected.department_name || 'Support'}</p></div>
            <button class="link" on:click={backToList}>Back</button>
          </div>
          <div class="thread">
            {#each selected.messages || [] as msg (msg.id)}
              <div class="msg">
                <time>{new Date(msg.created_at).toLocaleString()}</time>
                <p>{msg.body_text}</p>
              </div>
            {/each}
          </div>
          {#if selected.status !== 'closed'}
            <div class="reply">
              <textarea bind:value={replyText} rows="4" placeholder="Write a reply…"></textarea>
              <Button on:click={sendReply} disabled={working || !replyText.trim()}>Send reply</Button>
            </div>
          {:else}
            <p class="closed">This ticket is closed.</p>
          {/if}
        {/if}
      </article>
    </div>
  {/if}
</section>

<style>
  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-head h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 14px;
    color: var(--text-tertiary);
  }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(260px, .8fr) minmax(0, 1.2fr); gap: 18px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); box-shadow: var(--shadow-sm); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
  .list > button.selected, .list > button:hover { background: var(--portal-accent-soft); }
  .list small { display: block; margin-top: 3px; color: var(--text-tertiary); }
  .meta { font-size: 12px; text-transform: capitalize; color: var(--text-tertiary); }
  .detail { padding: 20px; }
  .panel:not(.list):not(.detail) { padding: 20px; }
  .head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
  .head h2 { margin: 0 0 4px; font-size: 18px; }
  .head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .link { background: none; border: none; color: var(--portal-accent); cursor: pointer; font-weight: 600; }
  .thread { display: grid; gap: 10px; margin-bottom: 14px; max-height: 360px; overflow: auto; }
  .msg { padding: 12px; border-radius: 8px; background: var(--bg-secondary); }
  .msg time { display: block; font-size: 11px; color: var(--text-tertiary); margin-bottom: 6px; }
  .msg p { margin: 0; white-space: pre-wrap; font-size: 14px; line-height: 1.45; }
  .reply textarea { width: 100%; padding: 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); margin-bottom: 10px; }
  label { display: grid; gap: 6px; margin-bottom: 14px; font-size: 13px; font-weight: 600; }
  input, select, textarea { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .actions { display: flex; gap: 10px; }
  .empty, .closed { padding: 24px; color: var(--text-tertiary); text-align: center; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } }
</style>
