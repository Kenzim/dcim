<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  
  onMount(async () => {
    await checkAuth();
  });
  
  function handleAdminLogin() {
    navigate('/admin');
  }
</script>

<div class="home-container">
  <div class="home-content">
    <div class="home-header">
      <div class="logo-icon">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
      </div>
      <h1 class="home-title">DCIM</h1>
      <p class="home-subtitle">Data Center Infrastructure Management</p>
    </div>
    
    <div class="home-actions">
      {#if $isAuthenticated}
        <a href="/admin" class="btn-primary">Go to Admin Panel</a>
      {:else}
        <button class="btn-primary" on:click={handleAdminLogin}>Admin Login</button>
      {/if}
      <a href="/client" class="btn-secondary">Client Portal</a>
    </div>
  </div>
</div>

<style>
  .home-container {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
  }

  .home-content {
    text-align: center;
    color: var(--text-primary);
    max-width: 600px;
  }

  .logo-icon {
    width: 80px;
    height: 80px;
    margin: 0 auto 24px;
    background: var(--accent-color);
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .logo-icon svg {
    color: white;
  }

  .logo-icon svg {
    width: 48px;
    height: 48px;
  }

  .home-title {
    font-size: 48px;
    font-weight: 700;
    margin: 0 0 12px;
    letter-spacing: -1px;
  }

  .home-subtitle {
    font-size: 18px;
    opacity: 0.9;
    margin: 0 0 48px;
    font-weight: 300;
  }

  .home-actions {
    display: flex;
    gap: 16px;
    justify-content: center;
    flex-wrap: wrap;
  }

  .btn-primary,
  .btn-secondary {
    padding: 14px 32px;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    text-decoration: none;
    transition: all 0.2s ease;
    display: inline-block;
    border: none;
    cursor: pointer;
    font-family: inherit;
  }

  .btn-primary {
    background: var(--accent-color);
    color: white;
    box-shadow: var(--shadow-md);
  }

  .btn-primary:hover {
    background: var(--accent-dark);
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg);
  }

  .btn-secondary {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 2px solid var(--border-color);
  }

  .btn-secondary:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
  }
</style>
