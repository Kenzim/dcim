<script>
  import { criterionLines } from '../../../lib/billingCriterionFormat.js';

  /** One switch criterion item as the engines API returns it. */
  export let item;

  $: lines = criterionLines(item);
</script>

{#each lines as line}
  <div class="ba-hint">
    {line.label}{line.text || !line.bullets.length ? `: ${line.text}` : ':'}
    {#if line.bullets.length}
      <ul class="bullets">{#each line.bullets as bullet}<li>{bullet}</li>{/each}</ul>
    {/if}
  </div>
{/each}

<style>
  .bullets { margin: 2px 0 4px; padding-left: 18px; }
</style>
