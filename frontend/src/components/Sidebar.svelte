<script>
  import { logout } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import { currentRoute } from '../lib/router.js';
  import { sidebarOpen, closeSidebar } from '../lib/mobileNav.js';

  // Close the mobile drawer whenever the route changes (e.g. nav link click).
  $: if ($currentRoute) closeSidebar();

  function handleKeydown(e) {
    if (e.key === 'Escape') closeSidebar();
  }

  async function handleLogout() {
    try {
      await logout();
      navigate('/');
    } catch (err) {
      console.error('Logout error:', err);
    }
  }
  
  // Reactive active state checks - these will update when $currentRoute changes
  $: currentPath = $currentRoute || window.location.pathname;
  $: isDashboardActive = currentPath === '/admin' || currentPath === '/admin/';
  $: isServersActive = currentPath.startsWith('/admin/servers');
  $: isSwitchesActive = currentPath.startsWith('/admin/switches');
  $: isLocationsActive = currentPath.startsWith('/admin/locations');
  $: isRacksActive = currentPath.startsWith('/admin/racks');
  $: isPluginsActive = currentPath.startsWith('/admin/plugins');
  $: isOSTemplatesActive = currentPath.startsWith('/admin/os-templates');
  $: isBillingIntegrationsActive = currentPath.startsWith('/admin/billing-integrations');
  $: isBillingActive = currentPath === '/admin/billing' || currentPath.startsWith('/admin/billing/');
  $: isResellersActive = currentPath.startsWith('/admin/resellers');
  $: isResellerGroupsActive = currentPath.startsWith('/admin/reseller-groups');
  $: isServicesActive = currentPath.startsWith('/admin/services');
  $: isUsersActive = currentPath.startsWith('/admin/users');
  $: isAdminsActive = currentPath.startsWith('/admin/admins');
  $: isScriptsActive = currentPath.startsWith('/admin/scripts');
  $: isPermissionSetsActive = currentPath.startsWith('/admin/permission-sets');
  $: isAssetManagerActive = currentPath.startsWith('/admin/asset-manager');
  $: isProductCatalogActive = currentPath.startsWith('/admin/product-catalog');
  $: isVmTemplatesActive = currentPath.startsWith('/admin/vm-templates');
  $: isVmIpAllocationsActive = currentPath.startsWith('/admin/vm-ip-allocations');
  $: isProxmoxInventoryActive = currentPath.startsWith('/admin/proxmox-inventory');
  $: isProxyIpamActive = currentPath.startsWith('/admin/proxy-ipam');
  $: isProxyRunnersActive = currentPath.startsWith('/admin/proxy-runners');
  $: isProxyCatalogActive = currentPath.startsWith('/admin/proxy-catalog');
  $: isServerGroupsActive = currentPath.startsWith('/admin/server-groups');
  $: isUserActive = currentPath === '/admin/user' || currentPath === '/admin/user/';
</script>

<svelte:window on:keydown={handleKeydown} />

{#if $sidebarOpen}
  <div
    class="sidebar-backdrop"
    role="presentation"
    on:click={closeSidebar}
  ></div>
{/if}

<nav class="sidebar" class:open={$sidebarOpen}>
  <div class="sidebar-header">
    <a href="/admin" class="sidebar-logo-link">
      <div class="sidebar-logo">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        </svg>
      </div>
      <div class="sidebar-brand-text">
        <h2 class="sidebar-title">Rackflow</h2>
        <span class="sidebar-subtitle">Admin</span>
      </div>
    </a>
  </div>
  
  <div class="sidebar-nav">
    <ul class="nav-list">
      <li class="nav-item">
        <a href="/admin" class="nav-link" class:active={isDashboardActive}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
          </svg>
          <span>Home</span>
        </a>
      </li>
    </ul>

    <div class="nav-group">
      <div class="nav-group-label">BARE METAL</div>
      <ul class="nav-list">
        <li class="nav-item">
          <a href="/admin/servers" class="nav-link" class:active={isServersActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
            </svg>
            <span>Servers</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/switches" class="nav-link" class:active={isSwitchesActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
            </svg>
            <span>Switches</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/racks" class="nav-link" class:active={isRacksActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
            </svg>
            <span>Racks</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/server-groups" class="nav-link" class:active={isServerGroupsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
            </svg>
            <span>Server Groups</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/locations" class="nav-link" class:active={isLocationsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span>Locations</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/plugins" class="nav-link" class:active={isPluginsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            <span>Plugins</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/os-templates" class="nav-link" class:active={isOSTemplatesActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
            </svg>
            <span>OS Templates</span>
          </a>
        </li>
      </ul>
    </div>

    <div class="nav-group">
        <div class="nav-group-label">PROXMOX</div>
        <ul class="nav-list">
          <li class="nav-item">
            <a href="/admin/product-catalog" class="nav-link" class:active={isProductCatalogActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
              <span>VM Product Catalog</span>
            </a>
          </li>
          <li class="nav-item">
            <a href="/admin/vm-templates" class="nav-link" class:active={isVmTemplatesActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
              </svg>
              <span>VM Templates</span>
            </a>
          </li>
          <li class="nav-item">
            <a href="/admin/vm-ip-allocations" class="nav-link" class:active={isVmIpAllocationsActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M5 6h14a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2z" />
              </svg>
              <span>VM IP Allocations</span>
            </a>
          </li>
          <li class="nav-item">
            <a href="/admin/proxmox-inventory" class="nav-link" class:active={isProxmoxInventoryActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7h18M3 12h18M3 17h18" />
              </svg>
              <span>Proxmox Inventory</span>
            </a>
          </li>
        </ul>
      </div>

    <div class="nav-group">
      <div class="nav-group-label">SERVICES</div>
      <ul class="nav-list">
        <li class="nav-item">
          <a href="/admin/proxy-catalog" class="nav-link" class:active={isProxyCatalogActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
            <span>Proxy Catalog</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/proxy-ipam" class="nav-link" class:active={isProxyIpamActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <span>IPAM & Proxy</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/proxy-runners" class="nav-link" class:active={isProxyRunnersActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" />
            </svg>
            <span>Proxy Runners</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/services" class="nav-link" class:active={isServicesActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <span>Services</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/scripts" class="nav-link" class:active={isScriptsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
            </svg>
            <span>Scripts</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/permission-sets" class="nav-link" class:active={isPermissionSetsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-12V7a4 4 0 10-8 0v2" />
            </svg>
            <span>Permission Presets</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/asset-manager" class="nav-link" class:active={isAssetManagerActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
            <span>Asset Manager</span>
          </a>
        </li>
      </ul>
    </div>

    <div class="nav-group">
      <div class="nav-group-label">USERS</div>
      <ul class="nav-list">
        <li class="nav-item">
          <a href="/admin/users" class="nav-link" class:active={isUsersActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
            </svg>
            <span>Users</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/admins" class="nav-link" class:active={isAdminsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-12V7a4 4 0 10-8 0v2" />
            </svg>
            <span>Admins</span>
          </a>
        </li>
      </ul>
    </div>

    <div class="nav-group">
      <div class="nav-group-label">BILLING</div>
      <ul class="nav-list">
        <li class="nav-item">
          <a href="/admin/billing" class="nav-link" class:active={isBillingActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 14l6-6m-5.5.5h.01m4.99 5h.01M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16l3.5-2 3.5 2 3.5-2 3.5 2z" />
            </svg>
            <span>Billing</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/billing-integrations" class="nav-link" class:active={isBillingIntegrationsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span>Billing Integrations</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/resellers" class="nav-link" class:active={isResellersActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2a5 5 0 00-10 0v2m8-13a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span>Resellers</span>
          </a>
        </li>
        <li class="nav-item">
          <a href="/admin/reseller-groups" class="nav-link" class:active={isResellerGroupsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h10" />
            </svg>
            <span>Reseller Groups & Pricing</span>
          </a>
        </li>
      </ul>
    </div>
  </div>

  <div class="sidebar-footer">
    <a href="/admin/user" class="nav-link footer-link" class:active={isUserActive}>
      <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
      </svg>
      <span>Account</span>
    </a>
    <button class="btn-logout" on:click={handleLogout}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
      </svg>
      <span>Logout</span>
    </button>
  </div>
</nav>

<style>
  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    width: var(--admin-sidebar-width, 260px);
    background:
      radial-gradient(120% 60% at 0% 0%, var(--admin-sidebar-glow), transparent 55%),
      linear-gradient(180deg, var(--admin-sidebar-elevated) 0%, var(--admin-sidebar-bg) 100%);
    color: var(--admin-sidebar-text);
    border-right: 1px solid var(--admin-sidebar-border);
    display: flex;
    flex-direction: column;
    z-index: 100;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .sidebar-header {
    padding: 14px 14px 12px;
    border-bottom: 1px solid var(--admin-sidebar-border);
  }

  .sidebar-logo-link {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: inherit;
  }

  .sidebar-logo {
    width: 30px;
    height: 30px;
    background: linear-gradient(145deg, #155e75 0%, #0e7490 55%, #22d3ee 130%);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08), 0 8px 18px -10px rgba(34, 211, 238, 0.7);
  }

  .sidebar-logo svg {
    width: 15px;
    height: 15px;
    color: #f0f9ff;
  }

  .sidebar-brand-text {
    display: flex;
    flex-direction: column;
    gap: 0;
    min-width: 0;
  }

  .sidebar-title {
    font-size: 16px;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.03em;
    color: var(--admin-sidebar-text);
    line-height: 1.15;
  }

  .sidebar-subtitle {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--admin-sidebar-muted);
  }

  .sidebar-nav {
    flex: 1;
    padding: 8px 8px;
    overflow-y: auto;
  }

  .nav-group {
    margin-top: 12px;
  }

  .nav-group:first-of-type {
    margin-top: 6px;
  }

  .nav-group-label {
    padding: 0 8px 5px;
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--admin-sidebar-muted);
  }

  .sidebar-footer {
    padding: 8px 8px 10px;
    border-top: 1px solid var(--admin-sidebar-border);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .footer-link {
    padding: 6px 8px;
    border-radius: var(--radius-sm, 4px);
    text-align: left;
  }

  .btn-logout {
    width: 100%;
    padding: 7px 10px;
    background: var(--admin-sidebar-logout-bg);
    color: var(--admin-sidebar-logout-text);
    border: 1px solid var(--admin-sidebar-logout-border);
    border-radius: var(--radius-sm, 4px);
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }

  .btn-logout:hover {
    background: var(--admin-sidebar-logout-hover-bg);
  }

  .btn-icon {
    width: 15px;
    height: 15px;
  }

  .nav-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .sidebar-nav > .nav-list:first-child {
    margin-bottom: 2px;
  }

  .nav-item {
    margin: 0;
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 8px;
    color: var(--admin-sidebar-muted);
    text-decoration: none;
    border-radius: var(--radius-sm, 4px);
    transition: background 0.15s ease, color 0.15s ease;
    font-weight: 500;
    font-size: 13.5px;
    position: relative;
    border: 1px solid transparent;
  }

  .nav-link .nav-icon {
    color: inherit;
    opacity: 0.9;
  }

  .nav-link:hover {
    background: var(--admin-sidebar-hover);
    color: var(--admin-sidebar-text);
  }

  .nav-link:hover .nav-icon {
    color: var(--admin-sidebar-text);
  }

  .nav-link.active {
    background: var(--admin-sidebar-active-bg);
    color: var(--admin-sidebar-active-text);
    border-color: color-mix(in srgb, var(--admin-sidebar-accent) 28%, transparent);
    font-weight: 600;
  }

  .nav-link.active .nav-icon {
    color: var(--admin-sidebar-accent);
  }

  .nav-link.active::before {
    content: '';
    position: absolute;
    left: -1px;
    top: 5px;
    bottom: 5px;
    width: 2px;
    border-radius: 0 2px 2px 0;
    background: var(--admin-sidebar-accent);
  }

  .nav-icon {
    width: 15px;
    height: 15px;
    flex-shrink: 0;
  }

  .sidebar-backdrop {
    display: none;
  }

  @media (max-width: 768px) {
    .sidebar {
      transform: translateX(-100%);
      transition: transform 0.3s ease;
      z-index: 1100;
    }

    .sidebar.open {
      transform: translateX(0);
    }

    .sidebar-backdrop {
      display: block;
      position: fixed;
      inset: 0;
      background: var(--overlay-bg);
      z-index: 1050;
    }
  }
</style>
