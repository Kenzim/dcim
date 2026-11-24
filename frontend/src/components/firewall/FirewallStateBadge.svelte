<script>
  import { Badge } from '../ui/index.js';
  import { STATE_INFO } from '../../lib/firewallText.js';

  export let state = 'off';

  $: info = STATE_INFO[state] || { label: String(state || 'Unknown'), variant: 'muted', text: '' };
</script>

<span title={info.text}><Badge variant={info.variant} dot>{info.label}</Badge></span>
