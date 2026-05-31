<script>
  import { logout } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import { currentRoute } from '../lib/router.js';

  function flagEnabled(name) {
    const raw = import.meta.env[name];
    if (raw === undefined || raw === null) return false;
    const v = String(raw).trim().toLowerCase();
    return v === '1' || v === 'true' || v === 'yes' || v === 'on';
  }
  const showProxmoxArea = flagEnabled('VITE_ENABLE_PROXMOX');
  const showIpamProxyArea = flagEnabled('VITE_ENABLE_IPAM_PROXY');

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
  $: isServicesListActive = currentPath.startsWith('/admin/services-list');
  $: isVmServicesActive = currentPath.startsWith('/admin/vm-services');
  $: isBareMetalServicesActive = currentPath.startsWith('/admin/bare-metal-services');
  $: isScriptsActive = currentPath.startsWith('/admin/scripts');
  $: isAssetManagerActive = currentPath.startsWith('/admin/asset-manager');
  $: isProductCatalogActive = currentPath.startsWith('/admin/product-catalog');
  $: isVmTemplatesActive = currentPath.startsWith('/admin/vm-templates');
  $: isVmIpAllocationsActive = currentPath.startsWith('/admin/vm-ip-allocations');
  $: isProxmoxInventoryActive = currentPath.startsWith('/admin/proxmox-inventory');
  $: isProxyIpamActive = currentPath.startsWith('/admin/proxy-ipam');
  $: isServerGroupsActive = currentPath.startsWith('/admin/server-groups');
  $: isUserActive = currentPath === '/admin/user' || currentPath === '/admin/user/';
</script>

<nav class="sidebar">
  <div class="sidebar-header">
    <a href="/admin" class="sidebar-logo-link">
      <div class="sidebar-logo">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
      </div>
      <h2 class="sidebar-title">Rackflow</h2>
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
          <a href="/admin/bare-metal-services" class="nav-link" class:active={isBareMetalServicesActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7h16M4 12h16M4 17h16" />
            </svg>
            <span>Bare Metal Services</span>
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

    {#if showProxmoxArea}
      <div class="nav-group">
        <div class="nav-group-label">PROXMOX</div>
        <ul class="nav-list">
          <li class="nav-item">
            <a href="/admin/product-catalog" class="nav-link" class:active={isProductCatalogActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
              <span>Product Catalog</span>
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
          <li class="nav-item">
            <a href="/admin/vm-services" class="nav-link" class:active={isVmServicesActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5h18M3 12h18M3 19h18" />
              </svg>
              <span>VM Services</span>
            </a>
          </li>
        </ul>
      </div>
    {/if}

    <div class="nav-group">
      <div class="nav-group-label">SERVICES</div>
      <ul class="nav-list">
        {#if showIpamProxyArea}
          <li class="nav-item">
            <a href="/admin/proxy-ipam" class="nav-link" class:active={isProxyIpamActive}>
              <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
              <span>IPAM & Proxy</span>
            </a>
          </li>
        {/if}
        <li class="nav-item">
          <a href="/admin/services-list" class="nav-link" class:active={isServicesListActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <span>Unified Services & Users</span>
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
      <div class="nav-group-label">BILLING</div>
      <ul class="nav-list">
        <li class="nav-item">
          <a href="/admin/billing-integrations" class="nav-link" class:active={isBillingIntegrationsActive}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span>Billing Integrations</span>
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
    width: 260px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    border-right: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    z-index: 100;
    box-shadow: var(--shadow-md);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .sidebar-header {
    padding: 16px 16px;
    border-bottom: 1px solid var(--border-color);
  }

  .sidebar-logo-link {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: inherit;
  }

  .sidebar-logo {
    width: 36px;
    height: 36px;
    background: var(--accent-color);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .sidebar-logo svg {
    width: 20px;
    height: 20px;
    color: white;
  }

  .sidebar-title {
    font-size: 18px;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.5px;
  }

  .sidebar-nav {
    flex: 1;
    padding: 8px 0;
    overflow-y: auto;
  }

  .nav-group {
    margin-top: 16px;
  }

  .nav-group:first-of-type {
    margin-top: 8px;
  }

  .nav-group-label {
    padding: 8px 16px 6px 16px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
  }

  .sidebar-footer {
    padding: 12px 16px;
    border-top: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .footer-link {
    padding: 8px 12px;
    border-radius: 6px;
    text-align: left;
  }

  .btn-logout {
    width: 100%;
    padding: 8px 12px;
    background: var(--danger-color);
    color: white;
    border: 2px solid var(--danger-color);
    border-radius: 6px;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }

  .btn-logout:hover {
    background: var(--danger-color);
    border-color: var(--danger-color);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .btn-icon {
    width: 18px;
    height: 18px;
  }

  .nav-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }

  .sidebar-nav > .nav-list:first-child {
    margin-bottom: 8px;
  }

  .nav-item {
    margin: 0;
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px;
    color: var(--text-secondary);
    text-decoration: none;
    border-radius: 0;
    transition: all 0.2s ease;
    font-weight: 500;
    font-size: 14px;
    position: relative;
  }

  .nav-link .nav-icon {
    color: var(--text-secondary);
  }

  .nav-link:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .nav-link:hover .nav-icon {
    color: var(--text-primary);
  }

  .nav-link.active {
    background: var(--accent-color);
    color: white;
  }

  .nav-link.active .nav-icon {
    color: white;
  }

  .nav-link.active::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: var(--accent-color);
  }

  .nav-icon {
    width: 18px;
    height: 18px;
    flex-shrink: 0;
  }

  @media (max-width: 768px) {
    .sidebar {
      transform: translateX(-100%);
      transition: transform 0.3s ease;
    }
  }
</style>
