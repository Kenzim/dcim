<script>
  import { onDestroy, onMount } from 'svelte';
  import { loadStripe } from '@stripe/stripe-js';
  import QRCode from 'qrcode';
  import {
    completeResellerPayPalSetup,
    createResellerPayPalSetup,
    createResellerTopUp,
    createResellerUsdtDeposit,
    getResellerPanelInvoice,
    getResellerPaymentConfig,
    getResellerUsdtDeposit,
    listResellerPanelPaymentMethods,
    payResellerPanelInvoice,
  } from '../../lib/api.js';
  import { formatMoney, parseUsdToCents } from '../../lib/resellerMoney.js';
  import { Alert, Spinner } from '../ui/index.js';
  import StripeCardForm from './StripeCardForm.svelte';

  let amount = '';
  let invoice;
  let config;
  let methods = [];
  let gateway = 'stripe';
  let selectedMethodId = '';
  let deposit;
  let qrDataUrl = '';
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let pollTimer;

  $: gatewayMethods = methods.filter((method) => method.provider === gateway && method.enabled);

  onMount(load);
  onDestroy(() => clearTimeout(pollTimer));

  async function load() {
    const errors = [];
    try {
      config = await getResellerPaymentConfig();
    } catch (err) {
      errors.push(err.message || 'Payment config could not be loaded.');
    }
    try {
      methods = await listResellerPanelPaymentMethods();
    } catch (err) {
      methods = [];
      errors.push(err.message || 'Saved payment methods could not be loaded.');
    }
    try {
      const params = new URLSearchParams(window.location.search);
      const paypalToken = params.get('setup_token') || params.get('token');
      if (paypalToken) {
        window.history.replaceState({}, '', window.location.pathname);
        const method = await completeResellerPayPalSetup(paypalToken);
        methods = [...methods.filter((item) => item.id !== method.id), method];
        gateway = 'paypal';
        selectedMethodId = String(method.id);
        const pendingId = sessionStorage.getItem('rf_reseller_topup_invoice');
        sessionStorage.removeItem('rf_reseller_topup_invoice');
        if (pendingId) {
          invoice = await getResellerPanelInvoice(Number(pendingId));
          if (invoice.status !== 'paid') await payWithMethod(method.id);
        }
        success = 'PayPal account connected.';
      }
    } catch (err) {
      errors.push(err.message || 'PayPal setup could not be completed.');
    }
    error = errors.filter(Boolean).join(' ') || '';
    loading = false;
  }

  async function createInvoice() {
    working = true;
    error = '';
    success = '';
    try {
      const cents = parseUsdToCents(amount);
      invoice = await createResellerTopUp(cents);
      deposit = null;
      qrDataUrl = '';
      success = `Invoice #${invoice.invoice_number} created. Pay below or open Invoices anytime.`;
      const firstEnabled = ['stripe', 'paypal', 'usdt'].find((name) => config?.[name]?.enabled);
      if (firstEnabled) gateway = firstEnabled;
      selectedMethodId = String(methods.find((method) => method.provider === gateway && method.enabled)?.id || '');
    } catch (err) {
      error = err.message || 'Top-up invoice could not be created.';
    } finally {
      working = false;
    }
  }

  async function payWithMethod(methodId = Number(selectedMethodId)) {
    if (!invoice || !methodId) {
      error = 'Choose a saved payment method.';
      return;
    }
    working = true;
    error = '';
    let clientSecret = null;
    try {
      try {
        invoice = await payResellerPanelInvoice(invoice.id, Number(methodId));
        success = `${formatMoney(invoice.amount_cents)} added to your balance.`;
      } catch (err) {
        if (err.status !== 402 || !err.detail?.pending_action || !err.detail?.client_secret) throw err;
        clientSecret = err.detail.client_secret;
        const stripe = await loadStripe(config.stripe.publishable_key);
        if (!stripe) throw new Error('Stripe could not be loaded for authentication.');
        const confirmation = await stripe.confirmCardPayment(clientSecret);
        if (confirmation.error) throw new Error(confirmation.error.message);
        invoice = await getResellerPanelInvoice(invoice.id);
        success = invoice.status === 'paid'
          ? `${formatMoney(invoice.amount_cents)} added to your balance.`
          : 'Card authentication completed. Payment confirmation is processing.';
      }
    } catch (err) {
      error = err.message || 'Invoice payment failed.';
    } finally {
      clientSecret = null;
      working = false;
    }
  }

  async function cardSaved(event) {
    const method = event.detail;
    methods = [...methods, method];
    selectedMethodId = String(method.id);
    await payWithMethod(method.id);
  }

  async function startPayPal() {
    if (!invoice) return;
    working = true;
    error = '';
    try {
      const setup = await createResellerPayPalSetup();
      sessionStorage.setItem('rf_reseller_topup_invoice', String(invoice.id));
      window.location.assign(setup.approval_url);
    } catch (err) {
      error = err.message || 'PayPal approval could not be started.';
      working = false;
    }
  }

  async function startUsdt() {
    if (!invoice) return;
    working = true;
    error = '';
    try {
      deposit = await createResellerUsdtDeposit(invoice.id);
      qrDataUrl = await QRCode.toDataURL(deposit.payment_uri, { width: 220, margin: 1 });
      schedulePoll();
    } catch (err) {
      error = err.message || 'USDT deposit could not be created.';
    } finally {
      working = false;
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!deposit || deposit.status === 'expired' || invoice?.status === 'paid') return;
    pollTimer = setTimeout(pollDeposit, 8000);
  }

  async function pollDeposit() {
    try {
      [deposit, invoice] = await Promise.all([
        getResellerUsdtDeposit(invoice.id),
        getResellerPanelInvoice(invoice.id),
      ]);
      if (invoice.status === 'paid') success = 'USDT payment confirmed and credited.';
    } catch (err) {
      error = err.message || 'Deposit status could not be refreshed.';
    } finally {
      schedulePoll();
    }
  }

  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      success = 'Copied to clipboard.';
    } catch (_) {
      error = 'Clipboard access was denied.';
    }
  }
</script>

<section aria-labelledby="top-up-title">
  <h1 id="top-up-title">Top up balance</h1>
  <p class="intro">Create a USD invoice, then fund it using a configured gateway.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading payment options…</span></div>
  {:else}
    <div class="layout">
      <article class="panel">
        <h2>1. Amount</h2>
        <form on:submit|preventDefault={createInvoice}>
          <label for="topup-amount">USD amount</label>
          <div class="amount-input"><span>$</span><input id="topup-amount" bind:value={amount} inputmode="decimal" placeholder="100.00" aria-describedby="amount-help" /></div>
          <small id="amount-help">Use whole dollars or up to two decimal places. Minimum $5.00.</small>
          <button class="primary" type="submit" disabled={working}>{working ? 'Creating…' : 'Create invoice'}</button>
        </form>
      </article>

      {#if invoice}
        <article class="panel">
          <div class="invoice-heading"><div><h2>2. Pay invoice #{invoice.invoice_number}</h2><p>{formatMoney(invoice.remaining_cents, invoice.currency)} due</p></div><span class="status">{invoice.status}</span></div>
          {#if invoice.status === 'paid'}
            <Alert type="success">This top-up invoice is paid.</Alert>
          {:else}
            <div class="gateway-tabs" role="tablist" aria-label="Payment gateway">
              {#each ['stripe', 'paypal', 'usdt'] as name}
                <button type="button" class:active={gateway === name} on:click={() => { gateway = name; selectedMethodId = String(methods.find((method) => method.provider === name && method.enabled)?.id || ''); }} disabled={!config?.[name]?.enabled}>{name === 'usdt' ? 'USDT' : name[0].toUpperCase() + name.slice(1)}</button>
              {/each}
            </div>
            {#if !config?.[gateway]?.enabled}
              <p class="unavailable">{gateway} is not configured.</p>
            {:else if gateway === 'stripe'}
              {#if gatewayMethods.length}
                <label for="stripe-method">Saved card</label>
                <select id="stripe-method" bind:value={selectedMethodId}>
                  {#each gatewayMethods as method}<option value={String(method.id)}>{method.label || `${method.brand || 'Card'} •••• ${method.last4}`}</option>{/each}
                </select>
                <button class="primary" on:click={() => payWithMethod()} disabled={working}>Pay with saved card</button>
              {:else}
                <p>Add a card securely with Stripe, then this invoice will be paid.</p>
                <StripeCardForm publishableKey={config.stripe.publishable_key} submitLabel="Add card and pay" on:saved={cardSaved} />
              {/if}
            {:else if gateway === 'paypal'}
              {#if gatewayMethods.length}
                <label for="paypal-method">Saved PayPal account</label>
                <select id="paypal-method" bind:value={selectedMethodId}>
                  {#each gatewayMethods as method}<option value={String(method.id)}>{method.label || 'PayPal account'}</option>{/each}
                </select>
                <button class="paypal" on:click={() => payWithMethod()} disabled={working}>Pay with PayPal</button>
              {:else}
                <button class="paypal" on:click={startPayPal} disabled={working}>Connect PayPal and pay</button>
              {/if}
            {:else if gateway === 'usdt'}
              <p class="network-warning"><strong>ERC20 on Ethereum only.</strong> Sending on another network can permanently lose funds.</p>
              {#if !deposit}
                <button class="primary" on:click={startUsdt} disabled={working}>Create USDT deposit</button>
              {:else}
                <div class="deposit">
                  {#if qrDataUrl}<img src={qrDataUrl} alt="USDT EIP-681 payment QR code" />{/if}
                  <dl>
                    <div><dt>Exact amount</dt><dd>{deposit.amount} {deposit.token_symbol}</dd></div>
                    <div><dt>Network</dt><dd>{deposit.network}</dd></div>
                    <div><dt>Address</dt><dd><code>{deposit.deposit_address}</code><button on:click={() => copy(deposit.deposit_address)}>Copy</button></dd></div>
                    <div><dt>Contract</dt><dd><code>{deposit.contract_address}</code><button on:click={() => copy(deposit.contract_address)}>Copy</button></dd></div>
                    <div><dt>Status</dt><dd>{deposit.status.replaceAll('_', ' ')} ({deposit.confirmations}/{deposit.confirmations_required} confirmations)</dd></div>
                    <div><dt>Expires</dt><dd>{new Date(deposit.expires_at).toLocaleString()}</dd></div>
                  </dl>
                </div>
              {/if}
            {/if}
          {/if}
        </article>
      {/if}
    </div>
  {/if}
</section>

<style>
  h1 { margin: 0 0 6px; font-size: 32px; } h2 { margin: 0 0 16px; font-size: 18px; } .intro { margin: 0 0 24px; color: var(--text-secondary); }
  .state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }.layout { display: grid; gap: 18px; }
  .panel { padding: 22px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }
  form { display: grid; gap: 10px; max-width: 420px; } label { color: var(--text-primary); font-weight: 650; font-size: 14px; }
  .amount-input { display: flex; align-items: center; border: 1px solid var(--border-color); border-radius: 9px; overflow: hidden; background: var(--bg-primary); }
  .amount-input span { padding: 0 0 0 14px; color: var(--text-tertiary); font-weight: 750; }.amount-input input { flex: 1; border: 0; padding: 13px 10px; background: transparent; color: var(--text-primary); font: inherit; outline: none; }
  small { color: var(--text-tertiary); }.primary, .paypal { justify-self: start; margin-top: 6px; padding: 10px 16px; border: 0; border-radius: 8px; font-weight: 750; cursor: pointer; }
  .primary { background: var(--portal-accent); color: var(--portal-accent-contrast); }.paypal { background: #0070ba; color: white; }.invoice-heading { display: flex; justify-content: space-between; gap: 12px; }
  .invoice-heading p { color: var(--text-secondary); }.status { height: fit-content; padding: 5px 9px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); text-transform: capitalize; font-weight: 700; font-size: 12px; }
  .gateway-tabs { display: flex; gap: 8px; margin: 12px 0 20px; flex-wrap: wrap; }.gateway-tabs button { padding: 9px 14px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-secondary); color: var(--text-secondary); font-weight: 700; cursor: pointer; }
  .gateway-tabs button.active { border-color: var(--portal-accent); color: var(--portal-accent); background: var(--portal-accent-soft); }button:disabled { opacity: .5; cursor: not-allowed; }
  select { display: block; width: min(100%, 420px); margin: 8px 0; padding: 11px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .unavailable, .network-warning { padding: 12px; border-radius: 8px; background: var(--warning-bg); color: var(--warning-text); }.deposit { display: grid; grid-template-columns: auto 1fr; gap: 22px; align-items: start; }
  .deposit img { width: 220px; max-width: 100%; border-radius: 10px; }.deposit dl { margin: 0; display: grid; gap: 10px; }.deposit dl div { display: grid; gap: 4px; }.deposit dt { color: var(--text-tertiary); font-size: 12px; font-weight: 700; }.deposit dd { margin: 0; overflow-wrap: anywhere; }
  code { color: var(--text-primary); }.deposit dd button { margin-left: 8px; padding: 4px 7px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  @media (max-width: 680px) { .deposit { grid-template-columns: 1fr; } .invoice-heading { flex-direction: column; } }
</style>
