<script>
  import { onMount } from 'svelte';
  import {
    completeResellerPayPalSetup,
    createResellerPayPalSetup,
    deleteResellerPanelPaymentMethod,
    getResellerDashboard,
    getResellerPaymentConfig,
    listResellerPanelPaymentMethods,
    reorderResellerPanelPaymentMethods,
    updateResellerChargePreference,
  } from '../../lib/api.js';
  import { Alert, Spinner } from '../ui/index.js';
  import StripeCardForm from './StripeCardForm.svelte';

  let methods = [];
  let config;
  let preference = 'credit_first';
  let loading = true;
  let saving = false;
  let error = '';
  let success = '';

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      const params = new URLSearchParams(window.location.search);
      const paypalToken = params.get('setup_token') || params.get('token');
      if (paypalToken) {
        window.history.replaceState({}, '', window.location.pathname);
        await completeResellerPayPalSetup(paypalToken);
        success = 'PayPal account added.';
      }
      [methods, config] = await Promise.all([
        listResellerPanelPaymentMethods(),
        getResellerPaymentConfig(),
      ]);
      const dashboard = await getResellerDashboard();
      preference = dashboard.charge_preference;
    } catch (err) {
      error = err.message || 'Payment methods could not be loaded.';
    } finally {
      loading = false;
    }
  }

  function methodName(method) {
    if (method.label) return method.label;
    if (method.provider === 'stripe') return `${method.brand || 'Card'} •••• ${method.last4 || '----'}`;
    return method.brand === 'paypal' ? 'PayPal account' : method.method_type;
  }

  async function startPayPal() {
    saving = true;
    error = '';
    try {
      const setup = await createResellerPayPalSetup();
      window.location.assign(setup.approval_url);
    } catch (err) {
      error = err.message || 'PayPal setup could not be started.';
      saving = false;
    }
  }

  async function remove(method) {
    if (!window.confirm(`Remove ${methodName(method)}?`)) return;
    try {
      await deleteResellerPanelPaymentMethod(method.id);
      methods = methods.filter((item) => item.id !== method.id);
    } catch (err) {
      error = err.message || 'Payment method could not be removed.';
    }
  }

  async function move(index, direction) {
    const target = index + direction;
    if (target < 0 || target >= methods.length) return;
    const reordered = [...methods];
    [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
    saving = true;
    try {
      methods = await reorderResellerPanelPaymentMethods(
        reordered.map((method, tier) => ({ method_id: method.id, tier }))
      );
    } catch (err) {
      error = err.message || 'Payment order could not be saved.';
    } finally {
      saving = false;
    }
  }

  async function savePreference() {
    saving = true;
    error = '';
    try {
      await updateResellerChargePreference(preference);
      success = 'Charge preference updated.';
    } catch (err) {
      error = err.message || 'Charge preference could not be updated.';
    } finally {
      saving = false;
    }
  }
</script>

<section aria-labelledby="payment-methods-title">
  <div class="heading"><div><h1 id="payment-methods-title">Payment methods</h1><p>Methods are attempted in the order shown.</p></div></div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading payment methods…</span></div>
  {:else}
    <div class="layout">
      <div class="stack">
        <article class="panel">
          <h2>Saved methods</h2>
          {#if methods.length === 0}
            <p class="empty">No payment methods saved yet.</p>
          {:else}
            <ol class="method-list">
              {#each methods as method, index (method.id)}
                <li>
                  <div><strong>{methodName(method)}</strong><small>{method.provider} · priority {index + 1}</small></div>
                  <div class="row-actions">
                    <button aria-label={`Move ${methodName(method)} up`} on:click={() => move(index, -1)} disabled={index === 0 || saving}>↑</button>
                    <button aria-label={`Move ${methodName(method)} down`} on:click={() => move(index, 1)} disabled={index === methods.length - 1 || saving}>↓</button>
                    <button class="danger" on:click={() => remove(method)}>Remove</button>
                  </div>
                </li>
              {/each}
            </ol>
          {/if}
        </article>
        <article class="panel">
          <h2>Charge preference</h2>
          <label><input type="radio" bind:group={preference} value="credit_first" /> Use account credit before saved methods</label>
          <label><input type="radio" bind:group={preference} value="payment_first" /> Use saved methods before account credit</label>
          <button class="primary" on:click={savePreference} disabled={saving}>Save preference</button>
        </article>
      </div>
      <aside class="stack">
        <article class="panel">
          <h2>Add card</h2>
          {#if config?.stripe?.enabled}
            <StripeCardForm
              publishableKey={config.stripe.publishable_key}
              on:saved={(event) => { methods = [...methods, event.detail]; success = 'Card added.'; }}
            />
          {:else}
            <p class="empty">Stripe is not configured. Card entry is disabled.</p>
          {/if}
        </article>
        <article class="panel">
          <h2>Add PayPal</h2>
          <p>Approve Rackflow to use your PayPal account for reseller invoices.</p>
          <button class="paypal" on:click={startPayPal} disabled={!config?.paypal?.enabled || saving}>Continue with PayPal</button>
          {#if !config?.paypal?.enabled}<p class="empty">PayPal is not configured.</p>{/if}
        </article>
      </aside>
    </div>
  {/if}
</section>

<style>
  h1 { margin: 0 0 6px; font-size: 32px; } h2 { margin: 0 0 16px; font-size: 18px; } p { color: var(--text-secondary); }
  .heading { margin-bottom: 24px; }.heading p { margin: 0; }.state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }
  .layout { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, 1fr); gap: 18px; align-items: start; }
  .stack { display: grid; gap: 18px; }.panel { padding: 20px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }
  .method-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
  .method-list li { display: flex; justify-content: space-between; gap: 14px; align-items: center; padding: 13px; border: 1px solid var(--border-color); border-radius: 10px; }
  .method-list small { display: block; margin-top: 3px; color: var(--text-tertiary); text-transform: capitalize; }
  .row-actions { display: flex; gap: 6px; }.row-actions button { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 7px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .row-actions .danger { color: var(--danger-color); }.panel > label { display: block; margin: 11px 0; color: var(--text-secondary); }
  .primary, .paypal { margin-top: 10px; padding: 10px 15px; border: 0; border-radius: 8px; font-weight: 700; cursor: pointer; }
  .primary { background: var(--portal-accent); color: var(--portal-accent-contrast); }.paypal { background: #0070ba; color: #fff; }
  button:disabled { opacity: .55; cursor: not-allowed; }.empty { color: var(--text-tertiary); font-size: 14px; }
  @media (max-width: 780px) { .layout { grid-template-columns: 1fr; } .method-list li { align-items: flex-start; flex-direction: column; } }
</style>
