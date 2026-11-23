<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import {
    createTaxRule,
    getTaxRuleHistory,
    getTaxRuleUsage,
    listTaxClasses,
    listTaxPacks,
    listTaxRules,
    supersedeTaxRule,
    updateTaxRule,
  } from '../../../../lib/billingTaxAdminApi.js';
  import { ROUNDING_RULES, day, fromInput, inMinutes, label, toInput, when } from './taxUi.js';

  const NULLABLE_TEXT = ['country', 'region', 'postcode_pattern', 'reverse_charge_zone', 'legal_note_key'];

  const PAGE_SIZE = 100;

  let rules = [];
  let total = 0;
  let offset = 0;
  let packList = [];
  let classes = [];
  let loading = true;
  let error = '';
  let success = '';
  let working = false;
  let filters = { country: '', jurisdiction: '', classId: '', pack: '', currentOnly: false };

  // mode is create, edit or supersede
  let editor = null;
  let form = blankForm();
  let original = null;
  let validFrom = '';
  let formError = '';
  let offerSupersede = false;

  let history = null;

  onMount(async () => {
    try {
      [classes, packList] = await Promise.all([listTaxClasses(), listTaxPacks()]);
    } catch (err) {
      error = err.message;
    }
    await load();
  });

  async function load(page = offset) {
    loading = true;
    error = '';
    try {
      const result = await listTaxRules({
        country: filters.country.trim(),
        jurisdiction: filters.jurisdiction.trim(),
        tax_class_id: filters.classId,
        pack_id: filters.pack,
        current_only: filters.currentOnly ? 'true' : '',
        limit: PAGE_SIZE,
        offset: page,
      });
      rules = result.items;
      total = result.total;
      offset = page;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  const filtersChanged = () => load(0);

  function blankForm() {
    return {
      name: '', country: '', region: '', postcode_pattern: '', tax_class_id: '', customer_type: 'any',
      rate_percent: '', priority: '0', compound: false, compound_order: '0', jurisdiction: 'ANY',
      reverse_charge: false, reverse_charge_zone: '', legal_note_key: '', rounding_point: 'invoice_rate',
      rounding_rule: 'round_half_up', valid_to: '', enabled: true,
    };
  }

  function rowToForm(row) {
    return {
      name: row.name, country: row.country || '', region: row.region || '', postcode_pattern: row.postcode_pattern || '',
      tax_class_id: String(row.tax_class_id), customer_type: row.customer_type, rate_percent: String(row.rate_percent),
      priority: String(row.priority), compound: !!row.compound, compound_order: String(row.compound_order),
      jurisdiction: row.jurisdiction, reverse_charge: !!row.reverse_charge, reverse_charge_zone: row.reverse_charge_zone || '',
      legal_note_key: row.legal_note_key || '', rounding_point: row.rounding_point, rounding_rule: row.rounding_rule,
      valid_to: toInput(row.valid_to), enabled: !!row.enabled,
    };
  }

  function formValues(f) {
    const values = {
      name: f.name.trim(),
      tax_class_id: Number(f.tax_class_id),
      customer_type: f.customer_type,
      rate_percent: String(f.rate_percent).trim(),
      priority: Number(f.priority),
      compound: f.compound,
      compound_order: Number(f.compound_order),
      jurisdiction: (f.jurisdiction.trim() || 'ANY').toUpperCase(),
      reverse_charge: f.reverse_charge,
      rounding_point: f.rounding_point,
      rounding_rule: f.rounding_rule,
      valid_to: fromInput(f.valid_to),
      enabled: f.enabled,
    };
    NULLABLE_TEXT.forEach((key) => {
      const text = f[key].trim();
      values[key] = text ? (key === 'country' ? text.toUpperCase() : text) : null;
    });
    return values;
  }

  function checkForm() {
    if (!form.name.trim()) return 'A name is required.';
    if (!form.tax_class_id) return 'Choose a tax class.';
    if (!/^\d+(\.\d{1,4})?$/.test(String(form.rate_percent).trim())) return 'Enter the rate as a percentage with up to four decimals, for example 20 or 7.5.';
    if (!Number.isInteger(Number(form.priority)) || !Number.isInteger(Number(form.compound_order))) return 'Priority and compound order are whole numbers.';
    if (form.country.trim() && form.country.trim().length !== 2) return 'The country is a two-letter code.';
    if (form.reverse_charge && Number(form.rate_percent) !== 0) return 'A reverse charge rule has rate 0.';
    return '';
  }

  function openCreate() {
    editor = { mode: 'create' };
    form = blankForm();
    validFrom = '';
    formError = '';
    offerSupersede = false;
  }

  function openEdit(row) {
    editor = { mode: 'edit', row };
    form = rowToForm(row);
    original = formValues(form);
    formError = '';
    offerSupersede = false;
  }

  function openSupersede(row, keepForm = false) {
    editor = { mode: 'supersede', row };
    if (!keepForm) form = rowToForm(row);
    original = formValues(rowToForm(row));
    validFrom = inMinutes(10);
    formError = '';
    offerSupersede = false;
  }

  function changedFields() {
    const now = formValues(form);
    const changes = {};
    Object.keys(now).forEach((key) => {
      const a = now[key];
      const b = original[key];
      const same = key === 'rate_percent' ? Number(a) === Number(b) : a === b;
      if (!same) changes[key] = a;
    });
    return changes;
  }

  async function save() {
    formError = checkForm();
    if (formError) return;
    working = true;
    offerSupersede = false;
    try {
      if (editor.mode === 'create') {
        const body = Object.fromEntries(Object.entries(formValues(form)).filter(([, v]) => v !== null));
        if (validFrom) body.valid_from = fromInput(validFrom);
        const created = await createTaxRule(body);
        success = `Rule #${created.id} created; it applies from ${when(created.valid_from)}.`;
      } else if (editor.mode === 'edit') {
        const changes = changedFields();
        if (!Object.keys(changes).length) {
          formError = 'Nothing has changed.';
          working = false;
          return;
        }
        await updateTaxRule(editor.row.id, changes);
        success = `Rule #${editor.row.id} updated.`;
      } else {
        const at = fromInput(validFrom);
        if (!at) {
          formError = 'Choose when the new rule starts.';
          working = false;
          return;
        }
        if (new Date(at).getTime() < Date.now() - 60000) {
          formError = 'The start time may not be in the past.';
          working = false;
          return;
        }
        const changes = changedFields();
        if (!Object.keys(changes).length) {
          formError = 'Change at least one field to supersede the rule.';
          working = false;
          return;
        }
        const created = await supersedeTaxRule(editor.row.id, { ...changes, valid_from: at });
        success = `Rule #${editor.row.id} now ends at ${when(at)}; rule #${created.id} starts then.`;
      }
      editor = null;
      await load();
    } catch (err) {
      formError = err.message;
      offerSupersede = editor?.mode === 'edit' && err.code === 'tax_rule_in_use';
    } finally {
      working = false;
    }
  }

  async function openHistory(row) {
    history = { row, rows: [], usage: {}, loading: true, error: '' };
    try {
      const chain = await getTaxRuleHistory(row.id);
      const usage = {};
      await Promise.all(chain.map(async (item) => {
        usage[item.id] = (await getTaxRuleUsage(item.id)).invoice_line_taxes;
      }));
      history = { ...history, rows: chain, usage, loading: false };
    } catch (err) {
      history = { ...history, loading: false, error: err.message };
    }
  }

  function status(row) {
    if (!row.enabled) return ['disabled', ''];
    if (!row.in_effect) return ['not started', 'info'];
    if (row.valid_to && new Date(row.valid_to) <= new Date()) return ['ended', ''];
    return ['in effect', 'ok'];
  }

  function className(id) {
    return classes.find((c) => c.id === id)?.code || `#${id}`;
  }

  function sourceText(row) {
    if (row.pack_id) return `${row.pack_id} ${row.pack_version || ''}`.trim();
    if (row.legacy_tax_rate_id) return 'tax rates (mirrored)';
    return label(row.source);
  }

  $: packs = packList.map((p) => p.pack_id);
  $: shown = rules;
  $: lastShown = Math.min(offset + rules.length, total);
</script>

<div class="ba-panel">
  <div class="ba-inline" style="justify-content: space-between">
    <h3 style="margin:0">Tax rules <small>{total} in all</small></h3>
    <Button on:click={openCreate}>New rule</Button>
  </div>
  <p class="ba-muted">A rule that has started is never edited: supersede it to end it at a chosen time and start a changed copy, so published invoices keep the rule they used.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}
  <div class="ba-toolbar">
    <label>Country<input bind:value={filters.country} maxlength="2" placeholder="e.g. DE" style="width:80px" on:change={filtersChanged} /></label>
    <label>Jurisdiction<input bind:value={filters.jurisdiction} maxlength="16" placeholder="e.g. EU_OSS" style="width:120px" on:change={filtersChanged} /></label>
    <label>Class
      <select bind:value={filters.classId} on:change={filtersChanged}>
        <option value="">All</option>
        {#each classes as c (c.id)}<option value={String(c.id)}>{c.code}</option>{/each}
      </select>
    </label>
    <label>Pack
      <select bind:value={filters.pack} on:change={filtersChanged}>
        <option value="">All</option>
        <option value="_none">Not from a pack</option>
        {#each packs as p}<option value={p}>{p}</option>{/each}
      </select>
    </label>
    <label class="ba-inline" style="gap:8px;font-weight:500"><input type="checkbox" bind:checked={filters.currentOnly} on:change={filtersChanged} /> In effect now</label>
  </div>
  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else if shown.length === 0}
    <p class="ba-empty">No rules match.</p>
  {:else}
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>#</th><th>Name</th><th>Where</th><th>Class</th><th>Buyer</th><th class="ba-num">Rate</th><th>Valid</th><th>Source</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {#each shown as row (row.id)}
            {@const st = status(row)}
            <tr>
              <td>{row.id}</td>
              <td>{row.name}{#if row.reverse_charge} <span class="ba-pill info">reverse charge</span>{/if}</td>
              <td>{row.country || 'any'}<div class="ba-hint">{row.jurisdiction}{row.region ? `, ${row.region}` : ''}</div></td>
              <td>{className(row.tax_class_id)}</td>
              <td>{row.customer_type}</td>
              <td class="ba-num">{row.rate_percent}%</td>
              <td>{day(row.valid_from)} to {row.valid_to ? day(row.valid_to) : 'open'}</td>
              <td>{sourceText(row)}</td>
              <td><span class="ba-pill {st[1]}">{st[0]}</span></td>
              <td class="ba-inline">
                {#if row.legacy_tax_rate_id}
                  <span class="ba-hint">Changed under tax rates</span>
                {:else}
                  {#if !row.in_effect}<button class="ba-link" on:click={() => openEdit(row)}>Edit</button>{/if}
                  <button class="ba-link" on:click={() => openSupersede(row)}>Supersede</button>
                {/if}
                <button class="ba-link" on:click={() => openHistory(row)}>History</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="ba-inline" style="margin-top:8px">
      <Button variant="secondary" size="small" disabled={offset === 0 || loading} on:click={() => load(Math.max(0, offset - PAGE_SIZE))}>Previous</Button>
      <span class="ba-hint">Rules {offset + 1} to {lastShown} of {total}, newest first</span>
      <Button variant="secondary" size="small" disabled={offset + PAGE_SIZE >= total || loading} on:click={() => load(offset + PAGE_SIZE)}>Next</Button>
    </div>
  {/if}
</div>

{#if editor}
  <Modal
    size="large"
    title={editor.mode === 'create' ? 'New tax rule' : editor.mode === 'edit' ? `Edit rule #${editor.row.id}` : `Supersede rule #${editor.row.id}`}
    onClose={() => (editor = null)}
  >
    {#if editor.mode === 'supersede'}
      <Alert type="info">The current row ends when the new one starts. Change the fields that differ; the rest are copied.</Alert>
      <label class="ba-form" style="margin-bottom:12px">New rule starts (not in the past)
        <input type="datetime-local" bind:value={validFrom} />
      </label>
    {/if}
    <form class="ba-form" on:submit|preventDefault={save}>
      <label>Name<input bind:value={form.name} maxlength="255" /></label>
      <div class="ba-two">
        <label>Tax class
          <select bind:value={form.tax_class_id}>
            <option value="">Choose a class</option>
            {#each classes as c (c.id)}<option value={String(c.id)}>{c.code}: {c.name}</option>{/each}
          </select>
        </label>
        <label>Rate (%)<input bind:value={form.rate_percent} inputmode="decimal" placeholder="20" /></label>
      </div>
      <div class="ba-two">
        <label>Country (blank for any)<input bind:value={form.country} maxlength="2" placeholder="GB" /></label>
        <label>Jurisdiction
          <input bind:value={form.jurisdiction} maxlength="16" placeholder="ANY, EU_OSS or a country code" />
        </label>
      </div>
      <div class="ba-two">
        <label>Region<input bind:value={form.region} maxlength="128" /></label>
        <label>Postcode pattern (regular expression)<input bind:value={form.postcode_pattern} maxlength="128" /></label>
      </div>
      <div class="ba-two">
        <label>Buyer type
          <select bind:value={form.customer_type}>
            <option value="any">Any</option><option value="business">Business</option><option value="consumer">Consumer</option>
          </select>
        </label>
        <label>Legal note key<input bind:value={form.legal_note_key} maxlength="64" /></label>
      </div>
      <div class="ba-two">
        <label>Priority<input bind:value={form.priority} inputmode="numeric" /></label>
        <label>Compound order<input bind:value={form.compound_order} inputmode="numeric" /></label>
      </div>
      <div class="ba-two">
        <label>Rounding point
          <select bind:value={form.rounding_point}><option value="invoice_rate">Per invoice and rate</option><option value="line">Per line</option></select>
        </label>
        <label>Rounding rule
          <select bind:value={form.rounding_rule}>{#each ROUNDING_RULES as r}<option value={r}>{label(r)}</option>{/each}</select>
        </label>
      </div>
      <div class="ba-two">
        <label class="ba-check"><input type="checkbox" bind:checked={form.reverse_charge} /> Reverse charge (rate must be 0)</label>
        <label>Reverse charge zone<input bind:value={form.reverse_charge_zone} maxlength="16" placeholder="e.g. EU" /></label>
      </div>
      <div class="ba-two">
        <label class="ba-check"><input type="checkbox" bind:checked={form.compound} /> Compound (applied on top of earlier taxes)</label>
        <label class="ba-check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      </div>
      <div class="ba-two">
        {#if editor.mode === 'create'}
          <label>Starts (blank for now)<input type="datetime-local" bind:value={validFrom} /></label>
        {:else}
          <span></span>
        {/if}
        <label>Ends (blank for open ended)<input type="datetime-local" bind:value={form.valid_to} /></label>
      </div>
      {#if formError}
        <div class="ba-error">
          {formError}
          {#if offerSupersede}
            <div style="margin-top:8px">
              <Button size="small" on:click={() => openSupersede(editor.row, true)}>Supersede this rule instead</Button>
            </div>
          {/if}
        </div>
      {/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editor = null)}>Cancel</Button>
      <Button on:click={save} disabled={working}>{editor.mode === 'supersede' ? 'Supersede' : 'Save'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if history}
  <Modal size="large" title="History of rule #{history.row.id}" onClose={() => (history = null)}>
    {#if history.loading}
      <div class="ba-state"><Spinner /></div>
    {:else if history.error}
      <div class="ba-error">{history.error}</div>
    {:else}
      <p class="ba-muted">Each row replaced the one above it. Usage counts the invoice tax rows the rule produced; published rows are frozen.</p>
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Name</th><th class="ba-num">Rate</th><th>Starts</th><th>Ends</th><th class="ba-num">Invoice tax rows</th></tr></thead>
          <tbody>
            {#each history.rows as row (row.id)}
              <tr>
                <td>{row.id}{row.id === history.row.id ? ' (selected)' : ''}</td>
                <td>{row.name}</td>
                <td class="ba-num">{row.rate_percent}%</td>
                <td>{when(row.valid_from)}</td>
                <td>{row.valid_to ? when(row.valid_to) : 'open'}</td>
                <td class="ba-num">{history.usage[row.id] ?? '-'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (history = null)}>Close</Button>
    </svelte:fragment>
  </Modal>
{/if}
