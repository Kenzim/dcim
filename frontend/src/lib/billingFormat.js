// Labels for invoice and note values on the client and reseller screens.
// The public APIs show only today's seven statuses in `status` and the real
// value in `status_detail`; a value not listed here is shown as it came.

const INVOICE_STATUSES = {
  draft: { label: 'Draft', variant: 'muted' },
  open: { label: 'Open', variant: 'warning' },
  partially_paid: { label: 'Partly paid', variant: 'warning' },
  pending_action: { label: 'Action required', variant: 'warning' },
  overdue: { label: 'Overdue', variant: 'danger' },
  collections: { label: 'In collections', variant: 'danger' },
  failed: { label: 'Failed', variant: 'danger' },
  paid: { label: 'Paid', variant: 'success' },
  refunded: { label: 'Refunded', variant: 'info' },
  void: { label: 'Void', variant: 'muted' },
};

const UNPAID = new Set(['open', 'pending_action', 'partially_paid', 'overdue', 'collections']);

/** The most precise status the API gave: `status_detail`, else `status`. */
export function invoiceStatusValue(invoice) {
  return String(invoice?.status_detail || invoice?.status || '');
}

export function invoiceStatusLabel(value) {
  const known = INVOICE_STATUSES[value];
  return known ? known.label : String(value ?? '');
}

export function invoiceStatusVariant(value) {
  return INVOICE_STATUSES[value]?.variant || 'muted';
}

/** True when the status means money is still owed. */
export function invoiceIsUnpaid(invoice) {
  return UNPAID.has(invoiceStatusValue(invoice));
}

/** Money still due on an invoice; `remaining_cents` follows the invoice engine in use. */
export function invoiceDueCents(invoice) {
  const due = invoice?.remaining_cents ?? invoice?.balance?.remaining_cents ?? 0;
  return Math.max(0, Number(due) || 0);
}

const NOTE_REASONS = {
  cancel: 'Cancellation',
  refund: 'Refund',
  correction: 'Correction',
  goodwill: 'Goodwill',
  late_fee: 'Late fee',
  overpayment: 'Overpayment',
  merge: 'Merged invoices',
  write_off: 'Write-off',
};

const NOTE_OUTCOMES = {
  reduced_balance: 'Reduced the amount due',
  refunded: 'Refunded',
  credited_to_balance: 'Added to account credit',
  increased_balance: 'Increased the amount due',
};

const LEDGER_TYPES = {
  topup: 'Top-up',
  charge: 'Used on an invoice',
  refund: 'Refund',
  adjustment: 'Adjustment',
  reversal: 'Reversal',
};

export const noteKindLabel = (kind) => (kind === 'debit' ? 'Debit note' : kind === 'credit' ? 'Credit note' : String(kind ?? ''));
export const noteReasonLabel = (reason) => NOTE_REASONS[reason] || String(reason ?? '').replaceAll('_', ' ');
export const noteOutcomeLabel = (outcome) => NOTE_OUTCOMES[outcome] || String(outcome ?? '').replaceAll('_', ' ');
export const ledgerTypeLabel = (type) => LEDGER_TYPES[type] || String(type ?? '').replaceAll('_', ' ');

export function formatDate(value) {
  return value ? new Date(value).toLocaleDateString() : '—';
}
