<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../../ui/index.js';
  import { formatMoney } from '../../../../lib/resellerMoney.js';
  import { parseCents } from '../../../../lib/billingAdminApi.js';
  import { listTaxClasses, listTaxLegalNotes, previewTax } from '../../../../lib/billingTaxAdminApi.js';
  import { TAX_ID_TONE, fromInput, label, show } from './taxUi.js';

  const STATUSES = ['unchecked', 'valid', 'invalid', 'unavailable', 'format_invalid'];

  let classes = [];
  let notes = [];
  let running = false;
  let error = '';
  let result = null;
  let resultCurrency = '';

  let form = {
    currency: 'GBP',
    tax_point_at: '',
    prices_include_tax: false,
    purpose: 'order_charge',
    owner: 'retail',
    evidence: '',
    buyer: {
      country: '', region: '', postcode: '', is_business: false, tax_id: '', tax_id_type: '', tax_id_status: 'unchecked',
      exempt_all: false, exempt_reason: '',
    },
    lines: [{ key: 'line-1', amount: '100.00', tax_class_code: 'standard', kind: 'product' }],
  };

  onMount(async () => {
    try {
      [classes, notes] = await Promise.all([listTaxClasses(), listTaxLegalNotes()]);
    } catch (err) {
      error = err.message;
    }
  });

  function addLine() {
    form.lines = [...form.lines, { key: `line-${form.lines.length + 1}`, amount: '', tax_class_code: 'standard', kind: 'product' }];
  }

  function removeLine(index) {
    form.lines = form.lines.filter((_, i) => i !== index);
  }

  function build() {
    if (!/^[A-Za-z]{3}$/.test(form.currency.trim())) throw new Error('Enter a three-letter currency code.');
    if (!form.lines.length) throw new Error('Add at least one line.');
    const keys = new Set();
    const lines = form.lines.map((line) => {
      const key = line.key.trim();
      if (!key || keys.has(key)) throw new Error('Every line needs its own name.');
      keys.add(key);
      const cents = parseCents(line.amount, { allowZero: true, allowNegative: true });
      if (cents === null) throw new Error(`Line ${key}: enter the net amount as for example 12.50.`);
      return { key, net_cents: cents, tax_class_code: line.tax_class_code, kind: line.kind.trim() || 'product' };
    });
    const b = form.buyer;
    const buyer = { is_business: b.is_business, tax_id_status: b.tax_id_status, exempt_all: b.exempt_all };
    ['country', 'region', 'postcode', 'tax_id', 'tax_id_type', 'exempt_reason'].forEach((key) => {
      if (b[key].trim()) buyer[key] = key === 'country' ? b[key].trim().toUpperCase() : b[key].trim();
    });
    if (buyer.country && buyer.country.length !== 2) throw new Error('The buyer country is a two-letter code.');
    const body = {
      currency: form.currency.trim().toUpperCase(),
      prices_include_tax: form.prices_include_tax,
      purpose: form.purpose.trim() || 'order_charge',
      owner: form.owner,
      buyer,
      lines,
    };
    if (form.tax_point_at) body.tax_point_at = fromInput(form.tax_point_at);
    if (form.evidence.trim()) {
      try {
        const parsed = JSON.parse(form.evidence);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
        body.evidence = parsed;
      } catch (_) {
        throw new Error('The evidence must be a JSON object, for example {"billing_country": "DE"}.');
      }
    }
    return body;
  }

  async function run() {
    error = '';
    let body;
    try {
      body = build();
    } catch (err) {
      error = err.message;
      return;
    }
    running = true;
    try {
      result = await previewTax(body);
      resultCurrency = body.currency;
    } catch (err) {
      error = err.message;
    } finally {
      running = false;
    }
  }

  function noteText(key) {
    const note = notes.find((n) => n.key === key && n.locale === 'en') || notes.find((n) => n.key === key);
    return note ? note.text : '';
  }

  function money(cents) {
    try {
      return formatMoney(cents, resultCurrency);
    } catch (_) {
      return `${(Number(cents) / 100).toFixed(2)} ${resultCurrency}`;
    }
  }

  $: lineRows = result ? Object.values(result.lines) : [];
  $: reverseCharged = result ? lineRows.some((l) => l.parts.some((p) => p.reverse_charge)) || result.summary.some((s) => s.reverse_charge) : false;
  $: netTotal = result ? form.lines.reduce((sum, l) => sum + (parseCents(l.amount, { allowZero: true, allowNegative: true }) || 0), 0) : 0;
</script>

<div class="ba-panel">
  <h3>Tax calculator</h3>
  <p class="ba-muted">Runs the tax engine on a made-up basket and buyer with the rules in force at the chosen time. It only calculates; nothing is saved or invoiced.</p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  <form class="ba-form" on:submit|preventDefault={run}>
    <div class="ba-two">
      <label>Currency<input bind:value={form.currency} maxlength="3" /></label>
      <label>Tax point (blank for now)<input type="datetime-local" bind:value={form.tax_point_at} /></label>
    </div>
    <div class="ba-two">
      <label>Purpose<input bind:value={form.purpose} maxlength="32" /></label>
      <label>Seller
        <select bind:value={form.owner}><option value="retail">Retail</option><option value="reseller">Reseller</option></select>
      </label>
    </div>
    <label class="ba-check"><input type="checkbox" bind:checked={form.prices_include_tax} /> Amounts already include tax</label>

    <h3 style="margin:8px 0 0">Buyer</h3>
    <div class="ba-two">
      <label>Country<input bind:value={form.buyer.country} maxlength="2" placeholder="DE" /></label>
      <label>Region<input bind:value={form.buyer.region} /></label>
    </div>
    <div class="ba-two">
      <label>Postcode<input bind:value={form.buyer.postcode} /></label>
      <label class="ba-check"><input type="checkbox" bind:checked={form.buyer.is_business} /> Business buyer</label>
    </div>
    <div class="ba-two">
      <label>Tax ID<input bind:value={form.buyer.tax_id} /></label>
      <label>Tax ID type
        <select bind:value={form.buyer.tax_id_type}><option value="">None</option><option value="eu_vat">EU VAT</option><option value="gb_vat">GB VAT</option><option value="other">Other</option></select>
      </label>
    </div>
    <div class="ba-two">
      <label>Tax ID status
        <select bind:value={form.buyer.tax_id_status}>{#each STATUSES as s}<option value={s}>{label(s)}</option>{/each}</select>
      </label>
      <label class="ba-check"><input type="checkbox" bind:checked={form.buyer.exempt_all} /> Account is exempt</label>
    </div>
    {#if form.buyer.exempt_all}
      <label>Exemption reason<input bind:value={form.buyer.exempt_reason} maxlength="255" /></label>
    {/if}

    <h3 style="margin:8px 0 0">Basket</h3>
    <div class="ba-lines">
      {#each form.lines as line, index}
        <div class="ba-line" style="grid-template-columns: 1.4fr 1fr 1fr 1fr auto">
          <label>Line<input bind:value={line.key} maxlength="64" /></label>
          <label>Net amount<input bind:value={line.amount} inputmode="decimal" placeholder="0.00" /></label>
          <label>Class
            <select bind:value={line.tax_class_code}>
              {#each classes as c (c.id)}<option value={c.code}>{c.code}</option>{/each}
              {#if !classes.some((c) => c.code === line.tax_class_code)}<option value={line.tax_class_code}>{line.tax_class_code}</option>{/if}
            </select>
          </label>
          <label>Kind<input bind:value={line.kind} maxlength="32" /></label>
          <Button variant="secondary" size="small" on:click={() => removeLine(index)} disabled={form.lines.length === 1}>Remove</Button>
        </div>
      {/each}
    </div>
    <div class="ba-inline"><Button variant="secondary" size="small" on:click={addLine}>Add a line</Button></div>

    <label>Evidence (optional JSON object)
      <textarea bind:value={form.evidence} rows="2" placeholder={'{"billing_country": "DE"}'}></textarea>
    </label>
    <div class="ba-actions" style="margin:0"><Button type="submit" disabled={running}>Calculate</Button></div>
  </form>
</div>

{#if running}
  <div class="ba-state"><Spinner /></div>
{:else if result}
  <div class="ba-panel">
    <h3>Result</h3>
    <dl class="ba-grid">
      <div><dt>Net</dt><dd>{money(netTotal)}</dd></div>
      <div class="ba-due"><dt>Tax</dt><dd>{money(result.tax_cents)}</dd></div>
      <div><dt>Total</dt><dd>{money(form.prices_include_tax ? netTotal : netTotal + result.tax_cents)}</dd></div>
      <div><dt>Engine</dt><dd>{result.engine}</dd></div>
      <div><dt>Scope</dt><dd>{label(result.scope)}</dd></div>
      <div><dt>Seller registration</dt><dd>{result.registration_id ? `#${result.registration_id}` : 'none'}</dd></div>
      <div><dt>Reverse charge</dt><dd><span class="ba-pill {reverseCharged ? 'info' : ''}">{reverseCharged ? 'yes' : 'no'}</span></dd></div>
    </dl>

    <h3 style="margin-top:16px">Lines and parts</h3>
    <div class="ba-scroll">
      <table class="ba-table">
        <thead><tr><th>Line</th><th>Tax</th><th>Rate</th><th>Jurisdiction</th><th class="ba-num">Base</th><th class="ba-num">Tax</th><th>Rule</th><th>Source</th></tr></thead>
        <tbody>
          {#each lineRows as line (line.key)}
            {#if line.parts.length === 0}
              <tr><td>{line.key}</td><td colspan="3"><span class="ba-hint">No tax applies</span></td><td></td><td class="ba-num">{money(line.tax_cents)}</td><td></td><td></td></tr>
            {/if}
            {#each line.parts as part}
              <tr>
                <td>{line.key}</td>
                <td>{part.name}{#if part.reverse_charge} <span class="ba-pill info">reverse charge</span>{/if}</td>
                <td>{part.rate_percent}%</td>
                <td>{part.jurisdiction}</td>
                <td class="ba-num">{money(part.base_cents)}</td>
                <td class="ba-num">{money(part.tax_cents)}</td>
                <td>{part.rule_id ? `#${part.rule_id}` : '-'}</td>
                <td>{part.source}</td>
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
    </div>

    <h3 style="margin-top:16px">Summary</h3>
    {#if result.summary.length === 0}
      <p class="ba-empty">No tax.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Tax</th><th>Rate</th><th>Jurisdiction</th><th class="ba-num">Base</th><th class="ba-num">Tax</th></tr></thead>
          <tbody>
            {#each result.summary as entry}
              <tr>
                <td>{entry.name}{#if entry.reverse_charge} <span class="ba-pill info">reverse charge</span>{/if}</td>
                <td>{entry.rate_percent}%</td><td>{entry.jurisdiction}</td>
                <td class="ba-num">{money(entry.base_cents)}</td><td class="ba-num">{money(entry.tax_cents)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <h3 style="margin-top:16px">Legal notes</h3>
    {#if result.legal_note_keys.length === 0}
      <p class="ba-empty">No legal notes would print.</p>
    {:else}
      <ul>
        {#each result.legal_note_keys as key}
          <li><span class="ba-mono">{key}</span>: {noteText(key) || 'no text stored for this key'}</li>
        {/each}
      </ul>
    {/if}

    {#if Object.keys(result.evidence || {}).length}
      <h3 style="margin-top:16px">Evidence the engine recorded</h3>
      <dl class="ba-grid">
        {#each Object.entries(result.evidence) as [key, value]}
          <div><dt>{label(key)}</dt><dd style="font-weight:500; word-break:break-word">
            {#if key === 'tax_id_status'}<span class="ba-pill {TAX_ID_TONE[value] ?? ''}">{label(value)}</span>{:else}{show(value)}{/if}
          </dd></div>
        {/each}
      </dl>
    {/if}
    <p class="ba-hint ba-mono" style="margin-top:12px">context {result.context_hash}</p>
  </div>
{/if}
