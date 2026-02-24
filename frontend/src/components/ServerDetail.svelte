<script>
  import PageHeader from './PageHeader.svelte';
  import ServerControlsPanel from './ServerControlsPanel.svelte';
  import Servers from './Servers.svelte';
  import TrafficGraph from './ui/TrafficGraph.svelte';
  import { getServer, getPlugins, getLocations, getBootTask, createBootTask, cancelBootTask, listISOs, getScripts, getOSTemplates, getInstallationHistory, updateInstallationTaskStatus, purgePendingInstallationHistory, generatePassword, getServerGroups, updateServer, listCableRuns, getSwitches, getSwitchPorts, createCableRun, deleteCableRun, getServerBandwidth, testServerConnection, getServerPowerState, getAssets, getAssetFileUrl } from '../lib/api.js';
  import { link } from 'svelte-spa-router';
  import { navigate } from '../lib/router.js';

  export let serverId;
  export let onBack;

  let server = null;
  let showEditModal = false;
  let activeTab = 'overview'; // overview | networking | disks | credentials
  let plugins = [];
  let locations = [];
  let loading = true;
  let error = null;
  let expandedLogs = false;
  let bootTask = null;
  let isos = [];
  let loadingISOs = false;
  let selectedISO = null;
  let scripts = [];
  let loadingScripts = false;
  let selectedScript = null;
  let creatingBootTask = false;
  let refreshing = false;
  let templates = [];
  let loadingTemplates = false;
  let selectedTemplate = null;
  let templateParameters = {};
  let bootOperationsExpanded = true;
  let hardwareExpanded = false;
  let installationHistory = [];
  let loadingInstallationHistory = false;
  let installationHistoryExpanded = false;
  let serverGroups = [];
  let allServerGroups = [];
  let loadingServerGroups = false;
  let showServerGroupsModal = false;
  let selectedGroupIds = [];
  let savingServerGroups = false;

  // Cable run (server port <-> switch port) mapping
  let cableRunsForServer = [];
  let connectModalPort = null;
  let switches = [];
  let selectedSwitchId = null;
  let switchPorts = [];
  let selectedSwitchPortId = null;
  let connecting = false;
  let connectError = null;
  let loadingSwitchPorts = false;

  // Bandwidth (from linked switch ports)
  let serverBandwidth = null;
  let serverBandwidthLoading = false;
  let serverBandwidthError = null;
  let serverBandwidthHours = 24;
  let serverBandwidthResolution = 0; // 0 = raw, 5, 15, 60 min
  let serverBandwidthExpanded = false;

  // Left pane tabs (General, Additional Information, Files)
  let leftPaneTab = 'general';

  // Bottom right pane tabs (Management, Network, Disks, Credentials)
  let bottomTab = 'management';

  // Edit network config modal
  let showNetworkEditModal = false;
  let networkEditPorts = [];
  let savingNetworkConfig = false;
  let networkEditError = null;

  // Edit disks modal
  let showDiskEditModal = false;
  let diskEditList = [];
  let savingDiskEdit = false;
  let diskEditError = null;

  // Credentials: plugin config (e.g. IPMI) edit
  let pluginConfigEdit = {};
  let savingPluginConfig = false;
  let pluginConfigError = null;
  let testingPluginConnection = false;
  let pluginConfigTestMessage = null;

  let purgingPendingInstallation = false;
  $: pendingInstallationCount = (installationHistory || []).filter(e => e.status === 'pending').length;

  $: if (server) {
    generalNotesDraft = server.description ?? '';
    generalCommentsDraft = server.comments ?? '';
  }

  let leftPanePowerState = null; // 'on' | 'off' | 'unknown' | null (loading)

  let showPreviewAssetPicker = false;
  let previewAssets = [];
  let loadingPreviewAssets = false;
  let savingPreviewAsset = false;

  let generalNotesDraft = '';
  let generalCommentsDraft = '';
  let savingGeneralNotes = false;
  let savingGeneralComments = false;

  // Load when serverId is set (initial mount and when navigating between servers)
  $: if (serverId != null) {
    loadAllData();
  }

  // Load bandwidth when switching to Networking tab
  $: if (activeTab === 'networking' && serverId && !serverBandwidthLoading && !serverBandwidth) {
    loadServerBandwidth();
  }

  // Sync plugin config edit state when viewing Credentials tab or server changes
  $: if (bottomTab === 'credentials' && server) {
    const plugin = plugins.find(p => p.name === server.plugin_name);
    const props = plugin?.config_template?.properties || {};
    const defaults = {};
    for (const [k, v] of Object.entries(props)) {
      if (v.default !== undefined) defaults[k] = v.default;
    }
    pluginConfigEdit = { ...defaults, ...(server.plugin_config || {}) };
    pluginConfigError = null;
    pluginConfigTestMessage = null;
  }

  async function loadAllData() {
    const id = serverId != null ? Number(serverId) : null;
    await loadServer();
    await Promise.all([
      loadPlugins(),
      loadLocations(),
      loadISOs(),
      loadScripts(),
      loadTemplates(),
      loadServerGroups(),
      id != null ? loadCableRuns() : Promise.resolve(),
    ]);
    if (server) {
      await Promise.all([loadBootTask(), loadInstallationHistory(), loadServerBandwidth()]);
    }
    if (id != null) {
      getServerPowerState(id).then(r => { leftPanePowerState = (r && r.power_state) || 'unknown'; }).catch(() => { leftPanePowerState = 'unknown'; });
    } else {
      leftPanePowerState = null;
    }
  }

  async function loadCableRuns() {
    try {
      const id = serverId != null ? Number(serverId) : null;
      const runs = id != null ? await listCableRuns({ serverId: id }) : [];
      cableRunsForServer = Array.isArray(runs) ? runs : [];
    } catch (err) {
      console.error('Failed to load cable runs:', err);
      cableRunsForServer = [];
    }
  }

  async function loadServerBandwidth() {
    if (!serverId) return;
    serverBandwidthLoading = true;
    serverBandwidthError = null;
    try {
      serverBandwidth = await getServerBandwidth(serverId, serverBandwidthHours, serverBandwidthResolution);
    } catch (err) {
      serverBandwidthError = err.message;
      serverBandwidth = null;
    } finally {
      serverBandwidthLoading = false;
    }
  }

  $: firstPortWithSamples = serverBandwidth?.ports?.find(p => p.samples?.length > 0);

  function formatBytesServer(n) {
    if (n == null || n === undefined) return '—';
    if (n >= 1e12) return (n / 1e12).toFixed(2) + ' TB';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + ' KB';
    return String(n);
  }

  function cableRunForPort(portId) {
    if (portId == null && portId !== 0) return null;
    const pid = Number(portId);
    if (Number.isNaN(pid)) return null;
    return (cableRunsForServer || []).find(cr => {
      const aId = cr.end_a?.id != null ? Number(cr.end_a.id) : NaN;
      const bId = cr.end_b?.id != null ? Number(cr.end_b.id) : NaN;
      const aIsServer = (cr.end_a?.type || '').toLowerCase() === 'server';
      const bIsServer = (cr.end_b?.type || '').toLowerCase() === 'server';
      return (aIsServer && aId === pid) || (bIsServer && bId === pid);
    }) || null;
  }

  function otherEnd(cr, thisServerPortId) {
    if (!cr?.end_a || !cr?.end_b) return null;
    const pid = Number(thisServerPortId);
    if (cr.end_a.type === 'server' && Number(cr.end_a.id) === pid) return cr.end_b;
    if (cr.end_b.type === 'server' && Number(cr.end_b.id) === pid) return cr.end_a;
    return null;
  }

  $: connectedSwitchPortIds = new Set((cableRunsForServer || []).flatMap(cr => {
    if (cr.end_a?.type === 'switch') return [cr.end_a.id];
    if (cr.end_b?.type === 'switch') return [cr.end_b.id];
    return [];
  }));
  $: availableSwitchPorts = (switchPorts || []).filter(p => !connectedSwitchPortIds.has(p.id));

  function openConnectModal(port) {
    connectModalPort = port;
    connectError = null;
    selectedSwitchId = null;
    selectedSwitchPortId = null;
    switchPorts = [];
    switches = [];
    getSwitches()
      .then((list) => { switches = list || []; })
      .catch((err) => {
        connectError = err.message;
        switches = [];
      });
  }

  function closeConnectModal() {
    connectModalPort = null;
    selectedSwitchId = null;
    selectedSwitchPortId = null;
  }

  async function onSwitchChange() {
    selectedSwitchPortId = null;
    switchPorts = [];
    if (!selectedSwitchId) return;
    loadingSwitchPorts = true;
    connectError = null;
    try {
      const result = await getSwitchPorts(selectedSwitchId);
      switchPorts = result?.ports || [];
    } catch (err) {
      connectError = err.message;
    } finally {
      loadingSwitchPorts = false;
    }
  }

  async function handleConnect() {
    if (!connectModalPort || !selectedSwitchPortId) return;
    connecting = true;
    connectError = null;
    try {
      await createCableRun({
        port_a: { type: 'switch', id: selectedSwitchPortId },
        port_b: { type: 'server', id: connectModalPort.id }
      });
      await loadServer();
      await loadCableRuns();
      closeConnectModal();
    } catch (err) {
      connectError = err.message;
    } finally {
      connecting = false;
    }
  }

  async function handleDisconnect(port) {
    const cableRunId = port.cable_run?.cable_run_id ?? cableRunForPort(port.id)?.id;
    if (!cableRunId) return;
    if (!confirm(`Disconnect ${port.name} from switch port?`)) return;
    try {
      await deleteCableRun(cableRunId);
      await loadServer();
      await loadCableRuns();
    } catch (err) {
      console.error('Failed to disconnect:', err);
      alert(err.message);
    }
  }

  function openNetworkEditModal() {
    networkEditError = null;
    networkEditPorts = (server?.network_ports || []).map(p => ({
      id: p.id,
      name: p.name || '',
      mac_address: p.mac_address || '',
      speed_mbps: p.speed_mbps ?? 1000,
      lag_group: p.lag_group || '',
      monitor_bandwidth: p.monitor_bandwidth ?? false,
      pxe_boot: p.pxe_boot ?? false,
      pxe_ip: p.pxe_ip || '',
      description: p.description || ''
    }));
    showNetworkEditModal = true;
  }

  function closeNetworkEditModal() {
    showNetworkEditModal = false;
  }

  function addNetworkEditPort() {
    networkEditPorts = [...networkEditPorts, {
      name: '',
      mac_address: '',
      speed_mbps: 1000,
      lag_group: '',
      monitor_bandwidth: false,
      pxe_boot: false,
      pxe_ip: '',
      description: ''
    }];
  }

  function removeNetworkEditPort(index) {
    networkEditPorts = networkEditPorts.filter((_, i) => i !== index);
  }

  async function saveNetworkConfig() {
    if (!serverId) return;
    savingNetworkConfig = true;
    networkEditError = null;
    try {
      const payload = networkEditPorts.map(p => ({
        name: (p.name || '').trim(),
        mac_address: (p.mac_address || '').trim() || null,
        speed_mbps: parseInt(p.speed_mbps, 10) || 1000,
        lag_group: (p.lag_group || '').trim() || null,
        monitor_bandwidth: !!p.monitor_bandwidth,
        pxe_boot: !!p.pxe_boot,
        pxe_ip: p.pxe_boot && (p.pxe_ip || '').trim() ? (p.pxe_ip || '').trim() : null,
        description: (p.description || '').trim() || null
      }));
      await updateServer(serverId, { network_ports: payload });
      await loadServer();
      closeNetworkEditModal();
    } catch (err) {
      networkEditError = err.message;
    } finally {
      savingNetworkConfig = false;
    }
  }

  function openDiskEditModal() {
    diskEditList = (server?.disks || []).map(d => ({
      type: (d.type || 'ssd').toLowerCase(),
      capacity_gb: d.capacity_gb ?? 0,
      description: d.description ?? '',
      serial_number: d.serial_number ?? '',
      is_os_disk: !!d.is_os_disk
    }));
    diskEditError = null;
    showDiskEditModal = true;
  }

  function closeDiskEditModal() {
    showDiskEditModal = false;
  }

  function addDiskEdit() {
    diskEditList = [...diskEditList, {
      type: 'ssd',
      capacity_gb: 0,
      description: '',
      serial_number: '',
      is_os_disk: false
    }];
  }

  function removeDiskEdit(index) {
    diskEditList = diskEditList.filter((_, i) => i !== index);
  }

  async function saveDiskEdit() {
    if (!serverId) return;
    savingDiskEdit = true;
    diskEditError = null;
    try {
      const payload = diskEditList.map(d => ({
        type: (d.type || 'ssd').toLowerCase(),
        capacity_gb: parseInt(d.capacity_gb, 10) || 0,
        description: (d.description || '').trim() || null,
        serial_number: (d.serial_number || '').trim() || null,
        is_os_disk: !!d.is_os_disk
      }));
      await updateServer(serverId, { disks: payload });
      await loadServer();
      closeDiskEditModal();
    } catch (err) {
      diskEditError = err.message;
    } finally {
      savingDiskEdit = false;
    }
  }

  function getCurrentPlugin() {
    const name = server?.plugin_name;
    if (!name) return null;
    return plugins.find(p => p.name === name) || null;
  }

  async function savePluginConfig() {
    if (!serverId) return;
    savingPluginConfig = true;
    pluginConfigError = null;
    try {
      await updateServer(serverId, { plugin_config: pluginConfigEdit });
      await loadServer();
    } catch (err) {
      pluginConfigError = err.message;
    } finally {
      savingPluginConfig = false;
    }
  }

  async function saveGeneralNotes() {
    if (!serverId || generalNotesDraft === (server?.description ?? '')) return;
    savingGeneralNotes = true;
    try {
      await updateServer(serverId, { description: generalNotesDraft || null });
      await loadServer();
    } catch (err) {
      console.error('Failed to save notes:', err);
    } finally {
      savingGeneralNotes = false;
    }
  }

  async function saveGeneralComments() {
    if (!serverId || generalCommentsDraft === (server?.comments ?? '')) return;
    savingGeneralComments = true;
    try {
      await updateServer(serverId, { comments: generalCommentsDraft || null });
      await loadServer();
    } catch (err) {
      console.error('Failed to save comments:', err);
    } finally {
      savingGeneralComments = false;
    }
  }

  async function handlePurgePendingInstallation() {
    if (!serverId || pendingInstallationCount === 0) return;
    purgingPendingInstallation = true;
    try {
      await purgePendingInstallationHistory(serverId);
      await loadInstallationHistory();
    } catch (err) {
      alert('Failed to purge pending: ' + err.message);
    } finally {
      purgingPendingInstallation = false;
    }
  }

  async function testPluginConnection() {
    const plugin = getCurrentPlugin();
    if (!server?.plugin_name || !plugin) return;
    testingPluginConnection = true;
    pluginConfigTestMessage = null;
    try {
      const result = await testServerConnection(server.plugin_name, pluginConfigEdit);
      pluginConfigTestMessage = result?.message || 'Connection successful';
    } catch (err) {
      pluginConfigTestMessage = err.message || 'Connection failed';
    } finally {
      testingPluginConnection = false;
    }
  }

  async function loadServerGroups() {
    try {
      loadingServerGroups = true;
      allServerGroups = await getServerGroups();
      if (server && server.server_groups) {
        serverGroups = server.server_groups;
        selectedGroupIds = server.server_groups.map(g => g.id);
      }
    } catch (err) {
      console.error('Failed to load server groups:', err);
      allServerGroups = [];
    } finally {
      loadingServerGroups = false;
    }
  }

  function openServerGroupsModal() {
    selectedGroupIds = (server?.server_groups || []).map(g => g.id);
    showServerGroupsModal = true;
  }

  function closeServerGroupsModal() {
    showServerGroupsModal = false;
  }

  async function handleEditComplete() {
    showEditModal = false;
    await loadServer();
  }

  async function openPreviewAssetPicker() {
    showPreviewAssetPicker = true;
    loadingPreviewAssets = true;
    previewAssets = [];
    try {
      previewAssets = await getAssets('server_preview') || [];
      if (previewAssets.length === 0) previewAssets = await getAssets() || [];
    } catch (err) {
      console.error('Failed to load assets:', err);
    } finally {
      loadingPreviewAssets = false;
    }
  }

  function closePreviewAssetPicker() {
    showPreviewAssetPicker = false;
  }

  async function selectPreviewAsset(assetId) {
    if (!serverId) return;
    savingPreviewAsset = true;
    try {
      await updateServer(serverId, { preview_asset_id: assetId });
      await loadServer();
      closePreviewAssetPicker();
    } catch (err) {
      alert('Failed to set preview image: ' + err.message);
    } finally {
      savingPreviewAsset = false;
    }
  }

  async function clearPreviewAsset() {
    if (!serverId) return;
    savingPreviewAsset = true;
    try {
      await updateServer(serverId, { preview_asset_id: null });
      await loadServer();
      closePreviewAssetPicker();
    } catch (err) {
      alert('Failed to clear preview image: ' + err.message);
    } finally {
      savingPreviewAsset = false;
    }
  }

  function toggleGroupSelection(groupId) {
    if (selectedGroupIds.includes(groupId)) {
      selectedGroupIds = selectedGroupIds.filter(id => id !== groupId);
    } else {
      selectedGroupIds = [...selectedGroupIds, groupId];
    }
  }

  async function handleSaveServerGroups() {
    savingServerGroups = true;
    try {
      await updateServer(serverId, { server_group_ids: selectedGroupIds });
      await loadServer();
      closeServerGroupsModal();
    } catch (err) {
      alert('Failed to update server groups: ' + err.message);
    } finally {
      savingServerGroups = false;
    }
  }

  async function loadInstallationHistory() {
    try {
      loadingInstallationHistory = true;
      installationHistory = await getInstallationHistory(serverId) || [];
    } catch (err) {
      // Silently handle errors - installation history is optional
      installationHistory = [];
    } finally {
      loadingInstallationHistory = false;
    }
  }

  let updatingInstallationId = null;
  let installationStatusError = null;
  let installationStatusSelection = {}; // id -> selected status string

  async function setInstallationStatus(installation, newStatus, errorMessage = null) {
    updatingInstallationId = installation.id;
    installationStatusError = null;
    try {
      await updateInstallationTaskStatus(serverId, installation.id, {
        status: newStatus,
        error_message: errorMessage || undefined
      });
      await loadInstallationHistory();
    } catch (err) {
      installationStatusError = err.message;
    } finally {
      updatingInstallationId = null;
    }
  }

  function applyInstallationStatus(installation) {
    const newStatus = installationStatusSelection[installation.id] ?? installation.status;
    if (newStatus === 'pending' || newStatus === installation.status) return;
    if (newStatus === 'failed') {
      const msg = prompt('Error message (optional):', installation.error_message || '');
      if (msg === null) return;
      setInstallationStatus(installation, newStatus, msg || null);
    } else {
      setInstallationStatus(installation, newStatus);
    }
  }

  async function handleRefresh() {
    refreshing = true;
    try {
      await loadAllData();
    } catch (err) {
      console.error('Failed to refresh:', err);
    } finally {
      refreshing = false;
    }
  }

  async function loadServer() {
    try {
      loading = true;
      error = null;
      server = await getServer(serverId);
      if (!server) {
        error = 'Server not found';
      } else {
        // Update server groups from server data
        if (server.server_groups) {
          serverGroups = server.server_groups;
          selectedGroupIds = server.server_groups.map(g => g.id);
        }
      }
    } catch (err) {
      error = err.message;
      console.error('Failed to load server:', err);
    } finally {
      loading = false;
    }
  }

  async function loadPlugins() {
    try {
      plugins = await getPlugins();
    } catch (err) {
      console.error('Failed to load plugins:', err);
    }
  }

  async function loadLocations() {
    try {
      locations = await getLocations();
    } catch (err) {
      console.error('Failed to load locations:', err);
    }
  }

  function getPluginName() {
    return server.plugin_name || 'N/A';
  }

  function getLocationName() {
    if (server.location_name) return server.location_name;
    const loc = locations.find(l => l.id == server.location_id);
    return loc?.name || 'N/A';
  }

  async function loadISOs() {
    try {
      loadingISOs = true;
      isos = await listISOs();
    } catch (err) {
      console.error('Failed to load ISOs:', err);
      isos = [];
    } finally {
      loadingISOs = false;
    }
  }

  async function loadBootTask() {
    try {
      bootTask = await getBootTask(serverId);
    } catch (err) {
      console.error('Failed to load boot task:', err);
      bootTask = null;
    }
  }

  async function handleBootISO() {
    if (!selectedISO) {
      alert('Please select an ISO file');
      return;
    }

    if (!confirm(`Boot server "${server.name}" from ISO "${selectedISO.filename}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'iso',
        iso_url: selectedISO.url,
        description: `Boot from ${selectedISO.filename}`
      });
      await loadBootTask();
      alert('Boot task created successfully. Server will boot from ISO on next reboot.');
      selectedISO = null;
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function handleBootTempOS() {
    if (!confirm(`Boot server "${server.name}" into Debian Live OS?`)) return;
    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'temp_os',
        temp_os_id: 'debian-live',
        description: 'Boot into Debian Live OS'
      });
      await loadBootTask();
      alert('Boot task created. Server will boot into Debian Live OS on next reboot.');
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function loadScripts() {
    try {
      loadingScripts = true;
      // Filter to only enabled scripts that are user-executable or all enabled scripts for admin
      const allScripts = await getScripts();
      scripts = allScripts.filter(s => s.enabled);
    } catch (err) {
      console.error('Failed to load scripts:', err);
      scripts = [];
    } finally {
      loadingScripts = false;
    }
  }

  async function loadTemplates() {
    try {
      loadingTemplates = true;
      templates = await getOSTemplates();
    } catch (err) {
      console.error('Failed to load templates:', err);
      templates = [];
    } finally {
      loadingTemplates = false;
    }
  }

  function handleTemplateChange() {
    if (!selectedTemplate) {
      templateParameters = {};
      return;
    }
    
    const template = templates.find(t => t.id === selectedTemplate);
    if (template) {
      // Initialize parameters with defaults
      templateParameters = {};
      for (const [paramName, param] of Object.entries(template.parameters)) {
        if (param.default !== null && param.default !== undefined) {
          templateParameters[paramName] = param.default;
        } else if (param.type === 'boolean') {
          templateParameters[paramName] = false;
        } else if (param.type === 'number') {
          templateParameters[paramName] = 0;
        } else {
          templateParameters[paramName] = '';
        }
      }
    }
  }

  async function handleStartInstallation() {
    if (!selectedTemplate) {
      alert('Please select an OS template');
      return;
    }

    const template = templates.find(t => t.id === selectedTemplate);
    if (!template) {
      alert('Selected template not found');
      return;
    }

    // Validate required parameters
    for (const [paramName, param] of Object.entries(template.parameters)) {
      if (param.required) {
        const value = templateParameters[paramName];
        if (value === null || value === undefined || value === '') {
          alert(`Parameter "${param.label}" is required`);
          return;
        }
      }
    }

    if (!confirm(`Start OS installation for server "${server.name}" using template "${template.name}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'linux_script',
        template_id: selectedTemplate,
        template_parameters: templateParameters,
        description: `Install ${template.name}`
      });
      await loadBootTask();
      alert(`Installation job created successfully. Server will boot and start installation on next reboot.`);
      selectedTemplate = null;
      templateParameters = {};
    } catch (err) {
      alert('Failed to create installation job: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function handleRunScript() {
    if (!selectedScript) {
      alert('Please select a script');
      return;
    }

    // selectedScript is the script name or ID
    const script = scripts.find(s => s.name === selectedScript || s.id.toString() === selectedScript);
    if (!script) {
      alert('Selected script not found');
      return;
    }

    if (!confirm(`Boot server "${server.name}" into Ubuntu Live OS and run script "${script.name}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      // Use script name (backend accepts name or ID)
      await createBootTask(serverId, {
        boot_type: 'temp_os',
        temp_os_id: 'debian-live',
        custom_script: script.name,
        description: `Run script: ${script.name}`
      });
      await loadBootTask();
      alert(`Boot task created successfully. Server will boot into Ubuntu Live OS and run "${script.name}" on next reboot.`);
      selectedScript = null;
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function handleCancelBootTask() {
    if (!confirm(`Cancel boot task for server "${server.name}"?`)) {
      return;
    }

    try {
      await cancelBootTask(serverId);
      await loadBootTask();
      alert('Boot task cancelled successfully.');
    } catch (err) {
      alert('Failed to cancel boot task: ' + err.message);
    }
  }

  function formatFileSize(mb) {
    if (mb < 1024) {
      return `${mb.toFixed(2)} MB`;
    } else {
      return `${(mb / 1024).toFixed(2)} GB`;
    }
  }

  async function handleGeneratePassword(paramName) {
    const template = templates.find(t => t.id === selectedTemplate);
    if (!template || !template.parameters[paramName]) {
      return;
    }
    
    const param = template.parameters[paramName];
    const generateConfig = param.generate || {};
    
    // Use template config if available, otherwise use defaults
    const length = generateConfig.length || 16;
    const charset = generateConfig.charset || 'alphanumeric';
    const excludeAmbiguous = generateConfig.exclude_ambiguous !== undefined 
      ? generateConfig.exclude_ambiguous 
      : true;
    
    try {
      const password = await generatePassword(length, charset, excludeAmbiguous);
      templateParameters[paramName] = password;
      // Trigger reactivity
      templateParameters = { ...templateParameters };
    } catch (err) {
      alert('Failed to generate password: ' + err.message);
    }
  }

  async function handleCopyPassword(paramName, event) {
    const password = templateParameters[paramName];
    if (!password) return;
    
    try {
      // Try modern clipboard API first
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(password);
        // Show temporary feedback
        const button = event?.target?.closest('button');
        if (button) {
          const originalTitle = button.title;
          button.title = 'Copied!';
          setTimeout(() => {
            button.title = originalTitle;
          }, 2000);
        }
        return;
      }
      
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = password;
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      textArea.style.top = '-999999px';
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      try {
        const successful = document.execCommand('copy');
        if (successful) {
          const button = event?.target?.closest('button');
          if (button) {
            const originalTitle = button.title;
            button.title = 'Copied!';
            setTimeout(() => {
              button.title = originalTitle;
            }, 2000);
          }
        } else {
          throw new Error('execCommand copy failed');
        }
      } catch (e) {
        alert('Failed to copy password. Please copy manually.');
      } finally {
        document.body.removeChild(textArea);
      }
    } catch (err) {
      alert('Failed to copy password. Please copy manually.');
    }
  }

  async function handleCopyCredential(value, event) {
    if (!value) return;
    
    try {
      // Try modern clipboard API first
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(String(value));
        // Show temporary feedback
        const button = event?.target?.closest('button');
        if (button) {
          const originalTitle = button.title;
          button.title = 'Copied!';
          setTimeout(() => {
            button.title = originalTitle;
          }, 2000);
        }
        return;
      }
      
      // Fallback for browsers that don't support clipboard API
      const textArea = document.createElement('textarea');
      textArea.value = String(value);
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      textArea.style.top = '-999999px';
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      try {
        const successful = document.execCommand('copy');
        if (successful) {
          const button = event?.target?.closest('button');
          if (button) {
            const originalTitle = button.title;
            button.title = 'Copied!';
            setTimeout(() => {
              button.title = originalTitle;
            }, 2000);
          }
        } else {
          throw new Error('execCommand copy failed');
        }
      } catch (e) {
        alert('Failed to copy. Please copy manually.');
      } finally {
        document.body.removeChild(textArea);
      }
    } catch (err) {
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = String(value);
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      document.body.appendChild(textArea);
      textArea.select();
      try {
        document.execCommand('copy');
        const button = event?.target?.closest('button');
        if (button) {
          const originalTitle = button.title;
          button.title = 'Copied!';
          setTimeout(() => {
            button.title = originalTitle;
          }, 2000);
        }
      } catch (e) {
        alert('Failed to copy. Please copy manually.');
      }
      document.body.removeChild(textArea);
    }
  }
</script>

<div class="server-detail-page">
  <PageHeader title={server ? `Server Details\u00A0\u00A0›\u00A0\u00A0${server.name}` : 'Server Details'}>
    <svelte:fragment slot="actions">
      <button class="refresh-button" on:click={handleRefresh} disabled={refreshing || loading}>
        <svg xmlns="http://www.w3.org/2000/svg" class="refresh-icon" class:spinning={refreshing} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
        <span>{refreshing ? 'Refreshing...' : 'Refresh'}</span>
      </button>
    </svelte:fragment>
  </PageHeader>

  {#if loading}
  <div class="content-body">
    <div class="loading">Loading server details...</div>
  </div>
{:else if error}
  <div class="content-body">
    <div class="error">Error: {error}</div>
    <button class="btn-primary" on:click={onBack}>Back to Servers</button>
  </div>
{:else if server}
  <div class="detail-layout">
    <aside class="left-pane">
      <div class="left-pane-top">
        <div class="server-preview-placeholder-wrap">
          {#if server.preview_asset_id}
            <img class="server-preview-image" src={getAssetFileUrl(server.preview_asset_id)} alt="" />
          {:else}
            <div class="server-preview-placeholder" aria-hidden="true">
              <svg xmlns="http://www.w3.org/2000/svg" class="preview-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
              </svg>
              <span class="preview-label">Server preview</span>
            </div>
          {/if}
          <button type="button" class="server-preview-edit-image-btn" on:click={openPreviewAssetPicker} title="Select image from asset manager">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
            <span>Image</span>
          </button>
        </div>
        <div class="server-preview-title-row">
          <h2 class="server-preview-title">{server.name}</h2>
          <div class="server-preview-actions">
            <span
              class="power-pill"
              class:power-on={leftPanePowerState === 'on'}
              class:power-off={leftPanePowerState === 'off'}
              class:power-unknown={leftPanePowerState !== 'on' && leftPanePowerState !== 'off'}
              title={leftPanePowerState === 'on' ? 'Power on' : leftPanePowerState === 'off' ? 'Power off' : 'Power state unknown'}
            >
              {leftPanePowerState === 'on' ? 'On' : leftPanePowerState === 'off' ? 'Off' : '—'}
            </span>
            <button type="button" class="left-pane-edit-btn" on:click={() => showEditModal = true} title="Edit server">Edit</button>
          </div>
        </div>
        <dl class="server-preview-details">
          <div class="detail-row">
            <dt>IP Address</dt>
            <dd>{server.server_ip || '—'}</dd>
          </div>
          <div class="detail-row">
            <dt>Hostname</dt>
            <dd>{server.hostname || server.name || '—'}</dd>
          </div>
          <div class="detail-row">
            <dt>MAC Address</dt>
            <dd>{server.network_ports?.[0]?.mac_address || 'Unassigned'}</dd>
          </div>
        </dl>
      </div>
      <div class="left-pane-tabs-wrap">
        <nav class="left-pane-tabs" role="tablist">
          <button type="button" role="tab" class="left-pane-tab" class:active={leftPaneTab === 'general'} on:click={() => leftPaneTab = 'general'}>General</button>
          <button type="button" role="tab" class="left-pane-tab" class:active={leftPaneTab === 'additional'} on:click={() => leftPaneTab = 'additional'}>Additional Information</button>
          <button type="button" role="tab" class="left-pane-tab" class:active={leftPaneTab === 'files'} on:click={() => leftPaneTab = 'files'}>Files</button>
        </nav>
        <div class="left-pane-tab-content">
          {#if leftPaneTab === 'general'}
            <div class="general-pane">
              <section class="general-details">
                <h4 class="general-section-title">Server Details</h4>
                <dl class="general-details-list">
                  <div class="general-detail-row">
                    <dt>Location</dt>
                    <dd>{server.location_name || '—'}</dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>Rack</dt>
                    <dd>
                      {#if server.rack_name}
                        {server.rack_name}
                      {:else if server.rack_unit != null}
                        — <span class="rack-hint">(assign in Edit)</span>
                      {:else}
                        —
                      {/if}
                    </dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>Position</dt>
                    <dd>{server.rack_unit != null ? ((server.rack_units ?? 1) > 1 ? `U${server.rack_unit}–${server.rack_unit + (server.rack_units ?? 1) - 1} (${server.rack_units}U)` : `U${server.rack_unit}`) : '—'}</dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>{server.server_groups?.length === 1 ? 'Group' : 'Groups'}</dt>
                    <dd>{server.server_groups?.length ? server.server_groups.map(g => g.name).join(', ') : '—'}</dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>Plugin</dt>
                    <dd>{server.plugin_name || '—'}</dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>Boot mode</dt>
                    <dd>{server.pxe_boot_mode || server.boot_mode || '—'}</dd>
                  </div>
                  <div class="general-detail-row">
                    <dt>Status</dt>
                    <dd>{server.enabled !== false ? 'Enabled' : 'Disabled'}</dd>
                  </div>
                </dl>
              </section>
              <section class="general-notes">
                <label class="general-notes-label" for="general-notes-input">Notes</label>
                <textarea
                  id="general-notes-input"
                  class="general-notes-input"
                  placeholder="Notes…"
                  bind:value={generalNotesDraft}
                  on:blur={saveGeneralNotes}
                  rows="3"
                />
                {#if savingGeneralNotes}
                  <span class="general-save-hint">Saving…</span>
                {/if}
              </section>
              <section class="general-comments">
                <label class="general-comments-label" for="general-comments-input">Comments</label>
                <textarea
                  id="general-comments-input"
                  class="general-comments-input"
                  placeholder="Comments…"
                  bind:value={generalCommentsDraft}
                  on:blur={saveGeneralComments}
                  rows="3"
                />
                {#if savingGeneralComments}
                  <span class="general-save-hint">Saving…</span>
                {/if}
              </section>
            </div>
          {:else if leftPaneTab === 'additional'}
            <p class="tab-placeholder">Additional information — add later.</p>
          {:else}
            <p class="tab-placeholder">Files — add later.</p>
          {/if}
        </div>
      </div>
    </aside>
    <div class="content-body">
      <div class="right-top">
        <section class="right-panel traffic-panel">
          <h3 class="panel-title">Traffic</h3>
          {#if serverBandwidthLoading}
            <p class="panel-empty">Loading traffic data…</p>
          {:else if serverBandwidthError}
            <p class="panel-empty">{serverBandwidthError}</p>
          {:else if firstPortWithSamples}
            <div class="traffic-graph-wrap">
              <TrafficGraph
                samples={firstPortWithSamples.samples}
                width={400}
                height={180}
                graphId="main"
              />
            </div>
          {:else}
            <p class="panel-empty">No network ports with bandwidth data. Connect server ports to switch ports and run the SNMP bandwidth poller.</p>
          {/if}
        </section>
        <section class="right-panel activity-panel">
          <h3 class="panel-title">Activity Log</h3>
          {#if loadingInstallationHistory}
            <p class="panel-empty">Loading…</p>
          {:else if installationHistory.length === 0}
            <p class="panel-empty">No activity yet.</p>
          {:else}
            <ul class="activity-list">
              {#each installationHistory.slice(0, 20) as entry}
                <li class="activity-item">
                  <span class="activity-time">{entry.created_at ? new Date(entry.created_at).toLocaleString() : '—'}</span>
                  <span class="activity-desc">{entry.os_name || entry.template_id || entry.description || 'Installation'}</span>
                  <span class="activity-status" class:completed={entry.status === 'completed'} class:failed={entry.status === 'failed'} class:pending={entry.status === 'pending' || entry.status === 'in_progress'}>{entry.status?.replace('_', ' ') || '—'}</span>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </div>
      <div class="right-bottom">
        <nav class="bottom-tabs" role="tablist">
          <button type="button" role="tab" class="bottom-tab" class:active={bottomTab === 'management'} on:click={() => bottomTab = 'management'}>Management</button>
          <button type="button" role="tab" class="bottom-tab" class:active={bottomTab === 'network'} on:click={() => bottomTab = 'network'}>Network</button>
          <button type="button" role="tab" class="bottom-tab" class:active={bottomTab === 'disks'} on:click={() => bottomTab = 'disks'}>Disks</button>
          <button type="button" role="tab" class="bottom-tab" class:active={bottomTab === 'credentials'} on:click={() => bottomTab = 'credentials'}>Credentials</button>
        </nav>
        <div class="bottom-tab-content" class:credentials-tab-active={bottomTab === 'credentials'}>
          {#if bottomTab === 'management'}
            <div class="boot-ops-management">
              {#if bootTask && (bootTask.status === 'pending' || bootTask.status === 'in_progress')}
                <div class="boot-task-banner-inline">
                  <span class="status-badge" class:pending={bootTask.status === 'pending'} class:in-progress={bootTask.status === 'in_progress'}>{bootTask.status.replace('_', ' ')}</span>
                  <span class="boot-task-desc-inline">{bootTask.description || bootTask.boot_type}</span>
                  <button type="button" class="btn-secondary btn-sm" on:click={handleCancelBootTask}>Cancel</button>
                </div>
              {/if}
              <div class="boot-ops-grid">
                <div class="boot-op-card">
                  <span class="boot-op-label">Boot from ISO</span>
                  {#if loadingISOs}
                    <span class="boot-op-hint">Loading…</span>
                  {:else if isos.length === 0}
                    <span class="boot-op-hint">No ISOs available</span>
                  {:else}
                    <div class="boot-op-row">
                      <select bind:value={selectedISO} disabled={creatingBootTask} class="boot-op-select">
                        <option value={null}>Select ISO</option>
                        {#each isos as iso}
                          <option value={iso}>{iso.filename}</option>
                        {/each}
                      </select>
                      <button type="button" class="btn-primary btn-sm" on:click={handleBootISO} disabled={!selectedISO || creatingBootTask}>{creatingBootTask ? '…' : 'Boot'}</button>
                    </div>
                  {/if}
                </div>
                <div class="boot-op-card">
                  <span class="boot-op-label">Temporary OS</span>
                  <span class="boot-op-hint">Debian Live for manual ops</span>
                  <button type="button" class="btn-primary btn-sm" on:click={handleBootTempOS} disabled={creatingBootTask}>{creatingBootTask ? '…' : 'Boot'}</button>
                </div>
                <div class="boot-op-card">
                  <span class="boot-op-label">Run Script</span>
                  {#if loadingScripts}
                    <span class="boot-op-hint">Loading…</span>
                  {:else if scripts.length === 0}
                    <span class="boot-op-hint">No scripts available</span>
                  {:else}
                    <div class="boot-op-row">
                      <select bind:value={selectedScript} disabled={creatingBootTask} class="boot-op-select">
                        <option value="">Select script</option>
                        {#each scripts as script}
                          <option value={script.name}>{script.name}</option>
                        {/each}
                      </select>
                      <button type="button" class="btn-primary btn-sm" on:click={handleRunScript} disabled={creatingBootTask || !selectedScript}>{creatingBootTask ? '…' : 'Run'}</button>
                    </div>
                  {/if}
                </div>
                <div class="boot-op-card boot-op-card-install">
                  <span class="boot-op-label">Install OS</span>
                  {#if loadingTemplates}
                    <span class="boot-op-hint">Loading…</span>
                  {:else if templates.length === 0}
                    <span class="boot-op-hint">No templates available</span>
                  {:else}
                    <div class="boot-op-row">
                      <select class="boot-op-select" bind:value={selectedTemplate} on:change={handleTemplateChange} disabled={creatingBootTask || loadingTemplates}>
                        <option value="">Select template</option>
                        {#each templates as template}
                          <option value={template.id}>{template.name}</option>
                        {/each}
                      </select>
                      <button type="button" class="btn-primary btn-sm" on:click={handleStartInstallation} disabled={creatingBootTask || !selectedTemplate || loadingTemplates}>{creatingBootTask ? '…' : 'Start'}</button>
                    </div>
                    <!-- Fixed-height slot so layout doesn't shift when params load -->
                    <div class="boot-op-params-slot">
                      {#if selectedTemplate}
                        {@const template = templates.find(t => t.id === selectedTemplate)}
                        {#if template && Object.keys(template.parameters || {}).length > 0}
                          {@const paramEntries = Object.entries(template.parameters)}
                          {#if paramEntries.length > 0}
                            <div class="boot-op-params-inner">
                              {#each paramEntries as [paramName, param]}
                                <div class="boot-op-param-row">
                                  <label for="mgmt-param-{paramName}">{param.label}</label>
                                  {#if param.type === 'select' && param.options}
                                    <select id="mgmt-param-{paramName}" bind:value={templateParameters[paramName]} disabled={creatingBootTask}>
                                      <option value="">Select</option>
                                      {#each param.options as option}
                                        <option value={option}>{option}</option>
                                      {/each}
                                    </select>
                                  {:else if param.type === 'password'}
                                    <div class="boot-op-param-input-row">
                                      <input id="mgmt-param-{paramName}" type="text" bind:value={templateParameters[paramName]} placeholder={param.help || param.label} disabled={creatingBootTask} />
                                      <div class="boot-op-param-actions">
                                        <button type="button" class="btn-icon-tool-sm" on:click={() => handleGeneratePassword(paramName)} disabled={creatingBootTask} title="Generate password">
                                          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                                        </button>
                                        <button type="button" class="btn-icon-tool-sm" on:click={(e) => handleCopyPassword(paramName, e)} disabled={creatingBootTask || !templateParameters[paramName]} title="Copy to clipboard">
                                          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
                                        </button>
                                      </div>
                                    </div>
                                  {:else if param.type === 'boolean'}
                                    <label class="boot-op-checkbox"><input type="checkbox" bind:checked={templateParameters[paramName]} disabled={creatingBootTask} /><span>{param.label}</span></label>
                                  {:else if param.type === 'number'}
                                    <input id="mgmt-param-{paramName}" type="number" bind:value={templateParameters[paramName]} placeholder={param.help} disabled={creatingBootTask} />
                                  {:else}
                                    <input id="mgmt-param-{paramName}" type="text" bind:value={templateParameters[paramName]} placeholder={param.help} disabled={creatingBootTask} />
                                  {/if}
                                </div>
                              {/each}
                            </div>
                          {:else}
                            <span class="boot-op-params-empty">No parameters</span>
                          {/if}
                        {:else}
                          <span class="boot-op-params-empty">No parameters for this template</span>
                        {/if}
                      {:else}
                        <span class="boot-op-params-empty">Select a template to configure parameters</span>
                      {/if}
                    </div>
                  {/if}
                </div>
              </div>
            </div>
          {:else if bottomTab === 'network'}
            <div class="network-tab-content">
              <div class="network-tab-toolbar">
                <button type="button" class="btn-secondary btn-sm" on:click={openNetworkEditModal}>Edit network config</button>
              </div>
              {#if server.network_ports && server.network_ports.length > 0}
                <div class="network-ports-wrap">
                  <table class="network-ports-table">
                    <thead>
                      <tr>
                        <th>Port</th>
                        <th>MAC</th>
                        <th>Speed</th>
                        <th>Connected to</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each server.network_ports as port}
                        {@const cr = port.cable_run ?? cableRunForPort(port.id)}
                        {@const hasConnection = !!port.cable_run || !!cableRunForPort(port.id)}
                        {@const oe = cr ? otherEnd(cr, port.id) : null}
                        <tr>
                          <td>{port.name}</td>
                          <td class="mono">{port.mac_address || '—'}</td>
                          <td>{port.speed_mbps ? port.speed_mbps + ' Mbps' : '—'}</td>
                          <td>
                            {#if port.cable_run}
                              <a href={port.cable_run.other_end_type === 'switch' ? `/admin/switches/${port.cable_run.other_end_device_id}` : `/admin/servers/${port.cable_run.other_end_device_id}`} class="link">
                                {port.cable_run.other_end_device_name || port.cable_run.other_end_type} – {port.cable_run.other_end_port_name || ('Port #' + port.cable_run.other_end_port_id)}
                              </a>
                            {:else if oe}
                              <a href={oe.type === 'switch' ? `/admin/switches/${oe.device_id}` : `/admin/servers/${oe.device_id}`} class="link">
                                {oe.device_name || oe.type} – {oe.port_name || oe.id}
                              </a>
                            {:else}
                              <span class="text-muted">Not connected</span>
                            {/if}
                          </td>
                          <td>
                            {#if hasConnection}
                              <button type="button" class="btn-disconnect" on:click={() => handleDisconnect(port)}>Disconnect</button>
                            {:else}
                              <button type="button" class="btn-connect" on:click={() => openConnectModal(port)}>Connect</button>
                            {/if}
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {:else}
                <p class="bottom-placeholder">No network ports. Use Edit network config or edit the server to add ports.</p>
              {/if}
            </div>
          {:else if bottomTab === 'disks'}
            <div class="disks-panel">
              <div class="panel-toolbar">
                <button type="button" class="btn-secondary btn-sm" on:click={openDiskEditModal}>Edit disks</button>
              </div>
              {#if server?.disks?.length}
                <div class="disks-table-wrap">
                  <table class="data-table">
                    <thead>
                      <tr>
                        <th>Type</th>
                        <th>Capacity (GB)</th>
                        <th>Serial</th>
                        <th>Description</th>
                        <th>OS disk</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each server.disks as disk}
                        <tr>
                          <td>{disk.type}</td>
                          <td>{disk.capacity_gb}</td>
                          <td>{disk.serial_number || '—'}</td>
                          <td>{disk.description || '—'}</td>
                          <td>{disk.is_os_disk ? 'Yes' : 'No'}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {:else}
                <p class="bottom-placeholder">No disks. Click "Edit disks" to add disks.</p>
              {/if}
            </div>
          {:else if bottomTab === 'credentials'}
            <div class="credentials-panel">
              <section class="credentials-left plugin-config-section">
                <h3 class="credentials-section-title">Plugin configuration</h3>
                <div class="credentials-scroll">
                {#if !server?.plugin_name}
                  <p class="text-muted">No plugin assigned. Edit the server to assign a plugin (e.g. IPMI).</p>
                {:else}
                  {@const plugin = getCurrentPlugin()}
                  {#if !plugin}
                    <p class="text-muted">Plugin "{server.plugin_name}" not found.</p>
                  {:else if !plugin.config_template?.properties || Object.keys(plugin.config_template.properties).length === 0}
                    <p class="text-muted">No configurable credentials for this plugin.</p>
                  {:else}
                    {@const properties = plugin.config_template.properties || {}}
                    {#if pluginConfigError}
                      <div class="connect-modal-error" style="margin-bottom: 12px;">{pluginConfigError}</div>
                    {/if}
                    {#if pluginConfigTestMessage}
                      <div class="plugin-config-test-msg">{pluginConfigTestMessage}</div>
                    {/if}
                    <div class="plugin-config-form">
                      {#each Object.entries(properties) as [key, schema]}
                        <div class="form-group">
                          <label for="cred-config-{key}">
                            {schema.title || key}
                            {#if (plugin.config_template.required || []).includes(key)}
                              <span class="required">*</span>
                            {/if}
                          </label>
                          {#if schema.type === 'boolean'}
                            <input
                              id="cred-config-{key}"
                              type="checkbox"
                              checked={pluginConfigEdit[key] === true}
                              on:change={(e) => {
                                pluginConfigEdit = { ...pluginConfigEdit, [key]: e.target.checked };
                              }}
                            />
                          {:else if schema.type === 'integer'}
                            <input
                              id="cred-config-{key}"
                              type="number"
                              bind:value={pluginConfigEdit[key]}
                              placeholder={schema.default ?? ''}
                            />
                          {:else if schema.format === 'password'}
                            <input
                              id="cred-config-{key}"
                              type="password"
                              bind:value={pluginConfigEdit[key]}
                              placeholder={schema.description || ''}
                              autocomplete="off"
                            />
                          {:else}
                            <input
                              id="cred-config-{key}"
                              type="text"
                              bind:value={pluginConfigEdit[key]}
                              placeholder={schema.description || schema.default || ''}
                            />
                          {/if}
                          {#if schema.description}
                            <small class="field-help">{schema.description}</small>
                          {/if}
                        </div>
                      {/each}
                      <div class="plugin-config-actions form-group-full">
                        <button type="button" class="btn-secondary btn-sm" on:click={testPluginConnection} disabled={testingPluginConnection}>
                          {testingPluginConnection ? 'Testing…' : 'Test connection'}
                        </button>
                        <button type="button" class="btn-primary btn-sm" on:click={savePluginConfig} disabled={savingPluginConfig}>
                          {savingPluginConfig ? 'Saving…' : 'Save'}
                        </button>
                      </div>
                    </div>
                  {/if}
                {/if}
                </div>
              </section>
              <section class="credentials-right installation-credentials-section">
                <div class="credentials-section-header-row">
                  <h3 class="credentials-section-title">Credentials from installation history</h3>
                  {#if pendingInstallationCount > 0}
                    <button type="button" class="btn-secondary btn-sm" on:click={handlePurgePendingInstallation} disabled={purgingPendingInstallation} title="Delete all pending installation tasks">
                      {purgingPendingInstallation ? 'Purging…' : `Purge pending (${pendingInstallationCount})`}
                    </button>
                  {/if}
                </div>
                <div class="credentials-scroll">
                {#if loadingInstallationHistory}
                  <p class="text-muted">Loading…</p>
                {:else if !installationHistory.length}
                  <p class="text-muted">No installation history. Parameters used for past installations will appear here.</p>
                {:else}
                  {@const mostRecent = installationHistory[0]}
                  {@const olderHistory = installationHistory.slice(1)}
                  <div class="installation-most-recent">
                    <div class="installation-credentials-item installation-credentials-item-prominent">
                      <div class="installation-credentials-header">
                        <span class="installation-credentials-title">{mostRecent.os_name || mostRecent.template_id || 'Installation'}</span>
                        <span class="installation-credentials-badge">Most recent</span>
                      </div>
                      <span class="installation-credentials-meta">
                        {mostRecent.created_at ? new Date(mostRecent.created_at).toLocaleString() : '—'}
                        · {mostRecent.status?.replace('_', ' ') || '—'}
                      </span>
                      {#if mostRecent.template_parameters && Object.keys(mostRecent.template_parameters).length > 0}
                        <dl class="installation-credentials-params">
                          {#each Object.entries(mostRecent.template_parameters) as [paramKey, paramVal]}
                            <div class="param-row">
                              <dt>{paramKey}</dt>
                              <dd>{typeof paramVal === 'object' ? JSON.stringify(paramVal) : String(paramVal)}</dd>
                            </div>
                          {/each}
                        </dl>
                      {:else}
                        <p class="installation-credentials-empty">No parameters recorded</p>
                      {/if}
                    </div>
                  </div>
                  {#if olderHistory.length > 0}
                    <div class="installation-history-scroll-wrap">
                      <p class="installation-history-label">Older history</p>
                      <ul class="installation-credentials-list installation-credentials-history">
                      {#each olderHistory as entry}
                        <li class="installation-credentials-item">
                          <div class="installation-credentials-header">
                            <span class="installation-credentials-title">{entry.os_name || entry.template_id || 'Installation'}</span>
                            <span class="installation-credentials-meta">
                              {entry.created_at ? new Date(entry.created_at).toLocaleString() : '—'}
                              · {entry.status?.replace('_', ' ') || '—'}
                            </span>
                          </div>
                          {#if entry.template_parameters && Object.keys(entry.template_parameters).length > 0}
                            <dl class="installation-credentials-params">
                              {#each Object.entries(entry.template_parameters) as [paramKey, paramVal]}
                                <div class="param-row">
                                  <dt>{paramKey}</dt>
                                  <dd>{typeof paramVal === 'object' ? JSON.stringify(paramVal) : String(paramVal)}</dd>
                                </div>
                              {/each}
                            </dl>
                          {:else}
                            <p class="installation-credentials-empty">No parameters recorded</p>
                          {/if}
                        </li>
                      {/each}
                    </ul>
                    </div>
                  {/if}
                {/if}
                </div>
              </section>
            </div>
          {:else}
            <p class="bottom-placeholder">—</p>
          {/if}
        </div>
      </div>
    </div>
  </div>

  {#if showEditModal && server}
    <Servers embeddedEditServer={server} onEditComplete={handleEditComplete} />
  {/if}

  <!-- Preview image picker (asset manager) modal -->
  {#if showPreviewAssetPicker}
    <div class="modal-overlay" on:click={closePreviewAssetPicker} role="presentation">
      <div class="modal preview-asset-picker-modal" on:click|stopPropagation role="dialog" aria-labelledby="preview-picker-title">
        <div class="modal-header">
          <h3 id="preview-picker-title">Select preview image</h3>
          <button type="button" class="modal-close" on:click={closePreviewAssetPicker} aria-label="Close">×</button>
        </div>
        <div class="modal-body">
          <p class="text-muted" style="margin-bottom: 12px;">Choose an image from the asset manager. Upload images in Admin → Asset Manager with label "Server preview" (or use any image).</p>
          {#if loadingPreviewAssets}
            <p class="text-muted">Loading assets…</p>
          {:else if previewAssets.length === 0}
            <p class="text-muted">No images in asset manager. Upload some in Admin → Asset Manager first.</p>
          {:else}
            <div class="preview-asset-grid">
              {#each previewAssets as asset}
                <button
                  type="button"
                  class="preview-asset-thumb"
                  class:selected={server?.preview_asset_id === asset.id}
                  on:click={() => selectPreviewAsset(asset.id)}
                  disabled={savingPreviewAsset}
                  title={asset.filename}
                >
                  <img src={getAssetFileUrl(asset.id)} alt="" />
                  <span class="preview-asset-filename">{asset.filename}</span>
                </button>
              {/each}
            </div>
          {/if}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={clearPreviewAsset} disabled={savingPreviewAsset || !server?.preview_asset_id}>
            Clear image
          </button>
          <button type="button" class="btn-secondary" on:click={closePreviewAssetPicker} disabled={savingPreviewAsset}>Cancel</button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Connect server port to switch port modal -->
  {#if connectModalPort}
    <div class="connect-modal-backdrop" role="presentation" on:click={closeConnectModal} on:keydown={(e) => e.key === 'Escape' && closeConnectModal()}>
      <div class="connect-modal-box" role="dialog" aria-labelledby="connect-modal-title" on:click|stopPropagation>
        <h2 id="connect-modal-title">Connect {connectModalPort.name} to switch port</h2>
        {#if connectError}
          <div class="connect-modal-error">{connectError}</div>
        {/if}
        <div class="form-group">
          <label for="connect-switch">Switch</label>
          <select id="connect-switch" bind:value={selectedSwitchId} on:change={onSwitchChange} disabled={connecting}>
            <option value={null}>Select a switch…</option>
            {#each switches as s}
              <option value={s.id}>{s.name}</option>
            {/each}
          </select>
        </div>
        {#if selectedSwitchId}
          <div class="form-group">
            <label for="connect-port">Switch port</label>
            {#if loadingSwitchPorts}
              <p class="text-muted">Loading ports…</p>
            {:else}
              <select id="connect-port" bind:value={selectedSwitchPortId} disabled={connecting}>
                <option value={null}>Select a port…</option>
                {#each availableSwitchPorts as p}
                  <option value={p.id}>{p.name}</option>
                {/each}
              </select>
              {#if availableSwitchPorts.length === 0 && switchPorts.length > 0}
                <p class="text-muted">All ports on this switch are already connected.</p>
              {/if}
            {/if}
          </div>
        {/if}
        <div class="connect-modal-actions">
          <button type="button" class="btn-secondary" on:click={closeConnectModal} disabled={connecting}>Cancel</button>
          <button type="button" class="btn-primary" on:click={handleConnect} disabled={connecting || !selectedSwitchPortId}>
            {connecting ? 'Connecting…' : 'Connect'}
          </button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Edit network config modal -->
  {#if showNetworkEditModal}
    <div class="modal-overlay" on:click={closeNetworkEditModal}>
      <div class="modal network-edit-modal" on:click|stopPropagation>
        <div class="modal-header">
          <h3>Edit network config</h3>
          <button type="button" class="modal-close" on:click={closeNetworkEditModal} aria-label="Close">×</button>
        </div>
        <div class="modal-body">
          {#if networkEditError}
            <div class="connect-modal-error" style="margin-bottom: 12px;">{networkEditError}</div>
          {/if}
          <div class="network-edit-toolbar">
            <button type="button" class="btn-secondary btn-sm" on:click={addNetworkEditPort}>Add port</button>
          </div>
          {#if networkEditPorts.length === 0}
            <p class="text-muted">No ports yet. Click "Add port" to add one.</p>
          {:else}
            <div class="network-edit-ports">
              {#each networkEditPorts as port, i}
                <div class="network-edit-port-block">
                  <div class="network-edit-port-header">
                    <h4 class="network-edit-port-title">Port {i + 1}: {port.name || 'New port'}</h4>
                    <button type="button" class="btn-remove-port" on:click={() => removeNetworkEditPort(i)} title="Remove port">×</button>
                  </div>
                  <div class="network-edit-fields">
                    <div class="form-group">
                      <label>Name</label>
                      <input type="text" bind:value={port.name} placeholder="e.g. eth0" />
                    </div>
                    <div class="form-group">
                      <label>MAC address</label>
                      <input type="text" bind:value={port.mac_address} placeholder="XX:XX:XX:XX:XX:XX" />
                    </div>
                    <div class="form-group">
                      <label>Speed (Mbps)</label>
                      <input type="number" bind:value={port.speed_mbps} min="1" />
                    </div>
                    <div class="form-group">
                      <label>LAG group</label>
                      <input type="text" bind:value={port.lag_group} placeholder="e.g. bond0" />
                    </div>
                    <div class="form-group checkbox-group">
                      <label><input type="checkbox" bind:checked={port.monitor_bandwidth} /> Monitor bandwidth</label>
                    </div>
                    <div class="form-group checkbox-group">
                      <label><input type="checkbox" bind:checked={port.pxe_boot} /> PXE boot port</label>
                    </div>
                    {#if port.pxe_boot}
                      <div class="form-group full-width">
                        <label>PXE IP</label>
                        <input type="text" bind:value={port.pxe_ip} placeholder="e.g. 192.168.1.100" />
                      </div>
                    {/if}
                    <div class="form-group full-width">
                      <label>Description</label>
                      <input type="text" bind:value={port.description} placeholder="Optional" />
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeNetworkEditModal} disabled={savingNetworkConfig}>Cancel</button>
          <button type="button" class="btn-primary" on:click={saveNetworkConfig} disabled={savingNetworkConfig}>
            {savingNetworkConfig ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Edit disks modal -->
  {#if showDiskEditModal}
    <div class="modal-overlay" on:click={closeDiskEditModal}>
      <div class="modal disk-edit-modal" on:click|stopPropagation>
        <div class="modal-header">
          <h3>Edit disks</h3>
          <button type="button" class="modal-close" on:click={closeDiskEditModal} aria-label="Close">×</button>
        </div>
        <div class="modal-body">
          {#if diskEditError}
            <div class="connect-modal-error" style="margin-bottom: 12px;">{diskEditError}</div>
          {/if}
          <div class="disk-edit-toolbar">
            <button type="button" class="btn-secondary btn-sm" on:click={addDiskEdit}>Add disk</button>
          </div>
          {#if diskEditList.length === 0}
            <p class="text-muted">No disks yet. Click "Add disk" to add one.</p>
          {:else}
            <div class="disk-edit-list">
              {#each diskEditList as disk, i}
                <div class="disk-edit-block">
                  <div class="disk-edit-block-header">
                    <h4 class="disk-edit-block-title">Disk {i + 1}</h4>
                    <button type="button" class="btn-remove-port" on:click={() => removeDiskEdit(i)} title="Remove disk">×</button>
                  </div>
                  <div class="disk-edit-fields">
                    <div class="form-group">
                      <label>Type</label>
                      <select bind:value={disk.type}>
                        <option value="ssd">SSD</option>
                        <option value="hdd">HDD</option>
                      </select>
                    </div>
                    <div class="form-group">
                      <label>Capacity (GB)</label>
                      <input type="number" bind:value={disk.capacity_gb} min="0" />
                    </div>
                    <div class="form-group full-width">
                      <label>Serial number</label>
                      <input type="text" bind:value={disk.serial_number} placeholder="Optional" />
                    </div>
                    <div class="form-group full-width">
                      <label>Description</label>
                      <input type="text" bind:value={disk.description} placeholder="Optional" />
                    </div>
                    <div class="form-group checkbox-group">
                      <label><input type="checkbox" bind:checked={disk.is_os_disk} /> OS disk (installation target)</label>
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeDiskEditModal} disabled={savingDiskEdit}>Cancel</button>
          <button type="button" class="btn-primary" on:click={saveDiskEdit} disabled={savingDiskEdit}>
            {savingDiskEdit ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  {/if}
{/if}
</div>

<style>
  .server-detail-page {
    width: 100%;
    min-width: 0;
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--bg-secondary);
    color: var(--text-primary);
    transition: background-color 0.3s ease, color 0.3s ease;
  }

  .detail-layout {
    display: flex;
    flex: 1;
    min-height: 0;
    width: 100%;
    padding: 0;
    overflow: hidden;
  }

  .left-pane {
    width: 25%;
    min-width: 280px;
    max-width: 380px;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--bg-primary);
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04);
    flex-shrink: 0;
    margin: 16px 0 16px 16px;
    align-self: stretch;
    overflow: hidden;
  }

  .left-pane-top {
    flex-shrink: 0;
    padding: 0 0 16px 0;
    background: linear-gradient(180deg, var(--accent-color) 0%, var(--accent-dark) 100%);
    color: white;
    border-radius: 4px 4px 0 0;
  }

  .server-preview-placeholder-wrap {
    position: relative;
    aspect-ratio: 16 / 10;
    background: rgba(0, 0, 0, 0.15);
    border-radius: 4px 4px 0 0;
    overflow: hidden;
  }

  .server-preview-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  .server-preview-edit-image-btn {
    position: absolute;
    bottom: 8px;
    right: 8px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px;
    font-size: 11px;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.95);
    background: rgba(0, 0, 0, 0.5);
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 4px;
    cursor: pointer;
  }

  .server-preview-edit-image-btn:hover {
    background: rgba(0, 0, 0, 0.7);
    border-color: rgba(255, 255, 255, 0.5);
  }

  .server-preview-placeholder {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 16px;
  }

  .preview-icon {
    width: 48px;
    height: 48px;
    opacity: 0.8;
  }

  .preview-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    opacity: 0.9;
  }

  .server-preview-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px 8px;
    min-width: 0;
  }

  .server-preview-title-row .server-preview-title {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    line-height: 1.2;
    text-align: left;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .server-preview-title {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    line-height: 1.3;
  }

  .server-preview-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }

  .power-pill {
    display: inline-flex;
    align-items: center;
    padding: 3px 10px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    line-height: 1;
    margin-top: -2px;
  }

  .power-pill.power-on {
    background: #0d7d3d;
    color: #fff;
  }

  .power-pill.power-off {
    background: #c00;
    color: #fff;
  }

  .power-pill.power-unknown {
    background: rgba(255, 255, 255, 0.35);
    color: rgba(255, 255, 255, 0.95);
  }

  .left-pane-edit-btn {
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.95);
    background: rgba(255, 255, 255, 0.2);
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 4px;
    cursor: pointer;
  }

  .left-pane-edit-btn:hover {
    background: rgba(255, 255, 255, 0.3);
    border-color: rgba(255, 255, 255, 0.6);
  }

  .server-preview-details {
    margin: 0;
    padding: 8px 16px 12px;
    font-size: 13px;
  }

  .server-preview-details .detail-row {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: baseline;
    gap: 12px;
    padding: 6px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.15);
  }

  .server-preview-details .detail-row:last-child {
    border-bottom: none;
  }

  .server-preview-details dt {
    margin: 0;
    font-weight: 500;
    opacity: 0.9;
    min-width: 0;
  }

  .server-preview-details dd {
    margin: 0;
    font-weight: 600;
    text-align: right;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .left-pane-tabs-wrap {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--bg-primary);
  }

  .left-pane-tabs {
    display: flex;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border-color);
    padding: 0 4px 0 0;
    background: var(--bg-secondary);
  }

  .left-pane-tab {
    flex: 1;
    min-width: 0;
    padding: 8px 8px;
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    transition: color 0.2s, border-color 0.2s, background 0.2s;
  }

  .left-pane-tab:hover {
    color: var(--text-primary);
  }

  .left-pane-tab.active {
    color: var(--accent-color);
    border-bottom-color: var(--accent-color);
    background: var(--bg-primary);
  }

  .left-pane-tab-content {
    flex: 1;
    overflow-y: auto;
    padding: 14px 16px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .tab-placeholder {
    margin: 0;
    font-style: italic;
  }

  .general-pane {
    display: flex;
    flex-direction: column;
    gap: 16px;
    color: var(--text-primary);
  }

  .general-details {
    padding: 0;
  }

  .general-section-title {
    margin: 0 0 10px 0;
    font-size: 13px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .general-details-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .general-detail-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border-color);
  }

  .general-detail-row:last-child {
    border-bottom: none;
  }

  .general-details-list dt {
    margin: 0;
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .general-details-list dd {
    margin: 0;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-primary);
    text-align: right;
    word-break: break-word;
  }

  .rack-hint {
    font-size: 11px;
    font-weight: 400;
    color: var(--text-muted, #888);
  }

  .general-notes,
  .general-comments {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .general-notes-label {
    font-size: 12px;
    font-weight: 600;
    color: #b45309;
  }

  .general-notes-input {
    width: 100%;
    min-height: 60px;
    padding: 8px 10px;
    font-size: 13px;
    font-family: inherit;
    border: 1px solid #d97706;
    border-radius: 4px;
    background: var(--bg-primary);
    color: var(--text-primary);
    resize: vertical;
  }

  .general-notes-input:focus {
    outline: none;
    border-color: #b45309;
    box-shadow: 0 0 0 2px rgba(217, 119, 6, 0.2);
  }

  .general-comments-label {
    font-size: 12px;
    font-weight: 600;
    color: #047857;
  }

  .general-comments-input {
    width: 100%;
    min-height: 60px;
    padding: 8px 10px;
    font-size: 13px;
    font-family: inherit;
    border: 1px solid #059669;
    border-radius: 4px;
    background: var(--bg-primary);
    color: var(--text-primary);
    resize: vertical;
  }

  .general-comments-input:focus {
    outline: none;
    border-color: #047857;
    box-shadow: 0 0 0 2px rgba(4, 120, 87, 0.2);
  }

  .general-save-hint {
    font-size: 11px;
    color: var(--text-secondary);
    font-style: italic;
  }

  .content-body {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 16px;
    gap: 16px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    transition: background-color 0.3s ease, color 0.3s ease;
    overflow: hidden;
  }

  .right-top {
    flex: 0 0 42%;
    min-height: 0;
    max-height: 42%;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    overflow: hidden;
  }

  .right-panel {
    min-height: 0;
    overflow: hidden;
  }

  .right-bottom {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg-primary);
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04);
  }

  .bottom-tabs {
    display: flex;
    flex-shrink: 0;
    gap: 0;
    border-bottom: 1px solid var(--border-color);
    padding: 0 4px 0 0;
    background: var(--bg-secondary);
  }

  .bottom-tab {
    flex: 1;
    min-width: 0;
    padding: 10px 12px;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-tertiary);
    background: var(--bg-tertiary);
    border: none;
    border-bottom: 2px solid transparent;
    border-right: 1px solid var(--border-color);
    cursor: pointer;
    margin-bottom: -1px;
    transition: color 0.2s, border-color 0.2s, background 0.2s;
  }

  .bottom-tab:last-of-type {
    border-right: none;
  }

  .bottom-tab:hover {
    color: var(--text-primary);
    background: var(--bg-secondary);
  }

  .bottom-tab.active {
    color: var(--accent-color);
    border-bottom-color: var(--accent-color);
    background: var(--bg-primary);
    font-weight: 600;
  }

  .bottom-tab-content {
    flex: 1;
    overflow-y: auto;
    padding: 16px 18px;
    min-height: 0;
  }

  .bottom-tab-content.credentials-tab-active {
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 16px 18px;
  }

  .bottom-placeholder {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
    font-style: italic;
  }

  .network-tab-content {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .network-tab-toolbar {
    flex-shrink: 0;
  }

  .network-ports-wrap {
    overflow-x: auto;
    min-height: 0;
  }

  .disks-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .panel-toolbar {
    flex-shrink: 0;
  }

  .disks-table-wrap {
    overflow-x: auto;
    min-height: 0;
  }

  .network-ports-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  .network-ports-table th {
    text-align: left;
    padding: 8px 10px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
  }

  .network-ports-table td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
  }

  .network-ports-table .mono {
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .network-ports-table .text-muted {
    color: var(--text-tertiary);
    font-style: italic;
  }

  .btn-connect {
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    border-radius: 4px;
    border: 1px solid var(--accent-color);
    background: var(--accent-color);
    color: white;
    cursor: pointer;
  }

  .btn-connect:hover {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .btn-disconnect {
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    border-radius: 4px;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .btn-disconnect:hover {
    background: var(--danger-bg);
    color: var(--danger-text);
    border-color: var(--danger-color);
  }

  .boot-ops-management {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .boot-task-banner-inline {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    flex-wrap: wrap;
  }

  .boot-task-desc-inline {
    flex: 1;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .boot-ops-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 14px;
  }

  .boot-op-card {
    min-height: 88px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    background: var(--bg-secondary);
    border-radius: 4px;
  }

  .boot-op-card-install {
    min-height: 88px;
    grid-column: span 2;
  }

  .boot-op-label {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-primary);
  }

  .boot-op-hint {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .boot-op-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .boot-op-row .btn-sm {
    height: 34px;
    padding: 0 14px;
    box-sizing: border-box;
  }

  .boot-op-select {
    flex: 1;
    min-width: 0;
    height: 34px;
    padding: 0 32px 0 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    font-family: inherit;
    background-color: var(--bg-primary);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    color: var(--text-primary);
    appearance: none;
    cursor: pointer;
  }

  :global([data-theme="dark"]) .boot-op-select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  .boot-op-params-slot {
    min-height: 180px;
    max-height: 180px;
    overflow-y: auto;
    margin-top: 4px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
  }

  .boot-op-params-empty {
    font-size: 12px;
    color: var(--text-tertiary);
    font-style: italic;
  }

  .boot-op-params-inner {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .boot-op-param-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .boot-op-param-row > label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .boot-op-param-row input,
  .boot-op-param-row select {
    height: 34px;
    padding: 0 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    font-family: inherit;
    background: var(--bg-primary);
    color: var(--text-primary);
    box-sizing: border-box;
  }

  .boot-op-param-row select {
    padding-right: 32px;
    appearance: none;
    background-color: var(--bg-primary);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }

  :global([data-theme="dark"]) .boot-op-param-row select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  .boot-op-param-input-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }

  .boot-op-param-input-row input {
    flex: 1;
    min-width: 0;
  }

  .boot-op-param-actions {
    display: flex;
    gap: 0;
    flex-shrink: 0;
  }

  .btn-icon-tool-sm {
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--bg-primary);
    color: var(--text-secondary);
    cursor: pointer;
    transition: background 0.2s, color 0.2s, border-color 0.2s;
  }

  .btn-icon-tool-sm + .btn-icon-tool-sm {
    margin-left: -1px;
  }

  .btn-icon-tool-sm:first-child {
    border-radius: 4px 0 0 4px;
  }

  .btn-icon-tool-sm:last-child {
    border-radius: 0 4px 4px 0;
  }

  .btn-icon-tool-sm:only-child {
    border-radius: 4px;
  }

  .btn-icon-tool-sm:hover:not(:disabled) {
    background: var(--accent-color);
    color: white;
    border-color: var(--accent-color);
    z-index: 1;
  }

  .btn-icon-tool-sm:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-icon-tool-sm svg {
    width: 16px;
    height: 16px;
  }

  .boot-op-checkbox {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
  }

  .right-panel {
    background: var(--bg-primary);
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04);
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .panel-title {
    margin: 0 0 10px 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .panel-empty {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
    font-style: italic;
  }

  .traffic-graph-wrap {
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .traffic-graph-wrap :global(.traffic-graph) {
    flex-shrink: 0;
  }

  .activity-panel {
    overflow: hidden;
  }

  .activity-list {
    margin: 0;
    padding: 0 14px 12px 0;
    list-style: none;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  .activity-item {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border-color);
    font-size: 13px;
    align-items: baseline;
  }

  .activity-item:last-child {
    border-bottom: none;
  }

  .activity-time {
    color: var(--text-tertiary);
    font-size: 12px;
    white-space: nowrap;
  }

  .activity-desc {
    color: var(--text-primary);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .activity-status {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .activity-status.completed {
    color: var(--success-text);
  }

  .activity-status.failed {
    color: var(--danger-text);
  }

  .activity-status.pending {
    color: var(--text-secondary);
  }

  .top-section {
    display: grid;
    grid-template-columns: 1fr minmax(260px, 340px);
    gap: 20px;
    margin-bottom: 20px;
  }
  @media (max-width: 900px) {
    .top-section {
      grid-template-columns: 1fr;
    }
  }

  .tab-bar {
    display: flex;
    gap: 0;
    margin-bottom: 0;
    margin-top: 0;
    border: 1px solid var(--border-color);
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    background: var(--bg-primary);
    padding: 0 18px;
  }
  .tab-btn {
    padding: 10px 18px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-secondary);
    font-weight: 500;
    font-size: 13px;
    cursor: pointer;
    margin-bottom: -1px;
  }
  .tab-btn:hover {
    color: var(--text-primary);
  }
  .tab-btn.active {
    color: var(--accent-color);
    border-bottom-color: var(--accent-color);
  }
  /* First card after tabs: no top radius, shares border with tab bar so no gap */
  .tab-bar + .card {
    border-top-left-radius: 0;
    border-top-right-radius: 0;
    margin-top: 0;
  }

  .boot-actions-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }
  .boot-action {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    background: var(--bg-secondary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }
  .boot-action-label {
    font-weight: 600;
    font-size: 12px;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .boot-action-hint {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .boot-action-controls {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .boot-action-controls select {
    flex: 1;
    min-width: 120px;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    font-size: 13px;
  }
  .boot-task-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 14px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    flex-wrap: wrap;
  }
  .boot-task-banner-left {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }
  .boot-task-type { font-weight: 600; }
  .boot-task-desc { color: var(--text-secondary); font-size: 13px; }
  .boot-task-meta { font-size: 12px; color: var(--text-tertiary); }
  .boot-action-install-layout { display: flex; flex-direction: column; gap: 12px; width: 100%; }
  .boot-action-install-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .boot-action-install-row select { flex: 1; min-width: 180px; }
  .template-params-section { margin-top: 4px; }
  .template-params-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-secondary);
    border-radius: 6px;
    cursor: pointer;
    list-style: none;
    transition: background 0.2s, color 0.2s;
  }
  .template-params-summary::-webkit-details-marker { display: none; }
  .template-params-summary:hover { color: var(--text-primary); background: var(--bg-tertiary); }
  .params-chevron { flex-shrink: 0; transition: transform 0.2s; }
  .template-params-section[open] .params-chevron { transform: rotate(180deg); }
  .template-params-inline { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; padding: 12px; background: var(--bg-secondary); border-radius: 8px; }
  .param-row { display: flex; flex-direction: column; gap: 6px; }
  .param-row label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .param-input-row {
    display: flex;
    gap: 8px;
    align-items: center;
    min-width: 0;
  }
  .param-input-row input {
    flex: 1;
    min-width: 0;
  }
  .btn-icon-tool {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 6px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.2s;
  }
  .btn-icon-tool:hover:not(:disabled) { background: var(--accent-color); color: white; border-color: var(--accent-color); }
  .btn-icon-tool:disabled { opacity: 0.5; cursor: not-allowed; }
  .param-checkbox-label { display: flex; align-items: center; gap: 8px; cursor: pointer; font-weight: 500; }
  .param-row input[type="text"],
  .param-row input[type="number"],
  .param-row select {
    padding: 8px 12px;
    border: 2px solid var(--border-color);
    border-radius: 6px;
    font-size: 13px;
    background: var(--bg-primary);
    color: var(--text-primary);
  }
  .param-row input:focus, .param-row select:focus {
    outline: none;
    border-color: var(--accent-color);
  }
  .btn-icon-sm { padding: 4px 8px; font-size: 12px; border-radius: 4px; }
  .btn-sm { padding: 6px 12px; font-size: 13px; }

  .bandwidth-toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .select-sm { padding: 6px 10px; border-radius: 6px; font-size: 13px; }
  .traffic-port-section { margin-bottom: 24px; }
  .traffic-port-section:last-child { margin-bottom: 0; }
  .traffic-port-label { font-size: 13px; margin: 0 0 12px 0; color: var(--text-secondary); }
  .traffic-details { margin-top: 12px; }
  .data-table.compact { font-size: 13px; }
  .credentials-list { display: flex; flex-direction: column; gap: 12px; }
  .credential-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 8px 0; border-bottom: 1px solid var(--border-color); }
  .credential-row:last-child { border-bottom: none; }
  .credential-key { font-size: 12px; text-transform: capitalize; color: var(--text-secondary); }
  .credential-value { display: flex; align-items: center; gap: 8px; }
  .credential-value code { font-size: 13px; }
  .credential-meta .credential-value { color: var(--text-tertiary); font-size: 12px; }

  .loading, .error {
    padding: 32px;
    text-align: center;
    font-size: 16px;
  }

  .error {
    color: var(--danger-color);
  }

  .card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin-bottom: 16px;
    overflow: hidden;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .card-header {
    padding: 12px 18px;
    border-bottom: 1px solid var(--border-color);
    min-height: 42px;
    box-sizing: border-box;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg-primary);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .card-header.collapsible {
    cursor: pointer;
    user-select: none;
  }

  .card-header.collapsible:hover {
    background: var(--bg-tertiary);
  }

  .collapse-icon {
    transition: transform 0.2s ease;
    color: var(--text-secondary);
  }

  .collapse-icon.expanded {
    transform: rotate(180deg);
  }

  .card-header h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .card-header h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }


  .refresh-button {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    background: var(--accent-color);
    border: 1px solid var(--accent-color);
    border-radius: 6px;
    color: white;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.2s ease, border-color 0.2s ease;
    white-space: nowrap;
  }

  .refresh-button:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .refresh-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .refresh-icon {
    width: 18px;
    height: 18px;
    transition: transform 0.3s ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  .card-actions {
    display: flex;
    gap: 8px;
  }

  .card-body {
    padding: 16px 18px;
  }

  .server-info-body {
    padding: 14px 18px;
  }

  /* Main server info panel: left accent for hierarchy */
  .top-section .card:first-child {
    border-left: 3px solid var(--accent-color);
  }

  .server-controls-card {
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .server-controls-card :global(.server-controls-panel) {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .info-grid-two-col {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    column-gap: 20px;
    row-gap: 10px;
    margin: 0;
    font-size: 13px;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .info-item-full {
    grid-column: 1 / -1;
  }

  .info-item dt,
  .info-description-block dt {
    margin: 0;
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .info-item dd,
  .info-description-block dd {
    margin: 0;
    color: var(--text-primary);
    font-size: 13px;
  }

  .info-description-block {
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .info-description-block dd {
    white-space: pre-wrap;
    word-break: break-word;
  }

  .info-link {
    color: var(--accent-color);
    text-decoration: none;
  }

  .info-link:hover {
    text-decoration: underline;
  }

  .groups-inline {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .group-chip {
    display: inline-block;
    padding: 2px 8px;
    background: var(--accent-color);
    color: white;
    border-radius: 6px;
    font-size: 12px;
    text-decoration: none;
  }

  .group-chip:hover {
    opacity: 0.9;
  }

  .btn-link {
    background: none;
    border: none;
    color: var(--accent-color);
    cursor: pointer;
    font-size: 13px;
    padding: 0 4px;
  }

  .btn-link:hover {
    text-decoration: underline;
  }

  .btn-link-sm {
    font-size: 12px;
  }

  .power-state-inline {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .power-state-inline.power-on {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .power-state-inline.power-off {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .power-state-inline.power-unknown {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .btn-refresh-power {
    padding: 4px;
    flex-shrink: 0;
  }

  .status-badge, .status-badge-sm {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .status-badge.enabled {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.disabled {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .power-control-section {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .power-state-display {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .power-state-display label {
    font-weight: 600;
    color: var(--text-primary);
  }

  .power-state-badge {
    padding: 6px 16px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .power-state-badge.power-on {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .power-state-badge.power-off {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .power-state-badge.power-unknown {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .power-control-buttons {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .btn-power {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    font-size: 13px;
    flex: 1;
  }

  .btn-power:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-power-on {
    background: var(--success-color);
    color: white;
  }

  .btn-power-on:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-power-off {
    background: var(--danger-color);
    color: white;
  }

  .btn-power-off:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-power-reset {
    background: var(--warning-color);
    color: white;
  }

  .btn-power-reset:hover:not(:disabled) {
    filter: brightness(1.1);
  }


  .btn-icon {
    width: 18px;
    height: 18px;
  }

  .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    border-radius: 6px;
    transition: all 0.2s ease;
    color: var(--text-primary);
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
  }

  .btn-small {
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
  }
  
  .btn-secondary.btn-small {
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
    box-shadow: var(--shadow-sm);
  }
  
  .btn-secondary.btn-small:hover:not(:disabled) {
    background: var(--accent-color);
    border-color: var(--accent-color);
    color: white;
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .capabilities-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .capability-item {
    display: flex;
  }

  .capability-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    border: 1px solid var(--border-color);
    transition: all 0.2s ease;
  }

  .capability-badge.tested {
    background: var(--success-bg);
    color: var(--success-text);
    border-color: var(--success-color);
  }
  
  .capability-badge:not(.tested) {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }
  
  .capability-badge:hover {
    transform: translateY(-1px);
    box-shadow: var(--shadow-sm);
  }

  .capability-icon {
    width: 14px;
    height: 14px;
  }

  .test-logs-section {
    margin-top: 12px;
    padding-top: 12px;
    border-color: var(--border-color);
  }

  .test-logs-content {
    margin-top: 8px;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 300px;
    overflow-y: auto;
  }

  .no-capabilities {
    padding: 20px;
    text-align: center;
    color: var(--text-secondary);
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
  }

  .hardware-section {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .hardware-subsection {
    border-color: var(--border-color);
    padding-bottom: 12px;
  }

  .hardware-subsection:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }

  .subsection-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .subsection-header h4 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .hardware-subsection h4 {
    margin: 0 0 12px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .subsection-content {
    font-size: 13px;
  }

  .data-table {
    font-size: 13px;
  }

  .data-table.compact-table th {
    text-align: left;
    padding: 8px;
    font-weight: 600;
    color: var(--text-primary);
    border-color: var(--border-color);
    font-size: 12px;
  }

  .data-table.compact-table td {
    padding: 8px;
    border-color: var(--border-color);
    color: var(--text-primary);
    font-size: 13px;
  }

  .data-table th {
    text-align: left;
    padding: 8px 12px;
    font-weight: 600;
    color: var(--text-secondary);
    border-color: var(--border-color);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    background: var(--bg-secondary);
  }

  .data-table td {
    padding: 8px 12px;
    border-color: var(--border-color);
    color: var(--text-primary);
    font-size: 13px;
  }

  .data-table tbody tr:hover {
    background: var(--bg-tertiary);
  }

  .table-scroll-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    margin: 0 -1px;
  }

  .data-table-ports {
    min-width: 920px;
    width: max-content;
  }

  .data-table-ports th,
  .data-table-ports td {
    white-space: nowrap;
    padding: 10px 14px;
  }

  .data-table-ports th:first-child,
  .data-table-ports td:first-child {
    min-width: 72px;
    padding-left: 14px;
  }

  .connected-to-empty {
    color: var(--text-secondary);
    font-style: italic;
  }

  .link {
    color: var(--accent-light);
    text-decoration: none;
    font-weight: 500;
  }

  .link:hover {
    text-decoration: underline;
    color: var(--accent-light);
    filter: brightness(1.15);
  }

  .btn-primary {
    padding: 8px 16px;
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
    font-size: 13px;
    transition: background 0.2s ease, border-color 0.2s ease;
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .btn-secondary {
    padding: 8px 16px;
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
    font-size: 13px;
    transition: background 0.2s ease, border-color 0.2s ease;
  }
  
  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-tertiary);
    border-color: var(--border-color);
  }

  .btn-text {
    background: none;
    border: none;
    color: var(--accent-color);
    cursor: pointer;
    font-weight: 600;
    padding: 4px 8px;
    border-radius: 4px;
    transition: background 0.2s ease;
  }

  .btn-text:hover {
    background: var(--info-bg);
  }

  .boot-task-info {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .boot-task-status {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  
  .boot-task-status strong {
    color: var(--text-primary);
    font-weight: 700;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
    color: var(--text-primary);
  }
  
  .boot-task-details div span {
    color: var(--text-primary);
    font-weight: 500;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: var(--text-primary);
    font-weight: 700;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-badge.in-progress {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .status-badge.completed {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.failed {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .boot-options-section {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 24px;
    align-items: stretch;
  }

  @media (max-width: 1200px) {
    .boot-options-section {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (max-width: 600px) {
    .boot-options-section {
      grid-template-columns: 1fr;
    }
  }

  .boot-option-group,
  .boot-option {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 100%;
  }

  .boot-option-group .boot-option-actions,
  .boot-option .boot-option-actions {
    margin-top: auto;
    padding-top: 8px;
  }

  .boot-option-group h4,
  .boot-option h4 {
    margin: 0 0 6px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .boot-option-description {
    color: var(--text-secondary);
    font-size: 12px;
    margin: 0 0 10px 0;
  }

  .boot-option-description-small {
    font-size: 0.9em;
    margin-top: 0.5em;
  }

  .no-isos {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label,
  .form-group label {
    font-weight: 700;
    color: var(--text-primary);
    font-size: 14px;
    margin-bottom: 8px;
    display: block;
  }

  .iso-selector select,
  .form-group select {
    padding: 10px 12px;
    padding-right: 36px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    font-weight: 500;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s ease;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23{encodeURIComponent(getComputedStyle(document.documentElement).getPropertyValue('--text-primary').replace('#', ''))}' d='M6 9L1 4h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 12px center;
    background-size: 12px;
    box-shadow: var(--shadow-sm);
  }
  
  .iso-selector select option,
  .form-group select option {
    background: var(--bg-primary);
    color: var(--text-primary);
    padding: 8px;
  }
  
  .iso-selector select:disabled,
  .form-group select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--bg-tertiary);
  }

  .iso-selector select:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring);
  }
  
  .iso-selector select:hover:not(:disabled),
  .form-group select:hover:not(:disabled) {
    border-color: var(--accent-color);
  }

  .iso-info {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .template-info {
    margin: 12px 0;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .template-description {
    color: var(--text-secondary);
    margin-bottom: 12px;
    font-size: 12px;
  }

  .template-parameters-form {
    margin-top: 16px;
  }

  .template-parameters-form h5 {
    margin: 0 0 12px 0;
    color: var(--text-primary);
    font-size: 14px;
    font-weight: 600;
  }

  .template-parameters-form .form-group {
    margin-bottom: 12px;
  }

  .template-parameters-form label {
    display: block;
    margin-bottom: 4px;
    font-weight: 600;
    color: var(--text-primary);
    font-size: 13px;
  }

  .template-parameters-form .required {
    color: var(--danger-color);
  }

  .template-parameters-form input[type="text"],
  .template-parameters-form input[type="password"],
  .template-parameters-form input[type="number"],
  .template-parameters-form select {
    width: 100%;
    padding: 8px 10px;
    padding-right: 32px;
    border: 2px solid var(--border-color);
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s ease;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath fill='%23cbd5e1' d='M5 7.5L1 3.5h8z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    background-size: 10px;
    box-shadow: var(--shadow-sm);
  }
  
  :global([data-theme="light"]) .template-parameters-form select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath fill='%23475569' d='M5 7.5L1 3.5h8z'/%3E%3C/svg%3E");
  }
  
  .template-parameters-form select option {
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .template-parameters-form input:focus,
  .template-parameters-form select:focus {
    outline: none;
    border-color: var(--accent-color);
  }

  .template-parameters-form .checkbox-label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    font-weight: normal;
  }

  .template-parameters-form .checkbox-label input[type="checkbox"] {
    width: auto;
    cursor: pointer;
  }

  .field-help {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }


  .credentials-info {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .credential-item {
    padding: 10px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .credential-item strong {
    display: block;
    margin-bottom: 4px;
    color: var(--text-primary);
    font-size: 13px;
  }

  .credential-item code {
    word-break: break-all;
    font-size: 13px;
  }

  .credentials-panel {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }

  .credentials-left,
  .credentials-right {
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 16px;
    background: var(--bg-primary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
    overflow: hidden;
  }

  .credentials-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }

  .credentials-section {
    flex-shrink: 0;
    padding: 16px;
    background: var(--bg-primary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .credentials-section-title {
    margin: 0 0 12px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .credentials-left .credentials-section-title,
  .credentials-right .credentials-section-title {
    flex-shrink: 0;
  }

  .credentials-section-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-shrink: 0;
    margin-bottom: 4px;
  }

  .credentials-section-header-row .credentials-section-title {
    margin: 0;
  }

  .plugin-config-form {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 16px;
    flex: 1;
    min-height: 0;
    align-content: start;
  }

  .plugin-config-form .form-group {
    margin-bottom: 0;
  }

  .plugin-config-form .form-group-full {
    grid-column: 1 / -1;
  }

  .plugin-config-form label {
    display: block;
    margin-bottom: 4px;
    font-weight: 600;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .plugin-config-form .required {
    color: var(--danger-color, #c00);
  }

  .plugin-config-form input[type="text"],
  .plugin-config-form input[type="number"],
  .plugin-config-form input[type="password"] {
    width: 100%;
    max-width: 100%;
    padding: 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .plugin-config-form .field-help {
    display: block;
    margin-top: 4px;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .plugin-config-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 8px;
  }

  .plugin-config-test-msg {
    padding: 8px 12px;
    margin-bottom: 12px;
    font-size: 13px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    color: var(--text-primary);
  }

  .installation-most-recent {
    flex-shrink: 0;
    margin-bottom: 12px;
  }

  .installation-credentials-item-prominent {
    padding: 14px;
    background: var(--bg-secondary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
    border-left: 4px solid var(--accent-color);
  }

  .installation-credentials-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--accent-color);
    margin-left: auto;
  }

  .installation-history-scroll-wrap {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .installation-history-label {
    margin: 0 0 8px 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .installation-credentials-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .installation-credentials-history {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding-right: 4px;
  }

  .installation-credentials-item {
    padding: 12px;
    background: var(--bg-secondary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .installation-credentials-header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 12px;
    margin-bottom: 8px;
  }

  .installation-credentials-title {
    font-weight: 600;
    font-size: 13px;
    color: var(--text-primary);
  }

  .installation-credentials-meta {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .installation-credentials-params {
    margin: 0;
    font-size: 12px;
    display: grid;
    gap: 4px 16px;
    grid-template-columns: auto 1fr;
  }

  .installation-credentials-params .param-row {
    display: contents;
  }

  .installation-credentials-params dt {
    margin: 0;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .installation-credentials-params dd {
    margin: 0;
    word-break: break-all;
    color: var(--text-primary);
  }

  .installation-credentials-empty {
    margin: 0;
    font-size: 12px;
    color: var(--text-secondary);
    font-style: italic;
  }

  .boot-task-info {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .boot-task-status {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  
  .boot-task-status strong {
    color: var(--text-primary);
    font-weight: 700;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
    color: var(--text-primary);
  }
  
  .boot-task-details div span {
    color: var(--text-primary);
    font-weight: 500;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: var(--text-primary);
    font-weight: 700;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-badge.in-progress {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .status-badge.completed {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.failed {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .no-isos {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label,
  .form-group label {
    font-weight: 700;
    color: var(--text-primary);
    font-size: 14px;
    margin-bottom: 8px;
    display: block;
  }

  .iso-selector select,
  .form-group select {
    padding: 10px 12px;
    padding-right: 36px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    font-weight: 500;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s ease;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23{encodeURIComponent(getComputedStyle(document.documentElement).getPropertyValue('--text-primary').replace('#', ''))}' d='M6 9L1 4h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 12px center;
    background-size: 12px;
    box-shadow: var(--shadow-sm);
  }
  
  .iso-selector select option,
  .form-group select option {
    background: var(--bg-primary);
    color: var(--text-primary);
    padding: 8px;
  }
  
  .iso-selector select:disabled,
  .form-group select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--bg-tertiary);
  }

  .iso-selector select:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring);
  }
  
  .iso-selector select:hover:not(:disabled),
  .form-group select:hover:not(:disabled) {
    border-color: var(--accent-color);
  }

  .iso-info {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .installation-status-error { padding: 8px 12px; margin-bottom: 12px; background: var(--danger-bg); color: var(--danger-text); border-radius: 6px; font-size: 13px; }
  .installation-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; margin-bottom: 8px; }
  .install-status-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-right: 4px; }
  .install-status-saving { font-size: 12px; color: var(--text-secondary); font-style: italic; }
  .installation-status-error { padding: 8px 12px; margin-bottom: 12px; background: var(--danger-bg); color: var(--danger-text); border-radius: 6px; font-size: 13px; }
  .install-status-select { padding: 4px 8px; font-size: 12px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-primary); color: var(--text-primary); min-width: 7rem; }
  .btn-install-set {
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 6px;
    border: 1px solid var(--accent-color);
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: all 0.2s ease;
  }
  .btn-install-set:hover:not(:disabled) {
    background: var(--accent-color);
    color: white;
  }
  .btn-install-set:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .install-status-saving { font-size: 12px; color: var(--text-secondary); font-style: italic; }
  .no-installations {
    color: var(--text-secondary);
    font-size: 13px;
    padding: 12px;
    text-align: center;
  }

  .installation-history-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .installation-item {
    padding: 10px 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .installation-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 14px;
    font-size: 13px;
  }

  .installation-name {
    margin-right: 4px;
  }

  .installation-meta-inline {
    color: var(--text-secondary);
    font-size: 12px;
  }

  /* Edit Server Groups modal */
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 20px;
  }

  .modal-overlay .modal {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 100%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-xl);
    color: var(--text-primary);
  }

  .modal-overlay .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-overlay .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }

  .modal-overlay .modal-body {
    padding: 24px;
  }

  .modal-overlay .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-top: 1px solid var(--border-color);
  }

  .modal-close {
    padding: 0;
    width: 28px;
    height: 28px;
    font-size: 20px;
    line-height: 1;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    border-radius: 4px;
  }

  .modal-close:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .network-edit-modal {
    max-width: 560px;
    max-height: 85vh;
  }

  .network-edit-modal .modal-body {
    max-height: 60vh;
    overflow-y: auto;
  }

  .network-edit-toolbar {
    margin-bottom: 12px;
  }

  .network-edit-toolbar .btn-sm {
    padding: 6px 12px;
    font-size: 12px;
  }

  .network-edit-ports {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .network-edit-port-block {
    padding: 14px;
    background: var(--bg-secondary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .network-edit-port-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 12px;
  }

  .network-edit-port-title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .btn-remove-port {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--bg-primary);
    color: var(--text-secondary);
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
  }

  .btn-remove-port:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .disk-edit-modal {
    max-width: 520px;
    max-height: 85vh;
  }

  .disk-edit-modal .modal-body {
    max-height: 60vh;
    overflow-y: auto;
  }

  .preview-asset-picker-modal {
    max-width: 560px;
    max-height: 85vh;
  }

  .preview-asset-picker-modal .modal-body {
    max-height: 50vh;
    overflow-y: auto;
  }

  .preview-asset-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 12px;
  }

  .preview-asset-thumb {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 0;
    border: 2px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    cursor: pointer;
    overflow: hidden;
    transition: border-color 0.2s, box-shadow 0.2s;
  }

  .preview-asset-thumb:hover {
    border-color: var(--accent-color);
  }

  .preview-asset-thumb.selected {
    border-color: var(--accent-color);
    box-shadow: 0 0 0 2px var(--accent-color);
  }

  .preview-asset-thumb img {
    width: 100%;
    aspect-ratio: 1;
    object-fit: cover;
    display: block;
  }

  .preview-asset-filename {
    padding: 4px 6px;
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
  }

  .disk-edit-toolbar {
    margin-bottom: 12px;
  }

  .disk-edit-toolbar .btn-sm {
    padding: 6px 12px;
    font-size: 12px;
  }

  .disk-edit-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .disk-edit-block {
    padding: 14px;
    background: var(--bg-secondary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .disk-edit-block-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 12px;
  }

  .disk-edit-block-title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .disk-edit-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 16px;
  }

  .disk-edit-fields .form-group {
    margin-bottom: 0;
  }

  .disk-edit-fields .form-group.full-width {
    grid-column: 1 / -1;
  }

  .disk-edit-fields .checkbox-group {
    grid-column: span 2;
  }

  .disk-edit-fields label {
    display: block;
    margin-bottom: 4px;
    font-weight: 600;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .disk-edit-fields input[type="text"],
  .disk-edit-fields input[type="number"],
  .disk-edit-fields select {
    width: 100%;
    padding: 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .disk-edit-fields .checkbox-group label {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 500;
    cursor: pointer;
  }

  .network-edit-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 16px;
  }

  .network-edit-fields .form-group {
    margin-bottom: 0;
  }

  .network-edit-fields .form-group.full-width {
    grid-column: 1 / -1;
  }

  .network-edit-fields .checkbox-group {
    grid-column: span 2;
  }

  .network-edit-fields label {
    display: block;
    margin-bottom: 4px;
    font-weight: 600;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .network-edit-fields input[type="text"],
  .network-edit-fields input[type="number"] {
    width: 100%;
    padding: 6px 10px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .network-edit-fields .checkbox-group label {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 500;
    cursor: pointer;
  }

  .server-group-selection {
    max-height: 400px;
    overflow-y: auto;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 10px;
    margin: 15px 0;
  }

  .checkbox-label {
    display: block;
    padding: 10px;
    border-bottom: 1px solid var(--border-color);
    cursor: pointer;
  }

  .checkbox-label:last-child {
    border-bottom: none;
  }

  .checkbox-label:hover {
    background-color: var(--bg-tertiary);
  }

  .checkbox-label input {
    margin-right: 10px;
  }

  .installation-status-error {
    padding: 8px 12px;
    margin-bottom: 12px;
    background: var(--danger-bg);
    color: var(--danger-text);
    border-radius: 6px;
    font-size: 13px;
  }
  .installation-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
    margin-bottom: 8px;
  }
  .install-status-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-right: 4px; }
  .install-status-saving { font-size: 12px; color: var(--text-secondary); font-style: italic; }
  .installation-logs {
    margin-top: 8px;
  }

  .installation-logs summary {
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    color: var(--accent-color);
    padding: 4px 0;
  }

  .installation-logs-content {
    margin-top: 8px;
    padding: 10px;
    background: #1e293b;
    color: #e2e8f0;
    border-radius: 6px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 400px;
    overflow-y: auto;
  }

  .installation-error {
    margin-top: 8px;
    padding: 8px;
    background: #fee2e2;
    border-radius: 6px;
    font-size: 13px;
    color: #991b1b;
  }

  .btn-small.btn-primary {
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
  }

  .btn-small.btn-primary:hover {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .btn-small.btn-danger {
    background: var(--danger-bg);
    color: var(--danger-text);
    border: 1px solid var(--danger-color, #c44);
  }

  .btn-small.btn-danger:hover {
    filter: brightness(1.1);
  }

  .connect-modal-backdrop {
    position: fixed;
    inset: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .connect-modal-box {
    position: relative;
    z-index: 1;
    flex-shrink: 0;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    padding: 1.5rem;
    min-width: 320px;
    max-width: 90vw;
    box-shadow: var(--shadow-md);
  }

  .connect-modal-box h2 {
    margin: 0 0 1rem 0;
    font-size: 1.15rem;
  }

  .connect-modal-box .form-group {
    margin-bottom: 1rem;
  }

  .connect-modal-box .form-group label {
    display: block;
    margin-bottom: 0.35rem;
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .connect-modal-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    margin-top: 1.25rem;
  }

  .connect-modal-error {
    padding: 0.75rem;
    margin-bottom: 1rem;
    background: var(--danger-bg);
    color: var(--danger-text);
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .bandwidth-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.5rem;
  }

  .bandwidth-controls .form-inline {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .bandwidth-controls .form-inline label {
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .bandwidth-controls select {
    padding: 0.35rem 0.6rem;
    border-radius: 0.35rem;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .bandwidth-port-block {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .bandwidth-port-block:first-of-type {
    margin-top: 1rem;
    padding-top: 0;
    border-top: none;
  }

  .bandwidth-port-title {
    margin: 0 0 0.75rem 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .bandwidth-table {
    font-size: 0.875rem;
  }

  .bandwidth-table th,
  .bandwidth-table td {
    padding: 0.5rem 0.75rem;
  }

  .bandwidth-help {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }

  .bandwidth-help strong {
    color: var(--text-primary);
  }
</style>
