<script>
  // Renders plugin panels for one service from Rackflow's render document
  // (docs/plugin-system.md section 10). Plugin text is always shown as text.
  import { onMount } from 'svelte';
  import { getServicePluginUi, runServicePluginAction } from '../lib/api.js';

  export let serviceId;
  /** @type {'admin' | 'client'} */
  export let audience = 'admin';
  export let heading = 'Add-ons';

  let panels = [];
  let failed = false;
  let busyKey = '';
  let messages = {};
  let forms = {};

  const TONES = ['success', 'warning', 'danger', 'info', 'neutral'];

  async function load() {
    try {
      const doc = await getServicePluginUi(serviceId, audience);
      panels = doc.panels || [];
      failed = false;
      initForms(panels);
    } catch (_) {
      panels = [];
      failed = audience === 'admin';
    }
  }

  onMount(load);
  $: serviceId, load();

  function keyOf(el) {
    return `${el.plugin_id}:${el.action_id}`;
  }

  function initForms(list) {
    const next = {};
    for (const panel of list) {
      for (const el of panel.elements || []) {
        if (el.type !== 'action') continue;
        const values = {};
        for (const p of el.params || []) {
          values[p.key] = p.default ?? (p.type === 'boolean' ? false : '');
        }
        next[keyOf(el)] = values;
      }
    }
    forms = next;
  }

  function tone(value) {
    return TONES.includes(value) ? value : 'neutral';
  }

  async function run(el) {
    const key = keyOf(el);
    if (el.confirm && !window.confirm(el.confirm)) return;
    busyKey = key;
    messages = { ...messages, [key]: null };
    try {
      const params = {};
      for (const p of el.params || []) {
        const v = forms[key]?.[p.key];
        if (v === '' || v === null || v === undefined) continue;
        params[p.key] = v;
      }
      const result = await runServicePluginAction(serviceId, el.plugin_id, el.action_id, params, !!el.confirm, audience);
      const replaced = result.panels || [];
      const firstIndex = panels.findIndex((p) => p.plugin_id === el.plugin_id);
      const others = panels.filter((p) => p.plugin_id !== el.plugin_id);
      const at = firstIndex < 0 ? others.length : Math.min(firstIndex, others.length);
      panels = [...others.slice(0, at), ...replaced, ...others.slice(at)];
      initForms(panels);
      messages = { ...messages, [key]: { ok: true, text: result.message || 'Done.' } };
    } catch (e) {
      messages = { ...messages, [key]: { ok: false, text: e.message || 'The action failed' } };
    } finally {
      busyKey = '';
    }
  }
</script>

{#if panels.length}
  <section class="plugin-panels" aria-label={heading}>
    {#each panels as panel (panel.plugin_id + ':' + panel.panel_id)}
      <div class="plugin-panel">
        <h3 class="plugin-panel-title">{panel.title}</h3>
        {#if panel.notice}
          <div class="plugin-notice tone-warning">{panel.notice}</div>
        {/if}
        <div class="plugin-elements">
          {#each panel.elements as el, i (i)}
            {#if el.type === 'field'}
              <div class="plugin-row">
                <span class="plugin-label">{el.label}</span>
                <span class="plugin-value">{el.value}</span>
              </div>
            {:else if el.type === 'badge'}
              <div class="plugin-row">
                <span class="plugin-label">{el.label}</span>
                <span class="plugin-badge tone-{tone(el.tone)}">{el.text}</span>
              </div>
            {:else if el.type === 'text'}
              {#if el.tone}
                <div class="plugin-notice tone-{tone(el.tone)}">{el.text}</div>
              {:else}
                <p class="plugin-text">{el.text}</p>
              {/if}
            {:else if el.type === 'divider'}
              <hr class="plugin-divider" />
            {:else if el.type === 'action'}
              <form class="plugin-action" on:submit|preventDefault={() => run(el)}>
                {#each el.params || [] as p (p.key)}
                  <label class="plugin-param">
                    <span>{p.label}{p.required ? ' *' : ''}</span>
                    {#if p.type === 'text'}
                      <textarea bind:value={forms[keyOf(el)][p.key]} maxlength={p.max_length || undefined} required={p.required}></textarea>
                    {:else if p.type === 'integer' || p.type === 'number'}
                      <input type="number" bind:value={forms[keyOf(el)][p.key]} min={p.min ?? undefined} max={p.max ?? undefined}
                        step={p.type === 'integer' ? 1 : 'any'} required={p.required} />
                    {:else if p.type === 'boolean'}
                      <input type="checkbox" bind:checked={forms[keyOf(el)][p.key]} />
                    {:else if p.type === 'select'}
                      <select bind:value={forms[keyOf(el)][p.key]} required={p.required}>
                        {#each p.options || [] as o (String(o.value))}
                          <option value={o.value}>{o.label}</option>
                        {/each}
                      </select>
                    {:else}
                      <input type="text" bind:value={forms[keyOf(el)][p.key]} maxlength={p.max_length || undefined} required={p.required} />
                    {/if}
                    {#if p.help}<small class="plugin-help">{p.help}</small>{/if}
                  </label>
                {/each}
                <button type="submit" class="btn btn-small btn-{el.style === 'danger' ? 'danger' : el.style === 'primary' ? 'primary' : 'secondary'}"
                  disabled={busyKey === keyOf(el) || panel.health === 'unavailable'}>
                  {busyKey === keyOf(el) ? 'Working…' : el.label}
                </button>
                {#if messages[keyOf(el)]}
                  <span class="plugin-message" class:ok={messages[keyOf(el)].ok} class:bad={!messages[keyOf(el)].ok}>
                    {messages[keyOf(el)].text}
                  </span>
                {/if}
              </form>
            {/if}
          {/each}
        </div>
      </div>
    {/each}
  </section>
{:else if failed}
  <p class="plugin-unavailable">Plugin panels unavailable.</p>
{/if}

<style>
  .plugin-panels {
    display: grid;
    gap: 16px;
    margin-top: 16px;
  }
  .plugin-panel {
    background: var(--bg-primary, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: var(--radius-lg, 10px);
    padding: 16px 20px;
  }
  .plugin-panel-title {
    margin: 0 0 12px;
    font-size: 15px;
    font-weight: 650;
  }
  .plugin-elements {
    display: grid;
    gap: 8px;
  }
  .plugin-row {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    font-size: 14px;
  }
  .plugin-label {
    color: var(--text-secondary, #6b7280);
  }
  .plugin-value {
    text-align: right;
    word-break: break-word;
  }
  .plugin-badge {
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 650;
  }
  .plugin-text {
    margin: 0;
    font-size: 14px;
    color: var(--text-secondary, #6b7280);
  }
  .plugin-notice {
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
  }
  .tone-success { background: #dcfce7; color: #166534; }
  .tone-warning { background: #fef3c7; color: #92400e; }
  .tone-danger { background: #fee2e2; color: #991b1b; }
  .tone-info { background: #dbeafe; color: #1e40af; }
  .tone-neutral { background: #f3f4f6; color: #374151; }
  .plugin-divider {
    border: 0;
    border-top: 1px solid var(--border-color, #e5e7eb);
    margin: 4px 0;
  }
  .plugin-action {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 8px;
  }
  .plugin-param {
    display: grid;
    gap: 4px;
    font-size: 13px;
  }
  .plugin-help {
    color: var(--text-secondary, #6b7280);
  }
  .plugin-message {
    font-size: 13px;
  }
  .plugin-message.ok { color: #166534; }
  .plugin-message.bad { color: #991b1b; }
  .plugin-unavailable {
    font-size: 13px;
    color: var(--text-secondary, #6b7280);
  }
</style>
