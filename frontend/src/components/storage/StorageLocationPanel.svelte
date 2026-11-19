<script>
  import { createEventDispatcher } from 'svelte';
  import Button from '../ui/Button.svelte';

  // A location from /api/admin/storage/locations: failover and public SFTP settings.
  export let location;
  export let disabled = false;

  const dispatch = createEventDispatcher();
  let host = location.sftp_host || '';
  let port = location.sftp_port || 2222;
  let vip = location.sftp_virtual_ip || '';
  let importKey = '';
  let showImport = false;

  const seen = (iso) => (iso ? new Date(iso).toLocaleString() : '—');

  function save(extra = {}) {
    dispatch('save', {
      sftp_host: host.trim(),
      sftp_port: Number(port),
      sftp_virtual_ip: vip.trim(),
      ...extra,
    });
  }

  function hostKey(body) {
    dispatch('hostkey', body);
    importKey = '';
    showImport = false;
  }
</script>

<div class="panel">
  <div class="line">
    <span class="badge" class:ok={location.ha_active}>{location.ha_active ? 'Failover active' : 'Failover not active'}</span>
    {#if !location.ha_enabled}<span class="hint">switched off for this location</span>
    {:else if !location.ha_active}<span class="hint">waiting for every node to run runner 0.3.0 or later</span>{/if}
    {#if location.rebalancing}<span class="badge">Rebalancing</span>{/if}
  </div>
  <div class="actions">
    <Button size="small" variant="secondary" {disabled} on:click={() => save({ ha_enabled: !location.ha_enabled })}>
      {location.ha_enabled ? 'Switch failover off' : 'Switch failover on'}
    </Button>
    <Button size="small" variant="secondary" {disabled} on:click={() => save({ auto_failback: !location.auto_failback })}>
      Automatic return to preferred node: {location.auto_failback ? 'on' : 'off'}
    </Button>
    <Button size="small" {disabled} on:click={() => dispatch('rebalance')}>Rebalance</Button>
  </div>

  <h3>SFTP</h3>
  <label>Public host name <input bind:value={host} placeholder="files.example.net" {disabled} /></label>
  <label>Port <input type="number" min="1" max="65535" bind:value={port} {disabled} /></label>
  <label>Virtual IP with prefix length <input bind:value={vip} placeholder="203.0.113.10/24 (empty: SFTP per node)" {disabled} /></label>
  {#if location.warnings}{#each location.warnings as warning}<p class="warn">{warning}</p>{/each}{/if}
  <div class="actions">
    <Button size="small" {disabled} on:click={() => save()}>Save</Button>
    <Button size="small" variant="secondary" disabled={disabled || (!location.sftp_enabled && !host.trim())} on:click={() => save({ sftp_enabled: !location.sftp_enabled })}>
      {location.sftp_enabled ? 'Turn SFTP off' : 'Turn SFTP on'}
    </Button>
  </div>
  <span class="hint">SFTP is {location.sftp_enabled ? 'on' : 'off'} for this location.</span>

  <h3>Host key</h3>
  {#if location.sftp_host_key_fingerprint}
    <span class="hint mono">{location.sftp_host_key_fingerprint}</span>
  {:else}
    <span class="hint">No location key yet: each node keeps its own key.</span>
  {/if}
  <div class="actions">
    <Button size="small" variant="secondary" {disabled} on:click={() => confirm('Generate a new host key? Clients will see a host key change.') && hostKey({ generate: true })}>
      Generate
    </Button>
    <Button size="small" variant="secondary" {disabled} on:click={() => (showImport = !showImport)}>Import</Button>
  </div>
  {#if showImport}
    <textarea rows="5" bind:value={importKey} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" {disabled}></textarea>
    <div class="actions">
      <Button size="small" disabled={disabled || !importKey.trim()} on:click={() => hostKey({ private_key: importKey })}>Import key</Button>
    </div>
    <span class="hint">The private key is stored sealed and is never shown again; only the fingerprint is.</span>
  {/if}

  {#if location.events?.length}
    <h3>Recent failover events</h3>
    <div class="events">
      {#each location.events as event (event.id)}
        <div class="event"><span class="mono">{seen(event.at)}</span> <strong>{event.kind}</strong>
          {#if event.tenant_id}tenant {event.tenant_id}{/if}
          {#if event.detail}<small>{event.detail}</small>{/if}</div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .panel { display: flex; flex-direction: column; gap: 6px; }
  h3 { margin: 10px 0 0; font-size: 0.95rem; }
  .line, .actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .hint { font-size: 0.85rem; color: var(--text-secondary); }
  .warn { margin: 0; color: var(--warning-text); font-size: 0.85rem; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; background: var(--bg-tertiary, var(--bg-secondary)); color: var(--text-secondary); }
  .badge.ok { background: var(--success-bg); color: var(--success-text); }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; font-weight: 600; }
  input, textarea { padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font-family: inherit; }
  .events { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; }
  .event small { display: block; color: var(--text-secondary); }
</style>
