const API_BASE = '/api';

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
  const response = await fetch(`${API_BASE}/servers/interaction/servers/${serverId}/boot-task`, {
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
  const response = await fetch(`${API_BASE}/servers/interaction/servers/${serverId}/boot-task`, {
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
  const response = await fetch(`${API_BASE}/servers/interaction/servers/${serverId}/boot-task`, {
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

