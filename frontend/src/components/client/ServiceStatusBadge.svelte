<script>
  import { Badge } from '../ui/index.js';

  /** Service lifecycle status: active | pending | suspended | terminated | ... */
  export let status = '';
  /** Live power state: on | off | unknown (optional). */
  export let powerState = undefined;

  $: normalized = String(status || '').toLowerCase();
  $: isLifecycleIssue = normalized && normalized !== 'active';
</script>

{#if isLifecycleIssue}
  {#if normalized === 'suspended'}
    <Badge variant="warning" dot>Suspended</Badge>
  {:else if normalized === 'terminated'}
    <Badge variant="danger" dot>Terminated</Badge>
  {:else if normalized === 'pending'}
    <Badge variant="info" dot>Provisioning</Badge>
  {:else}
    <Badge variant="muted" dot>{status}</Badge>
  {/if}
{:else if powerState === 'on'}
  <Badge variant="success" dot>Running</Badge>
{:else if powerState === 'off'}
  <Badge variant="muted" dot>Stopped</Badge>
{:else if normalized === 'active'}
  <Badge variant="success" dot>Active</Badge>
{:else}
  <Badge variant="muted" dot>{status || 'Unknown'}</Badge>
{/if}
