// Display helpers for cable runs that may pass through patch panels.

export function deviceUrl(type, id) {
  if (id === null || id === undefined) return null;
  if (type === 'server') return `/admin/servers/${id}`;
  if (type === 'switch') return `/admin/switches/${id}`;
  if (type === 'patch') return `/admin/patch-panels/${id}`;
  return null;
}

export function deviceKindLabel(type) {
  if (type === 'server') return 'Server';
  if (type === 'switch') return 'Switch';
  if (type === 'patch') return 'Patch panel';
  return '';
}

/**
 * Parts of a flat "other end" summary (other_end_*, via, reaches_*, path_complete) as returned for
 * server NICs and switch ports.
 * direct: the port the cable plugs into; via: patch panel ports the path passes through;
 * far: the server or switch port the path reaches (null when it stops at a patch panel);
 * complete: whether the path reaches a server or switch port.
 */
export function cablePathParts(summary) {
  if (!summary) return null;
  const direct = {
    type: summary.other_end_type,
    deviceId: summary.other_end_device_id,
    deviceName: summary.other_end_device_name || deviceKindLabel(summary.other_end_type),
    portName: summary.other_end_port_name || `Port #${summary.other_end_port_id}`,
    side: summary.other_end_side || null,
    url: deviceUrl(summary.other_end_type, summary.other_end_device_id),
  };
  const via = (summary.via || []).map((hop) => ({
    deviceId: hop.patch_panel_id,
    deviceName: hop.patch_panel_name || 'Patch panel',
    portName: hop.port_name || `Port #${hop.port_id}`,
    url: deviceUrl('patch', hop.patch_panel_id),
  }));
  let far = null;
  if (direct.type !== 'patch') {
    far = direct;
  } else if (summary.reaches_port_id !== null && summary.reaches_port_id !== undefined) {
    far = {
      type: summary.reaches_type,
      deviceId: summary.reaches_device_id,
      deviceName: summary.reaches_device_name || deviceKindLabel(summary.reaches_type),
      portName: summary.reaches_port_name || `Port #${summary.reaches_port_id}`,
      side: null,
      url: deviceUrl(summary.reaches_type, summary.reaches_device_id),
    };
  }
  return { direct, via, far, complete: summary.path_complete !== false && far !== null };
}

export function viaText(via) {
  return via.map((hop) => `${hop.deviceName} ${hop.portName}`).join(', ');
}
