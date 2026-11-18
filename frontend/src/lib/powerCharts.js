/** Turn power history responses into the chart lists PowerHistoryPanel draws. */
import { chartSeries } from './powerFormat.js';

const POWER_COLOUR = '#7c3aed';
const CURRENT_COLOUR = '#0891b2';
const LIMIT_COLOUR = '#dc2626';
const PALETTE = ['#7c3aed', '#0891b2', '#f97316', '#16a34a', '#db2777', '#ca8a04'];

function byColour(items, metric, labelOf, idOf) {
  return items.map((item, i) => chartSeries(item.metrics?.[metric], idOf(item), labelOf(item), PALETTE[i % PALETTE.length]));
}

export function buildPduCharts(history) {
  const charts = [
    { title: 'Power', unit: 'W', series: [chartSeries(history.metrics.power_w, 'power', 'Power', POWER_COLOUR)] },
  ];
  const inlets = history.inlets || [];
  if (inlets.length > 1) {
    charts.push({
      title: 'Power by inlet (an inactive ATS input reads zero)',
      unit: 'W',
      series: byColour(inlets, 'power_w', (i) => i.name, (i) => `inlet-${i.id}`),
    });
  }
  for (const bank of history.banks || []) {
    charts.push({
      title: `Bank ${bank.name} current`,
      unit: 'A',
      series: [chartSeries(bank.metrics.current_a, `bank-${bank.name}`, `Bank ${bank.name}`, CURRENT_COLOUR)],
      referenceLines: bank.rated_current_a
        ? [{ value: bank.rated_current_a, label: `Rated ${bank.rated_current_a} A`, color: LIMIT_COLOUR }]
        : [],
    });
  }
  return charts;
}

export function buildFeedCharts(history) {
  const limits = history.limits || {};
  const power = [chartSeries(history.metrics.power_w, 'power', 'Total power', POWER_COLOUR)];
  const inlets = history.inlets || [];
  const charts = [
    {
      title: 'Power',
      unit: 'W',
      series: power,
      referenceLines: limits.usable_w ? [{ value: limits.usable_w, label: `Usable ${Math.round(limits.usable_w)} W`, color: LIMIT_COLOUR }] : [],
    },
    {
      title: 'Current',
      unit: 'A',
      series: [chartSeries(history.metrics.current_a, 'current', 'Total current', CURRENT_COLOUR)],
      referenceLines: limits.usable_current_a
        ? [{ value: limits.usable_current_a, label: `Derated limit ${limits.usable_current_a} A`, color: LIMIT_COLOUR }]
        : [],
    },
  ];
  if (inlets.length > 1) {
    charts.push({
      title: 'Power by PDU inlet',
      unit: 'W',
      series: byColour(inlets, 'power_w', (i) => `${i.pdu_name} / ${i.name}`, (i) => `inlet-${i.id}`),
    });
  }
  return charts;
}

export function buildRackCharts(history) {
  const limits = history.limits || {};
  return [
    {
      title: 'Power drawn by the rack PDUs',
      unit: 'W',
      series: [chartSeries(history.metrics.power_w, 'power', 'Power', POWER_COLOUR)],
      referenceLines: limits.usable_w ? [{ value: limits.usable_w, label: `Usable feed capacity ${Math.round(limits.usable_w)} W`, color: LIMIT_COLOUR }] : [],
    },
  ];
}

export function buildServerCharts(history) {
  return [
    { title: 'Power', unit: 'W', series: [chartSeries(history.metrics.power_w, 'power', 'Power', POWER_COLOUR)] },
    { title: 'Current', unit: 'A', series: [chartSeries(history.metrics.current_a, 'current', 'Current', CURRENT_COLOUR)] },
  ];
}
