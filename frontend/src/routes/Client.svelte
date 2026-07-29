<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, user, checkAuth } from '../stores/auth.js';
  import { currentRoute, navigate } from '../lib/router.js';
  import { setImpersonationToken } from '../lib/api.js';
  import ClientShell from '../components/client/ClientShell.svelte';
  import ClientDashboard from '../components/client/ClientDashboard.svelte';
  import ClientServiceList from '../components/client/ClientServiceList.svelte';
  import ClientServiceDetail from '../components/client/ClientServiceDetail.svelte';

  let authChecked = false;
  let impersonating = false;

  // Route parsing (same pattern as Admin.svelte): /client -> dashboard,
  // /client/services -> list, /client/services/:id -> detail.
  let routeName = 'dashboard';
  let serviceId = null;

  $: {
    const path = $currentRoute || window.location.pathname;
    const routePath = path.startsWith('/') ? path.slice(1) : path;
    const parts = routePath.split('/').filter((p) => p);

    if (parts.length === 0 || (parts.length === 1 && parts[0] === 'client')) {
      routeName = 'dashboard';
      serviceId = null;
    } else if (parts[0] === 'client' && parts[1] === 'services') {
      if (parts.length === 2) {
        routeName = 'services';
        serviceId = null;
      } else if (parts.length === 3 && !isNaN(parseInt(parts[2], 10))) {
        routeName = 'service-detail';
        serviceId = parseInt(parts[2], 10);
      } else {
        routeName = 'services';
        serviceId = null;
      }
    } else {
      routeName = 'dashboard';
      serviceId = null;
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
  {#key serviceId}
    <ClientShell {impersonating}>
      {#if routeName === 'services'}
        <ClientServiceList />
      {:else if routeName === 'service-detail' && serviceId != null}
        <ClientServiceDetail {serviceId} />
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
