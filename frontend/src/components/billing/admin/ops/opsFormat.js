// Display helpers shared by the billing automation screens.
import { formatMoney } from '../../../../lib/resellerMoney.js';

export function label(value) {
  return String(value ?? '').replaceAll('_', ' ');
}

export function when(value) {
  return value ? new Date(value).toLocaleString() : '-';
}

/** Money in the record's own currency; the bare amount when the currency is unknown or not valid. */
export function money(cents, currency) {
  if (!currency) return `${cents} cents`;
  try {
    return formatMoney(cents, currency);
  } catch {
    return `${cents} cents (${currency})`;
  }
}

/** One value of a run item's detail; `*_cents` keys use the detail's own currency. */
export function detailValue(key, value, currency) {
  if (value === null || value === undefined) return '-';
  if (key.endsWith('_cents') && typeof value === 'number') return money(value, currency);
  if (/_at$/.test(key) && typeof value === 'string') return when(value);
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** The detail of a run item or forecast item as [key, text] pairs, without the fields shown elsewhere. */
export function detailPairs(detail, skip = []) {
  const source = detail && typeof detail === 'object' ? detail : {};
  const currency = typeof source.currency === 'string' ? source.currency : undefined;
  return Object.entries(source)
    .filter(([key]) => !skip.includes(key))
    .map(([key, value]) => [label(key), detailValue(key, value, currency)]);
}
