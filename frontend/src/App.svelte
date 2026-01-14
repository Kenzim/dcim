<script>
  import { onMount } from 'svelte';
  import { isAuthenticated, checkAuth } from './stores/auth.js';
  import Login from './components/Login.svelte';
  import Dashboard from './components/Dashboard.svelte';
  
  // Bootstrap CSS
  import 'bootstrap/dist/css/bootstrap.min.css';
  // Global styles
  import './app.css';

  onMount(async () => {
    await checkAuth();
  });
</script>

<div class="app-container">
  {#if $isAuthenticated}
    <Dashboard />
  {:else}
    <Login />
  {/if}
</div>

<style>
  .app-container {
    min-height: 100vh;
  }
</style>

