<script>
  import { currentRoute, navigate } from '../../lib/router.js';
  import { user, logout } from '../../stores/auth.js';
  import { theme, toggleTheme } from '../../stores/theme.js';

  let menuOpen = false;
  let loggingOut = false;
  const links = [
    ['/reseller', 'Dashboard'],
    ['/reseller/top-up', 'Top up'],
    ['/reseller/payment-methods', 'Payments'],
    ['/reseller/invoices', 'Invoices'],
    ['/reseller/products', 'Products'],
    ['/reseller/clients', 'Clients'],
    ['/reseller/services', 'Services'],
    ['/reseller/stock', 'Stock'],
    ['/reseller/api', 'API'],
  ];

  function normalizePath(path) {
    const value = (path || '/').split('?')[0].split('#')[0];
    if (value.length > 1 && value.endsWith('/')) return value.slice(0, -1);
    return value || '/';
  }

  // Must be a reactive declaration: template calls to isActive() do not
  // track store reads inside the function body, so the active tab would stick.
  $: path = normalizePath($currentRoute || (typeof window !== 'undefined' ? window.location.pathname : '/'));

  function isActive(href, currentPath) {
    const target = normalizePath(href);
    if (target === '/reseller') return currentPath === '/reseller';
    return currentPath === target || currentPath.startsWith(`${target}/`);
  }

  async function handleLogout() {
    loggingOut = true;
    await logout();
    navigate('/login');
  }
</script>

<div class="reseller-shell">
  <header class="topbar">
    <div class="topbar-inner">
      <a href="/reseller" class="brand" aria-label="Rackflow reseller control panel">
        <span class="brand-mark" aria-hidden="true">R</span>
        <span>Rackflow</span>
        <small>Reseller</small>
      </a>
      <button
        class="menu-button"
        type="button"
        aria-label="Toggle reseller navigation"
        aria-expanded={menuOpen}
        on:click={() => (menuOpen = !menuOpen)}
      >☰</button>
      <nav class:open={menuOpen} aria-label="Reseller control panel">
        {#each links as [href, label]}
          <a
            href={href}
            class:active={isActive(href, path)}
            aria-current={isActive(href, path) ? 'page' : undefined}
            on:click={() => (menuOpen = false)}
          >{label}</a>
        {/each}
      </nav>
      <div class="account-actions">
        <button type="button" class="quiet" on:click={toggleTheme} aria-label="Toggle theme">
          {$theme === 'dark' ? '☀' : '☾'}
        </button>
        <span title={$user?.email || ''}>{$user?.username}</span>
        <button type="button" class="logout" on:click={handleLogout} disabled={loggingOut}>
          {loggingOut ? 'Signing out…' : 'Log out'}
        </button>
      </div>
    </div>
  </header>
  <main class="reseller-page"><slot /></main>
</div>

<style>
  .reseller-shell { min-height: 100vh; background: var(--bg-secondary); color: var(--text-primary); }
  .topbar {
    position: sticky; top: 0; z-index: 50;
    background: var(--portal-nav-bg); border-bottom: 1px solid var(--border-color);
    backdrop-filter: blur(12px);
  }
  .topbar-inner {
    max-width: 1320px; min-height: 64px; margin: 0 auto; padding: 8px 24px;
    display: flex; align-items: center; gap: 20px;
  }
  .brand { display: flex; align-items: center; gap: 8px; color: var(--text-primary); text-decoration: none; font-weight: 800; white-space: nowrap; }
  .brand-mark { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; background: var(--portal-accent); color: var(--portal-accent-contrast); }
  .brand small { padding: 3px 7px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); text-transform: uppercase; font-size: 9px; letter-spacing: .08em; }
  nav { flex: 1; display: flex; align-items: center; gap: 2px; }
  nav a {
    padding: 8px 9px; border-radius: 7px; color: var(--text-tertiary); text-decoration: none;
    font-size: 13px; font-weight: 650; white-space: nowrap;
    border-bottom: 2px solid transparent;
  }
  nav a:hover { background: var(--portal-accent-soft); color: var(--portal-accent); }
  nav a:focus { outline: none; }
  nav a:focus-visible { box-shadow: inset 0 0 0 2px var(--portal-accent); }
  nav a.active {
    background: var(--portal-accent-soft); color: var(--portal-accent);
    border-bottom-color: var(--portal-accent);
  }
  .account-actions { display: flex; align-items: center; gap: 9px; color: var(--text-secondary); font-size: 13px; white-space: nowrap; }
  button { font: inherit; }
  .quiet { width: 34px; height: 34px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); cursor: pointer; }
  .logout { border: 0; background: none; color: var(--text-tertiary); text-decoration: underline; cursor: pointer; }
  .menu-button { display: none; margin-left: auto; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); padding: 7px 10px; }
  .reseller-page { max-width: 1220px; margin: 0 auto; padding: 30px 24px 60px; }
  @media (max-width: 1100px) {
    .topbar-inner { flex-wrap: wrap; gap: 8px 16px; }
    nav { order: 3; flex-basis: 100%; overflow-x: auto; padding-bottom: 2px; }
    .account-actions { margin-left: auto; }
  }
  @media (max-width: 680px) {
    .topbar-inner { padding: 10px 16px; }
    .menu-button { display: block; }
    .account-actions span { display: none; }
    nav { display: none; flex-direction: column; align-items: stretch; overflow: visible; }
    nav.open { display: flex; }
    nav a { padding: 10px 12px; }
    .reseller-page { padding: 22px 16px 44px; }
  }
</style>
