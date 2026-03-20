const API_BASE = '/api';

export async function getInstallationHistory(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/installation-tasks`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    // Handle 404 gracefully - return empty array instead of throwing
    if (response.status === 404) {
      return [];
    }
    // For other errors, try to get error message
    try {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to get installation history');
    } catch (e) {
      // If response isn't JSON, throw a generic error
      throw new Error(`Failed to get installation history: ${response.statusText}`);
    }
  }

  return await response.json();
}

export async function getServerActivity(serverId, limit = 100) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/activity?limit=${encodeURIComponent(limit)}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return [];
    }
    try {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to get server activity');
    } catch (e) {
      throw new Error(`Failed to get server activity: ${response.statusText}`);
    }
  }

  return await response.json();
}

export async function runServerHardwareDetection(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/run`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to queue hardware detection');
  }
  return await response.json();
}

export async function listServerHardwareDetectionReports(serverId, statusFilter = null) {
  const query = statusFilter ? `?status_filter=${encodeURIComponent(statusFilter)}` : '';
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports${query}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list hardware detection reports');
  }
  return await response.json();
}

export async function getServerHardwareDetectionReport(serverId, reportId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports/${reportId}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to get hardware detection report');
  }
  return await response.json();
}

export async function getServerHardwareDetectionDiff(serverId, reportId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports/${reportId}/diff`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load hardware detection diff');
  }
  return await response.json();
}

export async function applyServerHardwareDetectionReport(serverId, reportId, payload = {}) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports/${reportId}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to apply hardware detection report');
  }
  return await response.json();
}

export async function rejectServerHardwareDetectionReport(serverId, reportId, payload = {}) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports/${reportId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to reject hardware detection report');
  }
  return await response.json();
}

export async function deleteServerHardwareDetectionReport(serverId, reportId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/hardware-detection/reports/${reportId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete hardware detection report');
  }
}

export async function updateInstallationTaskStatus(serverId, taskId, { status, error_message }) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/installation-tasks/${taskId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ status, error_message }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update installation status');
  }
  return await response.json();
}

export async function purgePendingInstallationHistory(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/installation-tasks/purge-pending`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to purge pending installation history');
  }
  return await response.json();
}

// Utility API functions
export async function generatePassword(length = 16, charset = 'alphanumeric', excludeAmbiguous = true) {
  const response = await fetch(`${API_BASE}/utils/generate-password`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      length,
      charset,
      exclude_ambiguous: excludeAmbiguous,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to generate password');
  }

  const data = await response.json();
  return data.password;
}

export async function login(username, password) {
  // Create an AbortController for timeout
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 10000); // 10 second timeout

  try {
    const response = await fetch(`${API_BASE}/users/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'include', // Important for cookies
      signal: controller.signal,
      body: JSON.stringify({
        username: username,
        password: password,
      }),
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      let errorDetail = 'Login failed';
      try {
        const error = await response.json();
        errorDetail = error.detail || errorDetail;
      } catch (e) {
        // If response is not JSON, use status text
        errorDetail = response.statusText || errorDetail;
      }
      throw new Error(errorDetail);
    }

    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Login request timed out. Please check your connection.');
    }
    throw error;
  }
}

export async function logout() {
  const response = await fetch(`${API_BASE}/users/logout`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Logout failed');
  }

  return await response.json();
}

export async function getCurrentUser() {
  const response = await fetch(`${API_BASE}/users/me`, {
    method: 'GET',
    credentials: 'include', // Important for cookies
  });

  if (!response.ok) {
    if (response.status === 401) {
      return null; // Not authenticated
    }
    throw new Error('Failed to get user');
  }

  return await response.json();
}

export async function getSessions() {
  const response = await fetch(`${API_BASE}/users/sessions`, {
    method: 'GET',
    credentials: 'include', // Important for cookies
  });

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      return []; // Not authenticated or not allowed (e.g. non-admin)
    }
    throw new Error('Failed to get sessions');
  }

  return await response.json();
}

export async function deleteSession(tokenId) {
  const response = await fetch(`${API_BASE}/users/sessions/${tokenId}`, {
    method: 'DELETE',
    credentials: 'include', // Important for cookies
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete session');
  }

  return await response.json();
}

export async function changePassword(currentPassword, newPassword) {
  const response = await fetch(`${API_BASE}/users/me/change-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to change password');
  }

  return await response.json();
}

export async function getPlugins() {
  const response = await fetch(`${API_BASE}/plugins/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 401) {
      return null; // Not authenticated
    }
    throw new Error('Failed to get plugins');
  }

  return await response.json();
}

export async function getSwitchPlugins() {
  const response = await fetch(`${API_BASE}/switch-plugins/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 401) {
      return null; // Not authenticated
    }
    throw new Error('Failed to get switch plugins');
  }

  return await response.json();
}

export async function getPluginDetails(pluginName) {
  const response = await fetch(`${API_BASE}/plugins/${pluginName}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get plugin details');
  }

  return await response.json();
}

// Location API functions
export async function getLocation(id) {
  const response = await fetch(`${API_BASE}/locations/${id}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to get location');
  }
  return await response.json();
}

export async function getLocations() {
  const response = await fetch(`${API_BASE}/locations/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get locations');
  }

  return await response.json();
}

export async function createLocation(name, description) {
  const response = await fetch(`${API_BASE}/locations/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ name, description }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create location');
  }

  return await response.json();
}

export async function updateLocation(id, name, description) {
  const response = await fetch(`${API_BASE}/locations/${id}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ name, description }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update location');
  }

  return await response.json();
}

export async function deleteLocation(id) {
  const response = await fetch(`${API_BASE}/locations/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete location');
  }
}

// Rack API functions
export async function getRacks(locationId = null, row = null) {
  const params = new URLSearchParams();
  if (locationId) params.append('location_id', locationId);
  if (row !== null) params.append('row', row);
  const url = params.toString() 
    ? `${API_BASE}/racks/?${params.toString()}`
    : `${API_BASE}/racks/`;
  const response = await fetch(url, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get racks');
  }

  return await response.json();
}

export async function getRack(id) {
  const response = await fetch(`${API_BASE}/racks/${id}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get rack');
  }

  return await response.json();
}

export async function createRack(locationId, name, units = 42, description = null, row = null, rowPosition = null, unitsStartFromBottom = true) {
  const body = { location_id: locationId, name, units, units_start_from_bottom: unitsStartFromBottom };
  if (description !== null) body.description = description;
  if (row !== null && row !== '') body.row = Number(row);
  if (rowPosition !== null && rowPosition !== '') body.row_position = Number(rowPosition);
  
  const response = await fetch(`${API_BASE}/racks/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create rack');
  }

  return await response.json();
}

export async function updateRack(id, name = null, units = null, description = null, row = null, rowPosition = null, unitsStartFromBottom = null) {
  const body = {};
  if (name !== null) body.name = name;
  if (units !== null) body.units = units;
  if (description !== null) body.description = description;
  if (row !== null && row !== '') body.row = Number(row);
  if (rowPosition !== null && rowPosition !== '') body.row_position = Number(rowPosition);
  if (unitsStartFromBottom !== null) body.units_start_from_bottom = unitsStartFromBottom;

  const response = await fetch(`${API_BASE}/racks/${id}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update rack');
  }

  return await response.json();
}

export async function deleteRack(id) {
  const response = await fetch(`${API_BASE}/racks/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete rack');
  }
}

export async function getRackServers(rackId) {
  const response = await fetch(`${API_BASE}/racks/${rackId}/servers`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get rack servers');
  }

  return await response.json();
}

// Server API functions
export async function getServers() {
  const response = await fetch(`${API_BASE}/servers/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get servers');
  }

  return await response.json();
}

export async function getServer(id) {
  const response = await fetch(`${API_BASE}/servers/${id}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get server');
  }

  return await response.json();
}

export async function getServerCapabilities(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/capabilities`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load server capabilities');
  }
  return await response.json();
}

export async function updateServerCapabilities(serverId, capabilityStates) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/capabilities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ capability_states: capabilityStates }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update server capabilities');
  }
  return await response.json();
}

// OS Templates API
export async function getOSTemplates() {
  const response = await fetch(`${API_BASE}/os-templates/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get OS templates');
  }

  return await response.json();
}

export async function getOSTemplate(templateId) {
  const response = await fetch(`${API_BASE}/os-templates/${templateId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get OS template');
  }

  return await response.json();
}

export async function reloadOSTemplates() {
  const response = await fetch(`${API_BASE}/os-templates/reload`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to reload OS templates');
  }

  return await response.json();
}

// Boot Tasks API
export async function getBootTask(serverId) {
  const response = await fetch(`${API_BASE}/servers/interaction/${serverId}/boot-task`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get boot task');
  }

  return await response.json();
}

export async function createBootTask(serverId, bootTaskData) {
  const response = await fetch(`${API_BASE}/servers/interaction/${serverId}/boot-task`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(bootTaskData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create boot task');
  }

  return await response.json();
}

// DHCP Service Management
export async function getDHCPStatus() {
  const response = await fetch(`${API_BASE}/dhcp/status`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(`Failed to get DHCP status: ${response.statusText}`);
  }
  return await response.json();
}

export async function startDHCPServer() {
  const response = await fetch(`${API_BASE}/dhcp/start`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to start DHCP server');
  }
  return await response.json();
}

export async function stopDHCPServer() {
  const response = await fetch(`${API_BASE}/dhcp/stop`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to stop DHCP server');
  }
  return await response.json();
}

export async function restartDHCPServer() {
  const response = await fetch(`${API_BASE}/dhcp/restart`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to restart DHCP server');
  }
  return await response.json();
}

export async function getDHCPConfig() {
  const response = await fetch(`${API_BASE}/dhcp/config`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(`Failed to get DHCP config: ${response.statusText}`);
  }
  return await response.json();
}

export async function updateDHCPConfig(config) {
  const response = await fetch(`${API_BASE}/dhcp/config`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update DHCP config');
  }
  return await response.json();
}

export async function regenerateDHCPConfig() {
  const response = await fetch(`${API_BASE}/dhcp/regenerate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to regenerate DHCP config');
  }
  return await response.json();
}

// TFTP Service Management
export async function getTFTPStatus() {
  const response = await fetch(`${API_BASE}/tftp/status`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(`Failed to get TFTP status: ${response.statusText}`);
  }
  return await response.json();
}

export async function startFTPServer() {
  const response = await fetch(`${API_BASE}/tftp/start`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to start TFTP server');
  }
  return await response.json();
}

export async function stopFTPServer() {
  const response = await fetch(`${API_BASE}/tftp/stop`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to stop TFTP server');
  }
  return await response.json();
}

export async function restartFTPServer() {
  const response = await fetch(`${API_BASE}/tftp/restart`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to restart TFTP server');
  }
  return await response.json();
}

export async function getTFTPConfig() {
  const response = await fetch(`${API_BASE}/tftp/config`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error(`Failed to get TFTP config: ${response.statusText}`);
  }
  return await response.json();
}

export async function updateTFTPConfig(config) {
  const response = await fetch(`${API_BASE}/tftp/config`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update TFTP config');
  }
  return await response.json();
}

// Service instances (per-location DHCP/TFTP runners)
export async function listServiceInstances(locationId) {
  const url = locationId
    ? `${API_BASE}/service-instances/?location_id=${locationId}`
    : `${API_BASE}/service-instances/`;
  const response = await fetch(url, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to list service instances');
  return await response.json();
}

export async function createServiceInstance(data) {
  const response = await fetch(`${API_BASE}/service-instances/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to create service instance');
  }
  return await response.json();
}

export async function getServiceInstance(id) {
  const response = await fetch(`${API_BASE}/service-instances/${id}`, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get service instance');
  return await response.json();
}

export async function updateServiceInstance(id, data) {
  const response = await fetch(`${API_BASE}/service-instances/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to update service instance');
  }
  return await response.json();
}

export async function deleteServiceInstance(id) {
  const response = await fetch(`${API_BASE}/service-instances/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to delete service instance');
}

export async function testServiceInstance(id, apiKey) {
  const response = await fetch(`${API_BASE}/service-instances/${id}/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ api_key: apiKey }),
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Test failed');
  }
  return await response.json();
}

export async function getLocationDHCPSettings(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/settings`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to get DHCP settings');
  }
  return await response.json();
}

export async function updateLocationDHCPSettings(locationId, data) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update DHCP settings');
  }
  return await response.json();
}

// Location-scoped DHCP
export async function getLocationDHCPStatus(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/status`, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get DHCP status');
  return await response.json();
}

export async function startLocationDHCP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/start`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to start');
  }
  return await response.json();
}

export async function stopLocationDHCP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/stop`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to stop');
  }
  return await response.json();
}

export async function restartLocationDHCP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/restart`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to restart');
  }
  return await response.json();
}

export async function regenerateLocationDHCP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/regenerate`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to regenerate');
  }
  return await response.json();
}

export async function getLocationDHCPLogs(locationId, limit = 100) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/dhcp/logs?limit=${limit}`, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get logs');
  return await response.json();
}

// Location-scoped TFTP
export async function getLocationTFTPStatus(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/tftp/status`, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get TFTP status');
  return await response.json();
}

export async function startLocationTFTP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/tftp/start`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to start');
  }
  return await response.json();
}

export async function stopLocationTFTP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/tftp/stop`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to stop');
  }
  return await response.json();
}

export async function restartLocationTFTP(locationId) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/tftp/restart`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || 'Failed to restart');
  }
  return await response.json();
}

export async function getLocationTFTPLogs(locationId, limit = 100) {
  const response = await fetch(`${API_BASE}/locations/${locationId}/tftp/logs?limit=${limit}`, { credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get logs');
  return await response.json();
}

export async function listTempOS() {
  const response = await fetch(`${API_BASE}/servers/interaction/temp-os`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to list temporary OSes');
  }

  return await response.json();
}

export async function cancelBootTask(serverId) {
  const response = await fetch(`${API_BASE}/servers/interaction/${serverId}/boot-task`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to cancel boot task');
  }

  return await response.json();
}

// ISO API
export async function listScripts() {
  const response = await fetch(`${API_BASE}/servers/interaction/scripts`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to list scripts');
  }

  return await response.json();
}

export async function listISOs() {
  const response = await fetch(`${API_BASE}/servers/interaction/isos`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to list ISOs');
  }

  return await response.json();
}

export async function createServer(serverData) {
  const response = await fetch(`${API_BASE}/servers/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(serverData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create server');
  }

  return await response.json();
}

export async function updateServer(id, serverData) {
  const response = await fetch(`${API_BASE}/servers/${id}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(serverData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update server');
  }

  return await response.json();
}

export async function deleteServer(id) {
  const response = await fetch(`${API_BASE}/servers/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete server');
  }
}

export async function testServerConnection(pluginName, pluginConfig) {
  const response = await fetch(`${API_BASE}/servers/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ plugin_name: pluginName, plugin_config: pluginConfig }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to test server connection');
  }

  return await response.json();
}

export async function testServerCapabilities(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/test-capabilities`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to test server capabilities');
  }

  return await response.json();
}

export async function testPluginCapabilities(pluginName, pluginConfig) {
  const response = await fetch(`${API_BASE}/servers/test-capabilities`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      plugin_name: pluginName,
      plugin_config: pluginConfig,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to test plugin capabilities');
  }

  return await response.json();
}

// Power control API functions
export async function getServerPowerState(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/power-state`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to get power state');
  }

  return await response.json();
}

export async function powerOnServer(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/power-on`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to power on server');
  }

  return await response.json();
}

export async function powerOffServer(serverId, force = false) {
  const url = new URL(`${API_BASE}/servers/${serverId}/power-off`, window.location.origin);
  if (force) {
    url.searchParams.append('force', 'true');
  }

  const response = await fetch(url.toString(), {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to power off server');
  }

  return await response.json();
}

export async function powerResetServer(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/power-reset`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to reset server');
  }

  return await response.json();
}

export async function getServerBootOptions(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/boot/options`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load boot options');
  }
  return await response.json();
}

export async function setServerBootOption(serverId, payload) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/boot/set`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to set boot option');
  }
  return await response.json();
}

export async function runBootOrderFix(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/boot/fix-boot-order`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to queue boot order correction');
  }
  return await response.json();
}

/**
 * Call a server plugin action by method name (for config-driven capability UI).
 * Maps known actions to existing endpoints.
 */
export async function callServerPluginAction(serverId, action, params = {}) {
  const stateActions = {
    get_power_state: () => getServerPowerState(serverId),
  };
  const postActions = {
    power_on: () => powerOnServer(serverId),
    power_off: () => powerOffServer(serverId, params.force),
    power_reset: () => powerResetServer(serverId),
    set_next_boot_device: () => setServerBootOption(serverId, params),
  };
  if (stateActions[action]) {
    return stateActions[action]();
  }
  if (postActions[action]) {
    return postActions[action]();
  }
  throw new Error(`Unknown plugin action: ${action}`);
}

// Network Switch API functions
export async function getSwitches(locationId = null, rackId = null, enabledOnly = false) {
  const params = new URLSearchParams();
  if (locationId) params.append('location_id', locationId);
  if (rackId) params.append('rack_id', rackId);
  if (enabledOnly) params.append('enabled_only', 'true');
  const url = params.toString() 
    ? `${API_BASE}/network-switches/?${params.toString()}`
    : `${API_BASE}/network-switches/`;
  const response = await fetch(url, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get switches');
  }

  return await response.json();
}

export async function getSwitch(id) {
  const response = await fetch(`${API_BASE}/network-switches/${id}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to get switch');
  }

  return await response.json();
}

export async function getSwitchPorts(switchId) {
  const response = await fetch(`${API_BASE}/network-switches/${switchId}/switch-ports`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get switch ports');
  }

  return await response.json();
}

export async function updateSwitchPorts(switchId, ports) {
  const response = await fetch(`${API_BASE}/network-switches/${switchId}/switch-ports`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ ports }),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || error.message || 'Failed to update switch ports');
  }

  return await response.json();
}

export async function getSwitchBandwidth(switchId, hours = 24, portIdentifier = null, resolutionMinutes = 0) {
  const params = new URLSearchParams();
  params.append('hours', String(hours));
  if (portIdentifier) params.append('port_identifier', portIdentifier);
  if (resolutionMinutes > 0) params.append('resolution_minutes', String(resolutionMinutes));
  const response = await fetch(`${API_BASE}/network-switches/${switchId}/bandwidth?${params}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    if (response.status === 404) throw new Error('Switch not found');
    throw new Error('Failed to get switch bandwidth');
  }
  return await response.json();
}

export async function getServerBandwidth(serverId, hours = 24, resolutionMinutes = 0) {
  const params = new URLSearchParams();
  params.append('hours', String(hours));
  if (resolutionMinutes > 0) params.append('resolution_minutes', String(resolutionMinutes));
  const response = await fetch(`${API_BASE}/servers/${serverId}/bandwidth?${params}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    if (response.status === 404) throw new Error('Server not found');
    throw new Error('Failed to get server bandwidth');
  }
  return await response.json();
}

export async function regenerateSwitchPorts(switchId) {
  const response = await fetch(`${API_BASE}/network-switches/${switchId}/regenerate-ports`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to regenerate switch ports');
  }

  return await response.json();
}

export async function createSwitch(switchData) {
  const response = await fetch(`${API_BASE}/network-switches/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(switchData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create switch');
  }

  return await response.json();
}

export async function updateSwitch(id, switchData) {
  const response = await fetch(`${API_BASE}/network-switches/${id}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(switchData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update switch');
  }

  return await response.json();
}

export async function deleteSwitch(id) {
  const response = await fetch(`${API_BASE}/network-switches/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete switch');
  }
}

// Cable run (server port <-> switch port mapping) API
export async function listCableRuns({ switchId, serverId } = {}) {
  const params = new URLSearchParams();
  if (switchId) params.append('switch_id', switchId);
  if (serverId) params.append('server_id', serverId);
  const url = params.toString() ? `${API_BASE}/cable-runs/?${params}` : `${API_BASE}/cable-runs/`;
  const response = await fetch(url, { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to list cable runs');
  return await response.json();
}

export async function createCableRun({ port_a, port_b, cable_type = null, speed_mbps = null, description = null }) {
  const response = await fetch(`${API_BASE}/cable-runs/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ port_a, port_b, cable_type, speed_mbps, description }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create cable run');
  }
  return await response.json();
}

export async function deleteCableRun(cableRunId) {
  const response = await fetch(`${API_BASE}/cable-runs/${cableRunId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete cable run');
  }
}

export async function testSwitchConnection(pluginName, pluginConfig) {
  const response = await fetch(`${API_BASE}/network-switches/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      plugin_name: pluginName,
      plugin_config: pluginConfig,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to test switch connection');
  }

  return await response.json();
}

// Billing Integration API functions
export async function getBillingIntegrations() {
  const response = await fetch(`${API_BASE}/billing/integrations`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get billing integrations');
  }

  return await response.json();
}

export async function getBillingIntegrationTypes() {
  const response = await fetch(`${API_BASE}/billing/integrations/types`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get integration types');
  }

  return await response.json();
}

export async function getBillingIntegration(integrationId) {
  const response = await fetch(`${API_BASE}/billing/integrations/${integrationId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get billing integration');
  }

  return await response.json();
}

export async function createBillingIntegration(data) {
  const response = await fetch(`${API_BASE}/billing/integrations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create billing integration');
  }

  return await response.json();
}

export async function updateBillingIntegration(integrationId, data) {
  const response = await fetch(`${API_BASE}/billing/integrations/${integrationId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update billing integration');
  }

  return await response.json();
}

export async function deleteBillingIntegration(integrationId) {
  const response = await fetch(`${API_BASE}/billing/integrations/${integrationId}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete billing integration');
  }
}

export async function rotateBillingIntegrationKey(integrationId) {
  const response = await fetch(`${API_BASE}/billing/integrations/${integrationId}/rotate-key`, {
    method: 'POST',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to rotate API key');
  }

  return await response.json();
}

// Services and External Users API functions
export async function getServices(params = {}) {
  const url = new URL(`${API_BASE}/admin/services`, window.location.origin);
  Object.keys(params).forEach(key => {
    if (params[key]) {
      url.searchParams.append(key, params[key]);
    }
  });

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get services');
  }

  return await response.json();
}

export async function getService(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get service');
  }

  return await response.json();
}

export async function getExternalUsers(integrationId = null) {
  const url = new URL(`${API_BASE}/admin/services/external-users`, window.location.origin);
  if (integrationId) {
    url.searchParams.append('integration_id', integrationId);
  }

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get external users');
  }

  return await response.json();
}

export async function getExternalUser(userId) {
  const response = await fetch(`${API_BASE}/admin/services/external-users/${userId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get external user');
  }

  return await response.json();
}

// Scripts API functions
export async function getScripts() {
  const url = `${API_BASE}/admin/scripts`;
  console.log('Fetching scripts from:', url);
  
  // Create an AbortController for timeout
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 10000); // 10 second timeout
  
  try {
    const response = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      signal: controller.signal
    });

    clearTimeout(timeoutId);
    console.log('Scripts response status:', response.status, response.statusText);

    if (!response.ok) {
      const errorText = await response.text();
      console.error('Scripts API error response:', errorText);
      let errorMessage = 'Failed to get scripts';
      try {
        const errorJson = JSON.parse(errorText);
        errorMessage = errorJson.detail || errorMessage;
      } catch {
        errorMessage = errorText || errorMessage;
      }
      throw new Error(errorMessage);
    }

    const scripts = await response.json();
    console.log('Scripts response data:', scripts);
    // Calculate size_bytes for each script
    return scripts.map(script => ({
      ...script,
      size_bytes: script.content ? script.content.length : 0
    }));
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === 'AbortError') {
      console.error('Scripts fetch timeout');
      throw new Error('Request timed out. Please check your connection.');
    }
    console.error('Scripts fetch error:', err);
    throw err;
  }
}

export async function getScript(scriptId) {
  const response = await fetch(`${API_BASE}/admin/scripts/${scriptId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get script');
  }

  const script = await response.json();
  return {
    ...script,
    size_bytes: script.content ? script.content.length : 0
  };
}

export async function createScript(data) {
  const response = await fetch(`${API_BASE}/admin/scripts`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create script');
  }

  return await response.json();
}

export async function updateScript(scriptId, data) {
  const response = await fetch(`${API_BASE}/admin/scripts/${scriptId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update script');
  }

  return await response.json();
}

// Asset manager API
export async function getAssets(label = null) {
  const url = label ? `${API_BASE}/assets?label=${encodeURIComponent(label)}` : `${API_BASE}/assets`;
  const response = await fetch(url, { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get assets');
  return await response.json();
}

export async function getAssetLabels() {
  const response = await fetch(`${API_BASE}/assets/labels`, { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get asset labels');
  return await response.json();
}

export async function getAsset(assetId) {
  const response = await fetch(`${API_BASE}/assets/${assetId}`, { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to get asset');
  return await response.json();
}

/** URL to display or download an asset image (use in img src or link). */
export function getAssetFileUrl(assetId) {
  return `${API_BASE}/assets/${assetId}/file`;
}

export async function uploadAsset(file, label, description = null) {
  const form = new FormData();
  form.append('file', file);
  form.append('label', label);
  if (description != null && description !== '') form.append('description', description);
  const response = await fetch(`${API_BASE}/assets`, {
    method: 'POST',
    credentials: 'include',
    body: form,
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to upload asset');
  }
  return await response.json();
}

export async function deleteAsset(assetId) {
  const response = await fetch(`${API_BASE}/assets/${assetId}`, { method: 'DELETE', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to delete asset');
}

// Server Group API functions
export async function getServerGroups() {
  const response = await fetch(`${API_BASE}/server-groups/`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get server groups');
  }

  return await response.json();
}

export async function getServerGroup(groupId) {
  const response = await fetch(`${API_BASE}/server-groups/${groupId}`, {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    throw new Error('Failed to get server group');
  }

  return await response.json();
}

export async function createServerGroup(name, description) {
  const response = await fetch(`${API_BASE}/server-groups/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ name, description }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create server group');
  }

  return await response.json();
}

export async function updateServerGroup(groupId, data) {
  const response = await fetch(`${API_BASE}/server-groups/${groupId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to update server group');
  }

  return await response.json();
}

export async function deleteServerGroup(groupId) {
  const response = await fetch(`${API_BASE}/server-groups/${groupId}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete server group');
  }
}

export async function addServersToGroup(groupId, serverIds) {
  const response = await fetch(`${API_BASE}/server-groups/${groupId}/servers`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ server_ids: serverIds }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to add servers to group');
  }

  return await response.json();
}

export async function removeServerFromGroup(groupId, serverId) {
  const response = await fetch(`${API_BASE}/server-groups/${groupId}/servers/${serverId}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to remove server from group');
  }

  return await response.json();
}

export async function deleteScript(scriptId) {
  const response = await fetch(`${API_BASE}/admin/scripts/${scriptId}`, {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to delete script');
  }
}

