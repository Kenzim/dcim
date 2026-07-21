<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, checkAuth } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import ClientServices from '../components/ClientServices.svelte';

  let authChecked = false;

  onMount(async () => {
    await checkAuth();
    authChecked = true;
    if (!$isAuthenticated && window.location.pathname !== '/login') {
      navigate('/login');
    }
  });
</script>

{#if !authChecked}
  <div class="client-container">
    <div class="client-content">
      <p>Loading...</p>
    </div>
  </div>
{:else if $isAuthenticated}
  <ClientServices />
{:else}
  <div class="client-container">
    <div class="client-content">
      <h1>Client Portal</h1>
      <p>Please sign in to view your services.</p>
      <a href="/login" class="btn-link">Login</a>
    </div>
  </div>
{/if}

<style>
  .client-container {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
  }

  .client-content {
    text-align: center;
    max-width: 600px;
  }

  h1 {
    font-size: 36px;
    font-weight: 700;
    margin: 0 0 16px;
    color: var(--text-primary);
  }

  p {
    font-size: 18px;
    color: var(--text-secondary);
    margin: 0 0 24px;
  }

  .btn-link {
    display: inline-block;
    padding: 12px 24px;
    background: var(--primary-color);
    color: white;
    text-decoration: none;
    border-radius: 8px;
    font-weight: 600;
    transition: all 0.2s ease;
  }

  .btn-link:hover {
    background: var(--primary-dark);
    transform: translateY(-2px);
  }
</style>
