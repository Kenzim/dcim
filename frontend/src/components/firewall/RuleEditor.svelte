<script>
  import { Button } from '../ui/index.js';
  import {
    ACTIONS,
    ACTION_LABEL,
    ICMP_TYPES,
    PORT_PROTOCOLS,
    PROTOCOLS,
    blankGroupRef,
    blankRule,
    protocolLabel,
    remoteLabel,
  } from './ruleModel.js';

  /** The ordered draft rules (see ruleModel.toDraft). */
  export let rules = [];
  /** Field errors by path, for example `rules[2].ports`. */
  export let errors = {};
  /** Path prefix the errors use. */
  export let prefix = 'rules';
  /** Security groups that can be added as one entry: [{id, name}]. */
  export let groups = [];
  /** IP sets a rule can name as its far end: [{id, name}]. */
  export let ipsets = [];
  export let allowGroups = true;
  /** Staff also edit the VM-side addresses. */
  export let staff = false;
  export let allowLog = false;
  export let disabled = false;
  export let emptyText = 'No rules yet.';

  let editingKey = null;
  let dragIndex = null;
  let overIndex = null;
  let groupChoice = '';
  let lastErrorSignature = '';

  $: groupName = (id) => groups.find((g) => String(g.id) === String(id))?.name || `group #${id}`;
  $: usableGroups = groups.filter((g) => !rules.some((r) => r.kind === 'group' && String(r.group_id) === String(g.id)));

  $: rowErrors = (index) =>
    Object.entries(errors || {})
      .filter(([path]) => path === `${prefix}[${index}]` || path.startsWith(`${prefix}[${index}].`))
      .map(([path, message]) => ({ field: path.slice(`${prefix}[${index}]`.length + 1), message }));
  $: fieldError = (index, field) => errors?.[`${prefix}[${index}].${field}`] || '';

  // Open the first row the server complained about so its message is visible.
  $: {
    const signature = Object.keys(errors || {}).sort().join('|');
    if (signature !== lastErrorSignature) {
      lastErrorSignature = signature;
      const match = Object.keys(errors || {})
        .map((path) => new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\[(\\d+)\\]`).exec(path))
        .find(Boolean);
      if (match && rules[Number(match[1])]) editingKey = rules[Number(match[1])]._key;
    }
  }

  function addRule() {
    const rule = blankRule();
    rules = [...rules, rule];
    editingKey = rule._key;
  }

  function addGroup() {
    if (!groupChoice) return;
    rules = [...rules, blankGroupRef(Number(groupChoice))];
    groupChoice = '';
  }

  function remove(index) {
    const [gone] = rules.slice(index, index + 1);
    rules = rules.filter((_, i) => i !== index);
    if (gone && gone._key === editingKey) editingKey = null;
  }

  function move(from, to) {
    if (to < 0 || to >= rules.length || from === to) return;
    const next = [...rules];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    rules = next;
  }

  function dragStart(event, index) {
    dragIndex = index;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(index));
  }
  function dragOver(event, index) {
    if (dragIndex === null) return;
    event.preventDefault();
    overIndex = index;
  }
  function drop(event, index) {
    event.preventDefault();
    if (dragIndex !== null) move(dragIndex, index);
    dragIndex = null;
    overIndex = null;
  }
  function dragEnd() {
    dragIndex = null;
    overIndex = null;
  }

  const toggleEdit = (rule) => (editingKey = editingKey === rule._key ? null : rule._key);
</script>

<div class="rules">
  {#if rules.length === 0}
    <p class="empty">{emptyText}</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="narrow" aria-label="Order"></th>
            <th class="narrow">On</th>
            <th>Action</th>
            <th>Direction</th>
            <th>Protocol</th>
            <th>Ports</th>
            <th>Source or destination</th>
            <th>Note</th>
            <th class="actions-col"></th>
          </tr>
        </thead>
        <tbody>
          {#each rules as rule, index (rule._key)}
            {@const errs = rowErrors(index)}
            <tr
              class:off={!rule.enabled}
              class:bad={errs.length > 0}
              class:over={overIndex === index && dragIndex !== index}
              on:dragover={(e) => dragOver(e, index)}
              on:drop={(e) => drop(e, index)}
            >
              <td class="narrow">
                <span
                  class="handle"
                  class:locked={disabled}
                  role="button"
                  tabindex="-1"
                  aria-label="Drag to reorder"
                  title="Drag to reorder"
                  draggable={!disabled}
                  on:dragstart={(e) => dragStart(e, index)}
                  on:dragend={dragEnd}>⋮⋮</span
                >
                <span class="num">{index + 1}</span>
              </td>
              <td class="narrow">
                <input type="checkbox" bind:checked={rule.enabled} {disabled} aria-label="Rule {index + 1} enabled" />
              </td>
              {#if rule.kind === 'group'}
                <td colspan="5"><span class="chip group">Group</span> {groupName(rule.group_id)}</td>
              {:else}
                <td><span class="chip" class:allow={rule.action === 'ACCEPT'} class:block={rule.action !== 'ACCEPT'}>{ACTION_LABEL[rule.action] || rule.action}</span></td>
                <td>{rule.direction === 'in' ? 'Incoming' : 'Outgoing'}</td>
                <td>{protocolLabel(rule.protocol)}{rule.ip_version !== 'any' ? ` (IPv${rule.ip_version})` : ''}</td>
                <td class="mono">{rule.ports || rule.icmp_type || 'Any'}</td>
                <td class="mono">{remoteLabel(rule, ipsets)}</td>
              {/if}
              <td class="note">{rule.comment}</td>
              <td class="actions-col">
                <div class="row-actions">
                  <button type="button" class="tiny" aria-label="Move up" title="Move up" on:click={() => move(index, index - 1)} disabled={disabled || index === 0}>↑</button>
                  <button type="button" class="tiny" aria-label="Move down" title="Move down" on:click={() => move(index, index + 1)} disabled={disabled || index === rules.length - 1}>↓</button>
                  <button type="button" class="tiny" {disabled} on:click={() => toggleEdit(rule)}>{editingKey === rule._key ? 'Close' : 'Edit'}</button>
                  <button type="button" class="tiny danger" {disabled} on:click={() => remove(index)}>Remove</button>
                </div>
              </td>
            </tr>
            {#if errs.length > 0 && editingKey !== rule._key}
              <tr class="err-row">
                <td colspan="9">{#each errs as e}<div class="field-error">{e.field ? `${e.field}: ` : ''}{e.message}</div>{/each}</td>
              </tr>
            {/if}
            {#if editingKey === rule._key}
              <tr class="edit-row">
                <td colspan="9">
                  {#if rule.kind === 'group'}
                    <div class="form">
                      <label class="wide">Note (kept here only)
                        <input type="text" bind:value={rule.comment} maxlength="120" {disabled} />
                        {#if fieldError(index, 'comment')}<span class="field-error">{fieldError(index, 'comment')}</span>{/if}
                      </label>
                      {#if fieldError(index, 'group_id')}<span class="field-error">{fieldError(index, 'group_id')}</span>{/if}
                    </div>
                  {:else}
                    <div class="form">
                      <label>Direction
                        <select bind:value={rule.direction} {disabled}>
                          <option value="in">Incoming (to this server)</option>
                          <option value="out">Outgoing (from this server)</option>
                        </select>
                        {#if fieldError(index, 'direction')}<span class="field-error">{fieldError(index, 'direction')}</span>{/if}
                      </label>
                      <label>Action
                        <select bind:value={rule.action} {disabled}>
                          {#each ACTIONS as action}<option value={action}>{ACTION_LABEL[action]}</option>{/each}
                        </select>
                        {#if fieldError(index, 'action')}<span class="field-error">{fieldError(index, 'action')}</span>{/if}
                      </label>
                      <label>IP version
                        <select bind:value={rule.ip_version} {disabled}>
                          <option value="any">IPv4 and IPv6</option>
                          <option value="4">IPv4 only</option>
                          <option value="6">IPv6 only</option>
                        </select>
                        {#if fieldError(index, 'ip_version')}<span class="field-error">{fieldError(index, 'ip_version')}</span>{/if}
                      </label>
                      <label>Protocol
                        <select bind:value={rule.protocol} {disabled}>
                          <option value="">Any</option>
                          {#each PROTOCOLS as protocol}<option value={protocol}>{protocolLabel(protocol)}</option>{/each}
                        </select>
                        {#if fieldError(index, 'protocol')}<span class="field-error">{fieldError(index, 'protocol')}</span>{/if}
                      </label>
                      {#if PORT_PROTOCOLS.includes(rule.protocol)}
                        <label>Destination ports
                          <input type="text" bind:value={rule.ports} placeholder="22 or 80,443 or 1000:2000" {disabled} />
                          {#if fieldError(index, 'ports')}<span class="field-error">{fieldError(index, 'ports')}</span>{/if}
                        </label>
                        <label>Source ports
                          <input type="text" bind:value={rule.source_ports} placeholder="Leave empty for any" {disabled} />
                          {#if fieldError(index, 'source_ports')}<span class="field-error">{fieldError(index, 'source_ports')}</span>{/if}
                        </label>
                      {:else if rule.protocol === 'icmp' || rule.protocol === 'ipv6-icmp'}
                        <label>ICMP type
                          <input type="text" list="fw-icmp-types" bind:value={rule.icmp_type} placeholder="echo-request" {disabled} />
                          {#if fieldError(index, 'icmp_type')}<span class="field-error">{fieldError(index, 'icmp_type')}</span>{/if}
                        </label>
                      {/if}
                      <label>{rule.direction === 'in' ? 'Allowed from' : 'Sent to'}
                        <select bind:value={rule.remoteMode} {disabled}>
                          <option value="any">Anywhere</option>
                          <option value="cidrs">These addresses</option>
                          {#if ipsets.length}<option value="ipset">An IP set</option>{/if}
                        </select>
                      </label>
                      {#if rule.remoteMode === 'cidrs'}
                        <label class="wide">Addresses (comma or space separated)
                          <input type="text" bind:value={rule.cidrsText} placeholder="203.0.113.0/24, 2001:db8::/32" {disabled} />
                          {#if fieldError(index, 'remote_cidrs')}<span class="field-error">{fieldError(index, 'remote_cidrs')}</span>{/if}
                        </label>
                      {:else if rule.remoteMode === 'ipset'}
                        <label>IP set
                          <select bind:value={rule.ipsetId} {disabled}>
                            <option value="">Choose an IP set</option>
                            {#each ipsets as set}<option value={String(set.id)}>{set.name}</option>{/each}
                          </select>
                          {#if fieldError(index, 'remote_ipset_id')}<span class="field-error">{fieldError(index, 'remote_ipset_id')}</span>{/if}
                        </label>
                      {/if}
                      {#if staff}
                        <label class="wide">This server's own addresses (optional)
                          <input type="text" bind:value={rule.localText} placeholder="Leave empty for all" {disabled} />
                          {#if fieldError(index, 'local_cidrs')}<span class="field-error">{fieldError(index, 'local_cidrs')}</span>{/if}
                        </label>
                      {/if}
                      <label class="wide">Note (kept here only)
                        <input type="text" bind:value={rule.comment} maxlength="120" {disabled} />
                        {#if fieldError(index, 'comment')}<span class="field-error">{fieldError(index, 'comment')}</span>{/if}
                      </label>
                      {#if allowLog}
                        <label class="check"><input type="checkbox" bind:checked={rule.log} {disabled} /> Log matching traffic</label>
                      {/if}
                      {#each errs.filter((e) => !e.field || !['direction', 'action', 'ip_version', 'protocol', 'ports', 'source_ports', 'icmp_type', 'remote_cidrs', 'remote_ipset_id', 'local_cidrs', 'comment', 'group_id'].includes(e.field)) as e}
                        <span class="field-error wide">{e.field ? `${e.field}: ` : ''}{e.message}</span>
                      {/each}
                    </div>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <datalist id="fw-icmp-types">
    {#each ICMP_TYPES as type}<option value={type}></option>{/each}
  </datalist>

  <div class="add-bar">
    <Button variant="secondary" size="small" {disabled} on:click={addRule}>Add rule</Button>
    {#if allowGroups && usableGroups.length}
      <span class="group-add">
        <select bind:value={groupChoice} {disabled} aria-label="Security group to add">
          <option value="">Add a security group…</option>
          {#each usableGroups as group}<option value={String(group.id)}>{group.name}</option>{/each}
        </select>
        <Button variant="secondary" size="small" disabled={disabled || !groupChoice} on:click={addGroup}>Add group</Button>
      </span>
    {/if}
  </div>
</div>

<style>
  .rules { display: flex; flex-direction: column; gap: 10px; }
  .empty { margin: 0; color: var(--text-tertiary); font-size: 13px; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); background: var(--bg-secondary); white-space: nowrap; }
  td { padding: 8px 10px; border-top: 1px solid var(--border-color); vertical-align: middle; }
  tr.off td:not(.actions-col):not(.narrow) { opacity: 0.55; }
  tr.bad td { background: var(--danger-bg); }
  tr.over td { box-shadow: inset 0 2px 0 var(--portal-accent, #0e7490); }
  .narrow { width: 1%; white-space: nowrap; }
  .handle { cursor: grab; color: var(--text-tertiary); user-select: none; padding: 0 4px; }
  .handle.locked { cursor: default; opacity: 0.4; }
  .num { color: var(--text-tertiary); font-size: 12px; }
  .mono { font-family: var(--font-mono); font-size: 12px; word-break: break-all; }
  .note { color: var(--text-secondary); max-width: 220px; overflow-wrap: anywhere; }
  .chip { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 650; background: var(--bg-tertiary); color: var(--text-secondary); }
  .chip.allow { background: var(--success-bg); color: var(--success-text); }
  .chip.block { background: var(--danger-bg); color: var(--danger-text); }
  .chip.group { background: var(--info-bg); color: var(--info-text); }
  .actions-col { text-align: right; white-space: nowrap; }
  .row-actions { display: inline-flex; gap: 4px; }
  .tiny { padding: 3px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); cursor: pointer; font-size: 12px; }
  .tiny.danger { color: var(--danger-color); border-color: var(--danger-color); }
  .tiny:disabled { opacity: 0.5; cursor: not-allowed; }
  .edit-row td { background: var(--bg-secondary); }
  .err-row td { background: var(--danger-bg); border-top: none; padding-top: 0; }
  .form { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px 14px; }
  .form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .form .wide { grid-column: 1 / -1; }
  .form .check { flex-direction: row; align-items: center; gap: 8px; }
  input[type='text'], select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; width: 100%; min-width: 0; }
  .field-error { color: var(--danger-text); font-size: 12px; font-weight: 500; }
  .add-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .group-add { display: inline-flex; gap: 6px; align-items: center; }
  .group-add select { width: auto; min-width: 200px; }
</style>
