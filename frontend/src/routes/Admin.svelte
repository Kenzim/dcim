<script>
  import { onMount } from 'svelte';
  import { currentRoute } from '../lib/router.js';
  import { isAuthenticated, user, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import Sidebar from '../components/Sidebar.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Login from '../components/Login.svelte';
  import User from '../components/User.svelte';
  import Dashboard from '../components/Dashboard.svelte';
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
  import McpKeys from '../components/McpKeys.svelte';
  import AdminBilling from '../components/AdminBilling.svelte';
  import Services from '../components/Services.svelte';
  import ServiceDetail from '../components/ServiceDetail.svelte';
  import Users from '../components/Users.svelte';
  import UserProfile from '../components/UserProfile.svelte';
  import Admins from '../components/Admins.svelte';
  import Scripts from '../components/Scripts.svelte';
  import ServerGroups from '../components/ServerGroups.svelte';
  import ServerGroupDetail from '../components/ServerGroupDetail.svelte';
  import AssetManager from '../components/AssetManager.svelte';
  import ProductCatalog from '../components/ProductCatalog.svelte';
  import VMTemplates from '../components/VMTemplates.svelte';
  import VMIpAllocations from '../components/VMIpAllocations.svelte';
  import ProxmoxInventory from '../components/ProxmoxInventory.svelte';
  import ProxmoxClusterDetail from '../components/ProxmoxClusterDetail.svelte';
  import ProxyIpam from '../components/ProxyIpam.svelte';
  import ProxyRunners from '../components/ProxyRunners.svelte';
  import ProxyCatalog from '../components/ProxyCatalog.svelte';
  import PermissionSets from '../components/PermissionSets.svelte';
  import Resellers from '../components/Resellers.svelte';
  import ResellerDetail from '../components/ResellerDetail.svelte';
  import ResellerGroups from '../components/ResellerGroups.svelte';
  import StoreCategories from '../components/store/StoreCategories.svelte';
  import StoreProducts from '../components/store/StoreProducts.svelte';
  import StoreCoupons from '../components/store/StoreCoupons.svelte';
  import CommerceOrders from '../components/commerce/CommerceOrders.svelte';
  import CommerceInvoices from '../components/commerce/CommerceInvoices.svelte';
  import CommerceTransactions from '../components/commerce/CommerceTransactions.svelte';
  import CommerceGatewayLogs from '../components/commerce/CommerceGatewayLogs.svelte';
  import CommerceEmailLog from '../components/commerce/CommerceEmailLog.svelte';
  import CommerceAudit from '../components/commerce/CommerceAudit.svelte';
  import SupportTickets from '../components/commerce/SupportTickets.svelte';

  let authChecked = false;
  
  onMount(async () => {
    await checkAuth();
    authChecked = true;
    // Non-admins must not see the admin panel; route by account role.
    if ($isAuthenticated && !$user?.is_admin) {
      navigate($user?.is_reseller ? '/reseller' : '/client');
    }
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
  let serviceId = null;
  let userId = null;
  let resellerId = null;
  let proxmoxClusterId = null;
  let redirecting = false;

  // Legacy routes removed by the services/users/admins split. Old bookmarks
  // and links still work: this maps them onto the new URLs.
  function legacyRedirectTarget(routePath) {
    if (routePath === 'services-list' || routePath === 'vm-services' || routePath === 'bare-metal-services') {
      return '/admin/services';
    }
    if (routePath.startsWith('vm-services/')) {
      return `/admin/services/${routePath.slice('vm-services/'.length)}`;
    }
    if (routePath.startsWith('bare-metal-services/')) {
      return `/admin/services/${routePath.slice('bare-metal-services/'.length)}`;
    }
    return null;
  }

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

    const redirectTarget = legacyRedirectTarget(routeName);
    redirecting = !!redirectTarget;
    if (redirectTarget) {
      navigate(redirectTarget);
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

    if (routeName && routeName.startsWith('services/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        serviceId = parseInt(routeParts[1], 10);
      } else {
        serviceId = null;
      }
    } else {
      serviceId = null;
    }

    if (routeName && routeName.startsWith('proxmox-inventory/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        proxmoxClusterId = parseInt(routeParts[1], 10);
      } else {
        proxmoxClusterId = null;
      }
    } else {
      proxmoxClusterId = null;
    }

    if (routeName && routeName.startsWith('users/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        userId = parseInt(routeParts[1], 10);
      } else {
        userId = null;
      }
    } else {
      userId = null;
    }

    if (routeName && routeName.startsWith('resellers/')) {
      const routeParts = routeName.split('/');
      if (routeParts.length > 1 && routeParts[1] && !isNaN(parseInt(routeParts[1], 10))) {
        resellerId = parseInt(routeParts[1], 10);
      } else {
        resellerId = null;
      }
    } else {
      resellerId = null;
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
    <div class="loading-container">
      <p>Redirecting...</p>
    </div>
  {:else}
  <div class="admin-container">
    <Sidebar />
    
    <main class="main-content">
      {#if redirecting}
        <div class="loading-container"><p>Redirecting…</p></div>
      {:else if routeName === 'dashboard' || routeName === ''}
        <PageHeader title="Dashboard" />
        <div class="content-body content-body-fill">
          <Dashboard />
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
      {:else if routeName === 'mcp-keys'}
        <McpKeys />
      {:else if routeName === 'billing'}
        <AdminBilling />
      {:else if routeName === 'resellers'}
        <Resellers />
      {:else if routeName.startsWith('resellers/') && resellerId}
        <ResellerDetail {resellerId} />
      {:else if routeName === 'reseller-groups'}
        <ResellerGroups />
      {:else if routeName === 'services'}
        <Services />
      {:else if routeName.startsWith('services/') && serviceId}
        <ServiceDetail serviceId={serviceId} />
      {:else if routeName === 'users'}
        <Users />
      {:else if routeName.startsWith('users/') && userId}
        <UserProfile {userId} />
      {:else if routeName === 'admins'}
        <Admins />
      {:else if routeName === 'scripts'}
        <Scripts />
      {:else if routeName === 'permission-sets'}
        <PermissionSets />
      {:else if routeName === 'asset-manager'}
        <AssetManager />
      {:else if routeName === 'product-catalog'}
        <ProductCatalog />
      {:else if routeName === 'vm-templates'}
        <VMTemplates />
      {:else if routeName === 'vm-ip-allocations'}
        <VMIpAllocations />
      {:else if routeName === 'proxmox-inventory'}
        <ProxmoxInventory />
      {:else if routeName.startsWith('proxmox-inventory/') && proxmoxClusterId}
        <ProxmoxClusterDetail clusterId={proxmoxClusterId} />
      {:else if routeName === 'proxy-ipam'}
        <ProxyIpam />
      {:else if routeName === 'proxy-runners'}
        <ProxyRunners />
      {:else if routeName === 'proxy-catalog'}
        <ProxyCatalog />
      {:else if routeName === 'server-groups'}
        <ServerGroups />
      {:else if routeName.startsWith('server-groups/') && groupId}
        <ServerGroupDetail groupId={groupId} />
      {:else if routeName === 'store/categories'}
        <StoreCategories />
      {:else if routeName === 'store/products'}
        <StoreProducts />
      {:else if routeName === 'store/coupons'}
        <StoreCoupons />
      {:else if routeName === 'commerce/orders'}
        <CommerceOrders />
      {:else if routeName === 'commerce/invoices'}
        <CommerceInvoices />
      {:else if routeName === 'commerce/transactions'}
        <CommerceTransactions />
      {:else if routeName === 'commerce/gateway-logs'}
        <CommerceGatewayLogs />
      {:else if routeName === 'commerce/email-log'}
        <CommerceEmailLog />
      {:else if routeName === 'commerce/audit'}
        <CommerceAudit />
      {:else if routeName === 'support/tickets'}
        <SupportTickets />
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
    background:
      radial-gradient(900px 420px at 100% -10%, rgba(14, 116, 144, 0.08), transparent 60%),
      linear-gradient(180deg, var(--admin-canvas-from) 0%, var(--admin-canvas-to) 100%);
    transition: background-color 0.3s ease;
  }

  .main-content {
    flex: 1;
    margin-left: var(--admin-sidebar-width, 260px);
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
    padding: 28px 32px 36px;
    color: var(--text-primary);
    transition: color 0.3s ease;
  }

  /* Dashboard only: fill the full viewport height available beside the
     sidebar/header instead of shrinking to its content, so the aggregate
     traffic chart can flex to occupy the remaining space. */
  .content-body-fill {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
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

    .content-body {
      padding: 16px;
    }
  }
</style>
