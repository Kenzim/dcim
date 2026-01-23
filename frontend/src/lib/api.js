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
    if (response.status === 401) {
      return null; // Not authenticated
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

export async function createRack(locationId, name, units = 42, description = null, row = null, rowPosition = null) {
  const body = { location_id: locationId, name, units };
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

export async function updateRack(id, name = null, units = null, description = null, row = null, rowPosition = null) {
  const body = {};
  if (name !== null) body.name = name;
  if (units !== null) body.units = units;
  if (description !== null) body.description = description;
  if (row !== null && row !== '') body.row = Number(row);
  if (rowPosition !== null && rowPosition !== '') body.row_position = Number(rowPosition);

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

export async function testServerConnection(pluginId, pluginConfig) {
  const response = await fetch(`${API_BASE}/servers/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({ plugin_id: pluginId, plugin_config: pluginConfig }),
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

export async function testPluginCapabilities(pluginId, pluginConfig) {
  const response = await fetch(`${API_BASE}/servers/test-capabilities`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      plugin_id: pluginId,
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

