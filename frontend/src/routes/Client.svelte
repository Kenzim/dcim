<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, user, checkAuth } from '../stores/auth.js';
  import { currentRoute, navigate } from '../lib/router.js';
  import { setImpersonationToken } from '../lib/api.js';
  import ClientShell from '../components/client/ClientShell.svelte';
  import ClientDashboard from '../components/client/ClientDashboard.svelte';
  import ClientServiceList from '../components/client/ClientServiceList.svelte';
  import ClientServiceDetail from '../components/client/ClientServiceDetail.svelte';
  import ClientBilling from '../components/client/ClientBilling.svelte';
  import ClientCheckout from '../components/client/ClientCheckout.svelte';
  import ClientSupport from '../components/client/ClientSupport.svelte';
  import ClientAccount from '../components/client/ClientAccount.svelte';

  let authChecked = false;
  let impersonating = false;

  let routeName = 'dashboard';
  let serviceId = null;
  let invoiceId = null;
  let ticketId = null;

  $: {
    const path = $currentRoute || window.location.pathname;
    const routePath = path.startsWith('/') ? path.slice(1) : path;
    const parts = routePath.split('/').filter((p) => p);

    serviceId = null;
    invoiceId = null;
    ticketId = null;

    if (parts.length === 0 || (parts.length === 1 && parts[0] === 'client')) {
      routeName = 'dashboard';
    } else if (parts[0] === 'client') {
      if (parts[1] === 'services') {
        if (parts.length === 2) routeName = 'services';
        else if (parts.length === 3 && !isNaN(parseInt(parts[2], 10))) {
          routeName = 'service-detail';
          serviceId = parseInt(parts[2], 10);
        } else routeName = 'services';
      } else if (parts[1] === 'billing') {
        routeName = 'billing';
        if (parts.length === 3 && !isNaN(parseInt(parts[2], 10))) {
          invoiceId = parseInt(parts[2], 10);
        }
      } else if (parts[1] === 'orders') {
        routeName = 'orders';
      } else if (parts[1] === 'checkout') {
        routeName = 'checkout';
      } else if (parts[1] === 'support') {
        routeName = 'support';
        if (parts.length === 3 && !isNaN(parseInt(parts[2], 10))) {
          ticketId = parseInt(parts[2], 10);
        }
      } else if (parts[1] === 'account') {
        routeName = 'account';
      } else {
        routeName = 'dashboard';
      }
    } else {
      routeName = 'dashboard';
    }
  }

  onMount(async () => {
    // Admin "Sign in as" handoff: /client?impersonate=<token> stashes the
    // token in sessionStorage (this tab only) and strips it from the URL so
    // it isn't re-used or bookmarked. The admin's own cookie session (in
    // whatever tab they came from) is never touched.
    const params = new URLSearchParams(window.location.search);
    const token = params.get('impersonate');
    if (token) {
      setImpersonationToken(token);
      impersonating = true;
      window.history.replaceState({}, '', '/client');
    } else {
      impersonating = !!(typeof window !== 'undefined' && window.sessionStorage.getItem('rf_impersonate_token'));
    }

    await checkAuth();
    authChecked = true;
    if (!$isAuthenticated && window.location.pathname !== '/login') {
      navigate('/login');
      return;
    }
    // Admin and reseller accounts have no ordinary client portal. A plain
    // admin cookie session gets bounced to /admin; an
    // impersonation Bearer session resolves to the (non-admin) target user
    // and passes through normally.
    if ($isAuthenticated && $user?.is_admin) {
      navigate('/admin');
    } else if ($isAuthenticated && $user?.is_reseller) {
      navigate('/reseller');
    }
  });
</script>

{#if !authChecked}
  <div class="client-gate">
    <p>Loading…</p>
  </div>
{:else if $isAuthenticated && !$user?.is_admin && !$user?.is_reseller}
  {#key `${routeName}-${serviceId}-${invoiceId}-${ticketId}`}
    <ClientShell {impersonating}>
      {#if routeName === 'services'}
        <ClientServiceList />
      {:else if routeName === 'service-detail' && serviceId != null}
        <ClientServiceDetail {serviceId} />
      {:else if routeName === 'billing' || routeName === 'orders'}
        <ClientBilling {impersonating} {invoiceId} initialTab={routeName === 'orders' ? 'orders' : 'invoices'} />
      {:else if routeName === 'checkout'}
        <ClientCheckout {impersonating} />
      {:else if routeName === 'support'}
        <ClientSupport {ticketId} />
      {:else if routeName === 'account'}
        <ClientAccount {impersonating} />
      {:else}
        <ClientDashboard />
      {/if}
    </ClientShell>
  {/key}
{:else if $isAuthenticated && ($user?.is_admin || $user?.is_reseller)}
  <div class="client-gate">
    <p>Redirecting to your control panel…</p>
  </div>
{:else}
  <div class="client-gate">
    <p>Please sign in to view your services.</p>
  </div>
{/if}

<style>
  .client-gate {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
  }
  .client-gate p {
    font-size: 16px;
    color: var(--text-secondary);
  }
</style>
