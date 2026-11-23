// Small helpers shared by the tax admin screens.

export const label = (value) => String(value ?? '').replaceAll('_', ' ');

export const when = (value) => (value ? new Date(value).toLocaleString() : '-');

export const day = (value) => (value ? new Date(value).toLocaleDateString() : '-');

const pad = (n) => String(n).padStart(2, '0');

/** An ISO time as the value of a datetime-local input (the browser's time zone). */
export function toInput(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The value of a datetime-local input as an ISO time, or null when empty. */
export function fromInput(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** A datetime-local value a number of minutes from now. */
export const inMinutes = (minutes) => toInput(new Date(Date.now() + minutes * 60000).toISOString());

export function show(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export const REGISTRATION_SCHEMES = [
  ['domestic', 'Domestic'],
  ['oss_union', 'Union one-stop shop'],
  ['oss_non_union', 'Non-union one-stop shop'],
];

export const ROUNDING_RULES = ['round_half_up', 'round_half_even', 'round_down', 'tax_floor'];

export const TAX_ID_TONE = {
  valid: 'ok',
  invalid: 'bad',
  format_invalid: 'bad',
  unavailable: 'warn',
  unchecked: '',
};
