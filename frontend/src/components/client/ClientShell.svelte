<script>
  import { theme, toggleTheme } from '../../stores/theme.js';
  import { currentRoute, navigate } from '../../lib/router.js';
  import { user, logout } from '../../stores/auth.js';

  /** Show the "signed in as client (admin session)" impersonation banner. */
  export let impersonating = false;

  let loggingOut = false;
  let menuOpen = false;

  $: path = $currentRoute || '/client';
  $: onDashboard = path === '/client' || path === '/client/';
  $: onServices = path.startsWith('/client/services');
  $: onBilling = path.startsWith('/client/billing') || path.startsWith('/client/orders');
  $: onCheckout = path.startsWith('/client/checkout');
  $: onSupport = path.startsWith('/client/support');
  $: onAccount = path.startsWith('/client/account');

  async function handleLogout() {
    loggingOut = true;
    await logout();
    navigate('/login');
  }
</script>

<div class="shell">
  <header class="topbar">
    <div class="topbar-inner">
      <a href="/client" class="brand" aria-label="Rackflow client portal">
        <svg class="brand-mark" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        </svg>
        <span class="brand-name">Rackflow</span>
        <span class="brand-tag">Portal</span>
      </a>

      <nav class="nav" class:nav-open={menuOpen} aria-label="Portal">
        <a href="/client" class:active={onDashboard} on:click={() => (menuOpen = false)}>Dashboard</a>
        <a href="/client/services" class:active={onServices} on:click={() => (menuOpen = false)}>Services</a>
        <a href="/client/billing" class:active={onBilling} on:click={() => (menuOpen = false)}>Billing</a>
        <a href="/client/support" class:active={onSupport} on:click={() => (menuOpen = false)}>Support</a>
        <a href="/client/account" class:active={onAccount} on:click={() => (menuOpen = false)}>Account</a>
      </nav>

      <div class="topbar-actions">
        <button type="button" class="icon-btn" on:click={toggleTheme} aria-label="Toggle theme">
          {#if $theme === 'dark'}
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="18" height="18">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
          {:else}
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="18" height="18">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
            </svg>
          {/if}
        </button>
        <span class="user-chip" title={$user?.email || ''}>
          <span class="user-avatar" aria-hidden="true">{($user?.username || '?').slice(0, 1).toUpperCase()}</span>
          <span class="user-name">{$user?.username}</span>
        </span>
        <button type="button" class="logout-btn" on:click={handleLogout} disabled={loggingOut}>
          {loggingOut ? 'Signing out…' : 'Log out'}
        </button>
        <button
          type="button"
          class="icon-btn menu-btn"
          class:menu-open={menuOpen}
          on:click={() => (menuOpen = !menuOpen)}
          aria-label="Toggle navigation"
          aria-expanded={menuOpen}
        >
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
      </div>
    </div>
  </header>

  {#if impersonating}
    <div class="impersonation-banner" role="status">
      You are viewing the portal as this client (admin session). Actions you take are performed as the client.
    </div>
  {/if}

  <main class="page">
    <slot />
  </main>
</div>

<style>
  .shell {
    min-height: 100vh;
    background: var(--bg-secondary);
  }
  .topbar {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--portal-nav-bg);
    border-bottom: 1px solid var(--portal-nav-border);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
  }
  .topbar-inner {
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 24px;
    height: 60px;
    display: flex;
    align-items: center;
    gap: 28px;
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 9px;
    text-decoration: none;
    color: var(--text-primary);
    flex-shrink: 0;
  }
  .brand-mark {
    width: 24px;
    height: 24px;
    color: var(--portal-accent);
  }
  .brand-name {
    font-size: 17px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .brand-tag {
    font-size: 10px;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 3px 7px;
    border-radius: var(--radius-pill);
    background: var(--portal-accent-soft);
    color: var(--portal-accent);
  }
  .nav {
    display: flex;
    gap: 4px;
    flex: 1;
  }
  .nav a {
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-tertiary);
    text-decoration: none;
    transition: color 0.15s ease, background 0.15s ease;
  }
  .nav a:hover {
    color: var(--text-primary);
    background: var(--bg-tertiary);
  }
  .nav a.active {
    color: var(--portal-accent);
    background: var(--portal-accent-soft);
  }
  .topbar-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 7px;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .icon-btn:hover {
    color: var(--portal-accent);
    border-color: var(--portal-accent);
  }
  .menu-btn {
    display: none;
  }
  .user-chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px 4px 4px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-pill);
    background: var(--bg-primary);
  }
  .user-avatar {
    width: 26px;
    height: 26px;
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
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .logout-btn {
    appearance: none;
    background: none;
    border: none;
    padding: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-tertiary);
    cursor: pointer;
    text-decoration: underline;
  }
  .logout-btn:hover {
    color: var(--danger-color);
  }
  .logout-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .impersonation-banner {
    padding: 9px 24px;
    background: var(--warning-bg);
    color: var(--warning-text);
    font-size: 13px;
    font-weight: 600;
    text-align: center;
    border-bottom: 1px solid var(--warning-color);
  }
  .page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px 24px 56px;
  }

  @media (max-width: 760px) {
    .topbar-inner {
      gap: 12px;
      padding: 0 16px;
    }
    .nav {
      display: none;
      position: absolute;
      top: 60px;
      left: 0;
      right: 0;
      flex-direction: column;
      gap: 2px;
      padding: 10px 16px 14px;
      background: var(--portal-nav-bg);
      border-bottom: 1px solid var(--portal-nav-border);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
    }
    .nav.nav-open {
      display: flex;
    }
    .menu-btn {
      display: inline-flex;
    }
    .menu-btn.menu-open {
      color: var(--portal-accent);
      border-color: var(--portal-accent);
    }
    .user-name {
      display: none;
    }
    .user-chip {
      padding: 4px;
      border: none;
      background: none;
    }
    .page {
      padding: 20px 16px 48px;
    }
  }
</style>
