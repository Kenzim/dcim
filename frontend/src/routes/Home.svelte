<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, user, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import { theme, toggleTheme } from '../stores/theme.js';

  onMount(async () => {
    await checkAuth();
  });

  function handleLogin() {
    navigate('/login');
  }
</script>

<div class="home">
  <button class="theme-toggle" type="button" on:click={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
    {#if $theme === 'dark'}
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
    {:else}
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" /></svg>
    {/if}
  </button>

  <div class="hero">
    <div class="brand">
      <div class="logo-icon">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        </svg>
      </div>
      <h1 class="title">Rackflow</h1>
      <p class="subtitle">Data Center Infrastructure Management</p>
    </div>

    <div class="actions">
      {#if $isAuthenticated && $user?.is_admin}
        <a href="/admin" class="btn-primary">Go to Admin Panel</a>
      {:else if $isAuthenticated && $user?.is_reseller}
        <a href="/reseller" class="btn-primary">Go to Reseller Panel</a>
      {:else if $isAuthenticated}
        <a href="/client" class="btn-primary">Go to My Services</a>
      {:else}
        <button class="btn-primary" type="button" on:click={handleLogin}>Sign in</button>
        <a href="/client" class="btn-secondary">Client Portal</a>
      {/if}
    </div>
  </div>
</div>

<style>
  .home {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
    position: relative;
  }

  .theme-toggle {
    position: absolute;
    top: 20px;
    right: 20px;
    width: 40px;
    height: 40px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-primary);
    border: 2px solid var(--portal-accent, var(--accent-color));
    border-radius: 8px;
    color: var(--portal-accent, var(--accent-color));
    cursor: pointer;
    transition: all 0.2s ease;
  }
  .theme-toggle:hover {
    background: var(--portal-accent, var(--accent-color));
    color: var(--portal-accent-contrast, white);
  }
  .theme-toggle svg {
    width: 22px;
    height: 22px;
  }

  .hero {
    width: 100%;
    max-width: 640px;
    text-align: center;
    padding: 64px 40px;
    border-radius: var(--radius-lg, 14px);
    background: linear-gradient(135deg, var(--portal-hero-from) 0%, var(--portal-hero-to) 100%);
    color: var(--portal-hero-text, white);
    box-shadow: var(--shadow-xl);
  }

  .logo-icon {
    width: 76px;
    height: 76px;
    margin: 0 auto 22px;
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .logo-icon svg {
    width: 44px;
    height: 44px;
    color: var(--portal-hero-text, white);
  }

  .title {
    font-size: 44px;
    font-weight: 750;
    margin: 0 0 10px;
    letter-spacing: -0.03em;
  }

  .subtitle {
    font-size: 16px;
    margin: 0 0 44px;
    font-weight: 300;
    color: var(--portal-hero-muted, rgba(255, 255, 255, 0.8));
  }

  .actions {
    display: flex;
    gap: 14px;
    justify-content: center;
    flex-wrap: wrap;
  }

  .btn-primary,
  .btn-secondary {
    padding: 13px 30px;
    border-radius: var(--radius-md, 10px);
    font-size: 15px;
    font-weight: 650;
    text-decoration: none;
    transition: all 0.2s ease;
    display: inline-block;
    border: none;
    cursor: pointer;
    font-family: inherit;
  }

  .btn-primary {
    background: var(--portal-hero-text, white);
    color: var(--portal-hero-from, #0f2a3d);
    box-shadow: var(--shadow-md);
  }
  .btn-primary:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg);
  }

  .btn-secondary {
    background: rgba(255, 255, 255, 0.12);
    color: var(--portal-hero-text, white);
    border: 1px solid rgba(255, 255, 255, 0.28);
  }
  .btn-secondary:hover {
    background: rgba(255, 255, 255, 0.22);
  }

  @media (max-width: 640px) {
    .hero {
      padding: 48px 24px;
    }
    .title {
      font-size: 34px;
    }
  }
</style>
