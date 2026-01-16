<script>
  import { onMount } from 'svelte';
  import { getPlugins } from '../lib/api.js';
  import PageHeader from './PageHeader.svelte';

  let plugins = [];
  let loading = true;
  let error = null;

  onMount(async () => {
    await loadPlugins();
  });

  async function loadPlugins() {
    loading = true;
    error = null;
    try {
      plugins = await getPlugins() || [];
    } catch (err) {
      error = err.message || 'Failed to load plugins';
      console.error('Error loading plugins:', err);
    } finally {
      loading = false;
    }
  }
</script>

<div class="plugins-page">
  <PageHeader title="Plugins" />

  <div class="page-content">
    {#if loading}
      <div class="loading-container">
        <div class="spinner"></div>
        <p>Loading plugins...</p>
      </div>
    {:else if error}
      <div class="alert alert-error">
        <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        {error}
      </div>
    {:else if plugins.length === 0}
      <div class="empty-state">
        <svg xmlns="http://www.w3.org/2000/svg" class="empty-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
        </svg>
        <h3>No plugins found</h3>
        <p>No plugins are currently available.</p>
      </div>
    {:else}
      <div class="plugins-grid">
        {#each plugins as plugin}
          <div class="plugin-card">
            <div class="plugin-header">
              <div class="plugin-icon">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                </svg>
              </div>
              <div class="plugin-title-section">
                <h3 class="plugin-name">{plugin.name}</h3>
                <span class="plugin-version">v{plugin.version}</span>
              </div>
              {#if plugin.registered}
                <span class="plugin-badge registered">Registered</span>
              {:else}
                <span class="plugin-badge unregistered">Not Registered</span>
              {/if}
            </div>

            <div class="plugin-body">
              <div class="plugin-categories">
                <h4 class="categories-title">Supported Categories:</h4>
                <div class="categories-list">
                  {#each plugin.supported_categories as category}
                    <span class="category-badge">{category.replace('_', ' ')}</span>
                  {/each}
                </div>
              </div>

              {#if plugin.available_capabilities && plugin.available_capabilities.length > 0}
                <div class="plugin-capabilities">
                  <h4 class="capabilities-title">Available Capabilities:</h4>
                  <div class="capabilities-list">
                    {#each plugin.available_capabilities as capability}
                      <span class="capability-badge">
                        {capability.replace(/_/g, ' ')}
                      </span>
                    {/each}
                  </div>
                  <p class="capabilities-note">Note: Capabilities are tested per server. Test capabilities when creating or editing a server.</p>
                </div>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .plugins-page {
    min-height: 100vh;
    background: #f8fafc;
  }

  .page-content {
    padding: 32px;
  }

  .loading-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 80px 20px;
    color: var(--text-secondary);
  }

  .spinner {
    width: 48px;
    height: 48px;
    border: 4px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 16px;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .alert {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
    border-radius: 12px;
    margin-bottom: 24px;
  }

  .alert-error {
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fecaca;
  }

  .alert-icon {
    width: 20px;
    height: 20px;
    flex-shrink: 0;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 80px 20px;
    text-align: center;
    color: var(--text-secondary);
  }

  .empty-icon {
    width: 64px;
    height: 64px;
    margin-bottom: 16px;
    opacity: 0.5;
  }

  .empty-state h3 {
    font-size: 20px;
    font-weight: 600;
    margin: 0 0 8px;
    color: var(--text-primary);
  }

  .empty-state p {
    margin: 0;
    font-size: 14px;
  }

  .plugins-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 24px;
  }

  .plugin-card {
    background: white;
    border-radius: 16px;
    padding: 24px;
    box-shadow: var(--shadow-md);
    border: 1px solid var(--border-color);
    transition: all 0.3s ease;
  }

  .plugin-card:hover {
    transform: translateY(-4px);
    box-shadow: var(--shadow-lg);
  }

  .plugin-header {
    display: flex;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 20px;
  }

  .plugin-icon {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .plugin-icon svg {
    width: 24px;
    height: 24px;
    color: white;
  }

  .plugin-title-section {
    flex: 1;
  }

  .plugin-name {
    font-size: 18px;
    font-weight: 700;
    margin: 0 0 4px;
    color: var(--text-primary);
  }

  .plugin-version {
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .plugin-badge {
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .plugin-badge.registered {
    background: #d1fae5;
    color: #065f46;
  }

  .plugin-badge.unregistered {
    background: #e2e8f0;
    color: #475569;
  }

  .plugin-body {
    margin-top: 20px;
  }

  .categories-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 12px;
  }

  .categories-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .category-badge {
    padding: 6px 12px;
    background: #f1f5f9;
    color: var(--text-primary);
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    text-transform: capitalize;
  }

  .plugin-capabilities {
    margin-top: 20px;
  }

  .capabilities-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 12px;
  }

  .capabilities-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .capability-badge {
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    text-transform: capitalize;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .capability-badge.tested {
    background: #d1fae5;
    color: #065f46;
  }

  .capability-badge.untested {
    background: #fef3c7;
    color: #92400e;
  }

  .capability-icon {
    width: 14px;
    height: 14px;
  }

  .plugin-actions {
    margin-top: 20px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .capabilities-note {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 8px;
    font-style: italic;
  }
</style>



