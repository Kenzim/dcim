<script>
  import { describeServerRule } from './ruleModel.js';

  /** {anti_spoof, smtp_out, management_blocked, rules[]} from the firewall view. */
  export let baseline = { rules: [] };
  export let ipsets = [];

  $: rows = (baseline.rules || []).map((rule) => ({ rule, ...describeServerRule(rule, ipsets) }));
  $: facts = [
    { label: 'Address anti-spoofing', on: !!baseline.anti_spoof, onText: 'On', offText: 'Off' },
    { label: 'Outgoing mail (port 25)', on: baseline.smtp_out === 'block', onText: 'Blocked', offText: 'Allowed' },
    { label: 'Provider management networks', on: !!baseline.management_blocked, onText: 'Blocked', offText: 'Not blocked' },
  ];
  $: hasAnything = rows.length > 0 || facts.some((fact) => fact.on);
</script>

<section class="baseline">
  <h4>Provider protections</h4>
  <p class="hint">These apply first and cannot be undone by your own rules.</p>
  <div class="facts">
    {#each facts as fact}
      <span class="fact" class:on={fact.on}><strong>{fact.label}</strong> {fact.on ? fact.onText : fact.offText}</span>
    {/each}
  </div>
  {#if rows.length}
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Action</th><th>Direction</th><th>Protocol</th><th>Ports</th><th>Source or destination</th><th>Note</th></tr>
        </thead>
        <tbody>
          {#each rows as row}
            <tr class:off={!row.draft.enabled}>
              <td>{row.action}</td>
              <td>{row.direction}</td>
              <td>{row.protocol}</td>
              <td class="mono">{row.ports}</td>
              <td class="mono">{row.remote}</td>
              <td>{row.draft.comment}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if !hasAnything}
    <p class="hint">No provider protections apply to this server.</p>
  {/if}
</section>

<style>
  .baseline { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; border: 1px dashed var(--border-color); border-radius: 10px; background: var(--bg-secondary); }
  h4 { margin: 0; font-size: 14px; }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .facts { display: flex; flex-wrap: wrap; gap: 8px; }
  .fact { font-size: 12px; padding: 3px 10px; border-radius: 999px; background: var(--bg-tertiary); color: var(--text-secondary); }
  .fact.on { background: var(--info-bg); color: var(--info-text); }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; padding: 5px 8px; font-size: 11px; text-transform: uppercase; color: var(--text-tertiary); }
  td { padding: 5px 8px; border-top: 1px solid var(--border-color); }
  tr.off td { opacity: 0.55; }
  .mono { font-family: var(--font-mono); }
</style>
