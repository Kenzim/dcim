const API_BASE = '/api';

// Central handling of expired/invalid sessions. A global fetch interceptor
// watches for 401 responses on authenticated API calls and notifies the app so
// it can clear auth state and route the user to the login screen. Passive auth
// probes and the login/logout endpoints are excluded so they don't cause loops.
let _onUnauthorized = null;

export function setUnauthorizedHandler(fn) {
  _onUnauthorized = fn;
}

const _UNAUTH_EXCLUDED = ['/client/login', '/client/logout', '/client/me'];

// Admin "sign in as" impersonation: when an admin opens a client profile's
// "Sign in as" link, the new tab lands on /client?impersonate=<token>. That
// token is stashed in sessionStorage (per-tab, so it never touches the
// admin's own cookie session in the original tab) and sent as a Bearer
// header on client-portal calls so the backend resolves the impersonated
// user instead of falling back to any shared cookie.
const IMPERSONATION_TOKEN_KEY = 'rf_impersonate_token';

export function setImpersonationToken(token) {
  try {
    if (token) {
      window.sessionStorage.setItem(IMPERSONATION_TOKEN_KEY, token);
    } else {
      window.sessionStorage.removeItem(IMPERSONATION_TOKEN_KEY);
    }
  } catch (_) {
    // sessionStorage unavailable (e.g. privacy mode) — impersonation just won't work.
  }
}

export function clearImpersonationToken() {
  setImpersonationToken(null);
}

function _impersonationHeaders() {
  try {
    const token = window.sessionStorage.getItem(IMPERSONATION_TOKEN_KEY);
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch (_) {
    return {};
  }
}

export function installFetchAuthInterceptor() {
  if (typeof window === 'undefined' || window.__rfFetchPatched) return;
  const originalFetch = window.fetch.bind(window);
  window.__rfFetchPatched = true;
  window.fetch = async (input, init) => {
    const response = await originalFetch(input, init);
    try {
      const url = typeof input === 'string' ? input : input?.url || '';
      const isApi = url.includes('/api/');
      const excluded = _UNAUTH_EXCLUDED.some((p) => url.includes(p));
      if (response.status === 401 && isApi && !excluded && typeof _onUnauthorized === 'function') {
        _onUnauthorized();
      }
    } catch (_) {
      // Never let interceptor bookkeeping break the actual request.
    }
    return response;
  };
}

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
    const response = await fetch(`${API_BASE}/client/login`, {
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
  const response = await fetch(`${API_BASE}/client/logout`, {
    method: 'POST',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  clearImpersonationToken();

  if (!response.ok) {
    throw new Error('Logout failed');
  }

  return await response.json();
}

export async function getCurrentUser() {
  const response = await fetch(`${API_BASE}/client/me`, {
    method: 'GET',
    credentials: 'include', // Important for cookies
    headers: { ..._impersonationHeaders() },
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

// Mint a one-time IPMI proxy launch ticket (admin) and return
// { launch_url, proxy_url, viewer_username, viewer_password, expires_in }.
export async function openServerIpmiConsole(serverId) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/ipmi-ticket`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to open IPMI console');
  }
  return await response.json();
}

export async function listIpmiKvmProfiles() {
  const response = await fetch(`${API_BASE}/ipmi-kvm/profiles`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load IPMI KVM profiles');
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

// Standalone proxy runners (generated API key; phone-home health)
export async function listProxyRunners() {
  const response = await fetch(`${API_BASE}/admin/proxy-runners`, { credentials: 'include' });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to list proxy runners');
  }
  return await response.json();
}

export async function createProxyRunner(data) {
  const response = await fetch(`${API_BASE}/admin/proxy-runners`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create proxy runner');
  }
  return await response.json();
}

export async function updateProxyRunner(id, data) {
  const response = await fetch(`${API_BASE}/admin/proxy-runners/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update proxy runner');
  }
  return await response.json();
}

export async function rotateProxyRunnerKey(id) {
  const response = await fetch(`${API_BASE}/admin/proxy-runners/${id}/rotate-key`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to rotate proxy runner key');
  }
  return await response.json();
}

export async function deleteProxyRunner(id) {
  const response = await fetch(`${API_BASE}/admin/proxy-runners/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete proxy runner');
  }
}

// Proxy IPAM subnet groups (named pools for catalog auto-assign)
export async function listProxySubnetGroups() {
  const response = await fetch(`${API_BASE}/admin/proxy-subnet-groups`, { credentials: 'include' });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to list proxy subnet groups');
  }
  return await response.json();
}

export async function createProxySubnetGroup(data) {
  const response = await fetch(`${API_BASE}/admin/proxy-subnet-groups`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create proxy subnet group');
  }
  return await response.json();
}

export async function updateProxySubnetGroup(id, data) {
  const response = await fetch(`${API_BASE}/admin/proxy-subnet-groups/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update proxy subnet group');
  }
  return await response.json();
}

export async function deleteProxySubnetGroup(id) {
  const response = await fetch(`${API_BASE}/admin/proxy-subnet-groups/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete proxy subnet group');
  }
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
    const text = await response.text();
    let message = 'Failed to delete server';
    try {
      const error = JSON.parse(text);
      message = error.detail || message;
    } catch {
      if (text) message = text.slice(0, 200);
    }
    throw new Error(message);
  }
}

export async function testServerConnection(pluginName, pluginConfig, serverId = null) {
  const response = await fetch(`${API_BASE}/servers/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ plugin_name: pluginName, plugin_config: pluginConfig, server_id: serverId }),
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

export async function previewServerKernelArgs(serverId, payload = {}) {
  const response = await fetch(`${API_BASE}/servers/${serverId}/boot/kernel-args-preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to preview kernel args');
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

export async function getAggregateBandwidth(hours = 24) {
  const params = new URLSearchParams();
  params.append('hours', String(hours));
  const response = await fetch(`${API_BASE}/network-switches/bandwidth/aggregate?${params}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error('Failed to get aggregate bandwidth');
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

async function mcpKeysRequest(path, { method = 'GET', body, errorMessage } = {}) {
  const options = { method, credentials: 'include' };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${API_BASE}/admin/mcp-keys${path}`, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    let message = errorMessage;
    if (typeof detail === 'string' && detail) {
      message = detail;
    } else if (Array.isArray(detail) && detail.length) {
      const msgs = detail.map((item) => item?.msg || item?.message).filter(Boolean);
      if (msgs.length) message = msgs.join('; ');
    } else if (detail?.message) {
      message = detail.message;
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return await response.json();
}

export async function getMcpKeys() {
  const data = await mcpKeysRequest('', { errorMessage: 'Failed to list MCP keys' });
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.items)) return data.items;
  return [];
}

export async function getMcpKey(keyId) {
  return mcpKeysRequest(`/${keyId}`, { errorMessage: 'Failed to get MCP key' });
}

export async function createMcpKey(data) {
  return mcpKeysRequest('', { method: 'POST', body: data, errorMessage: 'Failed to create MCP key' });
}

export async function updateMcpKey(keyId, data) {
  return mcpKeysRequest(`/${keyId}`, { method: 'PATCH', body: data, errorMessage: 'Failed to update MCP key' });
}

export async function rotateMcpKey(keyId) {
  return mcpKeysRequest(`/${keyId}/rotate`, { method: 'POST', errorMessage: 'Failed to rotate MCP key' });
}

export async function deleteMcpKey(keyId) {
  return mcpKeysRequest(`/${keyId}`, { method: 'DELETE', errorMessage: 'Failed to delete MCP key' });
}

// Services and External Users API functions
export async function getServices(params = {}) {
  const url = new URL(`${API_BASE}/admin/services`, window.location.origin);
  Object.keys(params).forEach(key => {
    const v = params[key];
    if (v === undefined || v === null || v === '') return;
    if (key === 'status_filter' && v === 'all') return;
    if (key === 'provisioning_source' && v === 'all') return;
    if (key === 'service_type' && v === 'all') return;
    url.searchParams.append(key, v);
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

export async function getVmServices(params = {}) {
  const url = new URL(`${API_BASE}/admin/services/vm`, window.location.origin);
  Object.keys(params).forEach((key) => {
    const v = params[key];
    if (v === undefined || v === null || v === '') return;
    if (key === 'status_filter' && v === 'all') return;
    url.searchParams.append(key, v);
  });
  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to get VM services');
  return await response.json();
}

export async function getBareMetalServices(params = {}) {
  const url = new URL(`${API_BASE}/admin/services/bare-metal`, window.location.origin);
  Object.keys(params).forEach((key) => {
    const v = params[key];
    if (v === undefined || v === null || v === '') return;
    if (key === 'status_filter' && v === 'all') return;
    url.searchParams.append(key, v);
  });
  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to get bare metal services');
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

export async function getVmService(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/vm/${serviceId}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to get VM service');
  return await response.json();
}

export async function getBareMetalService(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/bare-metal/${serviceId}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to get bare metal service');
  return await response.json();
}

export async function deleteServiceCompletely(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete service');
  }
}

export async function assignServiceOwner(serviceId, ownerUserId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/owner`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ owner_user_id: ownerUserId ?? null }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to assign service owner');
  }
  return await response.json();
}

export async function updateAdminServiceStatus(serviceId, nextStatus) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/status`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ status: nextStatus }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update service status');
  }
  return await response.json();
}

export async function listExternalUserLinks(userId = null) {
  const url = new URL(`${API_BASE}/admin/services/external-user-links`, window.location.origin);
  if (userId != null) url.searchParams.append('user_id', userId);
  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) throw new Error('Failed to list external identity links');
  return await response.json();
}

export async function createExternalUserLink(payload) {
  const response = await fetch(`${API_BASE}/admin/services/external-user-links`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create external identity link');
  }
  return await response.json();
}

export async function deleteExternalUserLink(linkId) {
  const response = await fetch(`${API_BASE}/admin/services/external-user-links/${linkId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete external identity link');
  }
}

/**
 * Admin: clone template + cloud-init / sizing on Proxmox for a VM service.
 * POST /admin/services/{serviceId}/provision-vm
 */
export async function provisionVmService(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/provision-vm`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    const detail = err.detail;
    throw new Error(
      typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'VM provisioning failed'
    );
  }
  return await response.json();
}

export async function vmPowerAction(serviceId, action) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/vm/power`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ action }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || `Failed VM power action: ${action}`);
  }
  return await response.json();
}

// Mint a VNC console session (admin) and return
// { ws_token, ws_path, vnc_password, expires_in }.
export async function listDeploymentJobs(serviceId, limit = 50) {
  const response = await fetch(
    `${API_BASE}/admin/services/${serviceId}/deployment-jobs?limit=${limit}`,
    { method: 'GET', credentials: 'include' }
  );
  if (!response.ok) throw new Error('Failed to list deployment jobs');
  return await response.json();
}

export async function getDeploymentJob(serviceId, jobId) {
  const response = await fetch(
    `${API_BASE}/admin/services/${serviceId}/deployment-jobs/${jobId}`,
    { method: 'GET', credentials: 'include' }
  );
  if (!response.ok) throw new Error('Failed to get deployment job');
  return await response.json();
}

// Which console types (noVNC/serial) a VM actually supports, e.g.
// { vnc: true, serial: false }. Fetched before showing "Open Console"
// controls so a picker only appears when the VM genuinely supports both.
export async function getAdminVmConsoleTypes(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/vm/console-types`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to check console availability');
  }
  return await response.json();
}

export async function getClientVmConsoleTypes(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/vm/console-types`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to check console availability');
  }
  return await response.json();
}

export async function createAdminVmVncSession(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/vm/vnc-session`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to open VNC console');
  }
  return await response.json();
}

export async function destroyVmGuest(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/vm/destroy`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to destroy VM');
  }
  return await response.json();
}

export async function recreateVmGuest(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/vm/recreate`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to recreate VM');
  }
  return await response.json();
}

/**
 * Admin: create a pending VM service (optional Proxmox placement, optional billing user).
 * POST /admin/services/vm
 */
export async function createAdminVmService(payload) {
  const response = await fetch(`${API_BASE}/admin/services/vm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    const detail = err.detail;
    throw new Error(
      typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'Failed to create VM service'
    );
  }
  return await response.json();
}

/** @deprecated Use createAdminVmService with placement fields set */
export async function createInternalTestVmService(payload) {
  return createAdminVmService(payload);
}

/**
 * Admin: create an http_proxy service with no linked server; IP(s) are
 * auto-assigned from IPAM immediately.
 * POST /admin/services/http-proxy
 */
export async function createAdminHttpProxyService(payload) {
  const response = await fetch(`${API_BASE}/admin/services/http-proxy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    const detail = err.detail;
    throw new Error(
      typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : 'Failed to create proxy service'
    );
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

export async function listMyServices(serviceType = null) {
  const url = new URL(`${API_BASE}/client/services/me`, window.location.origin);
  if (serviceType) url.searchParams.append('service_type', serviceType);
  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to list my services');
  }
  return await response.json();
}

// Owner-scoped service detail (client portal): base list fields plus live
// power_state, primary_ip, availability flags, and the effective
// `permissions` map so the UI can hide actions instead of probing 403s.
export async function getClientService(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}`, {
    method: 'GET',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to load service');
  }
  return await response.json();
}

// Power on/off/reboot/reset for a service the caller owns (client portal).
// `action` is one of 'on' | 'off' | 'reboot' | 'reset'.
export async function clientServicePower(serviceId, action) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/power`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ..._impersonationHeaders() },
    credentials: 'include',
    body: JSON.stringify({ action }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to power ${action}`);
  }
  return await response.json();
}

// Mint a one-time IPMI proxy launch ticket (client portal, for a bare-metal
// service the caller owns). Returns { launch_url, viewer_username, ... }.
export async function createClientIpmiTicket(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/ipmi-ticket`, {
    method: 'POST',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to open IPMI console');
  }
  return await response.json();
}

// List assigned proxy IP(s) + credentials + ready-to-use URLs (client
// portal, for an http_proxy service the caller owns).
export async function getClientProxyCredentials(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/proxy/credentials`, {
    method: 'GET',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to load proxy credentials');
  }
  return await response.json();
}

// Rotate credentials (new username+password, same IP(s)) for an http_proxy
// service the caller owns.
export async function rotateClientProxyCredentials(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/proxy/rotate`, {
    method: 'POST',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to rotate proxy credentials');
  }
  return await response.json();
}

// Mint a VNC console session (client portal, for a VM service the caller
// owns) and return { ws_token, ws_path, vnc_password, expires_in }.
export async function createClientVmVncSession(serviceId) {
  const response = await fetch(`${API_BASE}/client/services/${serviceId}/vm/vnc-session`, {
    method: 'POST',
    credentials: 'include',
    headers: { ..._impersonationHeaders() },
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to open VNC console');
  }
  return await response.json();
}

// Redeem a one-time VM VNC launch ticket (e.g. from a WHMCS popup landing on
// /vnc?t=...). Unauthenticated: the ticket itself is the credential. Returns
// the same session shape as the admin/client mint endpoints.
export async function redeemVmVncLaunchTicket(token) {
  const response = await fetch(`${API_BASE}/vnc/redeem`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Console link is invalid or has expired');
  }
  return await response.json();
}

export async function redeemIpmiKvmLaunchTicket(token) {
  const response = await fetch(`${API_BASE}/kvm/redeem`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Console link is invalid or has expired');
  }
  return await response.json();
}

// Mint a fresh Proxmox console proxy for an existing WS session (same
// ws_token, new vnc_password/port). Required for Reconnect: Proxmox's
// vncproxy/termproxy tickets die when the upstream WebSocket closes.
export async function refreshVmVncSession(wsToken) {
  const response = await fetch(`${API_BASE}/vnc/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: wsToken }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to reconnect console');
  }
  return await response.json();
}

// Power on/off/reboot the VM bound to a console WS session (unauthenticated
// /vnc popup uses the session token as the credential).
export async function consoleVmPowerAction(wsToken, action) {
  const response = await fetch(`${API_BASE}/vnc/power`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: wsToken, action }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || `Power action '${action}' failed`);
  }
  return await response.json();
}

// Admin: clients (non-admin users) API functions. Every client always has
// full portal access (impersonation / billing SSO) — there is no separate
// "enable portal" step; `has_password` just reflects whether direct
// username/password login is also enabled.
export async function listClients(params = {}) {
  const url = new URL(`${API_BASE}/admin/clients`, window.location.origin);
  Object.keys(params).forEach((key) => {
    const v = params[key];
    if (v === undefined || v === null || v === '' || v === 'all') return;
    url.searchParams.append(key, v);
  });
  const response = await fetch(url.toString(), { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to list clients');
  return await response.json();
}

export async function getClientProfile(userId) {
  const response = await fetch(`${API_BASE}/admin/clients/${userId}`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to load client');
  }
  return await response.json();
}

export async function createClient(payload) {
  const response = await fetch(`${API_BASE}/admin/clients`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create client');
  }
  return await response.json();
}

export async function setClientPassword(userId, password) {
  const response = await fetch(`${API_BASE}/admin/clients/${userId}/password`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to set password');
  }
  return await response.json();
}

export async function impersonateClient(userId) {
  const response = await fetch(`${API_BASE}/admin/clients/${userId}/impersonate`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to sign in as client');
  }
  return await response.json();
}

// Admin: admins (staff) API functions
export async function listAdmins() {
  const response = await fetch(`${API_BASE}/admin/admins`, { method: 'GET', credentials: 'include' });
  if (!response.ok) throw new Error('Failed to list admins');
  return await response.json();
}

export async function createAdminAccount(payload) {
  const response = await fetch(`${API_BASE}/admin/admins`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create admin');
  }
  return await response.json();
}

export async function updateAdminAccount(adminId, payload) {
  const response = await fetch(`${API_BASE}/admin/admins/${adminId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update admin');
  }
  return await response.json();
}

export async function deleteAdminAccount(adminId) {
  const response = await fetch(`${API_BASE}/admin/admins/${adminId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete admin');
  }
  return true;
}

// Scripts API functions
export async function getScripts() {
  const url = `${API_BASE}/admin/scripts`;

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

// Product catalog API
export async function listProductFamilies() {
  const response = await fetch(`${API_BASE}/product-catalog/families`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list product families');
  }
  return await response.json();
}

export async function createProductFamily(data) {
  const response = await fetch(`${API_BASE}/product-catalog/families`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create product family');
  }
  return await response.json();
}

export async function updateProductFamily(familyId, data) {
  const response = await fetch(`${API_BASE}/product-catalog/families/${familyId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update product family');
  }
  return await response.json();
}

export async function createCatalogProduct(data) {
  const response = await fetch(`${API_BASE}/product-catalog/products`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create product');
  }
  return await response.json();
}

export async function listCatalogProducts() {
  const response = await fetch(`${API_BASE}/product-catalog/products`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list products');
  }
  return await response.json();
}

export async function updateCatalogProduct(productId, data) {
  const response = await fetch(`${API_BASE}/product-catalog/products/${productId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update product');
  }
  return await response.json();
}

export async function deleteCatalogProduct(productId) {
  const response = await fetch(`${API_BASE}/product-catalog/products/${productId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete product');
  }
}

export async function listVmTemplates() {
  const response = await fetch(`${API_BASE}/product-catalog/vm-templates`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list VM templates');
  }
  return await response.json();
}

export async function listVmTemplateOsTypes(opts = {}) {
  const qs = opts.detailed ? '?detailed=true' : '';
  const response = await fetch(`${API_BASE}/product-catalog/vm-templates/os-types${qs}`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list VM template OS types');
  }
  return await response.json();
}

export async function listServiceStrategyActions(serviceId, { client = false } = {}) {
  const base = client ? `${API_BASE}/client/services` : `${API_BASE}/admin/services`;
  const response = await fetch(`${base}/${serviceId}/actions`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list strategy actions');
  }
  return await response.json();
}

export async function runServiceStrategyAction(serviceId, actionName, params = {}, { client = false } = {}) {
  const base = client ? `${API_BASE}/client/services` : `${API_BASE}/admin/services`;
  const response = await fetch(`${base}/${serviceId}/actions/${encodeURIComponent(actionName)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ params }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Failed to run action ${actionName}`);
  }
  return await response.json();
}

export async function listServiceAvailableIps(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/available-ips`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list available IPs');
  }
  return await response.json();
}

export async function reassignServiceIp(serviceId, allocationId, { resetNetwork = true } = {}) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/reassign-ip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      allocation_id: allocationId,
      reset_network: resetNetwork,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to reassign IP');
  }
  return await response.json();
}

export async function createVmTemplate(data) {
  const response = await fetch(`${API_BASE}/product-catalog/vm-templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create VM template');
  }
  return await response.json();
}

export async function updateVmTemplate(templateId, data) {
  const response = await fetch(`${API_BASE}/product-catalog/vm-templates/${templateId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update VM template');
  }
  return await response.json();
}

export async function deleteVmTemplate(templateId) {
  const response = await fetch(`${API_BASE}/product-catalog/vm-templates/${templateId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete VM template');
  }
}

export async function listVmIpAllocations(filters = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== null && value !== undefined && value !== '') {
      params.set(key, value);
    }
  }
  const qs = params.toString();
  const url = qs ? `${API_BASE}/vm-ip-allocations?${qs}` : `${API_BASE}/vm-ip-allocations`;
  const response = await fetch(url, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list VM IP allocations');
  }
  return await response.json();
}

export async function listVmIpAllocationTags() {
  const response = await fetch(`${API_BASE}/vm-ip-allocations/tags`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list VM IP allocation tags');
  }
  return await response.json();
}

export async function createVmIpAllocation(data) {
  const response = await fetch(`${API_BASE}/vm-ip-allocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create VM IP allocation');
  }
  return await response.json();
}

export async function createVmIpAllocationsBulk(data) {
  const response = await fetch(`${API_BASE}/vm-ip-allocations/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to bulk create VM IP allocations');
  }
  return await response.json();
}

export async function updateVmIpAllocation(id, data) {
  const response = await fetch(`${API_BASE}/vm-ip-allocations/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update VM IP allocation');
  }
  return await response.json();
}

export async function bulkUpdateVmIpAllocations(data) {
  const response = await fetch(`${API_BASE}/vm-ip-allocations/bulk`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to bulk update VM IP allocations');
  }
  return await response.json();
}

export async function deleteVmIpAllocation(id) {
  const response = await fetch(`${API_BASE}/vm-ip-allocations/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete VM IP allocation');
  }
}

export async function updateFamilyVmConfig(familyId, data) {
  const response = await fetch(`${API_BASE}/product-catalog/families/${familyId}/vm-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update family VM config');
  }
  return await response.json();
}

export async function updateProductVmConfig(productId, data) {
  const response = await fetch(`${API_BASE}/product-catalog/products/${productId}/vm-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update product VM config');
  }
  return await response.json();
}

export async function listCatalogOSProfiles() {
  const response = await fetch(`${API_BASE}/product-catalog/os-profiles`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list OS profiles');
  }
  return await response.json();
}

export async function createCatalogOSProfile(data) {
  const response = await fetch(`${API_BASE}/product-catalog/os-profiles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create OS profile');
  }
  return await response.json();
}

export async function attachCatalogOSProfile(familyId, osProfileId) {
  const response = await fetch(`${API_BASE}/product-catalog/families/${familyId}/os-profiles/${osProfileId}`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to attach OS profile');
  }
  return await response.json();
}

// Proxmox inventory API
export async function listProxmoxClusters() {
  const response = await fetch(`${API_BASE}/proxmox/clusters`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list Proxmox clusters');
  }
  return await response.json();
}

/** Distinct backup-capable storage names for product catalog dropdowns. */
export async function listProxmoxBackupStorages() {
  const response = await fetch(`${API_BASE}/proxmox/backup-storages`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list Proxmox backup storages');
  }
  return await response.json();
}

export async function createProxmoxCluster(data) {
  const response = await fetch(`${API_BASE}/proxmox/clusters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create Proxmox cluster');
  }
  return await response.json();
}

export async function updateProxmoxCluster(clusterId, data) {
  const response = await fetch(`${API_BASE}/proxmox/clusters/${clusterId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update Proxmox cluster');
  }
  return await response.json();
}

export async function syncProxmoxCluster(clusterId) {
  const response = await fetch(`${API_BASE}/proxmox/clusters/${clusterId}/sync`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to sync Proxmox cluster');
  }
  return await response.json();
}

export async function getProxmoxClusterInventory(clusterId) {
  const response = await fetch(`${API_BASE}/proxmox/clusters/${clusterId}/inventory`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load Proxmox inventory');
  }
  return await response.json();
}

function _vmBackupBase(serviceId, { client = false } = {}) {
  return client
    ? `${API_BASE}/client/services/${serviceId}/vm`
    : `${API_BASE}/admin/services/${serviceId}/vm`;
}

export async function listVmBackups(serviceId, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/backups`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list backups');
  }
  return await response.json();
}

export async function createVmBackup(serviceId, data = {}, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/backups`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create backup');
  }
  return await response.json();
}

export async function deleteVmBackup(serviceId, data, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/backups/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete backup');
  }
  return await response.json();
}

export async function restoreVmBackup(serviceId, data, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/backups/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to restore backup');
  }
  return await response.json();
}

export async function reinstallVmGuest(serviceId, data = {}, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/reinstall`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data || {}),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to reinstall VM');
  }
  return await response.json();
}

export async function getVmSshKeys(serviceId, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/ssh-keys`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load SSH keys');
  }
  return await response.json();
}

export async function putVmSshKeys(serviceId, sshPublicKeys, { client = false } = {}) {
  const response = await fetch(`${_vmBackupBase(serviceId, { client })}/ssh-keys`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ ssh_public_keys: sshPublicKeys }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to save SSH keys');
  }
  return await response.json();
}

export async function getProxmoxClusterOverview(clusterId) {
  const response = await fetch(`${API_BASE}/proxmox/clusters/${clusterId}/overview`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load Proxmox cluster overview');
  }
  return await response.json();
}

export async function upsertProxmoxNode(clusterId, data) {
  const response = await fetch(`${API_BASE}/proxmox/clusters/${clusterId}/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to upsert Proxmox node');
  }
  return await response.json();
}

export async function upsertProxmoxStorage(nodeId, data) {
  const response = await fetch(`${API_BASE}/proxmox/nodes/${nodeId}/storages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to upsert storage');
  }
  return await response.json();
}

export async function upsertProxmoxTemplate(nodeId, data) {
  const response = await fetch(`${API_BASE}/proxmox/nodes/${nodeId}/templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to upsert template');
  }
  return await response.json();
}

export async function addProxmoxCapacitySnapshot(nodeId, data) {
  const response = await fetch(`${API_BASE}/proxmox/nodes/${nodeId}/capacity`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to add capacity snapshot');
  }
  return await response.json();
}

// IPAM API
export async function listIpamSubnets() {
  const response = await fetch(`${API_BASE}/ipam/subnets`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list subnets');
  }
  return await response.json();
}

export async function createIpamSubnet(data) {
  const response = await fetch(`${API_BASE}/ipam/subnets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create subnet');
  }
  return await response.json();
}

export async function updateIpamSubnet(subnetId, data) {
  const response = await fetch(`${API_BASE}/ipam/subnets/${subnetId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update subnet');
  }
  return await response.json();
}

export async function deleteIpamSubnet(subnetId) {
  const response = await fetch(`${API_BASE}/ipam/subnets/${subnetId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete subnet');
  }
}

export async function listIpamAssignments() {
  const response = await fetch(`${API_BASE}/ipam/assignments`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list assignments');
  }
  return await response.json();
}

export async function assignIpamAddress(data) {
  const response = await fetch(`${API_BASE}/ipam/assignments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to assign IP');
  }
  return await response.json();
}

export async function listIpamHistory(serviceId = null) {
  const url = serviceId ? `${API_BASE}/ipam/history?service_id=${encodeURIComponent(serviceId)}` : `${API_BASE}/ipam/history`;
  const response = await fetch(url, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list assignment history');
  }
  return await response.json();
}

export async function listServiceIpAssignments(serviceId) {
  const response = await fetch(`${API_BASE}/ipam/services/${serviceId}/assignments`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to list service assignments');
  }
  return await response.json();
}

export async function releaseIpamAssignment(assignmentId) {
  const response = await fetch(`${API_BASE}/ipam/assignments/${assignmentId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to release assignment');
  }
}

export async function rotateIpamAssignment(assignmentId) {
  const response = await fetch(`${API_BASE}/ipam/assignments/${assignmentId}/rotate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to rotate credentials');
  }
  return await response.json();
}

// Client permission presets API
export async function getPermissionSetsCatalog() {
  const response = await fetch(`${API_BASE}/admin/permission-sets/catalog`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load permission catalog');
  }
  return await response.json();
}

export async function listPermissionSets() {
  const response = await fetch(`${API_BASE}/admin/permission-sets`, { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load permission sets');
  }
  return await response.json();
}

export async function createPermissionSet(data) {
  const response = await fetch(`${API_BASE}/admin/permission-sets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to create permission set');
  }
  return await response.json();
}

export async function updatePermissionSet(permissionSetId, data) {
  const response = await fetch(`${API_BASE}/admin/permission-sets/${permissionSetId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update permission set');
  }
  return await response.json();
}

export async function deletePermissionSet(permissionSetId) {
  const response = await fetch(`${API_BASE}/admin/permission-sets/${permissionSetId}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to delete permission set');
  }
}

export async function updateClientPermissionSet(userId, permissionSetId) {
  const response = await fetch(`${API_BASE}/admin/clients/${userId}/permission-set`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ permission_set_id: permissionSetId }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to assign permission set');
  }
  return await response.json();
}

export async function updateServicePermissions(serviceId, data) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/permissions`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to update service permissions');
  }
  return await response.json();
}

export async function getServiceEffectivePermissions(serviceId) {
  const response = await fetch(`${API_BASE}/admin/services/${serviceId}/effective-permissions`, {
    method: 'GET',
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to load effective permissions');
  }
  return await response.json();
}

// Reseller platform administration
async function resellerAdminRequest(path, options = {}) {
  let requestHeaders = options.headers;
  if (options.body) {
    requestHeaders = { 'Content-Type': 'application/json' };
    if (options.headers) {
      Object.assign(requestHeaders, options.headers);
    }
  }
  const response = await fetch(`${API_BASE}/admin${path}`, {
    credentials: 'include',
    ...options,
    headers: requestHeaders,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || detail?.code || options.errorMessage || 'Reseller administration request failed');
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.detail = detail;
    throw requestError;
  }
  if (response.status === 204) return null;
  return await response.json();
}

function resellerAdminQuery(filters = {}) {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, value);
  });
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}

export const listResellerGroups = () =>
  resellerAdminRequest('/reseller-groups', { errorMessage: 'Failed to list reseller groups' });
export const createResellerGroup = (data) =>
  resellerAdminRequest('/reseller-groups', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create reseller group' });
export const updateResellerGroup = (groupId, data) =>
  resellerAdminRequest(`/reseller-groups/${groupId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update reseller group' });
export const deleteResellerGroup = (groupId) =>
  resellerAdminRequest(`/reseller-groups/${groupId}`, { method: 'DELETE', errorMessage: 'Failed to delete reseller group' });

export const listResellers = (filters = {}) =>
  resellerAdminRequest(`/resellers${resellerAdminQuery(filters)}`, { errorMessage: 'Failed to list resellers' });
export const getReseller = (resellerId) =>
  resellerAdminRequest(`/resellers/${resellerId}`, { errorMessage: 'Failed to load reseller' });
export const createReseller = (data) =>
  resellerAdminRequest('/resellers', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create reseller' });
export const updateReseller = (resellerId, data) =>
  resellerAdminRequest(`/resellers/${resellerId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update reseller' });
export const disableReseller = (resellerId) =>
  resellerAdminRequest(`/resellers/${resellerId}`, { method: 'DELETE', errorMessage: 'Failed to disable reseller' });
export const rotateResellerKey = (resellerId) =>
  resellerAdminRequest(`/resellers/${resellerId}/rotate-key`, { method: 'POST', errorMessage: 'Failed to rotate reseller API key' });

export const listResellerBasePrices = () =>
  resellerAdminRequest('/resellers/prices', { errorMessage: 'Failed to load base prices' });
export const upsertResellerBasePrice = (productId, data) =>
  resellerAdminRequest(`/resellers/prices/${productId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update base price' });
export const listResellerGroupPrices = (groupId) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/prices`, { errorMessage: 'Failed to load group prices' });
export const upsertResellerGroupPrice = (groupId, productId, data) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/prices/${productId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update group price' });
export const deleteResellerGroupPrice = (groupId, productId) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/prices/${productId}`, { method: 'DELETE', errorMessage: 'Failed to clear group price' });

export const listResellerProductAccess = (resellerId) =>
  resellerAdminRequest(`/resellers/${resellerId}/product-access`, { errorMessage: 'Failed to load reseller product access' });
export const setResellerProductAccess = (resellerId, productId, allowed) =>
  resellerAdminRequest(`/resellers/${resellerId}/product-access/${productId}`, { method: 'PUT', body: JSON.stringify({ allowed }), errorMessage: 'Failed to update reseller product access' });
export const deleteResellerProductAccess = (resellerId, productId) =>
  resellerAdminRequest(`/resellers/${resellerId}/product-access/${productId}`, { method: 'DELETE', errorMessage: 'Failed to clear reseller product access' });
export const listResellerGroupAccess = (groupId) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/product-access`, { errorMessage: 'Failed to load group product access' });
export const setResellerGroupAccess = (groupId, productId, allowed) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/product-access/${productId}`, { method: 'PUT', body: JSON.stringify({ allowed }), errorMessage: 'Failed to update group product access' });
export const deleteResellerGroupAccess = (groupId, productId) =>
  resellerAdminRequest(`/reseller-groups/${groupId}/product-access/${productId}`, { method: 'DELETE', errorMessage: 'Failed to clear group product access' });

export const listResellerQuotas = (filters = {}) =>
  resellerAdminRequest(`/resellers/quotas${resellerAdminQuery(filters)}`, { errorMessage: 'Failed to load reseller quotas' });
export const createResellerQuota = (data) =>
  resellerAdminRequest('/resellers/quotas', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create reseller quota' });
export const updateResellerQuota = (quotaId, data) =>
  resellerAdminRequest(`/resellers/quotas/${quotaId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update reseller quota' });
export const deleteResellerQuota = (quotaId) =>
  resellerAdminRequest(`/resellers/quotas/${quotaId}`, { method: 'DELETE', errorMessage: 'Failed to delete reseller quota' });

export const adjustResellerCredit = (resellerId, data) =>
  resellerAdminRequest(`/resellers/${resellerId}/credit-adjustments`, { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to adjust reseller credit' });
export const listResellerLedger = (resellerId, filters = {}) =>
  resellerAdminRequest(`/resellers/${resellerId}/ledger${resellerAdminQuery(filters)}`, { errorMessage: 'Failed to load credit ledger' });
export const listResellerInvoices = (filters = {}) =>
  resellerAdminRequest(`/resellers/invoices${resellerAdminQuery(filters)}`, { errorMessage: 'Failed to load reseller invoices' });
export const getResellerInvoice = (invoiceId) =>
  resellerAdminRequest(`/resellers/invoices/${invoiceId}`, { errorMessage: 'Failed to load reseller invoice' });
export const listResellerPayments = (filters = {}) =>
  resellerAdminRequest(`/resellers/payments${resellerAdminQuery(filters)}`, { errorMessage: 'Failed to load reseller payments' });
export const getResellerPayment = (paymentId) =>
  resellerAdminRequest(`/resellers/payments/${paymentId}`, { errorMessage: 'Failed to load reseller payment' });
export const refundResellerPayment = (paymentId, data = {}) =>
  resellerAdminRequest(`/resellers/payments/${paymentId}/refund`, {
    method: 'POST',
    body: JSON.stringify(data),
    errorMessage: 'Failed to refund payment',
  });

// Session-authenticated reseller control panel. Keep this helper private so
// panel calls consistently use the HttpOnly session cookie and preserve
// structured payment errors such as Stripe's SCA client secret.
async function resellerPanelRequest(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_BASE}/reseller-panel${path}`, {
    ...options,
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    let message = options.errorMessage || 'Reseller panel request failed';
    if (typeof detail === 'string') {
      message = detail;
    } else if (detail?.message || detail?.code) {
      message = detail.message || detail.code;
    }
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  if (response.status === 204) return null;
  return await response.json();
}

export const getResellerDashboard = () => resellerPanelRequest('/dashboard');
export const getResellerPaymentConfig = () => resellerPanelRequest('/payment-config');
export const createResellerTopUp = (amountCents) =>
  resellerPanelRequest('/top-up-invoices', { method: 'POST', body: JSON.stringify({ amount_cents: amountCents }) });
export const createResellerStripeSetupIntent = () =>
  resellerPanelRequest('/stripe/setup-intent', { method: 'POST' });
export const createResellerPayPalSetup = () =>
  resellerPanelRequest('/paypal/setup-token', { method: 'POST' });
export const completeResellerPayPalSetup = (setupTokenId, tier = 1, label = null) =>
  resellerPanelRequest('/paypal/payment-methods', {
    method: 'POST',
    body: JSON.stringify({ setup_token_id: setupTokenId, tier, label }),
  });
export const listResellerPanelPaymentMethods = () => resellerPanelRequest('/payment-methods');
export const registerResellerStripeMethod = (paymentMethodId, tier = 1, label = null) =>
  resellerPanelRequest('/payment-methods', {
    method: 'POST',
    body: JSON.stringify({ payment_method_id: paymentMethodId, tier, label }),
  });
export const deleteResellerPanelPaymentMethod = (methodId) =>
  resellerPanelRequest(`/payment-methods/${methodId}`, { method: 'DELETE' });
export const reorderResellerPanelPaymentMethods = (methods) =>
  resellerPanelRequest('/payment-methods/reorder', {
    method: 'PUT',
    body: JSON.stringify({ methods }),
  });
export const updateResellerChargePreference = (chargePreference) =>
  resellerPanelRequest('/charge-preference', {
    method: 'PUT',
    body: JSON.stringify({ charge_preference: chargePreference }),
  });
export const listResellerPanelInvoices = (filters = {}) =>
  resellerPanelRequest(`/invoices${resellerAdminQuery(filters)}`);
export const getResellerPanelInvoice = (invoiceId) =>
  resellerPanelRequest(`/invoices/${invoiceId}`);
export const payResellerPanelInvoice = (invoiceId, paymentMethodId = null) =>
  resellerPanelRequest(`/invoices/${invoiceId}/pay`, {
    method: 'POST',
    body: JSON.stringify({ payment_method_id: paymentMethodId }),
  });
export const createResellerUsdtDeposit = (invoiceId) =>
  resellerPanelRequest(`/invoices/${invoiceId}/usdt-deposit`, { method: 'POST' });
export const getResellerUsdtDeposit = (invoiceId) =>
  resellerPanelRequest(`/invoices/${invoiceId}/usdt-deposit`);
export const listResellerPanelProducts = () => resellerPanelRequest('/products');
export const updateResellerProductClientPermissions = (productId, data) =>
  resellerPanelRequest(`/products/${productId}/client-permissions`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
export const listResellerPanelClients = (filters = {}) =>
  resellerPanelRequest(`/clients${resellerAdminQuery(filters)}`);
export const getResellerPanelClient = (clientId) =>
  resellerPanelRequest(`/clients/${clientId}`);
export const listResellerPanelServices = (filters = {}) =>
  resellerPanelRequest(`/services${resellerAdminQuery(filters)}`);
export const getResellerPanelService = (serviceId) =>
  resellerPanelRequest(`/services/${serviceId}`);
export const getResellerPanelQuotas = () => resellerPanelRequest('/quotas');
export const getResellerApiKey = () => resellerPanelRequest('/api-key');
export const rotateOwnResellerApiKey = (currentPassword) =>
  resellerPanelRequest('/api-key/rotate', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword }),
  });

// --- Retail commerce (store admin, commerce admin, client portal) ---

function commerceQuery(filters = {}) {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, value);
  });
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}

async function adminStoreRequest(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_BASE}/admin/store${path}`, {
    ...options,
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || options.errorMessage || 'Store admin request failed');
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.detail = detail;
    throw requestError;
  }
  if (response.status === 204) return null;
  return await response.json();
}

async function adminCommerceRequest(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_BASE}/admin/commerce${path}`, {
    ...options,
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || options.errorMessage || 'Commerce admin request failed');
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.detail = detail;
    throw requestError;
  }
  if (response.status === 204) return null;
  return await response.json();
}

async function adminSupportRequest(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_BASE}/admin/support${path}`, {
    ...options,
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || options.errorMessage || 'Support admin request failed');
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.detail = detail;
    throw requestError;
  }
  if (response.status === 204) return null;
  return await response.json();
}

async function clientCommerceRequest(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  Object.entries(_impersonationHeaders()).forEach(([key, value]) => {
    headers.set(key, value);
  });
  const response = await fetch(`${API_BASE}/client/commerce${path}`, {
    ...options,
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || options.errorMessage || 'Commerce request failed');
    const requestError = new Error(message);
    requestError.status = response.status;
    requestError.detail = detail;
    throw requestError;
  }
  if (response.status === 204) return null;
  return await response.json();
}

async function commercePdfRequest(path, { errorMessage = 'PDF download failed' } = {}) {
  const headers = new Headers(_impersonationHeaders());
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'GET',
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || errorMessage);
  }
  return await response.blob();
}

// Store admin
export const adminStoreListCategories = (filters = {}) =>
  adminStoreRequest(`/categories${commerceQuery(filters)}`, { errorMessage: 'Failed to list categories' });
export const adminStoreGetCategory = (categoryId) =>
  adminStoreRequest(`/categories/${categoryId}`, { errorMessage: 'Failed to load category' });
export const adminStoreCreateCategory = (data) =>
  adminStoreRequest('/categories', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create category' });
export const adminStoreUpdateCategory = (categoryId, data) =>
  adminStoreRequest(`/categories/${categoryId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update category' });
export const adminStoreDeleteCategory = (categoryId) =>
  adminStoreRequest(`/categories/${categoryId}`, { method: 'DELETE', errorMessage: 'Failed to delete category' });

export const adminStoreListProducts = (filters = {}) =>
  adminStoreRequest(`/products${commerceQuery(filters)}`, { errorMessage: 'Failed to list store products' });
export const adminStoreGetProduct = (productId) =>
  adminStoreRequest(`/products/${productId}`, { errorMessage: 'Failed to load store product' });
export const adminStoreCreateProduct = (data) =>
  adminStoreRequest('/products', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create store product' });
export const adminStoreUpdateProduct = (productId, data) =>
  adminStoreRequest(`/products/${productId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update store product' });
export const adminStoreDeleteProduct = (productId) =>
  adminStoreRequest(`/products/${productId}`, { method: 'DELETE', errorMessage: 'Failed to delete store product' });

export const adminStoreCreatePricePlan = (productId, data) =>
  adminStoreRequest(`/products/${productId}/plans`, { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create price plan' });
export const adminStoreUpdatePricePlan = (planId, data) =>
  adminStoreRequest(`/plans/${planId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update price plan' });
export const adminStoreDeletePricePlan = (planId) =>
  adminStoreRequest(`/plans/${planId}`, { method: 'DELETE', errorMessage: 'Failed to delete price plan' });
export const adminStoreCreatePlanCycle = (planId, data) =>
  adminStoreRequest(`/plans/${planId}/cycles`, { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create billing cycle' });
export const adminStoreUpdatePlanCycle = (cycleId, data) =>
  adminStoreRequest(`/cycles/${cycleId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update billing cycle' });
export const adminStoreDeletePlanCycle = (cycleId) =>
  adminStoreRequest(`/cycles/${cycleId}`, { method: 'DELETE', errorMessage: 'Failed to delete billing cycle' });

export const adminStoreListCoupons = (filters = {}) =>
  adminStoreRequest(`/coupons${commerceQuery(filters)}`, { errorMessage: 'Failed to list coupons' });
export const adminStoreCreateCoupon = (data) =>
  adminStoreRequest('/coupons', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create coupon' });
export const adminStoreUpdateCoupon = (couponId, data) =>
  adminStoreRequest(`/coupons/${couponId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update coupon' });

export const adminStoreListTaxRates = (filters = {}) =>
  adminStoreRequest(`/tax-rates${commerceQuery(filters)}`, { errorMessage: 'Failed to list tax rates' });
export const adminStoreCreateTaxRate = (data) =>
  adminStoreRequest('/tax-rates', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create tax rate' });
export const adminStoreUpdateTaxRate = (taxRateId, data) =>
  adminStoreRequest(`/tax-rates/${taxRateId}`, { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update tax rate' });

// Commerce admin
export const adminCommerceListOrders = (filters = {}) =>
  adminCommerceRequest(`/orders${commerceQuery(filters)}`, { errorMessage: 'Failed to list orders' });
export const adminCommerceGetOrder = (orderId) =>
  adminCommerceRequest(`/orders/${orderId}`, { errorMessage: 'Failed to load order' });
export const adminCommerceAcceptOrder = (orderId) =>
  adminCommerceRequest(`/orders/${orderId}/accept`, { method: 'POST', errorMessage: 'Failed to accept order' });
export const adminCommerceRetryOrder = (orderId) =>
  adminCommerceRequest(`/orders/${orderId}/retry-fulfill`, { method: 'POST', errorMessage: 'Failed to retry fulfillment' });
export const adminCommerceCancelOrder = (orderId) =>
  adminCommerceRequest(`/orders/${orderId}/cancel`, { method: 'POST', errorMessage: 'Failed to cancel order' });

export const adminCommerceListInvoices = (filters = {}) =>
  adminCommerceRequest(`/invoices${commerceQuery(filters)}`, { errorMessage: 'Failed to list invoices' });
export const adminCommerceGetInvoice = (invoiceId) =>
  adminCommerceRequest(`/invoices/${invoiceId}`, { errorMessage: 'Failed to load invoice' });
export const adminCommerceDownloadInvoicePdf = (invoiceId) =>
  commercePdfRequest(`/admin/commerce/invoices/${invoiceId}/pdf`, { errorMessage: 'Failed to download invoice PDF' });
export const adminCommerceMarkInvoicePaid = (invoiceId, reason) =>
  adminCommerceRequest(`/invoices/${invoiceId}/mark-paid`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
    errorMessage: 'Failed to mark invoice paid',
  });
export const adminCommerceVoidInvoice = (invoiceId) =>
  adminCommerceRequest(`/invoices/${invoiceId}/void`, { method: 'POST', errorMessage: 'Failed to void invoice' });

export const adminCommerceListTransactions = (filters = {}) =>
  adminCommerceRequest(`/transactions${commerceQuery(filters)}`, { errorMessage: 'Failed to list transactions' });
export const adminCommerceListGatewayLogs = (filters = {}) =>
  adminCommerceRequest(`/gateway-logs${commerceQuery(filters)}`, { errorMessage: 'Failed to list gateway logs' });
export const adminCommerceListEmailMessages = (filters = {}) =>
  adminCommerceRequest(`/email-messages${commerceQuery(filters)}`, { errorMessage: 'Failed to list email messages' });
export const adminCommerceRetryEmailMessage = (messageId) =>
  adminCommerceRequest(`/email-messages/${messageId}/retry`, { method: 'POST', errorMessage: 'Failed to retry email' });
export const adminCommerceListAuditEvents = (filters = {}) =>
  adminCommerceRequest(`/audit-events${commerceQuery(filters)}`, { errorMessage: 'Failed to list audit events' });
export const adminCommerceListBillingAccounts = (filters = {}) =>
  adminCommerceRequest(`/billing-accounts${commerceQuery(filters)}`, { errorMessage: 'Failed to list billing accounts' });
export const adminCommerceGetBillingAccount = (accountId) =>
  adminCommerceRequest(`/billing-accounts/${accountId}`, { errorMessage: 'Failed to load billing account' });

// Support admin
export const adminSupportListDepartments = () =>
  adminSupportRequest('/departments', { errorMessage: 'Failed to list departments' });
export const adminSupportListTickets = (filters = {}) =>
  adminSupportRequest(`/tickets${commerceQuery(filters)}`, { errorMessage: 'Failed to list tickets' });
export const adminSupportGetTicket = (ticketId) =>
  adminSupportRequest(`/tickets/${ticketId}`, { errorMessage: 'Failed to load ticket' });
export const adminSupportReplyTicket = (ticketId, data) =>
  adminSupportRequest(`/tickets/${ticketId}/reply`, {
    method: 'POST',
    body: JSON.stringify(data),
    errorMessage: 'Failed to reply to ticket',
  });
export const adminSupportPatchTicket = (ticketId, data) =>
  adminSupportRequest(`/tickets/${ticketId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
    errorMessage: 'Failed to update ticket',
  });

// Client commerce
export const clientCommerceListProducts = (filters = {}) =>
  clientCommerceRequest(`/products${commerceQuery(filters)}`, { errorMessage: 'Failed to list products' });
export const clientCommerceGetProduct = (productId) =>
  clientCommerceRequest(`/products/${productId}`, { errorMessage: 'Failed to load product' });
export const clientCommerceQuote = (data) =>
  clientCommerceRequest('/quote', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to get quote' });
export const clientCommerceCheckout = (data) =>
  clientCommerceRequest('/checkout', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Checkout failed' });

export const clientCommerceListOrders = (filters = {}) =>
  clientCommerceRequest(`/orders${commerceQuery(filters)}`, { errorMessage: 'Failed to list orders' });
export const clientCommerceGetOrder = (orderId) =>
  clientCommerceRequest(`/orders/${orderId}`, { errorMessage: 'Failed to load order' });

export const clientCommerceListInvoices = (filters = {}) =>
  clientCommerceRequest(`/invoices${commerceQuery(filters)}`, { errorMessage: 'Failed to list invoices' });
export const clientCommerceGetInvoice = (invoiceId) =>
  clientCommerceRequest(`/invoices/${invoiceId}`, { errorMessage: 'Failed to load invoice' });
export const clientCommerceDownloadInvoicePdf = (invoiceId) =>
  commercePdfRequest(`/client/commerce/invoices/${invoiceId}/pdf`, { errorMessage: 'Failed to download invoice PDF' });

export const clientCommerceListEmails = (filters = {}) =>
  clientCommerceRequest(`/emails${commerceQuery(filters)}`, { errorMessage: 'Failed to list emails' });
export const clientCommerceListActivity = (filters = {}) =>
  clientCommerceRequest(`/activity${commerceQuery(filters)}`, { errorMessage: 'Failed to load activity' });

export const clientCommerceGetProfile = () =>
  clientCommerceRequest('/profile', { errorMessage: 'Failed to load profile' });
export const clientCommerceUpdateProfile = (data) =>
  clientCommerceRequest('/profile', { method: 'PUT', body: JSON.stringify(data), errorMessage: 'Failed to update profile' });

export const clientCommerceListTickets = (filters = {}) =>
  clientCommerceRequest(`/tickets${commerceQuery(filters)}`, { errorMessage: 'Failed to list tickets' });
export const clientCommerceGetTicket = (ticketId) =>
  clientCommerceRequest(`/tickets/${ticketId}`, { errorMessage: 'Failed to load ticket' });
export const clientCommerceCreateTicket = (data) =>
  clientCommerceRequest('/tickets', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Failed to create ticket' });
export const clientCommerceReplyTicket = (ticketId, bodyText) =>
  clientCommerceRequest(`/tickets/${ticketId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ body_text: bodyText }),
    errorMessage: 'Failed to send message',
  });

export const clientCommerceDiscordAuthorizeUrl = () =>
  clientCommerceRequest('/discord/authorize-url', { errorMessage: 'Failed to get Discord link URL' });
export const clientCommerceDiscordCallback = (data) =>
  clientCommerceRequest('/discord/callback', { method: 'POST', body: JSON.stringify(data), errorMessage: 'Discord link failed' });
export const clientCommerceDiscordUnlink = () =>
  clientCommerceRequest('/discord', { method: 'DELETE', errorMessage: 'Failed to unlink Discord' });

export const clientCommerce2faSetup = () =>
  clientCommerceRequest('/2fa/setup', { method: 'POST', errorMessage: 'Failed to start 2FA setup' });
export const clientCommerce2faConfirm = (code) =>
  clientCommerceRequest('/2fa/confirm', { method: 'POST', body: JSON.stringify({ code }), errorMessage: 'Failed to confirm 2FA' });
export const clientCommerce2faDisable = (code) =>
  clientCommerceRequest('/2fa/disable', { method: 'POST', body: JSON.stringify({ code }), errorMessage: 'Failed to disable 2FA' });

/** Trigger browser download of a PDF blob returned by commerce PDF helpers. */
export function downloadPdfBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

