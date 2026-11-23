<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import * as api from '../../lib/vmFirewallApi.js';
  import RuleEditor from './RuleEditor.svelte';
  import { fieldErrorMap, payloadOf, toDraft } from './ruleModel.js';

  /** @type {'staff' | 'customer'} */
  export let audience = 'customer';

  $: staff = audience === 'staff';

  let groups = [];
  let ipsets = [];
  let loading = true;
  let loadError = '';
  let busy = false;
  let error = '';
  let success = '';
  let fieldErrors = {};
  // null: list only; otherwise the group being edited ({id: null} for a new one)
  let form = null;
  let detachOffer = null;

  const blankForm = () => ({ id: null, revision: undefined, name: '', description: '', client_selectable: false, rules: [] });

  async function load() {
    try {
      [groups, ipsets] = await Promise.all([api.listGroups(audience), api.listIpsets(audience).catch(() => [])]);
      loadError = '';
    } catch (err) {
      loadError = err.message || 'Failed to load the security groups';
    } finally {
      loading = false;
    }
  }

  function reset() {
    error = '';
    success = '';
    fieldErrors = {};
    detachOffer = null;
  }

  const startNew = () => {
    reset();
    form = blankForm();
  };

  async function startEdit(group) {
    reset();
    busy = true;
    try {
      const full = await api.getGroup(audience, group.id);
      form = {
        id: full.id,
        revision: full.revision,
        name: full.name,
        description: full.description || '',
        client_selectable: !!full.client_selectable,
        rules: (full.rules || []).map(toDraft),
      };
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  async function save() {
    reset();
    busy = true;
    const body = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      rules: payloadOf(form.rules),
    };
    if (staff) body.client_selectable = form.client_selectable;
    try {
      if (form.id == null) await api.createGroup(audience, body);
      else await api.updateGroup(audience, form.id, { ...body, revision: form.revision });
      success = form.id == null ? 'Security group created.' : 'Security group saved.';
      form = null;
      await load();
    } catch (err) {
      if (err.code === 'firewall_changed') {
        error = 'Someone else changed this group. Close it and open it again to see the latest version.';
      } else {
        error = err.message;
        fieldErrors = fieldErrorMap(err);
      }
    } finally {
      busy = false;
    }
  }

  async function remove(group, detach = false) {
    if (!detach && !confirm(`Delete the security group "${group.name}"?`)) return;
    reset();
    busy = true;
    try {
      await api.deleteGroup(audience, group.id, { detach });
      success = 'Security group deleted.';
      await load();
    } catch (err) {
      if (err.status === 409 && !detach) detachOffer = { group, message: err.message };
      else error = err.message;
    } finally {
      busy = false;
    }
  }

  const errorAt = (field) => fieldErrors[field] || '';

  onMount(load);
</script>

<section class="panel">
  <header class="head">
    <div>
      <h3>{staff ? 'Admin security groups' : 'Security groups'}</h3>
      <p class="hint">
        A security group is a named list of rules you can add to any {staff ? 'server' : 'of your servers'} as one entry.
        {#if staff}Groups marked "customers may use" can be attached by customers to their own servers.{/if}
      </p>
    </div>
    {#if !form}<Button size="small" disabled={busy || loading} on:click={startNew}>New security group</Button>{/if}
  </header>

  {#if error}<Alert type="error">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if detachOffer}
    <Alert type="warning">
      {detachOffer.message}
      <Button size="small" variant="danger" disabled={busy} on:click={() => remove(detachOffer.group, true)}>Detach it from servers and delete</Button>
    </Alert>
  {/if}

  {#if loading}
    <div class="loading"><Spinner /><span>Loading…</span></div>
  {:else if loadError}
    <Alert type="error">{loadError}</Alert>
  {:else if form}
    <div class="form">
      <div class="grid">
        <label>Name
          <input type="text" bind:value={form.name} maxlength="48" disabled={busy} />
          {#if errorAt('name')}<span class="field-error">{errorAt('name')}</span>{/if}
        </label>
        <label>Description
          <input type="text" bind:value={form.description} maxlength="255" disabled={busy} />
          {#if errorAt('description')}<span class="field-error">{errorAt('description')}</span>{/if}
        </label>
        {#if staff}
          <label class="check"><input type="checkbox" bind:checked={form.client_selectable} disabled={busy} /> Customers may use this group</label>
        {/if}
      </div>
      <RuleEditor
        bind:rules={form.rules}
        errors={fieldErrors}
        {ipsets}
        {staff}
        allowGroups={false}
        allowLog={staff}
        disabled={busy}
        emptyText="This group has no rules yet."
      />
      <div class="bar">
        <Button disabled={busy} on:click={save}>{busy ? 'Saving…' : 'Save group'}</Button>
        <Button variant="secondary" disabled={busy} on:click={() => ((form = null), reset())}>Cancel</Button>
      </div>
    </div>
  {:else if groups.length === 0}
    <p class="hint">No security groups yet.</p>
  {:else}
    <ul class="list">
      {#each groups as group (group.id)}
        <li>
          <div class="item-main">
            <strong>{group.name}</strong>
            {#if group.client_selectable}<span class="tag">customers may use</span>{/if}
            <span class="hint">{group.description || ''}</span>
            {#if group.rules}<span class="hint">{group.rules.length} {group.rules.length === 1 ? 'rule' : 'rules'}</span>{/if}
          </div>
          <div class="item-actions">
            <Button size="small" variant="secondary" disabled={busy} on:click={() => startEdit(group)}>Edit</Button>
            <Button size="small" variant="danger" disabled={busy} on:click={() => remove(group)}>Delete</Button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .panel { display: flex; flex-direction: column; gap: 12px; border: 1px solid var(--border-color); border-radius: 12px; padding: 16px 18px; background: var(--bg-primary); }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  h3 { margin: 0 0 4px; font-size: 16px; }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .loading { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
  .form { display: flex; flex-direction: column; gap: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px 14px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .check { flex-direction: row; align-items: center; gap: 8px; align-self: end; }
  input[type='text'] { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; }
  .bar { display: flex; gap: 10px; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--border-color); flex-wrap: wrap; }
  .list li:first-child { border-top: none; }
  .item-main { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .item-actions { display: flex; gap: 6px; }
  .tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--info-bg); color: var(--info-text); }
  .field-error { color: var(--danger-text); font-size: 12px; font-weight: 500; }
</style>
