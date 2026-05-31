<script>
  import { onMount } from 'svelte';
  import { currentRoute } from '../lib/router.js';
  import { isAuthenticated, user, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import Sidebar from '../components/Sidebar.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Login from '../components/Login.svelte';
  import User from '../components/User.svelte';
  import Plugins from '../components/Plugins.svelte';
  import Locations from '../components/Locations.svelte';
  import LocationDetail from '../components/LocationDetail.svelte';
  import Racks from '../components/Racks.svelte';
  import RackView from '../components/RackView.svelte';
  import RowView from '../components/RowView.svelte';
  import Servers from '../components/Servers.svelte';
  import ServerDetail from '../components/ServerDetail.svelte';
  import Switches from '../components/Switches.svelte';
  import SwitchDetail from '../components/SwitchDetail.svelte';
  import OSTemplates from '../components/OSTemplates.svelte';
  import BillingIntegrations from '../components/BillingIntegrations.svelte';
  import UnifiedServices from '../components/UnifiedServices.svelte';
  import ProxmoxServices from '../components/ProxmoxServices.svelte';
  import BareMetalServices from '../components/BareMetalServices.svelte';
  import ClientServices from '../components/ClientServices.svelte';
  import VMServiceDetail from '../components/VMServiceDetail.svelte';
  import BareMetalServiceDetail from '../components/BareMetalServiceDetail.svelte';
  import Scripts from '../components/Scripts.svelte';
  import ServerGroups from '../components/ServerGroups.svelte';
  import ServerGroupDetail from '../components/ServerGroupDetail.svelte';
  import AssetManager from '../components/AssetManager.svelte';
  import ProductCatalog from '../components/ProductCatalog.svelte';
  import VMTemplates from '../components/VMTemplates.svelte';
  import VMIpAllocations from '../components/VMIpAllocations.svelte';
  import ProxmoxInventory from '../components/ProxmoxInventory.svelte';
  import ProxyIpam from '../components/ProxyIpam.svelte';

  function flagEnabled(name) {
    const raw = import.meta.env[name];
    if (raw === undefined || raw === null) return false;
    const v = String(raw).trim().toLowerCase();
    return v === '1' || v === 'true' || v === 'yes' || v === 'on';
  }
  const showProxmoxArea = flagEnabled('VITE_ENABLE_PROXMOX');
  const showIpamProxyArea = flagEnabled('VITE_ENABLE_IPAM_PROXY');

  let authChecked = false;
  
  onMount(async () => {
    await checkAuth();
    authChecked = true;
  });

  // Get current route name from location
  let routeName = 'dashboard';
  let serverId = null;
  let switchId = null;
  let rackId = null;
  let rowLocationId = null;
  let rowNumber = null;
  let groupId = null;
  let locationId = null;
  let vmServiceId = null;
  let bareMetalServiceId = null;
  
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
    if (routeName && routeName.startsWith('servers/') && !routeName.startsWith('server-groups/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        serverId = routeParts[1];
      }
    } else {
      serverId = null;
    }
    
    // Extract server group ID from URL if it's a server group detail route
    if (routeName && routeName.startsWith('server-groups/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        groupId = parseInt(routeParts[1], 10);
      }
    } else {
      groupId = null;
    }
    
    // Extract switch ID from URL if it's a switch detail route
    if (routeName && routeName.startsWith('switches/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1]) {
        switchId = routeParts[1];
      }
    } else {
      switchId = null;
    }
    
    // Extract row location and row number from URL if it's a row view route (check this first)
    if (routeName && routeName.startsWith('racks/rows/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 3 && routeParts[2] && routeParts[3]) {
        rowLocationId = parseInt(routeParts[2], 10);
        rowNumber = parseInt(routeParts[3], 10);
      }
    } else {
      rowLocationId = null;
      rowNumber = null;
    }

    // Extract location ID from URL for location detail
    if (routeName && routeName.startsWith('locations/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        locationId = parseInt(routeParts[1], 10);
      } else {
        locationId = null;
      }
    } else {
      locationId = null;
    }

    if (routeName && routeName.startsWith('vm-services/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        vmServiceId = parseInt(routeParts[1], 10);
      } else {
        vmServiceId = null;
      }
    } else {
      vmServiceId = null;
    }

    if (routeName && routeName.startsWith('bare-metal-services/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        bareMetalServiceId = parseInt(routeParts[1], 10);
      } else {
        bareMetalServiceId = null;
      }
    } else {
      bareMetalServiceId = null;
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
  }

</script>

{#if !authChecked}
  <div class="loading-container">
    <p>Loading...</p>
  </div>
{:else if $isAuthenticated}
  {#if !$user?.is_admin}
    <ClientServices />
  {:else}
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
      {:else if routeName === 'switches'}
        <Switches />
      {:else if routeName.startsWith('switches/') && switchId}
        <SwitchDetail switchId={switchId} onBack={() => navigate('/admin/switches')} />
      {:else if routeName === 'locations'}
        <Locations />
      {:else if routeName.startsWith('locations/') && locationId}
        <LocationDetail locationId={locationId} onBack={() => navigate('/admin/locations')} />
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
      {:else if routeName === 'billing-integrations'}
        <BillingIntegrations />
      {:else if routeName === 'services-list'}
        <UnifiedServices />
      {:else if routeName === 'vm-services' && showProxmoxArea}
        <ProxmoxServices />
      {:else if routeName.startsWith('vm-services/') && vmServiceId && showProxmoxArea}
        <VMServiceDetail serviceId={vmServiceId} />
      {:else if routeName === 'bare-metal-services'}
        <BareMetalServices />
      {:else if routeName.startsWith('bare-metal-services/') && bareMetalServiceId}
        <BareMetalServiceDetail serviceId={bareMetalServiceId} />
      {:else if routeName === 'scripts'}
        <Scripts />
      {:else if routeName === 'asset-manager'}
        <AssetManager />
      {:else if routeName === 'product-catalog' && showProxmoxArea}
        <ProductCatalog />
      {:else if routeName === 'vm-templates' && showProxmoxArea}
        <VMTemplates />
      {:else if routeName === 'vm-ip-allocations' && showProxmoxArea}
        <VMIpAllocations />
      {:else if routeName === 'proxmox-inventory' && showProxmoxArea}
        <ProxmoxInventory />
      {:else if routeName === 'proxy-ipam' && showIpamProxyArea}
        <ProxyIpam />
      {:else if routeName === 'server-groups'}
        <ServerGroups />
      {:else if routeName.startsWith('server-groups/') && groupId}
        <ServerGroupDetail groupId={groupId} />
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
  {/if}
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
    height: 100vh;
    max-height: 100vh;
    min-height: 0;
    display: flex;
    flex-direction: column;
    min-width: 0;
    overflow-y: auto;
    overflow-x: hidden;
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
