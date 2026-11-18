// Display helpers shared by the NFS storage pages.

export const GIB = 1024 ** 3;

export const STORAGE_STATUS_LABELS = {
  ready: 'Ready',
  provisioning: 'Setting up',
  error: 'Problem',
  suspended: 'Suspended',
  unlinked: 'No network',
  terminated: 'Terminated',
  deleting: 'Deleting',
};

export function formatBytes(bytes) {
  if (bytes == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export const percent = (part, whole) => (whole ? Math.min(100, Math.round(((part || 0) / whole) * 100)) : 0);
