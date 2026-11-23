// Plain-language labels for tax and cancellation values on the client and reseller screens.
// A value not listed here is shown as it came, with underscores turned into spaces.

import { formatMoney } from './resellerMoney.js';

const humanise = (value) => String(value ?? '').replaceAll('_', ' ');

// --- Tax ID ---

const TAX_ID_TYPES = {
  eu_vat: 'EU VAT number',
  gb_vat: 'UK VAT number',
  other: 'Other tax number',
};

export const TAX_ID_TYPE_OPTIONS = Object.entries(TAX_ID_TYPES).map(([value, label]) => ({ value, label }));

export const taxIdTypeLabel = (type) => TAX_ID_TYPES[type] || humanise(type);

const TAX_ID_STATUSES = {
  unchecked: { label: 'Not checked', variant: 'muted' },
  valid: { label: 'Valid', variant: 'success' },
  invalid: { label: 'Not valid', variant: 'danger' },
  format_invalid: { label: 'Wrong format', variant: 'danger' },
  unavailable: { label: 'Could not be checked', variant: 'warning' },
};

/**
 * What to tell a client about the result of a tax ID check:
 * `{ label, variant, text }`, where `text` is a sentence.
 */
export function taxIdStatusInfo(status, hasTaxId = true) {
  const known = TAX_ID_STATUSES[status] || { label: humanise(status), variant: 'muted' };
  let text;
  switch (status) {
    case 'valid':
      text = 'Your number was checked and is valid.';
      break;
    case 'invalid':
      text = 'The tax authority does not recognise this number. Check it for mistakes and save again.';
      break;
    case 'format_invalid':
      text = 'This number is not in the right format for the country. Check it and save again.';
      break;
    case 'unavailable':
      text = 'We could not check your number because the checking service is not available. We will try again later.';
      break;
    default:
      text = hasTaxId
        ? 'This number has not been checked yet.'
        : 'Add a tax number if you are buying as a business.';
  }
  return { ...known, text };
}

// --- Tax on a quote or invoice ---

/** A rate such as "20.0000" shown as "20%"; "5.5000" as "5.5%". */
export function formatRate(rate) {
  const number = Number(rate);
  if (!Number.isFinite(number)) return String(rate ?? '');
  return `${Number(number.toFixed(4))}%`;
}

/** True when a quote or invoice carries tax detail from the tax engine. */
export function hasTaxDetail(source) {
  if (!source) return false;
  return (
    (Array.isArray(source.tax_summary) && source.tax_summary.length > 0)
    || (Array.isArray(source.tax_lines) && source.tax_lines.length > 0)
    || (Array.isArray(source.legal_notes) && source.legal_notes.length > 0)
    || source.reverse_charge === true
  );
}

/**
 * A legal note as `{ key, text }`. Invoices list `{ key, text }` and a quote lists the same in
 * `legal_note_texts`, both rendered by the server from the stored (and possibly edited) text,
 * so the text is shown as given. A bare key (a quote from an older server) is shown as words.
 */
export function legalNoteView(note) {
  if (note && typeof note === 'object') {
    const key = String(note.key ?? '');
    return { key, text: note.text || humanise(key) };
  }
  const key = String(note ?? '');
  return { key, text: humanise(key) };
}

/** The notes of a quote or invoice: the rendered texts when it has them, else its note keys. */
export function legalNotesOf(source) {
  if (Array.isArray(source?.legal_note_texts) && source.legal_note_texts.length > 0) return source.legal_note_texts;
  return Array.isArray(source?.legal_notes) ? source.legal_notes : [];
}

/** The label of one tax summary entry, for example "VAT 20%" or "VAT 0% (reverse charge)". */
export function taxEntryLabel(entry) {
  const rate = formatRate(entry.rate_percent);
  return `${entry.name} ${rate}${entry.reverse_charge ? ' (reverse charge)' : ''}`;
}

// --- Tax refusals at checkout ---

// Codes the quote and checkout routes answer with (409, `detail: { code, message }`).
// `requote`: the tax or the price may have moved, so a new quote is needed;
// `retry`: nothing was placed and the same order can be sent again;
// `later`: it cannot succeed yet, so show the message and stop.
const TAX_REFUSAL_ACTIONS = {
  tax_quote_changed: 'requote',
  tax_rules_changed: 'requote',
  tax_publish_retry: 'retry',
  tax_fx_rate_missing: 'later',
  tax_id_unverifiable: 'later',
};

/** The code of a failed commerce request (`detail` is `{ code, message }` for a tax refusal), or ''. */
export function refusalCode(err) {
  const detail = err?.detail;
  return detail && typeof detail === 'object' && typeof detail.code === 'string' ? detail.code : '';
}

/** What a client can do about a failed quote or order: `requote`, `retry`, `later` or `none`. */
export function taxRefusalAction(err) {
  return TAX_REFUSAL_ACTIONS[refusalCode(err)] || 'none';
}

// --- Sterling VAT on an invoice in another currency ---

const FX_SOURCES = {
  hmrc_monthly: "HMRC's monthly exchange rate",
};

/** "2026-11" as "November 2026". */
export function formatPeriod(period) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(period ?? ''));
  if (!match) return String(period ?? '');
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1));
  return new Intl.DateTimeFormat('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' }).format(date);
}

/**
 * The sterling VAT total of an invoice as a view model, or null when the invoice has none.
 * `{ pending: true }` while the month's rate is not stored yet (the month is known, the
 * amount is not); otherwise `{ pending: false, amount, rate, period, source }` as text.
 */
export function sterlingVatView(invoice) {
  if (!invoice) return null;
  const cents = invoice.tax_total_gbp_cents;
  const period = invoice.fx_period;
  if (cents === null || cents === undefined) {
    return period ? { pending: true, period: formatPeriod(period) } : null;
  }
  const rate = Number(invoice.fx_rate);
  return {
    pending: false,
    amount: formatMoney(cents, 'GBP'),
    rate: Number.isFinite(rate) ? `${rate.toFixed(4)} ${invoice.currency} to £1` : '',
    period: formatPeriod(period),
    source: FX_SOURCES[invoice.fx_source] || humanise(invoice.fx_source),
  };
}

// --- Cancellation requests ---

const CANCELLATION_STATUSES = {
  pending: { label: 'Being processed', variant: 'warning' },
  scheduled: { label: 'Scheduled', variant: 'info' },
  completed: { label: 'Cancelled', variant: 'success' },
  withdrawn: { label: 'Withdrawn', variant: 'muted' },
  failed: { label: 'Failed', variant: 'danger' },
};

export const cancellationStatusLabel = (status) => CANCELLATION_STATUSES[status]?.label || humanise(status);
export const cancellationStatusVariant = (status) => CANCELLATION_STATUSES[status]?.variant || 'muted';

export const cancellationTypeLabel = (type) => (
  type === 'immediate' ? 'Straight away' : type === 'end_of_period' ? 'At the end of the billing period' : humanise(type)
);

/** Who asked for the cancellation, worded for the person reading (`viewer` is `client` or `reseller`). */
export function cancellationSourceLabel(source, viewer = 'client') {
  if (source === 'admin') return 'Our support team';
  if (source === viewer) return 'You';
  return { client: 'The client', reseller: 'The reseller' }[source] || humanise(source);
}

export const cancellationInvoiceActionLabel = (action) => (
  action === 'cancel_unpaid'
    ? 'Cancels unpaid invoices for later periods'
    : action === 'keep'
      ? 'Keeps unpaid invoices'
      : humanise(action)
);

/** Only a scheduled request that has not yet taken effect can be withdrawn. */
export function cancellationCanWithdraw(request, now = Date.now()) {
  if (request?.status !== 'scheduled') return false;
  if (!request.effective_at) return true;
  return new Date(request.effective_at).getTime() > now;
}
