<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import StorageSharesPanel from '../storage/StorageSharesPanel.svelte';
  import StorageSftpPanel from '../storage/StorageSftpPanel.svelte';
  import { clientListStorage, clientStorageNetworks, clientLinkStorage } from '../../lib/api.js';
  import { STORAGE_STATUS_LABELS } from '../../lib/storage.js';

  let storages = [];
  let networksByService = {};
  let chosen = {};
  let loading = true;
  let working = false;
  let error = '';
  let success = '';

  async function load() {
    try {
      storages = await clientListStorage();
      const unlinked = storages.filter((s) => s.status === 'unlinked');
      const lists = await Promise.all(unlinked.map((s) => clientStorageNetworks(s.service_id)));
      networksByService = Object.fromEntries(unlinked.map((s, i) => [s.service_id, lists[i]]));
    } catch (err) {
      error = err.message || 'Failed to load storage';
    } finally {
      loading = false;
    }
  }

  async function link(storage) {
    const choice = chosen[storage.service_id];
    if (!choice) return;
    const networkId = choice === 'new' ? null : Number(choice);
    const network = (networksByService[storage.service_id] || []).find((n) => n.id === networkId);
    const target = networkId ? network?.name : 'a new private network';
    if (!confirm(`Link ${storage.service_name} to ${target}? A storage stays on the network it is linked to.`)) return;
    working = true;
    error = '';
    success = '';
    try {
      await clientLinkStorage(storage.service_id, networkId);
      success = networkId
        ? 'Storage linked. It is being set up on your network.'
        : 'A private network was created and the storage linked to it. It is being set up.';
      await load();
    } catch (err) {
      error = err.message || 'Failed to link storage';
    } finally {
      working = false;
    }
  }

  function replaced(event) {
    const updated = event.detail;
    if (updated) storages = storages.map((s) => (s.service_id === updated.service_id ? updated : s));
  }

  onMount(load);
</script>

<section aria-labelledby="storage-title">
  <div class="page-head">
    <div>
      <h1 id="storage-title">Storage</h1>
      <p class="sub">NFS storage on your private network, also reachable over SFTP. It is kept whether or not you have servers with us.</p>
    </div>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if storages.length === 0}
    <div class="empty">
      <h2>No storage yet</h2>
      <p>Order NFS storage, then link it to one of your <a href="/client/networks">private networks</a> or let us create one.</p>
    </div>
  {:else}
    {#each storages as storage (storage.service_id)}
      <article class="panel">
        <div class="head">
          <div>
            <h2>{storage.service_name}</h2>
            <p>
              {storage.size_gb} GB
              {#if storage.network}
                · {storage.network.name} <span class="mono">{storage.network.cidr}</span>
                · endpoint <span class="mono">{storage.endpoint_ip}</span>
              {/if}
            </p>
          </div>
          <span class="badge" class:ok={storage.status === 'ready'} class:bad={storage.status === 'error'}>
            {STORAGE_STATUS_LABELS[storage.status] || storage.status}
          </span>
        </div>
        {#if storage.status_detail}<p class="hint">{storage.status_detail}.</p>{/if}

        {#if storage.status === 'unlinked'}
          {@const networks = networksByService[storage.service_id] || []}
          {#if networks.length === 0}
            <p class="hint">You have no private networks yet. One is created for you when you link{storage.location_name ? ` (in ${storage.location_name})` : ''}.</p>
          {/if}
          <div class="link">
              <select bind:value={chosen[storage.service_id]} aria-label="Private network">
                <option value="">Choose a private network</option>
                <option value="new">Create a new private network</option>
                {#each networks as network (network.id)}
                  <option value={network.id} disabled={!network.available}>
                    {network.name} ({network.cidr}, {network.location_name}){network.reason ? `: ${network.reason}` : ''}
                  </option>
                {/each}
              </select>
              <Button size="small" disabled={working || !chosen[storage.service_id]} on:click={() => link(storage)}>Link</Button>
          </div>
          <StorageSftpPanel {storage} client={true} on:change={replaced} />
        {:else}
          <StorageSharesPanel {storage} client={true} on:change={replaced} />
          <StorageSftpPanel {storage} client={true} on:change={replaced} />
          {#if storage.endpoint_ip}
            <p class="hint">
              Mount from any address you allowed on {storage.network?.cidr}. The storage endpoint
              <span class="mono">{storage.endpoint_ip}</span> is reserved for it on your network.
            </p>
          {/if}
        {/if}
      </article>
    {/each}
  {/if}
</section>

<style>
  .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
  .page-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -0.02em; }
  .sub { margin: 4px 0 0; font-size: 14px; color: var(--text-tertiary); }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--portal-card-bg, var(--bg-primary));
    box-shadow: var(--shadow-sm);
    padding: 20px;
    margin-bottom: 16px;
  }
  .panel h2 { margin: 0 0 4px; font-size: 18px; }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
  .head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: var(--radius-pill); border: 1px solid var(--border-color); font-size: 12px; font-weight: 600; color: var(--warning-color, #b7791f); }
  .badge.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .badge.bad { color: var(--danger-color); border-color: var(--danger-color); }
  .hint { margin: 10px 0 0; font-size: 13px; color: var(--text-tertiary); line-height: 1.5; }
  .empty a { color: var(--portal-accent); }
  .link { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-top: 12px; }
  select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); min-width: 0; }
  .empty { text-align: center; padding: 56px 24px; background: var(--portal-card-bg); border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); }
  .empty h2 { margin: 0 0 6px; font-size: 18px; font-weight: 700; color: var(--text-primary); }
  .empty p { margin: 0; font-size: 14px; }
</style>
