export function formatWatts(value) {
  if (value === null || value === undefined) return 'N/A';
  return `${Math.round(Number(value)).toLocaleString('en-GB')} W`;
}

export function formatAmps(value) {
  if (value === null || value === undefined) return 'N/A';
  return `${Number(value).toFixed(1)} A`;
}

export function formatRelativeTime(value) {
  if (!value) return 'Never';
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return new Date(value).toLocaleString();
}

export function formatRackPosition(item) {
  if (!item.rack_id) return 'N/A';
  const name = item.rack_name || 'N/A';
  if (item.rack_units === 0) return `${name} (0U)`;
  if (item.rack_unit == null) return name;
  const size = item.rack_units ?? 1;
  return size > 1
    ? `${name} (U${item.rack_unit}-${item.rack_unit + size - 1})`
    : `${name} (U${item.rack_unit})`;
}

export function feedUsageTone(feed) {
  if (feed.over_limit) return 'danger';
  if (feed.usable_current_a && feed.current_a >= feed.usable_current_a * 0.9) return 'warning';
  return 'ok';
}

export function feedUsagePercent(feed) {
  if (!feed.amperage) return 0;
  return Math.min(100, ((feed.current_a || 0) / feed.amperage) * 100);
}

export function feedLimitPercent(feed) {
  if (!feed.amperage || !feed.usable_current_a) return 0;
  return Math.min(100, (feed.usable_current_a / feed.amperage) * 100);
}
