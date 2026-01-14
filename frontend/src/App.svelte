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

{#if $isAuthenticated}
  <Dashboard />
{:else}
  <Login />
{/if}

