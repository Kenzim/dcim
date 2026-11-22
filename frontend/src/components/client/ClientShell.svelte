<script>
  import { tick } from 'svelte';
  import { theme, toggleTheme } from '../../stores/theme.js';
  import { currentRoute, navigate } from '../../lib/router.js';
  import { user, logout } from '../../stores/auth.js';

  /** Show the "signed in as client (admin session)" impersonation banner. */
  export let impersonating = false;

  let loggingOut = false;
  let menuOpen = false;
  let sidebarEl;
  let menuBtn;

  $: path = $currentRoute || '/client';
  $: onDashboard = path === '/client' || path === '/client/';
  $: onServices = path.startsWith('/client/services') || path.startsWith('/client/cancellations');
  $: onNetworks = path.startsWith('/client/networks');
  $: onStorage = path.startsWith('/client/storage');
  $: onBackupSlots = path.startsWith('/client/backup-slots');
  $: onBilling = path.startsWith('/client/billing') || path.startsWith('/client/orders') || path.startsWith('/client/notes');
  $: onCheckout = path.startsWith('/client/checkout');
  $: onSupport = path.startsWith('/client/support');
  $: onAccount = path.startsWith('/client/account');

  // Icon paths match the ones used by the admin sidebar.
  $: links = [
    { href: '/client', label: 'Dashboard', active: onDashboard, icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6' },
    { href: '/client/services', label: 'Services', active: onServices, icon: 'M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01' },
    { href: '/client/networks', label: 'Networks', active: onNetworks, icon: 'M12 5v6m0 0H6v4m6-4h6v4M10 3h4v2h-4zM4 15h4v4H4zm12 0h4v4h-4z' },
    { href: '/client/storage', label: 'Storage', active: onStorage, icon: 'M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3' },
    { href: '/client/backup-slots', label: 'Backup slots', active: onBackupSlots, icon: 'M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4' },
    { href: '/client/billing', label: 'Billing', active: onBilling, icon: 'M9 14l6-6m-5.5.5h.01m4.99 5h.01M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16l3.5-2 3.5 2 3.5-2 3.5 2z' },
    { href: '/client/support', label: 'Support', active: onSupport, icon: 'M18.364 5.636l-3.536 3.536m0 5.656l3.536 3.536M9.172 9.172L5.636 5.636m3.536 9.192l-3.536 3.536M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-5 0a4 4 0 11-8 0 4 4 0 018 0z' },
    { href: '/client/account', label: 'Account', active: onAccount, icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z' },
  ];

  function closeMenu() {
    menuOpen = false;
  }

  async function toggleMenu() {
    menuOpen = !menuOpen;
    if (menuOpen) {
      await tick();
      sidebarEl?.querySelector('a')?.focus();
    }
  }

  function handleKeydown(e) {
    if (e.key === 'Escape' && menuOpen) {
      closeMenu();
      menuBtn?.focus();
    }
  }

  async function handleLogout() {
    loggingOut = true;
    await logout();
    navigate('/login');
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="shell">
  {#if impersonating}
    <div class="impersonation-banner" role="status">
      You are viewing the portal as this client (admin session). Actions you take are performed as the client.
    </div>
  {/if}

  <div class="shell-body">
    {#if menuOpen}
      <div class="sidebar-backdrop" role="presentation" on:click={closeMenu}></div>
    {/if}

    <aside class="sidebar" class:open={menuOpen} id="portal-sidebar" bind:this={sidebarEl}>
      <div class="sidebar-header">
        <a href="/client" class="sidebar-logo-link" aria-label="Rackflow client portal" on:click={closeMenu}>
          <span class="sidebar-logo">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
          </span>
          <span class="sidebar-brand-text">
            <span class="sidebar-title">Rackflow</span>
            <span class="sidebar-subtitle">Portal</span>
          </span>
        </a>
      </div>

      <nav class="sidebar-nav" aria-label="Portal">
        <ul class="nav-list">
          {#each links as link (link.href)}
            <li>
              <a href={link.href} class="nav-link" class:active={link.active} aria-current={link.active ? 'page' : undefined} on:click={closeMenu}>
                <svg xmlns="http://www.w3.org/2000/svg" class="nav-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={link.icon} />
                </svg>
                <span>{link.label}</span>
              </a>
            </li>
          {/each}
        </ul>
      </nav>

      <div class="sidebar-footer">
        <div class="footer-row">
          <span class="user-chip" title={$user?.email || ''}>
            <span class="user-avatar" aria-hidden="true">{($user?.username || '?').slice(0, 1).toUpperCase()}</span>
            <span class="user-name">{$user?.username}</span>
          </span>
          <button type="button" class="icon-btn" on:click={toggleTheme} aria-label="Toggle theme" title="Toggle theme">
            {#if $theme === 'dark'}
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="18" height="18" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
            {:else}
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="18" height="18" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
              </svg>
            {/if}
          </button>
        </div>
        <button type="button" class="btn-logout" on:click={handleLogout} disabled={loggingOut}>
          <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
          </svg>
          <span>{loggingOut ? 'Signing out…' : 'Log out'}</span>
        </button>
      </div>
    </aside>

    <div class="main">
      <header class="mobile-bar">
        <button
          type="button"
          class="icon-btn menu-btn"
          class:menu-open={menuOpen}
          bind:this={menuBtn}
          on:click={toggleMenu}
          aria-label="Toggle navigation"
          aria-expanded={menuOpen}
          aria-controls="portal-sidebar"
        >
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <a href="/client" class="mobile-brand" aria-label="Rackflow client portal">
          <svg class="mobile-brand-mark" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <span class="mobile-brand-name">Rackflow</span>
          <span class="mobile-brand-tag">Portal</span>
        </a>
      </header>

      <main class="page">
        <slot />
      </main>
    </div>
  </div>
</div>

<style>
  .shell {
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    background: var(--bg-secondary);
  }
  .shell-body {
    flex: 1;
    min-height: 0;
    display: flex;
    position: relative;
  }

  .impersonation-banner {
    flex-shrink: 0;
    padding: 9px 24px;
    background: var(--warning-bg);
    color: var(--warning-text);
    font-size: 13px;
    font-weight: 600;
    text-align: center;
    border-bottom: 1px solid var(--warning-color);
  }

  /* Sidebar: same tokens and structure as the admin sidebar */
  .sidebar {
    width: var(--admin-sidebar-width, 260px);
    flex-shrink: 0;
    background:
      radial-gradient(120% 60% at 0% 0%, var(--admin-sidebar-glow), transparent 55%),
      linear-gradient(180deg, var(--admin-sidebar-elevated) 0%, var(--admin-sidebar-bg) 100%);
    color: var(--admin-sidebar-text);
    border-right: 1px solid var(--admin-sidebar-border);
    display: flex;
    flex-direction: column;
    min-height: 0;
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
    border-radius: var(--radius-sm, 4px);
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
    min-width: 0;
  }
  .sidebar-title {
    font-size: 16px;
    font-weight: 700;
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
    padding: 10px 8px;
    overflow-y: auto;
  }
  .nav-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .nav-link {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    color: var(--admin-sidebar-muted);
    text-decoration: none;
    border-radius: var(--radius-sm, 4px);
    transition: background 0.15s ease, color 0.15s ease;
    font-weight: 500;
    font-size: 14px;
    position: relative;
    border: 1px solid transparent;
  }
  .nav-link:hover {
    background: var(--admin-sidebar-hover);
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
    top: 6px;
    bottom: 6px;
    width: 2px;
    border-radius: 0 2px 2px 0;
    background: var(--admin-sidebar-accent);
  }
  .nav-icon {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
  }

  .sidebar-footer {
    padding: 10px 8px 12px;
    border-top: 1px solid var(--admin-sidebar-border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .footer-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .user-chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 4px 10px 4px 4px;
    border: 1px solid var(--admin-sidebar-border);
    border-radius: var(--radius-pill);
    background: var(--bg-primary);
  }
  .user-avatar {
    width: 26px;
    height: 26px;
    flex-shrink: 0;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--portal-accent);
    color: var(--portal-accent-contrast);
    font-size: 12px;
    font-weight: 750;
  }
  .user-name {
    font-size: 13px;
    font-weight: 650;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    padding: 7px;
    background: none;
    border: 1px solid var(--admin-sidebar-border);
    border-radius: 8px;
    color: var(--admin-sidebar-muted);
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .icon-btn:hover {
    color: var(--portal-accent);
    border-color: var(--portal-accent);
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
    transition: background 0.15s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }
  .btn-logout:hover {
    background: var(--admin-sidebar-logout-hover-bg);
  }
  .btn-logout:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .btn-icon {
    width: 15px;
    height: 15px;
  }

  .sidebar-logo-link:focus-visible,
  .nav-link:focus-visible,
  .icon-btn:focus-visible,
  .btn-logout:focus-visible,
  .mobile-brand:focus-visible {
    outline: 2px solid var(--admin-sidebar-accent);
    outline-offset: 2px;
  }

  .main {
    flex: 1;
    min-width: 0;
    overflow-y: auto;
    overflow-x: hidden;
  }
  .page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px 32px 56px;
  }

  .mobile-bar,
  .sidebar-backdrop {
    display: none;
  }

  @media (max-width: 768px) {
    .sidebar {
      position: absolute;
      top: 0;
      bottom: 0;
      left: 0;
      z-index: 1100;
      max-width: 85vw;
      transform: translateX(-100%);
      visibility: hidden;
      transition: transform 0.3s ease, visibility 0s linear 0.3s;
    }
    .sidebar.open {
      transform: translateX(0);
      visibility: visible;
      transition: transform 0.3s ease, visibility 0s;
    }
    .sidebar-backdrop {
      display: block;
      position: absolute;
      inset: 0;
      background: var(--overlay-bg);
      z-index: 1050;
    }
    .mobile-bar {
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      align-items: center;
      gap: 12px;
      height: 56px;
      padding: 0 16px;
      background: var(--portal-nav-bg);
      border-bottom: 1px solid var(--portal-nav-border);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
    }
    .menu-btn {
      color: var(--text-secondary);
      border-color: var(--border-color);
    }
    .menu-btn.menu-open {
      color: var(--portal-accent);
      border-color: var(--portal-accent);
    }
    .mobile-brand {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      text-decoration: none;
      color: var(--text-primary);
      border-radius: var(--radius-sm, 4px);
    }
    .mobile-brand-mark {
      width: 22px;
      height: 22px;
      color: var(--portal-accent);
    }
    .mobile-brand-name {
      font-size: 16px;
      font-weight: 750;
      letter-spacing: -0.02em;
    }
    .mobile-brand-tag {
      font-size: 10px;
      font-weight: 750;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 3px 7px;
      border-radius: var(--radius-pill);
      background: var(--portal-accent-soft);
      color: var(--portal-accent);
    }
    .impersonation-banner {
      padding: 8px 16px;
    }
    .page {
      padding: 20px 16px 48px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sidebar,
    .sidebar.open {
      transition: none;
    }
  }
</style>
