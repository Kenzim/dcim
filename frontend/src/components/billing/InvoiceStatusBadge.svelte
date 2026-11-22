<script>
  import { Badge } from '../ui/index.js';
  import { invoiceStatusLabel, invoiceStatusValue, invoiceStatusVariant } from '../../lib/billingFormat.js';

  /** An invoice as the client or reseller API returns it. */
  export let invoice;

  $: value = invoiceStatusValue(invoice);
</script>

<Badge variant={invoiceStatusVariant(value)} caps={false}>{invoiceStatusLabel(value)}</Badge>
