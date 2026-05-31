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

              {#if plugin.capabilities && plugin.capabilities.length > 0}
                <div class="plugin-capabilities">
                  <h4 class="capabilities-title">Capabilities:</h4>
                  <div class="capabilities-list">
                    {#each plugin.capabilities as cap}
                      <span class="capability-badge" class:optional={cap.optional}>
                        {cap.display_name || cap.id.replace(/_/g, ' ')}
                        {#if cap.optional}
                          <span class="optional-tag">(optional)</span>
                        {/if}
                      </span>
                    {/each}
                  </div>
                  <p class="capabilities-note">Capabilities are declared by the plugin. Optional capabilities can be enabled per server when creating or editing.</p>
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
    background: var(--bg-secondary);
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
    background: var(--danger-bg);
    color: var(--danger-text);
    border: 1px solid var(--danger-color);
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
    background: var(--bg-primary);
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
    background: var(--accent-color);
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
    background: var(--success-bg);
    color: var(--success-text);
  }

  .plugin-badge.unregistered {
    background: var(--bg-secondary);
    color: var(--text-secondary);
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
    background: var(--bg-tertiary);
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

  .capability-badge.optional .optional-tag {
    font-size: 0.75em;
    opacity: 0.8;
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

  .capabilities-note {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 8px;
    font-style: italic;
  }
</style>



