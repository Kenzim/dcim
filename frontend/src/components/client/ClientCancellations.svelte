<script>
  import { onMount } from 'svelte';
  import CancellationRequests from '../billing/shared/CancellationRequests.svelte';
  import {
    clientListCancellationRequests,
    clientWithdrawCancellationRequest,
  } from '../../lib/billingClientApi.js';
  import { listMyServices } from '../../lib/api.js';

  export let impersonating = false;

  let serviceNames = {};

  onMount(async () => {
    try {
      const services = await listMyServices();
      serviceNames = Object.fromEntries(services.map((service) => [service.id, service.name]));
    } catch (_) {
      serviceNames = {};
    }
  });
</script>

<section aria-labelledby="cancellations-title">
  <div class="page-head">
    <div>
      <h1 id="cancellations-title">Cancellation requests</h1>
      <p class="sub">Services you have asked to cancel. To cancel another one, open it from <a href="/client/services">Services</a>.</p>
    </div>
  </div>
  <CancellationRequests
    list={clientListCancellationRequests}
    withdraw={clientWithdrawCancellationRequest}
    viewer="client"
    disabled={impersonating}
    serviceNames={serviceNames}
    serviceHref={(id) => `/client/services/${id}`}
    emptyText="You have not asked to cancel any service."
  >
    <p slot="intro">A scheduled cancellation can be withdrawn until it takes effect.</p>
  </CancellationRequests>
</section>

<style>
  .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 4px; }
  .page-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -0.02em; }
  .sub { margin: 4px 0 0; font-size: 14px; color: var(--text-tertiary); }
  .sub a { color: var(--portal-accent); font-weight: 600; }
</style>
