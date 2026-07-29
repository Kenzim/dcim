<script>
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { loadStripe } from '@stripe/stripe-js';
  import {
    createResellerStripeSetupIntent,
    registerResellerStripeMethod,
  } from '../../lib/api.js';

  export let publishableKey;
  export let submitLabel = 'Save card';

  const dispatch = createEventDispatcher();
  let stripe;
  let elements;
  let card;
  let cardHost;
  let cardError = '';
  let error = '';
  let loading = false;
  let ready = false;

  onMount(async () => {
    try {
      stripe = await loadStripe(publishableKey);
      if (!stripe) throw new Error('Stripe could not be loaded.');
      elements = stripe.elements();
      card = elements.create('card', {
        style: {
          base: {
            color: getComputedStyle(document.documentElement).getPropertyValue('--text-primary').trim() || '#0f172a',
            fontSize: '16px',
            '::placeholder': { color: '#94a3b8' },
          },
        },
      });
      card.mount(cardHost);
      card.on('change', (event) => {
        cardError = event.error?.message || '';
      });
      ready = true;
    } catch (err) {
      error = err.message || 'Stripe is unavailable.';
    }
  });

  onDestroy(() => {
    card?.destroy();
  });

  async function saveCard() {
    if (!stripe || !card || !ready) return;
    loading = true;
    error = '';
    let clientSecret = null;
    try {
      const setup = await createResellerStripeSetupIntent();
      clientSecret = setup.client_secret;
      const result = await stripe.confirmCardSetup(clientSecret, {
        payment_method: { card },
      });
      if (result.error) throw new Error(result.error.message);
      const methodRef = typeof result.setupIntent.payment_method === 'string'
        ? result.setupIntent.payment_method
        : result.setupIntent.payment_method?.id;
      if (!methodRef) throw new Error('Stripe did not return a payment method.');
      const method = await registerResellerStripeMethod(methodRef);
      card.clear();
      dispatch('saved', method);
    } catch (err) {
      error = err.message || 'Card could not be saved.';
    } finally {
      clientSecret = null;
      loading = false;
    }
  }
</script>

<form class="stripe-form" on:submit|preventDefault={saveCard}>
  <label for="stripe-card-element">Card details</label>
  <div id="stripe-card-element" class="card-element" bind:this={cardHost} aria-label="Card details"></div>
  {#if cardError}<p class="field-error" role="alert">{cardError}</p>{/if}
  {#if error}<p class="field-error" role="alert">{error}</p>{/if}
  <button type="submit" class="primary" disabled={!ready || loading || !!cardError}>
    {loading ? 'Saving…' : submitLabel}
  </button>
</form>

<style>
  .stripe-form { display: grid; gap: 10px; }
  label { color: var(--text-primary); font-size: 14px; font-weight: 650; }
  .card-element {
    min-height: 46px;
    padding: 13px 14px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-primary);
  }
  .card-element:focus-within { border-color: var(--portal-accent); box-shadow: var(--focus-ring); }
  .field-error { margin: 0; color: var(--danger-color); font-size: 13px; }
  button.primary {
    justify-self: start;
    padding: 10px 16px;
    border: 0;
    border-radius: 8px;
    background: var(--portal-accent);
    color: var(--portal-accent-contrast);
    font-weight: 700;
    cursor: pointer;
  }
  button:disabled { opacity: .6; cursor: not-allowed; }
</style>
