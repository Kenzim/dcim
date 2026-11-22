<script>
  import { onMount } from 'svelte';
  import CancellationRequests from '../billing/shared/CancellationRequests.svelte';
  import {
    resellerListCancellationRequests,
    resellerWithdrawCancellationRequest,
  } from '../../lib/billingClientApi.js';
  import { listResellerPanelServices } from '../../lib/api.js';

  let serviceNames = {};

  onMount(async () => {
    try {
      const services = await listResellerPanelServices({ limit: 500 });
      serviceNames = Object.fromEntries(
        services.map((service) => [service.id, service.name || `Service #${service.id}`]),
      );
    } catch (_) {
      serviceNames = {};
    }
  });
</script>

<section aria-labelledby="cancellations-title">
  <div class="heading">
    <div>
      <h1 id="cancellations-title">Cancellation requests</h1>
      <p>Cancellations of your services. To request one, open the service on the <a href="/reseller/services">Services</a> page.</p>
    </div>
  </div>
  <CancellationRequests
    list={resellerListCancellationRequests}
    withdraw={resellerWithdrawCancellationRequest}
    viewer="reseller"
    {serviceNames}
    emptyText="No cancellation has been requested for your services."
  >
    <p slot="intro">A scheduled cancellation can be withdrawn until it takes effect.</p>
  </CancellationRequests>
</section>

<style>
  .heading { margin-bottom: 4px; }
  h1 { margin: 0 0 6px; font-size: 32px; }
  .heading p { margin: 0; color: var(--text-secondary); }
  .heading a { color: var(--portal-accent); font-weight: 600; }
</style>
