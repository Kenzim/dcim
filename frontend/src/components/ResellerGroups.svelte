<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Button, Modal, FormGroup, FormError } from './ui/index.js';
  import {
    createResellerGroup,
    deleteResellerGroup,
    deleteResellerGroupAccess,
    deleteResellerGroupPrice,
    listResellerBasePrices,
    listResellerGroupAccess,
    listResellerGroupPrices,
    listResellerGroups,
    setResellerGroupAccess,
    updateResellerGroup,
    upsertResellerBasePrice,
    upsertResellerGroupPrice,
  } from '../lib/api.js';

  let groups = [];
  let basePrices = [];
  let groupPrices = [];
  let groupAccess = [];
  let selectedGroupId = '';
  let loading = true;
  let error = '';
  let saving = {};
  let baseEdits = {};
  let overrideEdits = {};
  let showGroupModal = false;
  let editingGroup = null;
  let groupForm = { name: '', code: '', description: '', enabled: true };
  let formError = '';

  $: selectedGroup = groups.find((group) => group.id === Number(selectedGroupId));

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      [groups, basePrices] = await Promise.all([
        listResellerGroups(),
        listResellerBasePrices(),
      ]);
      baseEdits = Object.fromEntries(basePrices.map((row) => [
        row.product_id,
        {
          setup_cents: row.setup_cents ?? 0,
          monthly_cents: row.monthly_cents ?? 0,
        },
      ]));
      if (selectedGroupId && groups.some((group) => group.id === Number(selectedGroupId))) {
        await loadMatrix();
      } else if (groups.length) {
        selectedGroupId = String(groups[0].id);
        await loadMatrix();
      }
    } catch (err) {
      error = err.message || String(err);
    } finally {
      loading = false;
    }
  }

  async function loadMatrix() {
    if (!selectedGroupId) {
      groupPrices = [];
      groupAccess = [];
      return;
    }
    try {
      [groupPrices, groupAccess] = await Promise.all([
        listResellerGroupPrices(selectedGroupId),
        listResellerGroupAccess(selectedGroupId),
      ]);
      overrideEdits = Object.fromEntries(groupPrices.map((row) => [
        row.product_id,
        {
          setup_cents: row.override_setup_cents ?? '',
          monthly_cents: row.override_monthly_cents ?? '',
        },
      ]));
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function openGroupModal(group = null) {
    editingGroup = group;
    groupForm = group
      ? {
          name: group.name,
          code: group.code,
          description: group.description || '',
          enabled: group.enabled,
        }
      : { name: '', code: '', description: '', enabled: true };
    formError = '';
    showGroupModal = true;
  }

  async function saveGroup() {
    formError = '';
    try {
      if (editingGroup) {
        await updateResellerGroup(editingGroup.id, groupForm);
      } else {
        const created = await createResellerGroup(groupForm);
        selectedGroupId = String(created.id);
      }
      showGroupModal = false;
      await load();
    } catch (err) {
      formError = err.message || String(err);
    }
  }

  async function removeGroup(group) {
    if (!confirm(`Delete reseller group "${group.name}"?`)) return;
    try {
      await deleteResellerGroup(group.id);
      if (Number(selectedGroupId) === group.id) selectedGroupId = '';
      await load();
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function cents(value, nullable = false) {
    if (nullable && (value === '' || value === null || value === undefined)) return null;
    const result = Number(value);
    if (!Number.isInteger(result) || result < 0) throw new Error('Prices must be nonnegative integer cents');
    return result;
  }

  async function saveBasePrices() {
    saving = { ...saving, base: true };
    error = '';
    try {
      for (const row of basePrices) {
        const edit = baseEdits[row.product_id];
        await upsertResellerBasePrice(row.product_id, {
          setup_cents: cents(edit.setup_cents),
          monthly_cents: cents(edit.monthly_cents),
          currency: 'USD',
        });
      }
      await load();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      saving = { ...saving, base: false };
    }
  }

  async function saveOverride(row) {
    saving[`override-${row.product_id}`] = true;
    saving = saving;
    try {
      const edit = overrideEdits[row.product_id];
      await upsertResellerGroupPrice(selectedGroupId, row.product_id, {
        setup_cents: cents(edit.setup_cents, true),
        monthly_cents: cents(edit.monthly_cents, true),
      });
      await loadMatrix();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      saving[`override-${row.product_id}`] = false;
      saving = saving;
    }
  }

  async function clearOverride(row) {
    try {
      await deleteResellerGroupPrice(selectedGroupId, row.product_id);
      await loadMatrix();
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function accessFor(productId) {
    return groupAccess.find((row) => row.product_id === productId);
  }

  async function changeGroupAccess(productId, value) {
    try {
      const row = accessFor(productId);
      if (value === 'default') {
        if (row?.group_rule) await deleteResellerGroupAccess(selectedGroupId, productId);
      } else {
        await setResellerGroupAccess(selectedGroupId, productId, value === 'allow');
      }
      groupAccess = await listResellerGroupAccess(selectedGroupId);
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function money(centsValue) {
    if (centsValue === null || centsValue === undefined) return 'Not configured';
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(centsValue / 100);
  }
</script>

<PageHeader title="Reseller Groups & Pricing" />

<div class="page">
  {#if error}<div class="alert alert-danger">{error}</div>{/if}
  <div class="section-title">
    <div><h2>Groups</h2><p>Group defaults are inherited by member resellers unless a direct rule overrides them.</p></div>
    <Button on:click={() => openGroupModal()}>New group</Button>
  </div>

  {#if loading}
    <div class="state">Loading reseller pricing…</div>
  {:else}
    <div class="group-grid">
      {#each groups as group}
        <article class:selected={group.id === Number(selectedGroupId)}>
          <div class="group-heading"><strong>{group.name}</strong><span class:enabled={group.enabled} class:disabled={!group.enabled}>{group.enabled ? 'Enabled' : 'Disabled'}</span></div>
          <code>{group.code}</code>
          <p>{group.description || 'No description'}</p>
          <div class="counts">{group.member_count} members · {group.product_price_count} price overrides · {group.product_access_count} access rules</div>
          <div class="actions">
            <Button size="small" variant="secondary" on:click={() => { selectedGroupId = String(group.id); loadMatrix(); }}>{group.id === Number(selectedGroupId) ? 'Selected' : 'Select'}</Button>
            <Button size="small" variant="secondary" on:click={() => openGroupModal(group)}>Edit</Button>
            <Button size="small" variant="danger" on:click={() => removeGroup(group)}>Delete</Button>
          </div>
        </article>
      {:else}
        <div class="state">No reseller groups have been created.</div>
      {/each}
    </div>

    <section class="panel">
      <div class="section-title compact">
        <div><h2>Base product prices</h2><p>All amounts are integer cents in USD. Products without a configured base price cannot be sold.</p></div>
        <Button disabled={!basePrices.length || saving.base} on:click={saveBasePrices}>
          {saving.base ? 'Saving…' : 'Save prices'}
        </Button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Product</th><th>Setup cents</th><th>Monthly cents</th><th>Current</th></tr></thead>
          <tbody>
            {#each basePrices as row}
              <tr>
                <td><strong>{row.product.name}</strong><div class="muted">{row.product.code}</div></td>
                <td><input class="cents" type="number" min="0" step="1" bind:value={baseEdits[row.product_id].setup_cents} /></td>
                <td><input class="cents" type="number" min="0" step="1" bind:value={baseEdits[row.product_id].monthly_cents} /></td>
                <td>{row.configured ? `${money(row.setup_cents)} / ${money(row.monthly_cents)} monthly` : 'Not configured'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="section-title compact">
        <div>
          <h2>{selectedGroup ? `${selectedGroup.name} product matrix` : 'Group product matrix'}</h2>
          <p>Blank price fields inherit the base price. No access rule is explicit default-deny.</p>
        </div>
        <select bind:value={selectedGroupId} on:change={loadMatrix}>
          {#each groups as group}<option value={group.id}>{group.name}</option>{/each}
        </select>
      </div>
      {#if selectedGroup}
        <div class="table-wrap">
          <table class="matrix">
            <thead><tr><th>Product</th><th>Base setup / monthly</th><th>Setup override</th><th>Monthly override</th><th>Effective</th><th>Access default</th><th></th></tr></thead>
            <tbody>
              {#each groupPrices as row}
                <tr>
                  <td><strong>{row.product.name}</strong><div class="muted">{row.product.code}</div></td>
                  <td>{money(row.base_setup_cents)} / {money(row.base_monthly_cents)}</td>
                  <td><input class="cents" type="number" min="0" step="1" placeholder="Inherit" bind:value={overrideEdits[row.product_id].setup_cents} /></td>
                  <td><input class="cents" type="number" min="0" step="1" placeholder="Inherit" bind:value={overrideEdits[row.product_id].monthly_cents} /></td>
                  <td>{money(row.effective_setup_cents)} / {money(row.effective_monthly_cents)}</td>
                  <td>
                    <select value={accessFor(row.product_id)?.group_rule ? (accessFor(row.product_id).group_rule.allowed ? 'allow' : 'deny') : 'default'} on:change={(event) => changeGroupAccess(row.product_id, event.currentTarget.value)}>
                      <option value="default">Default deny (no rule)</option>
                      <option value="allow">Allow</option>
                      <option value="deny">Deny</option>
                    </select>
                  </td>
                  <td class="row-actions">
                    <Button size="small" disabled={saving[`override-${row.product_id}`]} on:click={() => saveOverride(row)}>Save price</Button>
                    {#if row.has_override}<Button size="small" variant="secondary" on:click={() => clearOverride(row)}>Clear</Button>{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <div class="state">Create or select a group to edit its product defaults.</div>
      {/if}
    </section>
  {/if}
</div>

{#if showGroupModal}
  <Modal title={editingGroup ? 'Edit reseller group' : 'Create reseller group'} onClose={() => (showGroupModal = false)}>
    {#if formError}<FormError>{formError}</FormError>{/if}
    <form id="reseller-group-form" on:submit|preventDefault={saveGroup}>
      <FormGroup label="Name" forId="group-name" required><input id="group-name" bind:value={groupForm.name} required /></FormGroup>
      <FormGroup label="Code" forId="group-code" required><input id="group-code" bind:value={groupForm.code} required /></FormGroup>
      <FormGroup label="Description" forId="group-description"><textarea id="group-description" rows="3" bind:value={groupForm.description}></textarea></FormGroup>
      <label class="enabled-check"><input type="checkbox" bind:checked={groupForm.enabled} /> Enabled</label>
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (showGroupModal = false)}>Cancel</Button>
      <Button type="submit" form="reseller-group-form">{editingGroup ? 'Save group' : 'Create group'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 32px; color: var(--text-primary); }
  .section-title { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
  .section-title h2 { margin: 0 0 4px; font-size: 20px; }
  .section-title p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .section-title.compact { margin-bottom: 14px; }
  .group-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr)); gap: 14px; margin-bottom: 22px; }
  .group-grid article { padding: 16px; border: 1px solid var(--border-color); border-radius: 11px; background: var(--bg-primary); cursor: pointer; }
  .group-grid article.selected { border-color: var(--accent-color); box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent-color) 20%, transparent); }
  .group-heading { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 6px; }
  .group-heading span { border-radius: 999px; padding: 2px 8px; font-size: 11px; }
  .group-heading .enabled { color: var(--success-text); background: var(--success-bg); }
  .group-heading .disabled { color: var(--danger-text); background: var(--danger-bg); }
  .group-grid p, .counts { color: var(--text-secondary); font-size: 12px; }
  .counts { min-height: 32px; }
  .actions, .row-actions { display: flex; flex-wrap: wrap; gap: 7px; }
  .panel { padding: 18px; margin-bottom: 20px; border: 1px solid var(--border-color); border-radius: 12px; background: var(--bg-primary); }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: 9px; }
  table { width: 100%; min-width: 850px; border-collapse: collapse; }
  table.matrix { min-width: 1150px; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); vertical-align: middle; }
  th { background: var(--bg-tertiary); color: var(--text-secondary); text-transform: uppercase; font-size: 11px; }
  tbody tr:last-child td { border-bottom: 0; }
  input, select, textarea {
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-primary);
    background-color: var(--bg-secondary);
  }
  input.cents { width: 125px; }
  .muted { color: var(--text-secondary); font-size: 12px; margin-top: 3px; }
  .state { padding: 36px; text-align: center; color: var(--text-secondary); }
  .enabled-check { display: flex; align-items: center; gap: 8px; color: var(--text-primary); }
  @media (max-width: 768px) {
    .page { padding: 16px; }
    .section-title { align-items: stretch; flex-direction: column; }
  }
</style>
