<script>
  import { onMount } from 'svelte';
  import { navigate } from '../../lib/router.js';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import {
    clientCommerceCheckout,
    clientCommerceGetProduct,
    clientCommerceListProducts,
    clientCommerceQuote,
  } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  export let impersonating = false;

  let step = 'browse';
  let products = [];
  let product = null;
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let quote = null;
  let couponCode = '';
  let termsAccepted = false;
  let confirmOpen = false;
  let config = {
    price_plan_id: '',
    cycle_interval: '',
    options: {},
  };

  onMount(loadProducts);

  async function loadProducts() {
    loading = true;
    error = '';
    try {
      products = await clientCommerceListProducts();
    } catch (err) {
      if (err.status === 503) {
        products = [];
        error = '';
      } else {
        error = err.message || 'Store is unavailable';
        products = [];
      }
    } finally {
      loading = false;
    }
  }

  async function selectProduct(id) {
    working = true;
    error = '';
    quote = null;
    try {
      product = await clientCommerceGetProduct(id);
      config = {
        price_plan_id: product.price_plans?.[0]?.id ? String(product.price_plans[0].id) : '',
        cycle_interval: product.price_plans?.[0]?.cycles?.[0]?.interval || '',
        options: {},
      };
      step = 'configure';
    } catch (err) {
      error = err.message || 'Failed to load product';
    } finally {
      working = false;
    }
  }

  $: selectedPlan = product?.price_plans?.find((p) => String(p.id) === config.price_plan_id);

  async function runQuote() {
    if (!product) return;
    working = true;
    error = '';
    try {
      quote = await clientCommerceQuote({
        frontend_product_id: product.id,
        price_plan_id: Number(config.price_plan_id),
        cycle_interval: config.cycle_interval || null,
        options: config.options,
        coupon_code: couponCode.trim() || null,
      });
      step = 'checkout';
    } catch (err) {
      error = err.message || 'Quote failed';
    } finally {
      working = false;
    }
  }

  function openConfirm() {
    if (!termsAccepted) {
      error = 'Accept the terms of service to continue.';
      return;
    }
    if (impersonating) {
      error = 'Checkout is disabled while viewing as this client.';
      return;
    }
    confirmOpen = true;
  }

  async function placeOrder() {
    if (!product || !quote) return;
    working = true;
    error = '';
    try {
      const order = await clientCommerceCheckout({
        frontend_product_id: product.id,
        price_plan_id: Number(config.price_plan_id),
        cycle_interval: config.cycle_interval || null,
        options: config.options,
        coupon_code: couponCode.trim() || null,
        checkout_nonce: crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        terms_version: '1',
      });
      success = `Order #${order.order_number} placed.`;
      confirmOpen = false;
      navigate(order.invoice_id ? `/client/billing/${order.invoice_id}` : '/client/billing');
    } catch (err) {
      error = err.message || 'Checkout failed';
      confirmOpen = false;
    } finally {
      working = false;
    }
  }
</script>

<section aria-labelledby="checkout-title">
  <div class="page-head">
    <div>
      <h1 id="checkout-title">Order a service</h1>
      <p class="sub">Browse products, configure options, and checkout.</p>
    </div>
    {#if step !== 'browse'}
      <Button variant="secondary" on:click={() => { step = 'browse'; product = null; quote = null; }}>Back to catalog</Button>
    {/if}
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading catalog…</span></div>
  {:else if step === 'browse'}
    {#if products.length === 0}
      <div class="empty">
        <h2>Store unavailable</h2>
        <p>No products are published yet. Check back later or contact support.</p>
      </div>
    {:else}
      <div class="grid">
        {#each products as row (row.id)}
          <article class="card">
            <h2>{row.name}</h2>
            <p>{row.short_description || 'No description'}</p>
            <Button on:click={() => selectProduct(row.id)} disabled={working}>Configure</Button>
          </article>
        {/each}
      </div>
    {/if}
  {:else if step === 'configure' && product}
    <div class="panel">
      <h2>{product.name}</h2>
      {#if product.description_html}<div class="desc">{@html product.description_html}</div>{/if}

      <label>Plan
        <select bind:value={config.price_plan_id} on:change={() => {
          const plan = product.price_plans.find((p) => String(p.id) === config.price_plan_id);
          config.cycle_interval = plan?.cycles?.[0]?.interval || '';
        }}>
          {#each product.price_plans || [] as plan (plan.id)}
            <option value={String(plan.id)}>{plan.name} ({plan.currency})</option>
          {/each}
        </select>
      </label>

      {#if selectedPlan?.cycles?.length}
        <label>Billing cycle
          <select bind:value={config.cycle_interval}>
            {#each selectedPlan.cycles as cycle (cycle.id)}
              <option value={cycle.interval}>{cycle.interval} — {formatMoney(cycle.price_cents, selectedPlan.currency)}</option>
            {/each}
          </select>
        </label>
      {/if}

      {#each product.options || [] as opt (opt.id)}
        <label>{opt.name}{opt.required ? ' *' : ''}
          <select
            on:change={(e) => {
              config.options = { ...config.options, [opt.code]: e.target.value };
            }}
          >
            <option value="">Select…</option>
            {#each opt.values as val (val.id)}
              <option value={val.code}>{val.name}{val.price_delta_cents ? ` (+${formatMoney(val.price_delta_cents, selectedPlan?.currency || 'USD')})` : ''}</option>
            {/each}
          </select>
        </label>
      {/each}

      <label>Coupon <input bind:value={couponCode} placeholder="Optional" /></label>
      <Button on:click={runQuote} disabled={working || !config.price_plan_id}>Get quote</Button>
    </div>
  {:else if step === 'checkout' && quote}
    <div class="panel">
      <h2>Review & checkout</h2>
      <ul class="lines">
        {#each quote.lines || [] as line}
          <li><span>{line.description}</span><strong>{formatMoney(line.total_cents, quote.currency)}</strong></li>
        {/each}
      </ul>
      <dl>
        <div><dt>Subtotal</dt><dd>{formatMoney(quote.subtotal_cents, quote.currency)}</dd></div>
        <div><dt>Tax</dt><dd>{formatMoney(quote.tax_cents, quote.currency)}</dd></div>
        <div><dt>Discount</dt><dd>{formatMoney(quote.discount_cents, quote.currency)}</dd></div>
        <div><dt>Total due now</dt><dd>{formatMoney(quote.total_cents, quote.currency)}</dd></div>
      </dl>
      <label class="check"><input type="checkbox" bind:checked={termsAccepted} /> I agree to the terms of service</label>
      <Button on:click={openConfirm} disabled={working || impersonating}>Place order</Button>
      {#if impersonating}<p class="hint">Checkout disabled during impersonation.</p>{/if}
    </div>
  {/if}
</section>

{#if confirmOpen && quote}
  <Modal title="Confirm order" onClose={() => (confirmOpen = false)}>
    <p>You will be charged <strong>{formatMoney(quote.total_cents, quote.currency)}</strong> for this order.</p>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (confirmOpen = false)}>Cancel</Button>
      <Button on:click={placeOrder} disabled={working}>{working ? 'Processing…' : 'Confirm & pay'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

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
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .card, .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 20px; box-shadow: var(--shadow-sm); }
  .card h2, .panel h2 { margin: 0 0 8px; font-size: 18px; }
  .card p { margin: 0 0 14px; color: var(--text-secondary); font-size: 14px; min-height: 40px; }
  .desc { margin-bottom: 16px; font-size: 14px; line-height: 1.5; color: var(--text-secondary); }
  label { display: grid; gap: 6px; margin-bottom: 14px; font-size: 13px; font-weight: 600; }
  input, select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .lines { list-style: none; margin: 0 0 16px; padding: 0; }
  .lines li { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border-color); }
  dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 0 0 16px; }
  dl div { padding: 10px; border-radius: 8px; background: var(--bg-secondary); }
  dt { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; }
  dd { margin: 4px 0 0; font-weight: 700; }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .hint { font-size: 12px; color: var(--text-tertiary); margin-top: 8px; }
  .empty { text-align: center; padding: 48px 24px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); }
  .empty h2 { margin: 0 0 8px; }
  .empty p { margin: 0; color: var(--text-secondary); }
</style>
