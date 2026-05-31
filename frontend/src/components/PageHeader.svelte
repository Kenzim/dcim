<script>
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { logout, user } from '../stores/auth.js';
  import { getCurrentUser } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { theme, toggleTheme } from '../stores/theme.js';

  export let title = 'Dashboard';

  let userData = null;
  let showUserMenu = false;
  let userMenuTimeout = null;

  // Use reactive statement to get user from store
  $: userData = $user;

  onMount(async () => {
    // Always try to load user data to ensure it's fresh
    await loadUserData();
  });

  async function loadUserData() {
    try {
      const data = await getCurrentUser();
      if (data) {
        user.set(data);
      }
    } catch (err) {
      console.error('Error loading user data:', err);
    }
  }

  function handleMouseEnter() {
    if (userMenuTimeout) {
      clearTimeout(userMenuTimeout);
      userMenuTimeout = null;
    }
    showUserMenu = true;
  }

  function handleMouseLeave() {
    userMenuTimeout = setTimeout(() => {
      showUserMenu = false;
      userMenuTimeout = null;
    }, 200);
  }

  async function handleLogout() {
    try {
      await logout();
    } catch (error) {
      console.error('Logout error:', error);
    } finally {
      showUserMenu = false;
      navigate('/');
    }
  }

  function handleUserClick() {
    navigate('/admin/user');
    showUserMenu = false;
  }
</script>

<div class="page-header">
  <h1 class="page-title">{title}</h1>
  <div class="header-actions">
    <slot name="actions" />
    <button class="theme-toggle" on:click={toggleTheme} title="Toggle theme">
      {#if $theme === 'light'}
        <svg xmlns="http://www.w3.org/2000/svg" class="theme-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
        </svg>
      {:else}
        <svg xmlns="http://www.w3.org/2000/svg" class="theme-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
        </svg>
      {/if}
    </button>
    {#if $user}
      <div 
        class="user-menu-container" 
        role="menu"
        tabindex="-1"
        on:mouseenter={handleMouseEnter}
        on:mouseleave={handleMouseLeave}
      >
        <button class="user-badge" on:click={handleUserClick}>
          <div class="user-avatar">
            {$user.username.charAt(0).toUpperCase()}
          </div>
          <span class="user-name">{$user.username}</span>
          <svg xmlns="http://www.w3.org/2000/svg" class="chevron-icon" class:rotated={showUserMenu} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {#if showUserMenu}
          <div class="user-dropdown" transition:fly={{ y: -8, duration: 150 }}>
            <button class="dropdown-item" on:click={() => { handleUserClick(); }}>
              <svg xmlns="http://www.w3.org/2000/svg" class="dropdown-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              <span>User Management</span>
            </button>
            <div class="dropdown-divider"></div>
            <button class="dropdown-item logout-item" on:click={() => { handleLogout(); showUserMenu = false; }}>
              <svg xmlns="http://www.w3.org/2000/svg" class="dropdown-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
              <span>Logout</span>
            </button>
          </div>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .page-header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg-primary);
    padding: 16px 32px;
    border-bottom: 1px solid var(--border-color);
    box-shadow: var(--shadow-sm);
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 60px;
    box-sizing: border-box;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .page-title {
    font-size: 22px;
    font-weight: 700;
    margin: 0;
    color: var(--text-primary);
    letter-spacing: -0.5px;
    line-height: 1.2;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .theme-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    padding: 0;
    background: var(--bg-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s ease;
    color: var(--accent-color);
  }

  .theme-toggle:hover {
    background: var(--accent-color);
    color: white;
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .theme-icon {
    width: 20px;
    height: 20px;
  }

  .user-menu-container {
    position: relative;
    /* Create invisible bridge to prevent gap issues */
  }

  .user-menu-container::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    height: 8px;
    /* Invisible bridge to keep hover active when moving to dropdown */
  }

  .user-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    background: var(--bg-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s ease;
    font-size: 14px;
  }

  .user-badge:hover {
    background: var(--accent-color);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }
  
  .user-badge:hover .user-name {
    color: white;
  }
  
  .user-badge:hover .chevron-icon {
    color: white;
  }

  .user-avatar {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--accent-color);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 12px;
    flex-shrink: 0;
  }

  .user-name {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 13px;
  }

  .chevron-icon {
    width: 16px;
    height: 16px;
    color: var(--text-secondary);
    transition: transform 0.2s ease;
  }

  .chevron-icon.rotated {
    transform: rotate(180deg);
  }

  .user-menu-container:hover .chevron-icon {
    transform: rotate(180deg);
  }

  .user-dropdown {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow: var(--shadow-lg);
    min-width: 200px;
    overflow: hidden;
    z-index: 1000;
  }

  .dropdown-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    color: var(--text-primary);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    transition: background 0.2s ease;
    border: none;
    background: none;
    width: 100%;
    text-align: left;
    cursor: pointer;
  }

  .dropdown-item:hover {
    background: var(--bg-tertiary);
  }

  .dropdown-item.logout-item {
    color: var(--danger-color);
  }

  .dropdown-item.logout-item:hover {
    background: var(--bg-tertiary);
    opacity: 0.9;
  }

  .dropdown-icon {
    width: 18px;
    height: 18px;
    flex-shrink: 0;
  }

  .dropdown-divider {
    height: 1px;
    background: var(--border-color);
    margin: 4px 0;
  }
</style>

