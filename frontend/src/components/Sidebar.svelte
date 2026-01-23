<script>
  import { logout } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import { currentRoute } from '../lib/router.js';

  async function handleLogout() {
    try {
      await logout();
      navigate('/');
    } catch (err) {
      console.error('Logout error:', err);
    }
  }
  
  // Helper to check if route is active
  function isActive(path) {
    const current = $currentRoute || window.location.pathname;
    if (path === '/admin') {
      return current === '/admin' || current === '/admin/';
    }
    return current.startsWith(path);
  }
  
  $: currentPath = $currentRoute || window.location.pathname;
</script>

<nav class="sidebar">
  <div class="sidebar-header">
    <a href="/admin" class="sidebar-logo-link">
      <div class="sidebar-logo">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
      </div>
      <h2 class="sidebar-title">DCIM</h2>
    </a>
  </div>
  
  <div class="sidebar-nav">
    <ul class="nav-list">
      <li class="nav-item">
        <a href="/admin" class="nav-link" class:active={currentPath === '/admin' || currentPath === '/admin/'}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
          </svg>
          <span>Dashboard</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/servers" class="nav-link" class:active={isActive('/admin/servers')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
          </svg>
          <span>Servers</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/locations" class="nav-link" class:active={isActive('/admin/locations')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
          <span>Locations</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/racks" class="nav-link" class:active={isActive('/admin/racks')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
          </svg>
          <span>Racks</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/plugins" class="nav-link" class:active={isActive('/admin/plugins')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
          <span>Plugins</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/os-templates" class="nav-link" class:active={isActive('/admin/os-templates')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
          </svg>
          <span>OS Templates</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/services" class="nav-link" class:active={isActive('/admin/services')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
          <span>Services</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/billing-integrations" class="nav-link" class:active={isActive('/admin/billing-integrations')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>Billing Integrations</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/services-list" class="nav-link" class:active={isActive('/admin/services-list')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          <span>Services & Users</span>
        </a>
      </li>
      <li class="nav-item">
        <a href="/admin/scripts" class="nav-link" class:active={isActive('/admin/scripts')}>
          <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
          </svg>
          <span>Scripts</span>
        </a>
      </li>
    </ul>
  </div>

  <div class="sidebar-footer">
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
    padding: 24px 20px;
    border-bottom: 1px solid var(--border-color);
  }

  .sidebar-logo-link {
    display: flex;
    align-items: center;
    gap: 12px;
    text-decoration: none;
    color: inherit;
  }

  .sidebar-logo {
    width: 40px;
    height: 40px;
    background: var(--accent-color);
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .sidebar-logo svg {
    width: 24px;
    height: 24px;
  }

  .sidebar-title {
    font-size: 20px;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.5px;
  }

  .sidebar-nav {
    flex: 1;
    padding: 20px 0;
    overflow-y: auto;
  }

  .sidebar-footer {
    padding: 20px;
    border-top: 1px solid var(--border-color);
  }

  .btn-logout {
    width: 100%;
    padding: 12px 16px;
    background: var(--danger-color);
    color: white;
    border: 2px solid var(--danger-color);
    border-radius: 10px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }

  .btn-logout:hover {
    background: #dc2626;
    border-color: #dc2626;
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

  .nav-item {
    margin: 4px 12px;
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    color: var(--text-secondary);
    text-decoration: none;
    border-radius: 10px;
    transition: all 0.2s ease;
    font-weight: 500;
  }

  .nav-link:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .nav-link.active {
    background: var(--accent-color);
    color: white;
    box-shadow: var(--shadow-md);
  }

  .nav-icon {
    width: 20px;
    height: 20px;
  }

  @media (max-width: 768px) {
    .sidebar {
      transform: translateX(-100%);
      transition: transform 0.3s ease;
    }
  }
</style>
