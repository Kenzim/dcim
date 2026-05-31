<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { listMyServices } from '../lib/api.js';

  let services = [];
  let loading = true;
  let error = null;

  function formatType(t) {
    if (t === 'vm') return 'Virtual machine';
    if (t === 'bare_metal') return 'Bare metal';
    if (t === 'http_proxy') return 'HTTP proxy';
    return t || 'Service';
  }

  onMount(async () => {
    try {
      services = await listMyServices();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  });
</script>

<PageHeader title="My Services" />

<div class="client-services">
  {#if loading}
    <p>Loading services...</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if services.length === 0}
    <p>No services assigned yet.</p>
  {:else}
    <div class="grid">
      {#each services as s}
        <article class="card">
          <h3>{s.name}</h3>
          <p><strong>Type:</strong> {formatType(s.service_type)}</p>
          <p><strong>Status:</strong> {s.status}</p>
          {#if s.service_type === 'vm'}
            <p><strong>Placement:</strong> cluster {s.proxmox_cluster_id ?? '—'} / {s.proxmox_node_name || '—'} / {s.proxmox_vmid ?? '—'}</p>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .client-services {
    padding: 32px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }
  .card {
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 16px;
    background: var(--bg-secondary);
  }
  .card h3 {
    margin-top: 0;
  }
  .error {
    color: var(--danger-color);
  }
</style>
