<script>
  import { onMount } from 'svelte';
  import { Alert, Button } from '../../ui/index.js';
  import { formatMoney } from '../../../lib/resellerMoney.js';
  import {
    centsToInput,
    createCycle,
    deleteCycle,
    getAdminSettings,
    parseCents,
    updateCycle,
  } from '../../../lib/billingAdminApi.js';
  import './billingAdmin.css';

  export let plan;
  export let onChanged = () => {};

  const SELLABLE = [
    [1, 'month', 1, 'Monthly'],
    [3, 'month', 3, 'Quarterly'],
    [6, 'month', 6, 'Semi-annually'],
    [12, 'year', 1, 'Annually'],
    [24, 'year', 2, 'Biennially (2 years)'],
    [36, 'year', 3, 'Triennially (3 years)'],
  ];

  let longCycles = null;
  let error = '';
  let working = false;
  let editingId = null;
  let form = blank();

  onMount(async () => {
    try {
      const settings = await getAdminSettings();
      longCycles = !!settings.items.find((item) => item.key === 'billing_long_cycles_enabled')?.value;
    } catch (_) {
      longCycles = null;
    }
  });

  function blank() {
    return { months: 1, price: '', setup: '', enabled: true };
  }

  function lengthText(cycle) {
    const unit = cycle.interval_unit;
    const count = cycle.interval_count;
    if (!unit) return cycle.interval;
    return `${count} ${unit}${count === 1 ? '' : 's'}`;
  }

  function edit(cycle) {
    editingId = cycle.id;
    error = '';
    form = {
      months: cycle.months,
      price: centsToInput(cycle.price_cents),
      setup: centsToInput(cycle.setup_cents ?? 0),
      enabled: cycle.enabled,
    };
  }

  function cancel() {
    editingId = null;
    form = blank();
    error = '';
  }

  async function save() {
    const price = parseCents(form.price, { allowZero: true });
    const setup = form.setup === '' ? 0 : parseCents(form.setup, { allowZero: true });
    if (price === null) {
      error = 'Enter the price per cycle, for example 9.99.';
      return;
    }
    if (setup === null) {
      error = 'Enter the setup fee as an amount such as 5.00, or leave it empty.';
      return;
    }
    const [, unit, count] = SELLABLE.find(([months]) => months === Number(form.months));
    const body = { interval_unit: unit, interval_count: count, price_cents: price, setup_cents: setup, enabled: form.enabled };
    working = true;
    error = '';
    try {
      if (editingId) await updateCycle(editingId, body);
      else await createCycle(plan.id, body);
      cancel();
      await onChanged();
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  async function remove(cycle) {
    working = true;
    error = '';
    try {
      await deleteCycle(cycle.id);
      if (editingId === cycle.id) cancel();
      await onChanged();
    } catch (err) {
      error = err.message;
    } finally {
      working = false;
    }
  }

  $: options = SELLABLE.filter(([months]) => months <= 12 || longCycles || Number(form.months) === months);
</script>

<div class="cycles">
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if !plan.cycles?.length}
    <p class="ba-muted">No cycles yet.</p>
  {:else}
    <table class="ba-table">
      <thead><tr><th>Cycle</th><th>Length</th><th class="ba-num">Price</th><th class="ba-num">Setup fee</th><th>State</th><th></th></tr></thead>
      <tbody>
        {#each plan.cycles as cycle (cycle.id)}
          <tr>
            <td>{cycle.interval}</td>
            <td>{lengthText(cycle)}</td>
            <td class="ba-num">{formatMoney(cycle.price_cents, plan.currency)}</td>
            <td class="ba-num">{formatMoney(cycle.setup_cents ?? 0, plan.currency)}</td>
            <td>
              <span class="ba-pill {cycle.enabled ? 'ok' : ''}">{cycle.enabled ? 'enabled' : 'disabled'}</span>
              {#if cycle.months > 12 && longCycles === false}<div class="ba-hint">Not sold: long cycles are off</div>{/if}
            </td>
            <td>
              <div class="ba-inline">
                <button class="ba-link" on:click={() => edit(cycle)}>Edit</button>
                <button class="ba-link" disabled={working} on:click={() => remove(cycle)}>Remove</button>
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="ba-inline form">
    <label>Length
      <select bind:value={form.months}>
        {#each options as [months, , , name]}<option value={months}>{name}</option>{/each}
      </select>
    </label>
    <label>Price ({plan.currency})<input bind:value={form.price} inputmode="decimal" placeholder="0.00" /></label>
    <label>Setup fee per cycle<input bind:value={form.setup} inputmode="decimal" placeholder="0.00" /></label>
    <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
    <Button on:click={save} disabled={working}>{editingId ? 'Save cycle' : 'Add cycle'}</Button>
    {#if editingId}<Button variant="secondary" on:click={cancel}>Cancel</Button>{/if}
  </div>
  <p class="ba-hint">
    The setup fee is charged with the first invoice of the cycle.
    {#if longCycles === false}Cycles longer than a year are hidden until "billing_long_cycles_enabled" is switched on in settings.
    {:else if longCycles === null}Whether long cycles are allowed could not be read; the server refuses them while the setting is off.{/if}
  </p>
</div>

<style>
  .cycles { display: grid; gap: 10px; margin: 8px 0; }
  .form { align-items: flex-end; }
  .form label { display: grid; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .form input, .form select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); font: inherit; font-weight: 400; }
  .form .check { display: flex; align-items: center; gap: 6px; font-weight: 500; }
</style>
