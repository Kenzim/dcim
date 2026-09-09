<script>
  import { onMount } from 'svelte';
  import { Alert, StatTile } from '../ui/index.js';
  import { user } from '../../stores/auth.js';
  import { listMyServices, clientCommerceListProducts } from '../../lib/api.js';
  import ServiceCard from './ServiceCard.svelte';

  let services = [];
  let storeAvailable = false;
  let loading = true;
  let error = '';

  $: running = services.filter((s) => s.power_state === 'on').length;
  $: stopped = services.filter((s) => s.power_state === 'off').length;
  $: attention = services.filter((s) =>
    ['suspended', 'terminated'].includes(String(s.status || '').toLowerCase())
  ).length;

  async function load() {
    try {
      services = await listMyServices();
      error = '';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    await load();
    try {
      const products = await clientCommerceListProducts({ limit: 1 });
      storeAvailable = products.length > 0;
    } catch (e) {
      if (e.status !== 503) storeAvailable = false;
    }
  });
</script>

<section class="hero">
  <div class="hero-copy">
    <h1>Hello, {$user?.username}</h1>
    <p>Here's the state of your infrastructure.</p>
  </div>
  <a href="/client/services" class="hero-cta">
    All services
    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15" aria-hidden="true">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
    </svg>
  </a>
</section>

{#if loading}
  <div class="kpis">
    {#each [1, 2, 3, 4] as _}
      <div class="skeleton-kpi" aria-hidden="true"></div>
    {/each}
  </div>
  <div class="grid">
    {#each [1, 2, 3] as _}
      <div class="skeleton-card" aria-hidden="true"></div>
    {/each}
  </div>
{:else if error}
  <Alert type="error">{error}</Alert>
{:else if services.length === 0}
  <div class="empty">
    <h2>Welcome to your portal</h2>
    <p>You don't have any services yet. Once a service is provisioned for your account, you can manage power, consoles, backups, and credentials from here.</p>
    {#if storeAvailable}
      <a href="/client/checkout" class="empty-cta">Browse products & order</a>
    {/if}
  </div>
{:else}
  <div class="kpis">
    <StatTile label="Services" value={services.length} tone="accent" />
    <StatTile label="Running" value={running} tone="success" />
    <StatTile label="Stopped" value={stopped} />
    <StatTile label="Needs attention" value={attention} tone={attention > 0 ? 'warning' : 'default'} hint={attention > 0 ? 'Suspended or terminated' : ''} />
  </div>

  <h2 class="section-title">Your services</h2>
  <div class="grid">
    {#each services as service (service.id)}
      <ServiceCard {service} showPower on:changed={load} />
    {/each}
  </div>
{/if}

<style>
  .hero {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 28px 30px;
    margin-bottom: 22px;
    border-radius: var(--radius-lg);
    background: linear-gradient(120deg, var(--portal-hero-from) 0%, var(--portal-hero-to) 100%);
    color: var(--portal-hero-text);
    box-shadow: var(--shadow-lg);
  }
  .hero h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .hero p {
    margin: 6px 0 0;
    font-size: 14px;
    color: var(--portal-hero-muted);
  }
  .hero-cta {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 10px 18px;
    border-radius: var(--radius-md);
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.24);
    color: var(--portal-hero-text);
    font-size: 14px;
    font-weight: 650;
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.15s ease;
  }
  .hero-cta:hover {
    background: rgba(255, 255, 255, 0.24);
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }
  .section-title {
    margin: 0 0 14px;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 16px;
  }
  .skeleton-kpi {
    height: 96px;
    border-radius: var(--radius-lg);
    background: linear-gradient(100deg, var(--bg-tertiary) 40%, var(--bg-secondary) 50%, var(--bg-tertiary) 60%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite;
  }
  .skeleton-card {
    height: 190px;
    border-radius: var(--radius-lg);
    background: linear-gradient(100deg, var(--bg-tertiary) 40%, var(--bg-secondary) 50%, var(--bg-tertiary) 60%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite;
  }
  @keyframes shimmer {
    to {
      background-position: -200% 0;
    }
  }
  .empty {
    text-align: center;
    padding: 56px 28px;
    background: var(--portal-card-bg);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-lg);
  }
  .empty h2 {
    margin: 0 0 8px;
    font-size: 20px;
    font-weight: 700;
  }
  .empty p {
    margin: 0 auto 16px;
    max-width: 480px;
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.55;
  }
  .empty-cta {
    display: inline-flex;
    padding: 10px 18px;
    border-radius: var(--radius-md);
    background: var(--portal-accent);
    color: var(--portal-accent-contrast);
    font-size: 14px;
    font-weight: 650;
    text-decoration: none;
  }
  .empty-cta:hover {
    filter: brightness(1.05);
  }
  @media (max-width: 900px) {
    .kpis {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (max-width: 640px) {
    .hero {
      flex-direction: column;
      align-items: flex-start;
      padding: 22px;
    }
    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
