<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import {
    adminStoreCreatePlanCycle,
    adminStoreCreatePricePlan,
    adminStoreCreateProduct,
    adminStoreDeletePlanCycle,
    adminStoreDeletePricePlan,
    adminStoreGetProduct,
    adminStoreListCategories,
    adminStoreListProducts,
    adminStoreUpdatePlanCycle,
    adminStoreUpdatePricePlan,
    adminStoreUpdateProduct,
    listCatalogProducts,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  let products = [];
  let categories = [];
  let catalogProducts = [];
  let loading = true;
  let saving = false;
  let error = '';
  let success = '';
  let selectedId = null;
  let detail = null;
  let showForm = false;
  let form = blankForm();
  let planForm = { name: 'Standard', currency: 'USD', pricing_model: 'recurring', setup_cents: 0, enabled: true };
  let cycleForm = { interval: 'monthly', price_cents: 0, enabled: true };

  function blankForm() {
    return {
      name: '',
      slug: '',
      short_description: '',
      description_md: '',
      category_id: '',
      product_id: '',
      service_type: 'vm',
      enabled: true,
      visibility: 'public',
      sort_order: 0,
      require_discord: false,
    };
  }

  onMount(async () => {
    try {
      [categories, catalogProducts] = await Promise.all([
        adminStoreListCategories({ enabled_only: true }).catch(() => []),
        listCatalogProducts().catch(() => []),
      ]);
    } catch (_) {
      categories = [];
      catalogProducts = [];
    }
    await loadList();
  });

  async function loadList() {
    loading = true;
    error = '';
    try {
      products = await adminStoreListProducts();
    } catch (err) {
      error = err.message || 'Failed to load products';
      products = [];
    } finally {
      loading = false;
    }
  }

  async function selectProduct(id) {
    selectedId = id;
    saving = false;
    try {
      detail = await adminStoreGetProduct(id);
    } catch (err) {
      error = err.message || 'Failed to load product';
      detail = null;
    }
  }

  function openCreate() {
    showForm = true;
    form = blankForm();
    detail = null;
    selectedId = null;
  }

  function openEditFromDetail() {
    if (!detail) return;
    form = {
      name: detail.name,
      slug: detail.slug,
      short_description: detail.short_description || '',
      description_md: detail.description_md || '',
      category_id: detail.category_id ? String(detail.category_id) : '',
      product_id: String(detail.product_id),
      service_type: detail.service_type,
      enabled: detail.enabled,
      visibility: detail.visibility,
      sort_order: detail.sort_order ?? 0,
      require_discord: detail.require_discord,
    };
    showForm = true;
  }

  async function saveProduct() {
    saving = true;
    error = '';
    success = '';
    const payload = {
      name: form.name.trim(),
      slug: form.slug.trim(),
      short_description: form.short_description.trim() || null,
      description_md: form.description_md,
      category_id: form.category_id ? Number(form.category_id) : null,
      product_id: Number(form.product_id),
      service_type: form.service_type,
      enabled: form.enabled,
      visibility: form.visibility,
      sort_order: Number(form.sort_order) || 0,
      require_discord: form.require_discord,
    };
    try {
      if (selectedId) {
        await adminStoreUpdateProduct(selectedId, payload);
        success = 'Product updated.';
      } else {
        const created = await adminStoreCreateProduct(payload);
        selectedId = created.id;
        success = 'Product created.';
      }
      showForm = false;
      await loadList();
      if (selectedId) await selectProduct(selectedId);
    } catch (err) {
      error = err.message || 'Save failed';
    } finally {
      saving = false;
    }
  }

  async function addPlan() {
    if (!selectedId) return;
    saving = true;
    error = '';
    try {
      await adminStoreCreatePricePlan(selectedId, {
        ...planForm,
        setup_cents: Math.round(Number(planForm.setup_cents) * 100) || 0,
      });
      await selectProduct(selectedId);
      success = 'Price plan added.';
    } catch (err) {
      error = err.message || 'Failed to add plan';
    } finally {
      saving = false;
    }
  }

  async function addCycle(planId) {
    saving = true;
    error = '';
    try {
      await adminStoreCreatePlanCycle(planId, {
        ...cycleForm,
        price_cents: Math.round(Number(cycleForm.price_cents) * 100) || 0,
      });
      await selectProduct(selectedId);
      success = 'Billing cycle added.';
    } catch (err) {
      error = err.message || 'Failed to add cycle';
    } finally {
      saving = false;
    }
  }

  async function removePlan(planId) {
    saving = true;
    try {
      await adminStoreDeletePricePlan(planId);
      await selectProduct(selectedId);
    } catch (err) {
      error = err.message || 'Failed to delete plan';
    } finally {
      saving = false;
    }
  }

  async function removeCycle(cycleId) {
    saving = true;
    try {
      await adminStoreDeletePlanCycle(cycleId);
      await selectProduct(selectedId);
    } catch (err) {
      error = err.message || 'Failed to delete cycle';
    } finally {
      saving = false;
    }
  }
</script>

<PageHeader title="Store Products">
  <svelte:fragment slot="actions">
      <Button on:click={openCreate}>Add product</Button>
    </svelte:fragment>
</PageHeader>

<div class="page">
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    <div class="layout">
      <div class="panel list">
        {#if products.length === 0}
          <p class="empty">No storefront products yet.</p>
        {:else}
          {#each products as row (row.id)}
            <button class:selected={selectedId === row.id} on:click={() => selectProduct(row.id)}>
              <span><strong>{row.name}</strong><small>{row.slug}</small></span>
              <span class="badge">{row.visibility}</span>
            </button>
          {/each}
        {/if}
      </div>

      <article class="panel detail">
        {#if !detail}
          <p class="empty">Select a product to edit plans, cycles, and description.</p>
        {:else}
          <div class="detail-head">
            <div>
              <h2>{detail.name}</h2>
              <p>{detail.short_description || 'No short description'}</p>
            </div>
            <Button variant="secondary" on:click={openEditFromDetail}>Edit details</Button>
          </div>

          <dl>
            <div><dt>Catalog product</dt><dd>#{detail.product_id}</dd></div>
            <div><dt>Service type</dt><dd>{detail.service_type}</dd></div>
            <div><dt>Visibility</dt><dd>{detail.visibility}</dd></div>
            <div><dt>Status</dt><dd>{detail.enabled ? 'Enabled' : 'Disabled'}</dd></div>
          </dl>

          <h3>Price plans</h3>
          {#each detail.price_plans || [] as plan (plan.id)}
            <div class="plan">
              <div class="plan-head">
                <strong>{plan.name}</strong>
                <span>{plan.pricing_model} · {plan.currency}</span>
                <Button variant="danger" on:click={() => removePlan(plan.id)}>Remove</Button>
              </div>
              <ul>
                {#each plan.cycles || [] as cycle (cycle.id)}
                  <li>
                    <span>{cycle.interval}</span>
                    <span>{formatMoney(cycle.price_cents, plan.currency)}</span>
                    <button class="link" on:click={() => removeCycle(cycle.id)}>Remove</button>
                  </li>
                {/each}
              </ul>
              <div class="inline-form">
                <select bind:value={cycleForm.interval}>
                  <option value="monthly">Monthly</option>
                  <option value="quarterly">Quarterly</option>
                  <option value="semiannually">Semi-annually</option>
                  <option value="annually">Annually</option>
                </select>
                <input type="number" step="0.01" bind:value={cycleForm.price_cents} placeholder="Price USD" />
                <Button on:click={() => addCycle(plan.id)} disabled={saving}>Add cycle</Button>
              </div>
            </div>
          {/each}

          <div class="inline-form plan-add">
            <input bind:value={planForm.name} placeholder="Plan name" />
            <select bind:value={planForm.pricing_model}>
              <option value="recurring">Recurring</option>
              <option value="one_time">One-time</option>
            </select>
            <input type="number" step="0.01" bind:value={planForm.setup_cents} placeholder="Setup USD" />
            <Button on:click={addPlan} disabled={saving || !selectedId}>Add plan</Button>
          </div>

          {#if detail.description_md}
            <h3>Description preview</h3>
            <div class="md-preview">{@html detail.description_html || detail.description_md}</div>
          {/if}
        {/if}
      </article>
    </div>
  {/if}
</div>

{#if showForm}
  <Modal title={selectedId ? 'Edit product' : 'New product'} size="large" onClose={() => (showForm = false)}>
    <div class="form-grid">
      <label>Name <input bind:value={form.name} required /></label>
      <label>Slug <input bind:value={form.slug} required /></label>
      <label>Category
        <select bind:value={form.category_id}>
          <option value="">None</option>
          {#each categories as cat (cat.id)}<option value={String(cat.id)}>{cat.name}</option>{/each}
        </select>
      </label>
      <label>Catalog product
        <select bind:value={form.product_id} required>
          <option value="">Select…</option>
          {#each catalogProducts as cp (cp.id)}<option value={String(cp.id)}>{cp.name} (#{cp.id})</option>{/each}
        </select>
      </label>
      <label>Service type
        <select bind:value={form.service_type}>
          <option value="vm">VM</option>
          <option value="bare_metal">Bare metal</option>
          <option value="http_proxy">HTTP proxy</option>
        </select>
      </label>
      <label>Visibility
        <select bind:value={form.visibility}>
          <option value="public">Public</option>
          <option value="private">Private (link only)</option>
          <option value="hidden">Hidden</option>
        </select>
      </label>
      <label class="full">Short description <input bind:value={form.short_description} /></label>
      <label class="full">Description (Markdown) <textarea bind:value={form.description_md} rows="8"></textarea></label>
      <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      <label class="check"><input type="checkbox" bind:checked={form.require_discord} /> Require Discord</label>
    </div>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (showForm = false)}>Cancel</Button>
      <Button on:click={saveProduct} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 28px 32px 36px; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(260px, .75fr) minmax(0, 1.25fr); gap: 18px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }
  .list > button { width: 100%; display: flex; justify-content: space-between; gap: 10px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--border-color); background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }
  .list > button.selected, .list > button:hover { background: var(--admin-sidebar-active-bg, var(--portal-accent-soft)); }
  .list small { display: block; color: var(--text-tertiary); margin-top: 3px; }
  .badge { font-size: 11px; text-transform: capitalize; padding: 3px 8px; border-radius: 999px; background: var(--bg-secondary); }
  .detail { padding: 20px; }
  .detail-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .detail-head h2 { margin: 0 0 4px; }
  .detail-head p { margin: 0; color: var(--text-secondary); font-size: 14px; }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0 0 20px; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; }
  h3 { margin: 18px 0 10px; font-size: 15px; }
  .plan { border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
  .plan-head { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
  ul { list-style: none; margin: 0 0 10px; padding: 0; }
  li { display: flex; gap: 12px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
  .inline-form { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .inline-form input, .inline-form select { padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .plan-add { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-color); }
  .empty { padding: 24px; color: var(--text-tertiary); text-align: center; }
  .md-preview { padding: 12px; border-radius: 8px; background: var(--bg-secondary); font-size: 14px; line-height: 1.5; }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-grid label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  .form-grid .full { grid-column: 1 / -1; }
  .form-grid input, .form-grid select, .form-grid textarea { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .link { background: none; border: none; color: var(--danger-color); cursor: pointer; font-size: 12px; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
</style>
