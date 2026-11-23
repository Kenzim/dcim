<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../ui/index.js';
  import * as api from '../../../lib/vmFirewallApi.js';
  import { ROLLOUT_INTERRUPTION_WARNING } from '../../../lib/firewallText.js';

  let locations = [];
  let forms = {};
  let loading = true;
  let loadError = '';
  let busyId = null;
  let errors = {};
  let messages = {};
  // {location, body, preview, refreshed} while the admin confirms a baseline rollout
  let rollout = null;

  const idOf = (loc) => loc.location_id ?? loc.id;
  const nameOf = (loc) => loc.name ?? loc.location_name ?? `Location ${idOf(loc)}`;
  const clustersOf = (loc) => loc.clusters || [];
  const isReady = (loc) => !!loc.ready;
  const checkedCount = (loc) => clustersOf(loc).filter((c) => c.readiness).length;
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');

  function formFrom(loc) {
    return {
      enabled: !!loc.enabled,
      staging: false,
      nic_flag_on_provision: loc.nic_flag_on_provision !== false,
      enforce_baseline_on_all: !!loc.enforce_baseline_on_all,
      smtp_block_new_vms: loc.smtp_block_new_vms !== false,
      anti_spoof_new_vms: !!loc.anti_spoof_new_vms,
      prefixes: (loc.ipv6_allowed_prefixes || []).join('\n'),
    };
  }

  function adopt(list) {
    locations = list;
    forms = Object.fromEntries(list.map((loc) => [idOf(loc), formFrom(loc)]));
  }

  async function load() {
    try {
      adopt(await api.listLocations());
      loadError = '';
    } catch (err) {
      loadError = err.message || 'Failed to load the locations';
    } finally {
      loading = false;
    }
  }

  const prefixList = (text) => text.split(/[\s,]+/).filter(Boolean);

  /** Only what differs from the saved location, so a save never rewrites untouched settings. */
  function changes(loc, form) {
    const before = formFrom(loc);
    const body = {};
    for (const key of ['enabled', 'nic_flag_on_provision', 'enforce_baseline_on_all', 'smtp_block_new_vms', 'anti_spoof_new_vms']) {
      if (form[key] !== before[key]) body[key] = form[key];
    }
    if (form.prefixes.trim() !== before.prefixes.trim()) {
      const list = prefixList(form.prefixes);
      body.ipv6_allowed_prefixes = list.length ? list : null;
    }
    if (body.enabled && form.staging) body.staging = true;
    return body;
  }

  $: dirty = (loc) => forms[idOf(loc)] !== undefined && Object.keys(changes(loc, forms[idOf(loc)])).length > 0;

  function describe(id, err) {
    errors = { ...errors, [id]: err.field ? `${err.field}: ${err.message}` : err.message };
  }

  async function submit(loc, body) {
    const id = idOf(loc);
    busyId = id;
    errors = { ...errors, [id]: '' };
    messages = { ...messages, [id]: '' };
    try {
      await api.updateLocation(id, body);
      messages = { ...messages, [id]: 'Saved.' };
      rollout = null;
      adopt(await api.listLocations());
    } catch (err) {
      if (err.code === 'confirmation_required' && err.preview) {
        rollout = { location: loc, body, preview: err.preview, refreshed: !!rollout };
      } else {
        describe(id, err);
      }
    } finally {
      busyId = null;
    }
  }

  // Switching the baseline on for every VM reaches existing servers, so it is previewed and confirmed first.
  const reachesExisting = (loc, form) => form.enabled && form.enforce_baseline_on_all && !(loc.enabled && loc.enforce_baseline_on_all);

  async function save(loc) {
    const id = idOf(loc);
    const form = forms[id];
    const body = changes(loc, form);
    if (!reachesExisting(loc, form)) return submit(loc, body);
    busyId = id;
    errors = { ...errors, [id]: '' };
    try {
      const preview = await api.previewRollout(id);
      rollout = { location: loc, body, preview, refreshed: false };
    } catch (err) {
      describe(id, err);
    } finally {
      busyId = null;
    }
  }

  async function resume(loc) {
    const id = idOf(loc);
    busyId = id;
    errors = { ...errors, [id]: '' };
    try {
      await api.resumeRollout(id);
      messages = { ...messages, [id]: 'Rollout resumed.' };
      adopt(await api.listLocations());
    } catch (err) {
      describe(id, err);
    } finally {
      busyId = null;
    }
  }

  const confirmRollout = () => submit(rollout.location, { ...rollout.body, confirm_existing_vms: rollout.preview.changed });

  async function recheck(loc) {
    const id = idOf(loc);
    busyId = id;
    errors = { ...errors, [id]: '' };
    try {
      await api.recheckReadiness(id);
      adopt(await api.listLocations());
    } catch (err) {
      describe(id, err);
    } finally {
      busyId = null;
    }
  }

  const servicesOf = (preview) => preview.services || (preview.service_ids || []).map((id) => ({ id, name: null }));

  onMount(load);
</script>

{#if loading}
  <div class="loading"><Spinner /><span>Loading…</span></div>
{:else if loadError}
  <Alert type="error">{loadError}</Alert>
{:else if locations.length === 0}
  <p class="hint">No locations found.</p>
{:else}
  <div class="cards">
    {#each locations as loc (idOf(loc))}
      {@const id = idOf(loc)}
      <article class="card">
        <header class="card-head">
          <h3>{nameOf(loc)}</h3>
          <span class="tag" class:on={loc.enabled}>{loc.enabled ? 'Firewall enabled' : 'Not enabled'}</span>
          <span class="tag" class:on={isReady(loc)} class:bad={checkedCount(loc) > 0 && !isReady(loc)}>
            {clustersOf(loc).length === 0 ? 'No clusters' : checkedCount(loc) === 0 ? 'Readiness not checked' : isReady(loc) ? 'Ready' : 'Not ready'}
          </span>
          {#if loc.rollout_paused_at}<span class="tag bad">Rollout paused</span>{/if}
        </header>

        {#if loc.rollout_paused_at}
          <Alert type="warning">
            <strong>The staged rollout is paused</strong> since {when(loc.rollout_paused_at)} after {loc.rollout_failures} failed {loc.rollout_failures === 1 ? 'attempt' : 'attempts'}.
            {#if loc.rollout_pause_reason}Reason: {loc.rollout_pause_reason}{/if}
            Servers still waiting are not touched until it is resumed.
            <div class="resume"><Button size="small" disabled={busyId === id} on:click={() => resume(loc)}>Resume rollout</Button></div>
          </Alert>
        {/if}

        <section class="readiness">
          <div class="readiness-head">
            <strong>Readiness</strong>
            <span class="hint">Checked {when(loc.readiness_checked_at)}</span>
            <Button size="small" variant="secondary" disabled={busyId === id} on:click={() => recheck(loc)}>Re-check now</Button>
          </div>
          {#each clustersOf(loc) as cluster (cluster.id)}
            {@const result = cluster.readiness}
            <div class="cluster" class:bad={!result?.ready} class:unchecked={!result}>
              <strong>{cluster.name || `Cluster ${cluster.id}`}</strong>: {result ? (result.ready ? 'ready' : 'not ready') : 'not checked yet'}
              {#if result?.reasons?.length}
                <ul>{#each result.reasons as reason}<li>{reason}</li>{/each}</ul>
              {/if}
              {#if result?.datacenter && Object.keys(result.datacenter).length}
                <p class="hint">Datacenter firewall settings: {Object.entries(result.datacenter).map(([k, v]) => `${k} ${v}`).join(', ')}</p>
              {/if}
            </div>
          {:else}
            <p class="hint">No Proxmox clusters are placed in this location.</p>
          {/each}
        </section>

        {#if forms[id]}
          <section class="settings">
            <label class="check"><input type="checkbox" bind:checked={forms[id].enabled} disabled={busyId === id} /> Enable the VM firewall here</label>
            {#if forms[id].enabled && !loc.enabled && !isReady(loc)}
              <label class="check sub"><input type="checkbox" bind:checked={forms[id].staging} disabled={busyId === id} /> Staging: enable although the readiness check has not passed (nothing is enforced and customers cannot see it)</label>
            {/if}
            <label class="check"><input type="checkbox" bind:checked={forms[id].nic_flag_on_provision} disabled={busyId === id} /> Switch on the network card firewall flag for new servers</label>
            <label class="check"><input type="checkbox" bind:checked={forms[id].smtp_block_new_vms} disabled={busyId === id} /> Block outgoing mail (port 25) on new servers</label>
            <label class="check"><input type="checkbox" bind:checked={forms[id].anti_spoof_new_vms} disabled={busyId === id} /> Also filter spoofed addresses and MAC addresses on new servers</label>
            <label class="check"><input type="checkbox" bind:checked={forms[id].enforce_baseline_on_all} disabled={busyId === id} /> Apply the baseline to every existing server here</label>
            {#if forms[id].enforce_baseline_on_all && !loc.enforce_baseline_on_all}
              <p class="hint sub">You will see how many existing servers this changes before it is switched on.</p>
            {/if}

            <label class="stack">IPv6 allowed prefixes (one per line)
              <textarea rows="3" bind:value={forms[id].prefixes} placeholder="2001:db8:1::/48" disabled={busyId === id}></textarea>
            </label>
            {#if loc.ipv6_allowed_prefixes_help}<p class="hint help">{loc.ipv6_allowed_prefixes_help}</p>{/if}
          </section>

          {#if errors[id]}<Alert type="error">{errors[id]}</Alert>{/if}
          {#if messages[id]}<Alert type="success">{messages[id]}</Alert>{/if}
          <div class="bar">
            <Button disabled={busyId === id || !dirty(loc)} on:click={() => save(loc)}>{busyId === id ? 'Working…' : 'Save settings'}</Button>
            <Button variant="secondary" disabled={busyId === id || !dirty(loc)} on:click={() => (forms[id] = formFrom(loc))}>Discard</Button>
          </div>
        {/if}
      </article>
    {/each}
  </div>
{/if}

{#if rollout}
  {@const preview = rollout.preview}
  <Modal title="Apply the baseline to existing servers?" onClose={() => (rollout = null)}>
    {#if rollout.refreshed}
      <Alert type="warning">The numbers changed since you last looked. Check them again before confirming.</Alert>
    {/if}
    <p>
      Switching this on in <strong>{nameOf(rollout.location)}</strong> changes the firewall of
      <strong>{preview.changed}</strong> existing {preview.changed === 1 ? 'server' : 'servers'}.
      They are updated in small batches rather than all at once.
    </p>
    {#if preview.blip > 0}
      <p><strong>{preview.blip}</strong> of them {preview.blip === 1 ? 'is' : 'are'} running and will get a short interruption. {ROLLOUT_INTERRUPTION_WARNING}</p>
    {/if}
    {#if preview.nic_flag_unknown > 0}
      <p>
        <strong>{preview.nic_flag_unknown}</strong> {preview.nic_flag_unknown === 1 ? 'server has' : 'servers have'} not been checked yet, so
        Rackflow does not know whether they will be interrupted.
      </p>
    {/if}
    {#if servicesOf(preview).length}
      <details>
        <summary>Which servers</summary>
        <p class="services">
          {#each servicesOf(preview) as item, i}
            <a href="/admin/services/{item.id}" target="_blank" rel="noopener">{item.name || `Service #${item.id}`}</a>{i < servicesOf(preview).length - 1 ? ', ' : ''}
          {/each}
        </p>
      </details>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (rollout = null)}>Cancel</Button>
      <Button disabled={busyId !== null} on:click={confirmRollout}>Confirm: change {preview.changed} {preview.changed === 1 ? 'server' : 'servers'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .loading { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .hint.sub { margin-left: 24px; }
  .hint.help { padding: 8px 10px; border-left: 3px solid var(--warning-color); background: var(--warning-bg); color: var(--warning-text); font-size: 13px; white-space: pre-line; }
  .cards { display: flex; flex-direction: column; gap: 16px; }
  .card { display: flex; flex-direction: column; gap: 14px; border: 1px solid var(--border-color); border-radius: 12px; padding: 16px 18px; background: var(--bg-primary); }
  .card-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h3 { margin: 0; font-size: 16px; }
  .tag { font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 650; background: var(--bg-tertiary); color: var(--text-secondary); }
  .tag.on { background: var(--success-bg); color: var(--success-text); }
  .tag.bad { background: var(--danger-bg); color: var(--danger-text); }
  .readiness { display: flex; flex-direction: column; gap: 8px; }
  .readiness-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .cluster { padding: 8px 12px; border-radius: 8px; background: var(--success-bg); color: var(--success-text); font-size: 13px; }
  .cluster.unchecked { background: var(--bg-tertiary); color: var(--text-secondary); }
  .resume { margin-top: 8px; }
  .cluster.bad { background: var(--danger-bg); color: var(--danger-text); }
  .cluster ul { margin: 4px 0 0; padding-left: 18px; }
  .settings { display: flex; flex-direction: column; gap: 8px; }
  .check { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .check.sub { margin-left: 24px; }
  .stack { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  textarea { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-family: var(--font-mono); font-size: 13px; }
  .bar { display: flex; gap: 10px; }
  .services { font-size: 13px; line-height: 1.6; }
</style>
