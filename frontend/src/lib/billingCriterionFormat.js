// Turns the detail of one switch criterion item (`{ item, ok, ...values }`) into plain lines.
// Each line is `{ label, text, bullets }`: `text` for a short value, `bullets` for a long list.

const humanise = (value) => String(value ?? '').replaceAll('_', ' ');
const capital = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

// What to do about a value that is not empty, for the keys the criterion items use.
const ACTIONS = {
  disabled: 'Enable these alerts',
  unregistered: 'Add a seller registration for',
  missing_periods: 'Store the HMRC exchange rate for',
  missing: 'Give these a tax rule',
  days_missing: 'No daily scan ran on',
  resellers_without_account: 'Resellers without a billing account',
  accounts_differing: 'Accounts whose cached balance differs',
};

// Keys that only repeat what another line says.
const HIDDEN = new Set(['required']);

const LABELS = {
  covers: 'The sign-off must cover',
  signoff: 'Signed off',
  open_mismatches: 'Open mismatches',
  open_divergence: 'Open differences between old and new tax',
  unconfirmed_divergence: 'Differences not yet confirmed',
  seller_country: 'Seller country',
  establishment_registration: 'Registration in the seller country',
  rules_in_force: 'Rules in force',
  quiet_days: 'Quiet days so far',
  required_days: 'Quiet days needed',
};

const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function scalar(value) {
  if (value === null || value === undefined || value === '') return 'not set';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
  if (typeof value === 'string' && ISO_TIME.test(value)) return new Date(value).toLocaleString();
  return String(value);
}

function entry(value) {
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([key, inner]) => `${humanise(key)} ${scalar(inner)}`).join(', ');
  }
  return scalar(value);
}

/** The lines that describe one criterion item, without its name and its ok flag. */
export function criterionLines(item) {
  return Object.entries(item || {})
    .filter(([key]) => key !== 'item' && key !== 'ok' && !HIDDEN.has(key))
    .map(([key, value]) => {
      const base = LABELS[key] || capital(humanise(key));
      if (Array.isArray(value)) {
        if (!value.length) return { label: base, text: 'none', bullets: [] };
        const parts = value.map(entry);
        const label = ACTIONS[key] || base;
        if (parts.some((part) => part.length > 40)) return { label, text: '', bullets: parts };
        return { label, text: parts.join(', '), bullets: [] };
      }
      if (value && typeof value === 'object') {
        const parts = Object.entries(value);
        if (!parts.length) return { label: base, text: 'none', bullets: [] };
        return { label: base, text: parts.map(([name, count]) => `${humanise(name)} ${scalar(count)}`).join(', '), bullets: [] };
      }
      return { label: base, text: scalar(value), bullets: [] };
    });
}
