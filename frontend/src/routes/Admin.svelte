<script>
  import { onMount } from 'svelte';
  import { currentRoute } from '../lib/router.js';
  import { isAuthenticated, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import Sidebar from '../components/Sidebar.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Login from '../components/Login.svelte';
  import User from '../components/User.svelte';
  import Plugins from '../components/Plugins.svelte';
  import Locations from '../components/Locations.svelte';
  import Racks from '../components/Racks.svelte';
  import RackView from '../components/RackView.svelte';
  import RowView from '../components/RowView.svelte';
  import Servers from '../components/Servers.svelte';
  import ServerDetail from '../components/ServerDetail.svelte';
  import OSTemplates from '../components/OSTemplates.svelte';
  import Services from '../components/Services.svelte';
  import BillingIntegrations from '../components/BillingIntegrations.svelte';
  import ServicesList from '../components/ServicesList.svelte';
  import Scripts from '../components/Scripts.svelte';

  let authChecked = false;
  
  onMount(async () => {
    await checkAuth();
    authChecked = true;
  });

  // Get current route name from location
  let routeName = 'dashboard';
  let serverId = null;
  let rackId = null;
  let rowLocationId = null;
  let rowNumber = null;
  
  $: {
    const path = $currentRoute || window.location.pathname;
    // Remove leading slash and split
    const routePath = path.startsWith('/') ? path.slice(1) : path;
    const parts = routePath.split('/').filter(p => p);
    
    if (parts.length === 0 || (parts.length === 1 && parts[0] === 'admin')) {
      routeName = 'dashboard';
    } else if (parts[0] === 'admin' && parts.length > 1) {
      routeName = parts.slice(1).join('/');
    } else {
      routeName = 'dashboard';
    }
    
    // Extract server ID from URL if it's a server detail route
    if (routeName && routeName.startsWith('servers/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1]) {
        serverId = routeParts[1];
      }
    } else {
      serverId = null;
    }
    
    // Extract rack ID from URL if it's a rack view route (but not a row route)
    if (routeName && routeName.startsWith('racks/') && !routeName.startsWith('racks/rows/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        rackId = parseInt(routeParts[1], 10);
      }
    } else {
      rackId = null;
    }
    
    // Extract row location and row number from URL if it's a row view route
    if (routeName && routeName.startsWith('rows/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 2 && routeParts[1] && routeParts[2]) {
        rowLocationId = parseInt(routeParts[1], 10);
        rowNumber = parseInt(routeParts[2], 10);
      }
    } else {
      rowLocationId = null;
      rowNumber = null;
    }
  }

</script>

{#if !authChecked}
  <div class="loading-container">
    <p>Loading...</p>
  </div>
{:else if $isAuthenticated}
  <div class="admin-container">
    <Sidebar />
    
    <main class="main-content">
      {#if routeName === 'dashboard' || routeName === ''}
        <PageHeader title="Dashboard" />
        <div class="content-body">
          <!-- Dashboard content will go here -->
        </div>
      {:else if routeName === 'servers'}
        <Servers />
      {:else if routeName.startsWith('servers/') && serverId}
        <ServerDetail serverId={serverId} onBack={() => navigate('/admin/servers')} />
      {:else if routeName === 'locations'}
        <Locations />
      {:else if routeName === 'racks'}
        <Racks />
      {:else if routeName.startsWith('racks/rows/') && rowLocationId && rowNumber}
        <RowView locationId={rowLocationId} row={rowNumber} onBack={() => navigate('/admin/racks')} />
      {:else if routeName.startsWith('racks/') && rackId}
        <RackView rackId={rackId} onBack={() => navigate('/admin/racks')} />
      {:else if routeName === 'plugins'}
        <Plugins />
      {:else if routeName === 'os-templates'}
        <OSTemplates />
      {:else if routeName === 'services'}
        <Services />
      {:else if routeName === 'billing-integrations'}
        <BillingIntegrations />
      {:else if routeName === 'services-list'}
        <ServicesList />
      {:else if routeName === 'scripts'}
        <Scripts />
      {:else if routeName === 'user'}
        <User />
      {:else}
        <PageHeader title="Not Found" />
        <div class="content-body">
          <p>Page not found</p>
        </div>
      {/if}
    </main>
  </div>
{:else}
  <!-- Show login page when not authenticated -->
  <Login />
{/if}

<style>
  .admin-container {
    display: flex;
    min-height: 100vh;
    background: var(--bg-secondary);
    transition: background-color 0.3s ease;
  }

  .main-content {
    flex: 1;
    margin-left: 260px;
    min-height: 100vh;
  }

  .content-body {
    padding: 32px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    transition: background-color 0.3s ease, color 0.3s ease;
  }

  .loading-container {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-primary);
  }

  @media (max-width: 768px) {
    .main-content {
      margin-left: 0;
    }
  }
</style>
