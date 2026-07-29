<script>
  import { onMount } from 'svelte';
  import { checkAuth, isAuthenticated, user } from '../stores/auth.js';
  import { currentRoute, navigate } from '../lib/router.js';
  import ApiKey from '../components/reseller/ApiKey.svelte';
  import Clients from '../components/reseller/Clients.svelte';
  import Dashboard from '../components/reseller/Dashboard.svelte';
  import Invoices from '../components/reseller/Invoices.svelte';
  import PaymentMethods from '../components/reseller/PaymentMethods.svelte';
  import Products from '../components/reseller/Products.svelte';
  import ResellerNav from '../components/reseller/ResellerNav.svelte';
  import Services from '../components/reseller/Services.svelte';
  import StockUsage from '../components/reseller/StockUsage.svelte';
  import TopUp from '../components/reseller/TopUp.svelte';

  let authChecked = false;
  let routeName = 'dashboard';

  $: {
    const path = $currentRoute || window.location.pathname;
    const parts = path.split('/').filter(Boolean);
    routeName = parts[0] === 'reseller' && parts.length > 1 ? parts[1] : 'dashboard';
  }

  onMount(async () => {
    await checkAuth();
    authChecked = true;
    if (!$isAuthenticated) {
      navigate('/login');
    } else if ($user?.is_admin) {
      navigate('/admin');
    } else if (!$user?.is_reseller) {
      navigate('/client');
    }
  });
</script>

{#if !authChecked}
  <div class="gate">Loading reseller panel…</div>
{:else if $isAuthenticated && $user?.is_reseller && !$user?.is_admin}
  <ResellerNav>
    {#if routeName === 'top-up'}
      <TopUp />
    {:else if routeName === 'payment-methods'}
      <PaymentMethods />
    {:else if routeName === 'invoices'}
      <Invoices />
    {:else if routeName === 'products'}
      <Products />
    {:else if routeName === 'clients'}
      <Clients />
    {:else if routeName === 'services'}
      <Services />
    {:else if routeName === 'stock'}
      <StockUsage />
    {:else if routeName === 'api'}
      <ApiKey />
    {:else}
      <Dashboard />
    {/if}
  </ResellerNav>
{:else}
  <div class="gate">Redirecting…</div>
{/if}

<style>
  .gate { min-height: 100vh; display: grid; place-items: center; padding: 20px; background: var(--bg-secondary); color: var(--text-secondary); }
</style>
