<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import * as api from '../../lib/vmFirewallApi.js';
  import { INTERRUPTION_WARNING, PRESET_FALLBACK, STATE_INFO, reasonText } from '../../lib/firewallText.js';
  import FirewallStateBadge from './FirewallStateBadge.svelte';
  import BaselineView from './BaselineView.svelte';
  import RuleEditor from './RuleEditor.svelte';
  import { POLICIES, POLICY_LABEL, payloadOf, sameDrafts, toDraft } from './ruleModel.js';

  export let serviceId;
  /** @type {'staff' | 'customer'} */
  export let audience = 'customer';
  /** False when the account may look at the firewall but not change it. */
  export let canEdit = true;

  $: staff = audience === 'staff';

  let fw = null;
  let loading = true;
  let loadError = '';
  let busy = false;
  let error = '';
  let notice = '';
  let success = '';
  let fieldErrors = {};
  let draft = [];
  let saved = [];
  let presets = PRESET_FALLBACK;
  let groupChoices = [];
  let showInterruption = false;
  let dialog = null;
  let checkResult = null;
  let admin = adminFrom(null);

  $: dirty = !sameDrafts(draft, saved);
  $: ipsets = fw?.ipsets ?? [];
  $: allGroups = [...(fw?.groups || []), ...groupChoices.filter((g) => !(fw?.groups || []).some((x) => x.id === g.id))];
  $: locked = !canEdit || busy;
  $: allowLog = staff || !!fw?.limits?.client_logging;
  $: lockout =
    !!fw &&
    fw.policy_in === 'DROP' &&
    !saved.some((rule) => rule.enabled && (rule.kind === 'group' || (rule.direction === 'in' && rule.action === 'ACCEPT')));
  $: suggested = (fw?.suggested_presets || []).map((key) => presets.find((p) => p.key === key)).filter(Boolean);
  $: showSuggestion = !!fw && fw.available !== false && !fw.client_enabled && saved.length === 0 && suggested.length > 0;
  $: limitText = fw?.limits?.max_rules_per_vm ? `${draft.length} of ${fw.limits.max_rules_per_vm} rules used` : '';

  function adminFrom(view) {
    const overrides = view?.admin_overrides || {};
    const tri = (value) => (value === true ? 'on' : value === false ? 'off' : '');
    return {
      managed: view ? view.managed !== false : true,
      excluded: !!view?.excluded,
      enforce_baseline: !!view?.enforce_baseline,
      smtp_out: overrides.smtp_out || '',
      anti_spoof: tri(overrides.anti_spoof),
      macfilter: tri(overrides.macfilter),
      block_management: tri(overrides.block_management),
      extra_ipfilter: (overrides.extra_ipfilter || []).join(', '),
    };
  }

  function adminOverrides() {
    const out = {};
    if (admin.smtp_out) out.smtp_out = admin.smtp_out;
    for (const key of ['anti_spoof', 'macfilter', 'block_management']) {
      if (admin[key]) out[key] = admin[key] === 'on';
    }
    const extra = admin.extra_ipfilter.split(/[\s,]+/).filter(Boolean);
    if (extra.length) out.extra_ipfilter = extra;
    return out;
  }

  function applyView(view, { keepDraft = false } = {}) {
    const hadEdits = dirty;
    fw = view;
    saved = view.rules.map(toDraft);
    if (!(keepDraft && hadEdits)) draft = view.rules.map(toDraft);
    admin = adminFrom(view);
  }

  async function load() {
    try {
      applyView(await api.getFirewall(audience, serviceId));
      loadError = '';
      api.listGroups(staff ? 'staff' : 'customer').then((list) => (groupChoices = list)).catch(() => {});
      api.listPresets().then((list) => list.length && (presets = list)).catch(() => {});
    } catch (err) {
      loadError = err.message || 'Failed to load the firewall';
    } finally {
      loading = false;
    }
  }

  async function handleError(err) {
    if (!(err instanceof api.FirewallApiError)) {
      error = err.message || 'The request failed';
      return;
    }
    if (err.code === 'firewall_changed') {
      const lost = dirty;
      try {
        applyView(await api.getFirewall(audience, serviceId));
      } catch (reloadErr) {
        error = reloadErr.message;
      }
      notice =
        'Someone else changed this firewall, so it has been reloaded.' +
        (lost ? ' Your unsaved edits were not kept; please make them again.' : '');
      return;
    }
    if (err.code === 'interruption_confirmation_required') {
      showInterruption = true;
      dialog = 'enable';
      return;
    }
    if (err.code === 'not_eligible' && err.reasons.length) {
      error = `${err.message}. ${err.reasons.map(reasonText).join(' ')}`;
      return;
    }
    if (err.field) fieldErrors = { [err.field]: err.message };
    error = err.field && err.field.startsWith('rules[') ? `Some rules need attention: ${err.message}` : err.message;
  }

  async function write(action, { keepDraft = false, done = '' } = {}) {
    busy = true;
    error = '';
    notice = '';
    success = '';
    fieldErrors = {};
    try {
      applyView(await action(), { keepDraft });
      if (done) success = done;
      return true;
    } catch (err) {
      await handleError(err);
      fw = fw;
      return false;
    } finally {
      busy = false;
    }
  }

  function requestEnabled(on) {
    if (!on) return confirmEnabled(false, false);
    if (fw.needs_interruption || lockout) {
      showInterruption = !!fw.needs_interruption;
      dialog = 'enable';
      return;
    }
    return confirmEnabled(true, false);
  }

  function confirmEnabled(on, accepted) {
    dialog = null;
    const body = { revision: fw.revision, client_enabled: on };
    if (on && accepted && showInterruption) body.confirm_interruption = true;
    return write(() => api.updateFirewall(audience, serviceId, body), {
      keepDraft: true,
      done: on ? 'Firewall switched on.' : 'Firewall switched off.',
    });
  }

  function onSwitch(event) {
    const on = event.target.checked;
    event.target.checked = !!fw.client_enabled;
    requestEnabled(on);
  }

  function setPolicy(key, value) {
    return write(() => api.updateFirewall(audience, serviceId, { revision: fw.revision, [key]: value }), {
      keepDraft: true,
      done: 'Policy saved.',
    });
  }

  const saveRules = () =>
    write(() => api.replaceRules(audience, serviceId, fw.revision, payloadOf(draft)), { done: 'Rules saved.' });

  function discard() {
    draft = saved.map((rule) => ({ ...rule }));
    fieldErrors = {};
    error = '';
  }

  function addPreset(event) {
    const key = event.target.value;
    event.target.value = '';
    if (!key) return;
    write(() => api.applyPreset(audience, serviceId, key, fw.revision), { done: 'Preset added.' });
  }

  function addSuggested() {
    const keys = fw.suggested_presets;
    return write(
      async () => {
        let view = fw;
        for (const key of keys) view = await api.applyPreset(audience, serviceId, key, view.revision);
        return view;
      },
      { done: 'Suggested rules added. Switch the firewall on when you are ready.' },
    );
  }

  function saveAdmin() {
    return write(
      () =>
        api.updateAdminFields(serviceId, {
          revision: fw.revision,
          managed: admin.managed,
          excluded: admin.excluded,
          enforce_baseline: admin.enforce_baseline,
          admin_overrides: adminOverrides(),
        }),
      { keepDraft: true, done: 'Admin settings saved.' },
    );
  }

  async function runCheck(kind) {
    busy = true;
    error = '';
    success = '';
    checkResult = null;
    try {
      if (kind === 'check') {
        checkResult = await api.checkNow(serviceId);
      } else {
        const { result, firewall } = await api.applyNow(serviceId);
        if (firewall) applyView(firewall, { keepDraft: true });
        if (result.ok) success = 'Applied to the hypervisor.';
        else error = `The apply did not complete (${result.state || 'error'})${result.error ? `: ${result.error}` : ''}`;
        return;
      }
      applyView(await api.getFirewall(audience, serviceId), { keepDraft: true });
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'Never');

  onMount(load);
</script>

<div class="fw">
  {#if loading}
    <div class="loading"><Spinner /><span>Loading the firewall…</span></div>
  {:else if loadError}
    <Alert type="error">{loadError}</Alert>
    <div><Button variant="secondary" on:click={load}>Retry</Button></div>
  {:else if fw}
    <header class="top">
      <div class="top-main">
        <h3>Firewall</h3>
        <FirewallStateBadge state={fw.state} />
        {#if STATE_INFO[fw.state]}<span class="state-text">{STATE_INFO[fw.state].text}</span>{/if}
      </div>
      <label class="switch" title={fw.available === false ? 'The firewall is not offered for this server' : dirty ? 'Save or discard your rule changes first' : ''}>
        <input type="checkbox" checked={!!fw.client_enabled} disabled={locked || dirty || fw.available === false} on:change={onSwitch} />
        <span>{fw.client_enabled ? 'On' : 'Off'}</span>
      </label>
    </header>

    {#if fw.message && fw.available !== false}<Alert type="info">{fw.message}</Alert>{/if}
    {#if notice}<Alert type="warning">{notice}</Alert>{/if}
    {#if error}<Alert type="error">{error}</Alert>{/if}
    {#if success}<Alert type="success">{success}</Alert>{/if}
    {#if !canEdit}<p class="hint">You can look at this firewall, but your account may not change it.</p>{/if}
    {#if fw.last_applied_at}<p class="hint">Last applied {when(fw.last_applied_at)}.</p>{/if}

    {#if fw.available === false}
      <Alert type="info">The firewall is not offered for this server{staff ? '' : ' at the moment'}.{#if staff && fw.reasons.length} See the reasons below.{/if}</Alert>
    {:else}
      <p class="hint">
        {fw.client_enabled
          ? 'The firewall is on: only traffic your rules allow is let through, and the default policies below decide the rest.'
          : 'The firewall is off. Provider protections still apply, but your own rules and policies are not enforced.'}
      </p>

      {#if showSuggestion}
        <section class="suggest">
          <h4>Suggested starting rules</h4>
          <p class="hint">Based on this server's operating system. Nothing is saved until you add them, and the firewall stays off until you switch it on.</p>
          <ul>
            {#each suggested as preset}<li><strong>{preset.label}</strong>: {preset.description}</li>{/each}
          </ul>
          <Button size="small" disabled={locked} on:click={addSuggested}>Add these rules</Button>
        </section>
      {/if}

      <section class="policies">
        <label>Incoming traffic that no rule matches
          <select value={fw.policy_in} disabled={locked} on:change={(e) => setPolicy('policy_in', e.target.value)}>
            {#each POLICIES as policy}<option value={policy}>{POLICY_LABEL[policy]}</option>{/each}
          </select>
        </label>
        <label>Outgoing traffic that no rule matches
          <select value={fw.policy_out} disabled={locked} on:change={(e) => setPolicy('policy_out', e.target.value)}>
            {#each POLICIES as policy}<option value={policy}>{POLICY_LABEL[policy]}</option>{/each}
          </select>
        </label>
      </section>

      <BaselineView baseline={fw.baseline} {ipsets} smtpHelp={fw.smtp_block_help} />

      <section class="rules">
        <div class="rules-head">
          <h4>Your rules</h4>
          <span class="hint">Rules run from the top; the first match wins. {limitText}</span>
        </div>
        <RuleEditor
          bind:rules={draft}
          errors={fieldErrors}
          groups={allGroups}
          {ipsets}
          {staff}
          {allowLog}
          disabled={locked}
          emptyText="You have no rules yet. With the default policy above, unmatched traffic is handled as that policy says."
        />
        <div class="save-bar">
          <Button disabled={locked || !dirty} on:click={saveRules}>{busy ? 'Saving…' : 'Save rules'}</Button>
          <Button variant="secondary" disabled={locked || !dirty} on:click={discard}>Discard changes</Button>
          {#if dirty}<span class="hint">You have unsaved changes.</span>{/if}
          <span class="spacer"></span>
          <label class="preset">
            <span class="sr">Add a preset</span>
            <select disabled={locked || dirty} on:change={addPreset} title={dirty ? 'Save or discard your rule changes first' : ''}>
              <option value="">Add a preset…</option>
              {#each presets as preset}<option value={preset.key} title={preset.description}>{preset.label}</option>{/each}
            </select>
          </label>
        </div>
      </section>
    {/if}

    {#if staff}
      <section class="admin">
        <h4>Staff settings</h4>
        <p class="hint">Customers never see these.</p>

        {#if fw.reasons.length}
          <div class="reasons">
            <strong>Why some protections are not applied</strong>
            <ul>
              {#each fw.reasons as reason}
                <li><code>{reason}</code> {reasonText(reason)}</li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="admin-grid">
          <label class="check"><input type="checkbox" bind:checked={admin.managed} disabled={locked} /> Managed (off means Rackflow never touches this server's firewall)</label>
          <label class="check"><input type="checkbox" bind:checked={admin.excluded} disabled={locked} /> Excluded (infrastructure, never managed)</label>
          <label class="check"><input type="checkbox" bind:checked={admin.enforce_baseline} disabled={locked} /> Enforce the baseline even while the customer firewall is off</label>
        </div>

        <div class="admin-grid overrides">
          <label>Outgoing mail (port 25)
            <select bind:value={admin.smtp_out} disabled={locked}>
              <option value="">Use the baseline</option>
              <option value="block">Block</option>
              <option value="allow">Allow</option>
            </select>
          </label>
          {#each [['anti_spoof', 'Address anti-spoofing'], ['macfilter', 'MAC address filter'], ['block_management', 'Block management networks']] as [key, label]}
            <label>{label}
              <select bind:value={admin[key]} disabled={locked}>
                <option value="">Use the baseline</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
          {/each}
          <label class="wide">Extra addresses allowed to send from this server
            <input type="text" bind:value={admin.extra_ipfilter} placeholder="203.0.113.9" disabled={locked} />
          </label>
        </div>

        <div class="save-bar">
          <Button disabled={locked} on:click={saveAdmin}>Save staff settings</Button>
          <Button variant="secondary" disabled={busy} on:click={() => runCheck('check')}>Check for drift</Button>
          <Button variant="secondary" disabled={busy} on:click={() => runCheck('apply')}>Apply now</Button>
        </div>

        {#if checkResult}
          <div class="check-result">
            {#if checkResult.in_sync}
              <p>In sync with the hypervisor.</p>
            {:else}
              <p>Differences found:</p>
              <ul>{#each checkResult.differences || [] as diff}<li>{diff}</li>{/each}</ul>
            {/if}
          </div>
        {/if}
        {#if fw.last_error}<p class="field-error">Last error: {fw.last_error}{#if fw.failing_since} (failing since {when(fw.failing_since)}){/if}</p>{/if}

        <dl class="facts">
          <div><dt>In sync</dt><dd>{fw.in_sync ? 'Yes' : 'No'}</dd></div>
          <div><dt>Revision</dt><dd>{fw.revision}{fw.applied_revision != null ? ` (applied ${fw.applied_revision})` : ' (never applied)'}</dd></div>
          <div><dt>Cluster ready</dt><dd>{fw.cluster_ready ? 'Yes' : 'No'}</dd></div>
          <div><dt>Network cards checked</dt><dd>{fw.nics_checked ? 'Yes' : 'Not yet'}</dd></div>
          <div><dt>net0 bridge</dt><dd>{fw.net0_bridge || 'Unknown'}</dd></div>
          <div><dt>Last checked</dt><dd>{when(fw.last_checked_at)}</dd></div>
          <div><dt>Next attempt</dt><dd>{fw.next_attempt_at ? when(fw.next_attempt_at) : 'None queued'}</dd></div>
          <div><dt>Staged rollout after</dt><dd>{fw.rollout_after ? when(fw.rollout_after) : 'Not staged'}</dd></div>
          <div><dt>Drift repairs (24 h)</dt><dd>{fw.drift_repairs_24h ?? 0}</dd></div>
        </dl>
        {#if fw.rendered}
          <details>
            <summary>What would be written to the hypervisor</summary>
            <pre>{JSON.stringify(fw.rendered, null, 2)}</pre>
          </details>
        {/if}
      </section>
    {/if}
  {/if}
</div>

{#if dialog === 'enable'}
  <Modal title="Turn the firewall on?" onClose={() => (dialog = null)}>
    {#if showInterruption}
      <p>{INTERRUPTION_WARNING}</p>
    {/if}
    {#if lockout}
      <p>
        Incoming traffic that no rule allows is blocked, and you have no rule allowing any. Once the firewall is on, you will
        not be able to connect to this server (including over SSH or Remote Desktop) until you add an allow rule. The console
        will still work.
      </p>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (dialog = null)}>Cancel</Button>
      <Button on:click={() => confirmEnabled(true, true)}>{showInterruption ? 'Accept and turn on' : 'Turn on'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .fw { display: flex; flex-direction: column; gap: 16px; }
  .loading { display: flex; align-items: center; gap: 10px; padding: 24px 0; color: var(--text-secondary); }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .top-main { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h3 { margin: 0; font-size: 16px; font-weight: 700; }
  h4 { margin: 0; font-size: 14px; }
  .state-text { font-size: 13px; color: var(--text-tertiary); }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .switch { display: inline-flex; align-items: center; gap: 8px; font-weight: 650; cursor: pointer; }
  .switch input { width: 18px; height: 18px; }
  section { display: flex; flex-direction: column; gap: 10px; }
  .suggest { padding: 12px 14px; border: 1px solid var(--info-color); border-radius: 10px; background: var(--info-bg); color: var(--info-text); align-items: flex-start; }
  .suggest ul { margin: 0; padding-left: 18px; font-size: 13px; }
  .policies { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  select, input[type='text'] { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; }
  .rules-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .save-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .preset select { min-width: 170px; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  .admin { padding: 14px; border: 1px solid var(--border-color); border-radius: 10px; background: var(--bg-secondary); }
  .admin-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px 14px; }
  .admin-grid .wide { grid-column: 1 / -1; }
  .check { flex-direction: row; align-items: center; gap: 8px; font-weight: 500; }
  .reasons { padding: 10px 12px; border: 1px solid var(--warning-color); border-radius: 8px; background: var(--warning-bg); color: var(--warning-text); font-size: 13px; }
  .reasons ul { margin: 6px 0 0; padding-left: 18px; }
  .reasons code { font-family: var(--font-mono); font-size: 12px; }
  .facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px 14px; margin: 0; font-size: 13px; }
  .facts dt { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); }
  .facts dd { margin: 0; }
  pre { max-height: 300px; overflow: auto; padding: 10px; border-radius: 6px; background: var(--bg-tertiary); font-family: var(--font-mono); font-size: 12px; }
  .check-result { font-size: 13px; }
  .check-result p { margin: 0 0 4px; }
  .field-error { color: var(--danger-text); font-size: 13px; margin: 0; }
</style>
