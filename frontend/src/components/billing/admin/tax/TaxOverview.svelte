<script>
  import { onMount } from 'svelte';
  import { Alert, Spinner } from '../../../ui/index.js';
  import { getEngines } from '../../../../lib/billingAdminApi.js';
  import { getTaxSettings, listTaxRegistrations, listTaxRules } from '../../../../lib/billingTaxAdminApi.js';
  import { day, label } from './taxUi.js';

  export let onOpenTab = () => {};

  const MODE_TONE = { v2: 'ok', shadow: 'info', legacy: '' };

  let engine = null;
  let settings = {};
  let registrations = [];
  let currentRuleCount = null;
  let loading = true;
  let errors = [];

  onMount(async () => {
    const results = await Promise.allSettled([getEngines(), getTaxSettings(), listTaxRegistrations(), listTaxRules({ current_only: 'true', limit: 1 })]);
    const [engines, taxSettings, regs, rules] = results;
    if (engines.status === 'fulfilled') engine = engines.value.engines.find((e) => e.engine === 'tax') || null;
    if (taxSettings.status === 'fulfilled') settings = taxSettings.value;
    if (regs.status === 'fulfilled') registrations = regs.value;
    if (rules.status === 'fulfilled') currentRuleCount = rules.value.total;
    errors = results.filter((r) => r.status === 'rejected').map((r) => r.reason.message);
    loading = false;
  });

  function active(reg) {
    const now = new Date();
    return reg.enabled && new Date(reg.valid_from) <= now && (!reg.valid_to || new Date(reg.valid_to) > now);
  }

  $: activeRegs = registrations.filter(active);
  $: openCount = Object.values(engine?.open_mismatches || {}).reduce((sum, n) => sum + Number(n || 0), 0);
  $: failing = (engine?.switch_criterion?.items || []).filter((item) => !item.ok);
</script>

{#if loading}
  <div class="ba-state"><Spinner /></div>
{:else}
  {#each errors as message}<Alert type="danger">{message}</Alert>{/each}
  <div class="ba-panel">
    <h3>Tax engine</h3>
    <dl class="ba-grid">
      <div><dt>Mode</dt><dd>{#if engine}<span class="ba-pill {MODE_TONE[engine.mode]}">{engine.mode}</span>{:else}-{/if}</dd></div>
      <div><dt>Rules and registrations</dt><dd>{#if settings.billing_tax_enabled === undefined}-{:else}<span class="ba-pill {settings.billing_tax_enabled ? 'ok' : 'warn'}">{settings.billing_tax_enabled ? 'enabled' : 'not enabled'}</span>{/if}</dd></div>
      <div><dt>Seller country</dt><dd>{settings.billing_tax_seller_country || 'not set'}</dd></div>
      <div><dt>Rules in effect now</dt><dd>{currentRuleCount ?? '-'}</dd></div>
      <div><dt>Active registrations</dt><dd>{activeRegs.length} of {registrations.length}</dd></div>
      <div><dt>Open mismatches</dt><dd>{engine ? openCount : '-'}</dd></div>
    </dl>
    <p class="ba-muted" style="margin-top:12px">
      <strong>legacy</strong> uses the old tax rates only; <strong>shadow</strong> keeps the old tax and records every difference from the new rules; <strong>v2</strong> uses the new rules.
      When rules and registrations are not enabled, only the rules mirrored from the old tax rates apply.
    </p>
    <p>
      Mismatches between the old and new tax are listed on the
      <a href="/admin/billing-engines">Billing Engines screen</a>, where the engine is also switched.
    </p>
    {#if failing.length}
      <p class="ba-hint">Not yet met for the switch to v2: {failing.map((item) => label(item.item)).join(', ')}.</p>
    {/if}
  </div>

  <div class="ba-panel">
    <h3>Registrations <small>{registrations.length}</small></h3>
    {#if registrations.length === 0}
      <p class="ba-empty">No seller registrations. Add one under the Registrations tab before enabling rules that need it.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Jurisdiction</th><th>Tax number</th><th>Country</th><th>Valid</th><th>Status</th></tr></thead>
          <tbody>
            {#each registrations as reg (reg.id)}
              <tr>
                <td>{reg.jurisdiction}</td>
                <td><span class="ba-mono">{reg.tax_number}</span></td>
                <td>{reg.country}</td>
                <td>{day(reg.valid_from)} to {reg.valid_to ? day(reg.valid_to) : 'open'}</td>
                <td><span class="ba-pill {active(reg) ? 'ok' : ''}">{active(reg) ? 'active' : reg.enabled ? 'not in range' : 'disabled'}</span></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <p class="ba-hint" style="margin-top:10px">
      <button class="ba-link" on:click={() => onOpenTab('registrations')}>Manage registrations</button> ·
      <button class="ba-link" on:click={() => onOpenTab('rules')}>Rules</button> ·
      <button class="ba-link" on:click={() => onOpenTab('packs')}>Rule packs</button> ·
      <button class="ba-link" on:click={() => onOpenTab('calculator')}>Calculator</button>
    </p>
  </div>
{/if}
