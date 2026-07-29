<script>
  import { onMount } from 'svelte';
  import {
    listResellerPanelProducts,
    updateResellerProductClientPermissions,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';
  import { Alert, Spinner } from '../ui/index.js';

  let products = [];
  let loading = true;
  let savingId = null;
  let error = '';
  let success = '';

  onMount(async () => {
    try {
      products = await listResellerPanelProducts();
    } catch (err) {
      error = err.message || 'Products could not be loaded.';
    } finally {
      loading = false;
    }
  });

  function setPermission(product, key, checked) {
    product.client_permissions = { ...product.client_permissions, [key]: checked };
    products = [...products];
  }

  async function save(product) {
    savingId = product.id;
    error = '';
    success = '';
    try {
      const updated = await updateResellerProductClientPermissions(product.id, {
        visible: product.client_visible,
        permissions: product.client_permissions,
      });
      products = products.map((item) => item.id === product.id ? updated : item);
      success = `${product.name} client settings saved.`;
    } catch (err) {
      error = err.message || 'Product client settings could not be saved.';
    } finally {
      savingId = null;
    }
  }
</script>

<section aria-labelledby="products-title">
  <div class="heading"><h1 id="products-title">Products</h1><p>Effective wholesale pricing and the actions your downstream clients may use.</p></div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading products…</span></div>
  {:else if products.length === 0}
    <div class="empty">No sellable products are allocated to this reseller account.</div>
  {:else}
    <div class="product-grid">
      {#each products as product (product.id)}
        <article class="product">
          <header>
            <div><span class="type">{product.service_type?.replaceAll('_', ' ')}</span><h2>{product.name}</h2><code>{product.code}</code></div>
            <label class="visibility"><input type="checkbox" bind:checked={product.client_visible} /> Visible to clients</label>
          </header>
          <div class="prices">
            <div><span>Setup</span><strong>{formatMoney(product.setup_cents, product.currency)}</strong><small>{product.setup_price_source}</small></div>
            <div><span>Monthly</span><strong>{formatMoney(product.monthly_cents, product.currency)}</strong><small>{product.monthly_price_source}</small></div>
          </div>
          <div class="specs">
            <h3>Effective specifications</h3>
            {#if Object.keys(product.effective_specs || {}).length}
              <dl>{#each Object.entries(product.effective_specs) as [key, value]}<div><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>{/each}</dl>
            {:else}<p>No resource specifications.</p>{/if}
          </div>
          <div class="permissions">
            <h3>Client permissions</h3>
            {#each product.permission_catalog as permission}
              <label class:blocked={!permission.rackflow_allowed}>
                <input
                  type="checkbox"
                  checked={!!product.client_permissions[permission.key]}
                  disabled={!permission.rackflow_allowed}
                  on:change={(event) => setPermission(product, permission.key, event.currentTarget.checked)}
                />
                <span>{permission.label}<small>{permission.rackflow_allowed ? permission.key : 'Blocked by Rackflow product policy'}</small></span>
              </label>
            {/each}
          </div>
          <button class="primary" on:click={() => save(product)} disabled={savingId === product.id}>
            {savingId === product.id ? 'Saving…' : 'Save client settings'}
          </button>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .heading { margin-bottom: 22px; }.heading h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }
  .empty { padding: 30px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); text-align: center; }.product-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; align-items: start; }
  .product { padding: 21px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }.product header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; }.product h2 { margin: 4px 0; font-size: 20px; }.type { color: var(--portal-accent); text-transform: uppercase; font-size: 10px; letter-spacing: .08em; font-weight: 800; }.product code { color: var(--text-tertiary); }
  .visibility { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 13px; white-space: nowrap; }.prices { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 18px 0; }.prices div { padding: 12px; border-radius: 9px; background: var(--bg-secondary); }.prices span, .prices small { display: block; color: var(--text-tertiary); font-size: 11px; text-transform: capitalize; }.prices strong { display: block; margin: 4px 0; font-size: 19px; }
  h3 { margin: 18px 0 10px; font-size: 14px; }.specs dl { display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; margin: 0; }.specs dl div { padding: 8px; border: 1px solid var(--border-color); border-radius: 7px; }.specs dt { color: var(--text-tertiary); font-size: 10px; text-transform: capitalize; }.specs dd { margin: 3px 0 0; font-weight: 700; overflow-wrap: anywhere; }.specs p { color: var(--text-tertiary); }
  .permissions { display: grid; gap: 8px; }.permissions h3 { margin-bottom: 2px; }.permissions label { display: flex; gap: 8px; align-items: flex-start; color: var(--text-primary); font-size: 13px; }.permissions label.blocked { opacity: .62; }.permissions small { display: block; margin-top: 2px; color: var(--text-tertiary); font-family: var(--font-mono); font-size: 10px; }
  .primary { margin-top: 18px; padding: 10px 15px; border: 0; border-radius: 8px; background: var(--portal-accent); color: var(--portal-accent-contrast); font-weight: 750; cursor: pointer; }.primary:disabled { opacity: .55; }
  @media (max-width: 900px) { .product-grid { grid-template-columns: 1fr; } }@media (max-width: 520px) { .product header { flex-direction: column; }.specs dl { grid-template-columns: repeat(2, 1fr); } }
</style>
