<script>
  import User from './User.svelte';
  import Plugins from './Plugins.svelte';
  import Locations from './Locations.svelte';
  import Servers from './Servers.svelte';
  import ServerDetail from './ServerDetail.svelte';
  import OSTemplates from './OSTemplates.svelte';
  import Services from './Services.svelte';
  import PageHeader from './PageHeader.svelte';
  import { logout } from '../stores/auth.js';

  let currentPage = 'dashboard';
  let selectedServerId = null;

  function navigate(page) {
    currentPage = page;
    selectedServerId = null;
  }

  function viewServer(serverId) {
    selectedServerId = serverId;
    currentPage = 'server-detail';
  }

  function backToServers() {
    currentPage = 'servers';
    selectedServerId = null;
  }

  async function handleLogout() {
    try {
      await logout();
      window.location.href = '/';
    } catch (err) {
      console.error('Logout error:', err);
    }
  }
</script>

<div class="dashboard-container">
  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-logo">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
      </div>
      <h2 class="sidebar-title">DCIM</h2>
    </div>
    
    <div class="sidebar-nav">
      <ul class="nav-list">
        <li class="nav-item" class:active={currentPage === 'dashboard'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('dashboard')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
            </svg>
            <span>Dashboard</span>
          </a>
        </li>
        <li class="nav-item" class:active={currentPage === 'servers'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('servers')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
            </svg>
            <span>Servers</span>
          </a>
        </li>
        <li class="nav-item" class:active={currentPage === 'locations'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('locations')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span>Locations</span>
          </a>
        </li>
        <li class="nav-item" class:active={currentPage === 'plugins'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('plugins')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            <span>Plugins</span>
          </a>
        </li>
        <li class="nav-item" class:active={currentPage === 'os-templates'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('os-templates')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
            </svg>
            <span>OS Templates</span>
          </a>
        </li>
        <li class="nav-item" class:active={currentPage === 'services'}>
          <a href="#" class="nav-link" on:click|preventDefault={() => navigate('services')}>
            <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            <span>Services</span>
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

  <!-- Main content -->
  <main class="main-content">
    {#if currentPage === 'dashboard'}
      <PageHeader title="Dashboard" onNavigate={navigate} />
      <div class="content-body">
        <!-- Dashboard content will go here -->
      </div>
    {:else if currentPage === 'servers'}
      <Servers onViewServer={viewServer} />
    {:else if currentPage === 'server-detail'}
      <ServerDetail serverId={selectedServerId} onBack={backToServers} />
    {:else if currentPage === 'locations'}
      <Locations />
    {:else if currentPage === 'plugins'}
      <Plugins />
    {:else if currentPage === 'os-templates'}
      <OSTemplates />
    {:else if currentPage === 'services'}
      <Services onNavigate={navigate} />
    {:else if currentPage === 'user'}
      <User />
    {/if}
  </main>
</div>

<style>
  .dashboard-container {
    display: flex;
    min-height: 100vh;
    background: #f8fafc;
  }

  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    width: 260px;
    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
    color: white;
    display: flex;
    flex-direction: column;
    z-index: 100;
    box-shadow: 4px 0 20px rgba(0, 0, 0, 0.1);
  }

  .sidebar-header {
    padding: 24px 20px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .sidebar-logo {
    width: 40px;
    height: 40px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
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
    border-top: 1px solid rgba(255, 255, 255, 0.1);
  }

  .btn-logout {
    width: 100%;
    padding: 12px 16px;
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
    border: 1px solid rgba(239, 68, 68, 0.2);
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
    background: rgba(239, 68, 68, 0.2);
    border-color: rgba(239, 68, 68, 0.3);
    transform: translateY(-1px);
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

  .nav-item.active .nav-link {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    color: rgba(255, 255, 255, 0.7);
    text-decoration: none;
    border-radius: 10px;
    transition: all 0.2s ease;
    font-weight: 500;
  }

  .nav-link:hover {
    background: rgba(255, 255, 255, 0.1);
    color: white;
  }

  .nav-icon {
    width: 20px;
    height: 20px;
  }


  .main-content {
    flex: 1;
    margin-left: 260px;
    min-height: 100vh;
  }


  .header-actions {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .user-badge {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 16px;
    background: #f8fafc;
    border-radius: 12px;
  }

  .user-avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 14px;
  }

  .user-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  .content-body {
    padding: 32px;
  }

  @media (max-width: 768px) {
    .sidebar {
      transform: translateX(-100%);
      transition: transform 0.3s ease;
    }

    .main-content {
      margin-left: 0;
    }
  }
</style>

