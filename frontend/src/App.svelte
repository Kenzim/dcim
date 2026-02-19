<script>
  import { onMount } from 'svelte';
  import { currentRoute, initRouter, getRouteComponent } from './lib/router.js';
  import routes from './routes/index.js';
  import { theme } from './stores/theme.js';
  
  // Global styles (theme store applies data-theme)
  import './app.css';
  
  let router;
  
  // Reactive component based on current route
  $: CurrentComponent = getRouteComponent(routes, $currentRoute || window.location.pathname);
  
  onMount(() => {
    router = initRouter(routes);
  });
</script>

<div class="app-container">
  {#if CurrentComponent}
    <svelte:component this={CurrentComponent} />
  {:else}
    <div style="padding: 20px; text-align: center;">
      <p>Loading route...</p>
    </div>
  {/if}
</div>

<style>
  .app-container {
    min-height: 100vh;
  }
</style>

