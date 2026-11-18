/** Formatting helpers shared by the power history graphs. */

export const POWER_RANGES = [
  { hours: 24, label: '24 hours' },
  { hours: 168, label: '7 days' },
  { hours: 720, label: '30 days' },
  { hours: 8760, label: '1 year' },
];

export function formatWatts(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 'n/a';
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 1 : 2)} kW`;
  return `${v.toFixed(v >= 100 ? 0 : 1)} W`;
}

export function formatAmps(value) {
  const v = Number(value);
  return Number.isFinite(v) ? `${v.toFixed(v >= 10 ? 1 : 2)} A` : 'n/a';
}

export function formatKwh(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 'n/a';
  return `${v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 0 : 1 })} kWh`;
}

/** Text explaining how an energy figure was worked out; empty when it came from a PDU counter. */
export function energyNote(energy) {
  if (!energy || energy.kwh == null) return '';
  if (energy.method === 'counter') return 'From the PDU energy counter';
  if (energy.method === 'integrated') return 'Estimated from power readings';
  return 'Mixed: counters where the PDU reports one, estimates elsewhere';
}

export function chartSeries(samples, id, label, color) {
  return { id, label, color, samples: samples || [], visible: true };
}
