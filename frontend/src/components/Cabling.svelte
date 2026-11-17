<script>
  import { onMount, onDestroy, tick } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { getCablingGraph, getCablingLldp, getLocations, getRacks, refreshSwitchLldp } from '../lib/api.js';
  import { theme } from '../stores/theme.js';

  const SPEED_BANDS = [
    { key: '1g', label: '1G or less', colour: '#94a3b8', test: (s) => s <= 1000 },
    { key: '10g', label: 'Up to 10G', colour: '#0891b2', test: (s) => s <= 10000 },
    { key: '25g', label: '25G', colour: '#7c3aed', test: (s) => s <= 25000 },
    { key: '40g', label: '40G to 50G', colour: '#d97706', test: (s) => s <= 50000 },
    { key: '100g', label: '100G and above', colour: '#db2777', test: () => true },
  ];
  const UNKNOWN_SPEED_COLOUR = '#a8a29e';
  const LINK_COLOURS = { up: '#059669', down: '#dc2626', stale: '#d97706', unknown: '#94a3b8' };
  const LINK_LABELS = { up: 'up', down: 'down', stale: 'status stale', unknown: 'not polled' };
  const LLDP_OVERLAY_COLOUR = '#2563eb';
  const LLDP_MISMATCH_COLOUR = '#dc2626';
  const LLDP_LABELS = {
    match: 'Matches',
    mismatch: 'Mismatch',
    missing_cable: 'Not recorded',
    unknown_neighbour: 'Unknown neighbour',
    no_lldp: 'No LLDP seen',
    unmapped_local_port: 'Unmapped local port',
  };
  const LLDP_PROBLEMS = ['mismatch', 'missing_cable', 'unknown_neighbour', 'unmapped_local_port'];
  const RESOLVED_BY_LABELS = { port_mac: 'port MAC address', chassis_mac: 'chassis MAC address', system_name: 'system name' };

  let cyContainer;
  let cy = null;
  let cytoscapeLib = null;
  let computeElkPositions = null;
  let stopElkWorker = null;
  let loadingLib = true;
  let loading = false;
  let layingOut = false;
  let layoutSeq = 0;
  let error = null;

  let locations = [];
  let allRacks = [];
  let locationId = '';
  let rackId = '';
  let focusId = null;

  let graph = null;
  let nodesById = new Map();
  let portsById = new Map();
  let edgesByCable = new Map();
  let portsByDevice = new Map();
  let pathsById = new Map();

  let colourMode = 'speed';
  let showPortLabels = false;
  let search = '';
  let searchMessage = '';

  let selection = null;
  let unsubscribeTheme = null;
  let resizeObserver = null;
  let loadSeq = 0;
  let destroyed = false;

  let lldpOn = false;
  let lldp = null;
  let lldpLoading = false;
  let lldpError = null;
  let lldpSeq = 0;
  let lldpRefreshing = null;
  let lldpRefreshMessage = '';
  let showMatched = false;

  $: lldpByPort = new Map((lldp?.results || []).filter((r) => r.switch_port).map((r) => [r.switch_port, r]));
  $: lldpProblems = (lldp?.results || []).filter((r) => LLDP_PROBLEMS.includes(r.status));
  $: lldpQuiet = (lldp?.results || []).filter((r) => !LLDP_PROBLEMS.includes(r.status));

  $: racksForLocation = locationId
    ? allRacks.filter((r) => String(r.location_id) === String(locationId))
    : allRacks;
  $: deviceNames = graph ? graph.nodes.map((n) => n.name).sort((a, b) => a.localeCompare(b)) : [];
  $: stats = graph ? summarise(graph) : null;

  function readParams() {
    const params = new URLSearchParams(window.location.search);
    locationId = params.get('location') || '';
    rackId = params.get('rack') || '';
    focusId = params.get('focus') || null;
    lldpOn = params.get('lldp') === '1';
  }

  function writeParams() {
    const params = new URLSearchParams();
    if (locationId) params.set('location', locationId);
    if (rackId) params.set('rack', rackId);
    if (selection?.type === 'device') params.set('focus', selection.id);
    if (lldpOn) params.set('lldp', '1');
    const qs = params.toString();
    const url = `${window.location.pathname}${qs ? `?${qs}` : ''}`;
    if (url !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(window.history.state, '', url);
    }
  }

  function formatSpeed(mbps) {
    if (!mbps) return 'unknown speed';
    if (mbps >= 1000) {
      const g = mbps / 1000;
      return `${Number.isInteger(g) ? g : g.toFixed(1)}G`;
    }
    return `${mbps}M`;
  }

  function speedColour(mbps) {
    if (!mbps) return UNKNOWN_SPEED_COLOUR;
    return SPEED_BANDS.find((b) => b.test(mbps)).colour;
  }

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function mixHex(a, b, weight) {
    const parse = (hex) => {
      const m = /^#([0-9a-f]{6})$/i.exec(hex);
      return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null;
    };
    const ca = parse(a);
    const cb = parse(b);
    if (!ca || !cb) return b;
    const mixed = ca.map((v, i) => Math.round(v * weight + cb[i] * (1 - weight)));
    return `#${mixed.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
  }

  function timeAgo(value) {
    if (!value) return null;
    const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (seconds < 90) return 'just now';
    const minutes = Math.round(seconds / 60);
    if (minutes < 90) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 36) return `${hours} h ago`;
    return new Date(value).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function portStatusText(port) {
    if (port.kind === 'switch') {
      if (!port.status_updated_at) return port.oper_status ? `link ${port.oper_status}` : null;
      const when = timeAgo(port.status_updated_at);
      if (port.status_fresh) return `link ${port.oper_status || 'unknown'}, polled ${when}`;
      return `status stale: link ${port.oper_status || 'unknown'} when last polled ${when}`;
    }
    if (port.link_up === null || port.link_up === undefined) return null;
    return `link ${port.link_up ? 'up' : 'down'} at last hardware detection (${timeAgo(port.link_checked_at) || 'date unknown'})`;
  }

  function summarise(g) {
    const counts = { up: 0, down: 0, stale: 0, unknown: 0 };
    for (const p of g.paths) counts[p.link_status] = (counts[p.link_status] || 0) + 1;
    return {
      servers: g.nodes.filter((n) => n.kind === 'server' && n.in_scope).length,
      switches: g.nodes.filter((n) => n.kind === 'switch' && n.in_scope).length,
      panels: g.nodes.filter((n) => n.kind === 'patch_panel' && n.in_scope).length,
      external: g.nodes.filter((n) => !n.in_scope).length,
      cables: g.edges.length,
      links: g.paths.length,
      incomplete: g.paths.filter((p) => !p.complete).length,
      ...counts,
    };
  }

  const KIND_LABELS = { server: 'Server', switch: 'Switch', patch_panel: 'Patch panel' };
  const KIND_RANK = { switch: 0, patch_panel: 1, server: 2 };

  function deviceUrl(node) {
    if (node.kind === 'server') return `/admin/servers/${node.device_id}`;
    if (node.kind === 'switch') return `/admin/switches/${node.device_id}`;
    return `/admin/patch-panels/${node.device_id}`;
  }

  function portLabel(port, side) {
    return side ? `${port.name} (${side})` : port.name;
  }

  function rackLabel(rid) {
    const rack = graph?.racks.find((r) => r.id === rid);
    if (!rack) return null;
    return rack.name;
  }

  function locationLabel(lid) {
    return graph?.locations.find((l) => l.id === lid)?.name || null;
  }

  function unitLabel(node) {
    if (node.rack_unit == null) return '';
    const units = node.rack_units || 1;
    return units > 1 ? `U${node.rack_unit} to U${node.rack_unit + units - 1}` : `U${node.rack_unit}`;
  }

  function placement(node) {
    const rack = node.rack_id ? rackLabel(node.rack_id) : null;
    return [rack, unitLabel(node)].filter(Boolean).join(' ');
  }

  function indexGraph(g) {
    nodesById = new Map(g.nodes.map((n) => [n.id, n]));
    portsById = new Map(g.ports.map((p) => [p.id, p]));
    edgesByCable = new Map(g.edges.map((e) => [e.cable_id, e]));
    pathsById = new Map(g.paths.map((p) => [p.id, p]));
    portsByDevice = new Map();
    for (const p of g.ports) {
      if (!portsByDevice.has(p.device)) portsByDevice.set(p.device, []);
      portsByDevice.get(p.device).push(p);
    }
  }

  function buildElements(g) {
    const multiLocation = g.locations.length > 1;
    const serverRacks = new Set();
    const unrackedLocations = new Set();
    for (const n of g.nodes) {
      if (n.kind !== 'server') continue;
      if (n.rack_id) serverRacks.add(n.rack_id);
      else unrackedLocations.add(n.location_id);
    }

    const elements = [];
    for (const rack of g.racks) {
      if (!serverRacks.has(rack.id)) continue;
      const loc = locationLabel(rack.location_id);
      elements.push({
        group: 'nodes',
        classes: 'group',
        data: { id: `rack:${rack.id}`, label: multiLocation && loc ? `${loc} / ${rack.name}` : rack.name },
      });
    }
    for (const lid of unrackedLocations) {
      const loc = locationLabel(lid);
      elements.push({
        group: 'nodes',
        classes: 'group',
        data: { id: `norack:${lid}`, label: multiLocation && loc ? `${loc} / no rack` : 'No rack' },
      });
    }

    for (const n of g.nodes) {
      const classes = [n.kind];
      if (!n.in_scope) classes.push('external');
      if (!n.enabled) classes.push('disabled');
      if (n.locked) classes.push('locked');
      let sub = unitLabel(n);
      if (n.kind === 'switch') sub = placement(n) || n.model || '';
      if (n.kind === 'patch_panel') sub = placement(n) || (n.medium === 'fibre' ? 'fibre panel' : 'patch panel');
      const data = {
        id: n.id,
        kind: n.kind,
        rackUnit: n.rack_unit,
        label: sub ? `${n.name}\n${sub}` : n.name,
      };
      if (n.kind === 'server') data.parent = n.rack_id ? `rack:${n.rack_id}` : `norack:${n.location_id}`;
      elements.push({ group: 'nodes', classes: classes.join(' '), data });
    }

    const groupOf = (node) => (node.kind === 'server' ? (node.rack_id ? `rack:${node.rack_id}` : `norack:${node.location_id}`) : node.id);
    const oriented = orientEdges(g);
    const proxies = new Set();
    for (const { src, dst } of oriented.values()) {
      const from = nodesById.get(src);
      const to = nodesById.get(dst);
      if (from.kind === 'server' && to.kind === 'server') continue;
      if (from.kind === 'server' || to.kind === 'server') proxies.add(`${groupOf(from)}|${groupOf(to)}`);
    }
    for (const key of proxies) {
      const [source, target] = key.split('|');
      elements.push({ group: 'edges', classes: 'proxy', data: { id: `proxy:${key}`, source, target } });
    }

    for (const e of g.edges) {
      const { src, dst, srcPort, dstPort, srcSide, dstSide, srcDangling, dstDangling } = oriented.get(e.cable_id);
      const classes = [LINK_COLOURS[e.link_status] ? e.link_status : 'unknown'];
      if (srcDangling) classes.push('dangling-source');
      if (dstDangling) classes.push('dangling-target');
      elements.push({
        group: 'edges',
        classes: classes.join(' '),
        data: {
          id: e.id,
          cableId: e.cable_id,
          pathId: e.path_id,
          source: src,
          target: dst,
          sourceLabel: portLabel(srcPort, srcSide),
          targetLabel: portLabel(dstPort, dstSide),
          speedColour: speedColour(e.effective_speed_mbps),
          statusColour: LINK_COLOURS[e.link_status] || LINK_COLOURS.unknown,
        },
      });
    }
    return elements;
  }

  // Direction of each cable for the layered layout: along its path from the switch end towards the
  // server end, so switches sit above patch panels and patch panels above servers.
  function orientEdges(g) {
    const out = new Map();
    const rankOfPort = (portId) => KIND_RANK[nodesById.get(portsById.get(portId)?.device)?.kind] ?? 1;
    for (const path of g.paths) {
      const startRank = rankOfPort(path.start_port);
      const endRank = rankOfPort(path.end_port);
      let reverse = startRank > endRank;
      if (startRank === endRank && path.hops.length === 1) {
        const a = nodesById.get(portsById.get(path.start_port)?.device);
        const b = nodesById.get(portsById.get(path.end_port)?.device);
        reverse = !!a && !!b && a.name.localeCompare(b.name) > 0;
      }
      const dangling = new Set();
      if (!path.complete) {
        if (portsById.get(path.start_port)?.kind === 'patch') dangling.add(`${path.start_port}|${path.start_side}`);
        if (portsById.get(path.end_port)?.kind === 'patch') dangling.add(`${path.end_port}|${path.end_side}`);
      }
      for (const hop of path.hops) {
        const [fromPort, fromSide, toPort, toSide] = reverse
          ? [hop.to_port, hop.to_side, hop.from_port, hop.from_side]
          : [hop.from_port, hop.from_side, hop.to_port, hop.to_side];
        const srcPort = portsById.get(fromPort);
        const dstPort = portsById.get(toPort);
        out.set(hop.cable_id, {
          src: srcPort.device,
          dst: dstPort.device,
          srcPort,
          dstPort,
          srcSide: fromSide,
          dstSide: toSide,
          srcDangling: dangling.has(`${fromPort}|${fromSide}`),
          dstDangling: dangling.has(`${toPort}|${toSide}`),
        });
      }
    }
    return out;
  }

  function stylesheet() {
    const text = cssVar('--text-primary', '#0f172a');
    const muted = cssVar('--text-tertiary', '#64748b');
    const surface = cssVar('--bg-primary', '#ffffff');
    const surfaceAlt = cssVar('--bg-secondary', '#f1f4f8');
    const border = cssVar('--border-color', '#d7dee8');
    const accent = cssVar('--accent-color', '#0e7490');
    const warning = cssVar('--warning-color', '#d97706');
    return [
      {
        selector: 'node',
        style: {
          label: 'data(label)',
          'font-family': cssVar('--font-sans', 'sans-serif'),
          'font-size': 11,
          color: text,
          'text-wrap': 'wrap',
          'text-max-width': 132,
          'text-valign': 'center',
          'text-halign': 'center',
          width: 144,
          height: 40,
          shape: 'round-rectangle',
          'background-color': surface,
          'border-width': 1.5,
          'border-color': border,
        },
      },
      {
        selector: 'node.switch',
        style: {
          width: 168,
          height: 44,
          'font-weight': 600,
          'background-color': mixHex(accent, surface, 0.14),
          'border-color': accent,
        },
      },
      {
        selector: 'node.patch_panel',
        style: {
          shape: 'cut-rectangle',
          width: 152,
          height: 34,
          'font-size': 10.5,
          'background-color': mixHex(muted, surface, 0.12),
          'border-color': muted,
          'border-width': 1.5,
        },
      },
      {
        selector: 'node.group',
        style: {
          shape: 'round-rectangle',
          'background-color': surfaceAlt,
          'background-opacity': 0.6,
          'border-width': 1,
          'border-style': 'dashed',
          'border-color': border,
          'text-valign': 'top',
          'text-halign': 'center',
          'text-margin-y': -4,
          'font-size': 12,
          'font-weight': 600,
          color: muted,
          padding: 14,
        },
      },
      { selector: 'node.disabled', style: { color: muted, 'background-color': surfaceAlt, 'border-style': 'dotted' } },
      { selector: 'node.server, node.switch, node.patch_panel', style: { 'z-compound-depth': 'top' } },
      { selector: 'node.locked', style: { 'border-color': warning, 'border-width': 2.5 } },
      { selector: 'node.external', style: { opacity: 0.6, 'border-style': 'dashed' } },
      {
        selector: 'edge',
        style: {
          width: 2,
          'curve-style': 'bezier',
          'line-color': (ele) => ele.data(colourMode === 'speed' ? 'speedColour' : 'statusColour') || UNKNOWN_SPEED_COLOUR,
          'font-size': 9,
          color: text,
          'text-background-color': surface,
          'text-background-opacity': 0.9,
          'text-background-padding': 2,
          'source-text-offset': 34,
          'target-text-offset': 34,
        },
      },
      { selector: 'edge.proxy', style: { display: 'none' } },
      {
        selector: 'edge.lldp-overlay',
        style: {
          width: 2,
          'line-color': LLDP_OVERLAY_COLOUR,
          'line-style': 'dashed',
          'line-dash-pattern': [3, 3],
          'curve-style': 'unbundled-bezier',
          'control-point-distances': 40,
          label: 'LLDP',
          'font-size': 9,
          color: LLDP_OVERLAY_COLOUR,
          'text-background-color': surface,
          'text-background-opacity': 0.9,
          'text-background-padding': 2,
        },
      },
      { selector: 'edge.lldp-overlay.mismatch', style: { 'line-color': LLDP_MISMATCH_COLOUR, color: LLDP_MISMATCH_COLOUR } },
      {
        selector: 'edge.lldp-mismatch',
        style: {
          label: 'LLDP mismatch',
          'font-size': 9,
          'font-weight': 700,
          color: LLDP_MISMATCH_COLOUR,
          'text-background-color': surface,
          'text-background-opacity': 0.95,
          'text-background-padding': 2,
          'underlay-color': LLDP_MISMATCH_COLOUR,
          'underlay-opacity': 0.25,
          'underlay-padding': 4,
        },
      },
      { selector: 'edge.down', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
      { selector: 'edge.unknown, edge.stale', style: { 'line-style': 'dotted' } },
      { selector: 'edge.dangling-target', style: { 'target-arrow-shape': 'tee', 'target-arrow-color': warning } },
      { selector: 'edge.dangling-source', style: { 'source-arrow-shape': 'tee', 'source-arrow-color': warning } },
      {
        selector: 'edge.ports, edge.highlighted',
        style: { 'source-label': 'data(sourceLabel)', 'target-label': 'data(targetLabel)' },
      },
      { selector: '.faded', style: { opacity: 0.15 } },
      { selector: 'node.group.faded', style: { opacity: 0.4 } },
      { selector: 'edge.highlighted', style: { width: 3.5, 'z-index': 10 } },
      { selector: 'node.highlighted', style: { 'border-width': 3 } },
      { selector: 'node.focus', style: { 'border-color': accent, 'border-width': 4, 'underlay-color': accent, 'underlay-opacity': 0.18, 'underlay-padding': 6 } },
      { selector: 'edge.focus', style: { width: 5, 'underlay-color': accent, 'underlay-opacity': 0.2, 'underlay-padding': 4 } },
    ];
  }

  function layoutOptions() {
    return {
      nodeLayoutOptions: (n) => {
        if (n.isParent()) {
          return {
            'elk.algorithm': 'box',
            'elk.aspectRatio': '0.05',
            'elk.spacing.nodeNode': '10',
            'elk.padding': '[top=30,left=12,bottom=12,right=12]',
          };
        }
        if (n.data('kind') === 'switch') {
          const incoming = n.incomers('edge').not('.proxy, .lldp-overlay').length;
          return incoming === 0 ? { 'elk.layered.layering.layerConstraint': 'FIRST' } : {};
        }
        return { 'elk.priority': String(n.data('rackUnit') ?? 0) };
      },
      layoutOptions: {
        algorithm: 'layered',
        'elk.aspectRatio': String(cy.width() / Math.max(cy.height(), 1)),
        'elk.direction': 'DOWN',
        'elk.hierarchyHandling': 'SEPARATE_CHILDREN',
        'elk.layered.layering.strategy': 'LONGEST_PATH',
        'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
        'elk.spacing.nodeNode': '36',
        'elk.layered.spacing.nodeNodeBetweenLayers': '90',
        'elk.spacing.componentComponent': '48',
      },
    };
  }

  // Runs ELK in its worker and applies the positions; `after` runs once they are applied.
  // A newer layout request, a new graph or leaving the page discards an older result.
  async function runLayout(after) {
    if (!cy) return;
    const seq = ++layoutSeq;
    const target = cy;
    layingOut = true;
    try {
      const positions = await computeElkPositions(target.elements().not('.lldp-overlay'), layoutOptions());
      if (seq !== layoutSeq || destroyed || cy !== target) return;
      target
        .nodes()
        .filter((n) => !n.isParent())
        .layout({ name: 'preset', positions: (n) => positions[n.id()] || null, fit: false, animate: false })
        .run();
      after?.();
    } catch (err) {
      if (seq === layoutSeq && !destroyed) error = `Could not lay out the graph: ${err.message}`;
    } finally {
      if (seq === layoutSeq) layingOut = false;
    }
  }

  async function loadLibrary() {
    const [{ default: cytoscape }, elkModule] = await Promise.all([import('cytoscape'), import('../lib/elkLayout.js')]);
    computeElkPositions = elkModule.computeElkPositions;
    stopElkWorker = elkModule.stopElkWorker;
    cytoscapeLib = cytoscape;
  }

  async function loadFilters() {
    try {
      const [locs, racks] = await Promise.all([getLocations(), getRacks()]);
      locations = locs || [];
      allRacks = racks || [];
    } catch (err) {
      console.error('Failed to load locations or racks', err);
    }
  }

  async function loadGraph() {
    const seq = ++loadSeq;
    loading = true;
    error = null;
    try {
      const g = await getCablingGraph({ locationId: locationId || null, rackId: rackId || null });
      if (seq !== loadSeq || destroyed) return;
      graph = g;
      indexGraph(g);
      await tick();
      if (seq !== loadSeq || destroyed) return;
      renderGraph();
      if (lldpOn) loadLldp();
    } catch (err) {
      if (seq === loadSeq) error = err.message;
    } finally {
      if (seq === loadSeq) loading = false;
    }
  }

  async function loadLldp() {
    const seq = ++lldpSeq;
    lldpLoading = true;
    lldpError = null;
    try {
      const result = await getCablingLldp({ locationId: locationId || null, rackId: rackId || null });
      if (seq !== lldpSeq || destroyed) return;
      if (selection?.type === 'lldp') clearSelection();
      lldp = result;
      applyLldp();
    } catch (err) {
      if (seq === lldpSeq) lldpError = err.message;
    } finally {
      if (seq === lldpSeq) lldpLoading = false;
    }
  }

  function clearLldp() {
    if (!cy) return;
    cy.edges('.lldp-overlay').remove();
    cy.edges().removeClass('lldp-mismatch');
  }

  function applyLldp() {
    if (!cy) return;
    clearLldp();
    if (!lldpOn || !lldp) return;
    const overlays = [];
    lldp.results.forEach((r, index) => {
      if (r.status === 'mismatch' && r.cable_id) {
        const pathId = edgesByCable.get(r.cable_id)?.path_id;
        if (pathId) pathEdges(new Set([pathId])).addClass('lldp-mismatch');
      }
      const seen = r.observed?.resolved?.device;
      if (!seen || !['mismatch', 'missing_cable'].includes(r.status)) return;
      if (cy.getElementById(seen).empty() || cy.getElementById(r.switch).empty()) return;
      overlays.push({
        group: 'edges',
        classes: `lldp-overlay ${r.status}`,
        data: { id: `lldp:${index}`, source: r.switch, target: seen, lldpIndex: index },
      });
    });
    if (overlays.length) cy.add(overlays);
  }

  function toggleLldp() {
    writeParams();
    if (lldpOn) loadLldp();
    else clearLldp();
  }

  async function refreshLldp(sw) {
    lldpRefreshing = sw.id;
    lldpRefreshMessage = '';
    try {
      const result = await refreshSwitchLldp(sw.id);
      lldpRefreshMessage = `${sw.name}: ${result.neighbours} neighbour${result.neighbours === 1 ? '' : 's'} read.`;
      await loadLldp();
    } catch (err) {
      lldpRefreshMessage = `${sw.name}: ${err.message}`;
      await loadLldp();
    } finally {
      lldpRefreshing = null;
    }
  }

  function lldpObservedText(observed) {
    if (!observed) return 'nothing seen';
    const r = observed.resolved;
    if (r) return [r.device_name, r.port_name].filter(Boolean).join(' ');
    const parts = [observed.sys_name, observed.port_desc || observed.port_id].filter(Boolean);
    return parts.length ? parts.join(' ') : observed.chassis_id || 'unidentified device';
  }

  function lldpRecordedText(recorded) {
    if (!recorded) return 'no cable recorded';
    return [recorded.device_name, recorded.port_name].filter(Boolean).join(' ');
  }

  function lldpSwitchName(r) {
    return nodesById.get(r.switch)?.name || lldp?.switches.find((s) => s.id === r.switch_id)?.name || `Switch ${r.switch_id}`;
  }

  function selectLldp(r) {
    if (!cy) return;
    const index = lldp.results.indexOf(r);
    const overlay = cy.getElementById(`lldp:${index}`);
    const pathId = r.cable_id ? edgesByCable.get(r.cable_id)?.path_id : null;
    const cable = pathId ? pathEdges(new Set([pathId])) : cy.collection();
    const sw = cy.getElementById(r.switch);
    const focus = overlay.nonempty() ? overlay : cable.nonempty() ? cable : sw;
    if (focus.empty()) return;
    const members = focus.union(cable).union(overlay);
    highlight(members.union(members.connectedNodes()).union(sw), focus);
    selection = { type: 'lldp', index };
    cy.animate({ center: { eles: members.union(sw) } }, { duration: 300 });
    writeParams();
  }

  function renderGraph() {
    if (destroyed || !cytoscapeLib || !cyContainer || !graph) return;
    const wanted = focusId || (selection?.type === 'device' ? selection.id : null);
    if (cy) cy.destroy();
    selection = null;
    cy = cytoscapeLib({
      container: cyContainer,
      elements: buildElements(graph),
      style: stylesheet(),
      minZoom: 0.1,
      maxZoom: 3,
      wheelSensitivity: 0.3,
      boxSelectionEnabled: false,
    });
    cy.on('tap', 'node', (evt) => {
      const node = evt.target;
      if (node.hasClass('group')) {
        clearSelection();
        return;
      }
      selectDevice(node.id());
    });
    cy.on('tap', 'edge', (evt) => {
      const index = evt.target.data('lldpIndex');
      if (index !== undefined && lldp?.results[index]) selectLldp(lldp.results[index]);
      else selectCable(evt.target.data('cableId'));
    });
    cy.on('tap', (evt) => {
      if (evt.target === cy) clearSelection();
    });
    if (showPortLabels) cy.edges().addClass('ports');
    runLayout(() => {
      cy.resize();
      cy.fit(undefined, 30);
      applyLldp();
      if (wanted && nodesById.has(wanted)) {
        selectDevice(wanted, true);
      }
      focusId = null;
    });
  }

  function clearHighlight() {
    if (!cy) return;
    cy.elements().removeClass('faded highlighted focus');
  }

  function clearSelection() {
    selection = null;
    clearHighlight();
    writeParams();
  }

  function highlight(collection, focus) {
    clearHighlight();
    cy.elements().addClass('faded');
    const withParents = collection.union(collection.nodes().ancestors());
    withParents.removeClass('faded');
    collection.addClass('highlighted');
    focus.addClass('focus');
  }

  function pathEdges(pathIds) {
    return cy.edges().filter((e) => pathIds.has(e.data('pathId')));
  }

  function selectDevice(id, centre = false) {
    if (!cy) return;
    const ele = cy.getElementById(id);
    if (ele.empty()) return;
    const direct = ele.connectedEdges().not('.proxy, .lldp-overlay');
    const cables = pathEdges(new Set(direct.map((e) => e.data('pathId'))));
    highlight(cables.union(cables.connectedNodes()).union(ele), ele);
    selection = { type: 'device', id };
    if (centre) cy.animate({ center: { eles: ele }, zoom: Math.max(cy.zoom(), 1) }, { duration: 300 });
    writeParams();
  }

  function selectCable(cableId, fromPortId = null) {
    if (!cy) return;
    const edge = edgesByCable.get(cableId);
    if (!edge) return;
    const ele = cy.getElementById(edge.id);
    const cables = pathEdges(new Set([edge.path_id]));
    highlight(cables.union(cables.connectedNodes()), ele);
    selection = { type: 'path', cableId, fromPortId };
    writeParams();
  }

  function endpoint(portId, side) {
    const port = portsById.get(portId);
    return port ? { kind: 'end', port, side, node: nodesById.get(port.device) } : null;
  }

  // Ordered steps of the path a cable belongs to: end port, cable, patch panel port (in one side, out
  // the other), cable, ..., end port. Starts at fromPortId when given, otherwise at the server end.
  function pathSteps(sel) {
    const edge = edgesByCable.get(sel.cableId);
    const path = edge && pathsById.get(edge.path_id);
    if (!path) return null;
    let hops = path.hops;
    if (sel.fromPortId && sel.fromPortId === path.end_port && sel.fromPortId !== path.start_port) {
      hops = [...hops].reverse().map((h) => ({
        cable_id: h.cable_id, from_port: h.to_port, from_side: h.to_side, to_port: h.from_port, to_side: h.from_side,
      }));
    }
    const steps = [];
    hops.forEach((hop, i) => {
      if (i === 0) steps.push(endpoint(hop.from_port, hop.from_side));
      steps.push({ kind: 'cable', edge: edgesByCable.get(hop.cable_id), selected: hop.cable_id === sel.cableId });
      const next = hops[i + 1];
      if (next && next.from_port === hop.to_port) {
        const port = portsById.get(hop.to_port);
        steps.push({ kind: 'through', port, node: nodesById.get(port?.device), inSide: hop.to_side, outSide: next.from_side });
      } else {
        steps.push(endpoint(hop.to_port, hop.to_side));
      }
    });
    if (steps.some((s) => !s || (s.kind !== 'cable' && (!s.port || !s.node)))) return null;
    return { path, steps, first: steps[0], last: steps[steps.length - 1] };
  }

  // For a server or switch port: the port at the other end of its path, and how many patch panels
  // the path passes through.
  function peerOf(port) {
    if (!port.cable_id) return null;
    const edge = edgesByCable.get(port.cable_id);
    const path = edge && pathsById.get(edge.path_id);
    if (!path) return null;
    const atStart = path.start_port === port.id;
    const other = portsById.get(atStart ? path.end_port : path.start_port);
    if (!other) return null;
    return {
      port: other,
      side: atStart ? path.end_side : path.start_side,
      node: nodesById.get(other.device),
      edge,
      panels: path.hops.length - 1,
      complete: path.complete,
    };
  }

  // For one side of a patch panel port: the directly connected port on the other end of that cable.
  function patchNeighbour(port, side) {
    const cableId = port[`${side}_cable_id`];
    const edge = cableId && edgesByCable.get(cableId);
    if (!edge) return null;
    const atSource = edge.source_port === port.id && edge.source_side === side;
    const other = portsById.get(atSource ? edge.target_port : edge.source_port);
    if (!other) return null;
    return { port: other, side: atSource ? edge.target_side : edge.source_side, node: nodesById.get(other.device), edge };
  }

  function runSearch() {
    const q = search.trim().toLowerCase();
    searchMessage = '';
    if (!q || !graph) return;
    const match =
      graph.nodes.find((n) => n.name.toLowerCase() === q) ||
      graph.nodes.find((n) => n.name.toLowerCase().startsWith(q)) ||
      graph.nodes.find((n) => n.name.toLowerCase().includes(q) || (n.ip && n.ip === q));
    if (match) selectDevice(match.id, true);
    else searchMessage = 'No matching device in this view.';
  }

  function fit() {
    cy?.animate({ fit: { padding: 30 } }, { duration: 250 });
  }

  function zoomBy(factor) {
    if (!cy) return;
    const level = Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor));
    cy.zoom({ level, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }

  function relayout() {
    if (!cy) return;
    clearLldp();
    runLayout(() => {
      cy.fit(undefined, 30);
      applyLldp();
    });
  }

  function exportPng() {
    if (!cy) return;
    const dataUrl = cy.png({ full: true, scale: 2, bg: cssVar('--bg-primary', '#ffffff') });
    const a = document.createElement('a');
    const stamp = new Date().toISOString().slice(0, 10);
    a.href = dataUrl;
    a.download = `rackflow-cabling-${stamp}.png`;
    a.setAttribute('data-router-ignore', '');
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function onLocationChange() {
    const rack = allRacks.find((r) => String(r.id) === String(rackId));
    if (rackId && locationId && (!rack || String(rack.location_id) !== String(locationId))) rackId = '';
    selection = null;
    writeParams();
    loadGraph();
  }

  function onRackChange() {
    selection = null;
    writeParams();
    loadGraph();
  }

  function togglePortLabels() {
    if (!cy) return;
    if (showPortLabels) cy.edges().addClass('ports');
    else cy.edges().removeClass('ports');
  }

  function onColourModeChange() {
    cy?.style(stylesheet());
  }

  function onPopState() {
    if (!window.location.pathname.startsWith('/admin/cabling')) return;
    const before = `${locationId}|${rackId}`;
    readParams();
    if (`${locationId}|${rackId}` !== before) {
      loadGraph();
    } else if (focusId && nodesById.has(focusId)) {
      selectDevice(focusId, true);
      focusId = null;
    }
  }

  onMount(async () => {
    readParams();
    window.addEventListener('popstate', onPopState);
    let wasEmpty = true;
    resizeObserver = new ResizeObserver(([entry]) => {
      const empty = entry.contentRect.width === 0 || entry.contentRect.height === 0;
      if (cy && !empty) {
        cy.resize();
        if (wasEmpty) cy.fit(undefined, 30);
      }
      wasEmpty = empty;
    });
    resizeObserver.observe(cyContainer);
    loadFilters();
    try {
      await loadLibrary();
    } catch (err) {
      error = `Could not load the graph library: ${err.message}`;
    } finally {
      loadingLib = false;
    }
    if (cytoscapeLib && !destroyed) {
      await loadGraph();
      if (destroyed) return;
      let first = true;
      unsubscribeTheme = theme.subscribe(() => {
        if (first) {
          first = false;
          return;
        }
        requestAnimationFrame(() => cy?.style(stylesheet()));
      });
    }
  });

  onDestroy(() => {
    destroyed = true;
    window.removeEventListener('popstate', onPopState);
    resizeObserver?.disconnect();
    unsubscribeTheme?.();
    cy?.destroy();
    cy = null;
    stopElkWorker?.();
  });
</script>

<PageHeader title="Cabling" />

<div class="admin-page cabling-page">
  <div class="toolbar">
    <label class="field">
      <span>Location</span>
      <select bind:value={locationId} on:change={onLocationChange}>
        <option value="">All locations</option>
        {#each locations as loc (loc.id)}
          <option value={String(loc.id)}>{loc.name}</option>
        {/each}
      </select>
    </label>
    <label class="field">
      <span>Rack</span>
      <select bind:value={rackId} on:change={onRackChange}>
        <option value="">All racks</option>
        {#each racksForLocation as rack (rack.id)}
          <option value={String(rack.id)}>{rack.name}</option>
        {/each}
      </select>
    </label>
    <form class="field search" on:submit|preventDefault={runSearch}>
      <span>Find device</span>
      <input
        type="search"
        list="cabling-device-names"
        placeholder="Name or IP"
        bind:value={search}
        on:change={runSearch}
      />
      <datalist id="cabling-device-names">
        {#each deviceNames as name}
          <option value={name}></option>
        {/each}
      </datalist>
    </form>
    <label class="field">
      <span>Colour by</span>
      <select bind:value={colourMode} on:change={onColourModeChange}>
        <option value="speed">Speed</option>
        <option value="status">Link status</option>
      </select>
    </label>
    <label class="check">
      <input type="checkbox" bind:checked={showPortLabels} on:change={togglePortLabels} />
      Port labels
    </label>
    <label class="check" title="Compare recorded cables with the LLDP neighbours the switches report">
      <input type="checkbox" bind:checked={lldpOn} on:change={toggleLldp} />
      LLDP check
      {#if lldpOn && lldp && lldpProblems.length}<span class="count-badge">{lldpProblems.length}</span>{/if}
    </label>
    <div class="tools">
      <button type="button" class="btn-secondary btn-sm" on:click={fit} title="Fit to screen">Fit</button>
      <button type="button" class="btn-secondary btn-sm" on:click={() => zoomBy(1.25)} title="Zoom in" aria-label="Zoom in">+</button>
      <button type="button" class="btn-secondary btn-sm" on:click={() => zoomBy(0.8)} title="Zoom out" aria-label="Zoom out">&minus;</button>
      <button type="button" class="btn-secondary btn-sm" on:click={relayout} title="Run the layout again">Re-layout</button>
      <button type="button" class="btn-primary btn-sm" on:click={exportPng} disabled={!cy}>Export PNG</button>
    </div>
  </div>
  {#if searchMessage}
    <div class="search-message">{searchMessage}</div>
  {/if}

  <div class="workspace">
    <div class="canvas-wrap">
      <div class="canvas" bind:this={cyContainer}></div>
      {#if loadingLib || loading || layingOut}
        <div class="overlay">{loadingLib ? 'Loading graph library…' : loading ? 'Loading cabling…' : 'Laying out…'}</div>
      {:else if error}
        <div class="overlay error">{error}</div>
      {:else if graph && graph.nodes.length === 0}
        <div class="overlay">No servers or switches match this filter.</div>
      {/if}

      <details class="legend" open>
        <summary>Legend</summary>
        {#if colourMode === 'speed'}
          <div class="legend-title">Cable speed</div>
          {#each SPEED_BANDS as band (band.key)}
            <div class="legend-row"><span class="swatch" style="background:{band.colour}"></span>{band.label}</div>
          {/each}
          <div class="legend-row"><span class="swatch" style="background:{UNKNOWN_SPEED_COLOUR}"></span>Unknown</div>
        {:else}
          <div class="legend-title">Link status</div>
          <div class="legend-row"><span class="swatch" style="background:{LINK_COLOURS.up}"></span>Up</div>
          <div class="legend-row"><span class="swatch" style="background:{LINK_COLOURS.down}"></span>Down</div>
          <div class="legend-row"><span class="swatch" style="background:{LINK_COLOURS.stale}"></span>Stale (not polled recently)</div>
          <div class="legend-row"><span class="swatch" style="background:{LINK_COLOURS.unknown}"></span>Not polled</div>
        {/if}
        <div class="legend-title legend-gap">Line style</div>
        <div class="legend-row"><span class="line solid"></span>Link up</div>
        <div class="legend-row"><span class="line dashed"></span>Link down</div>
        <div class="legend-row"><span class="line dotted"></span>Status stale or not polled</div>
        <div class="legend-title legend-gap">Devices</div>
        <div class="legend-row"><span class="box switch"></span>Switch</div>
        <div class="legend-row"><span class="box panel-box"></span>Patch panel</div>
        <div class="legend-row"><span class="box"></span>Server</div>
        <div class="legend-row"><span class="box locked"></span>Locked</div>
        <div class="legend-row"><span class="box external"></span>Outside filter</div>
        <div class="legend-row"><span class="line dangling"></span>Path ends at a patch panel</div>
        {#if lldpOn}
          <div class="legend-title legend-gap">LLDP check</div>
          <div class="legend-row"><span class="line lldp-mismatch"></span>Cable disagrees with LLDP</div>
          <div class="legend-row"><span class="line dotted lldp-seen"></span>Seen by LLDP, not recorded</div>
          <div class="legend-row"><span class="line dotted lldp-seen-bad"></span>What LLDP sees instead</div>
        {/if}
      </details>
    </div>

    <aside class="panel">
      {#if selection?.type === 'device' && nodesById.get(selection.id)}
        {@const node = nodesById.get(selection.id)}
        {@const ports = portsByDevice.get(node.id) || []}
        <div class="panel-head">
          <span class="kind">{KIND_LABELS[node.kind] || node.kind}</span>
          <h3><a href={deviceUrl(node)}>{node.name}</a></h3>
          <div class="meta">
            {#if placement(node)}<span>{placement(node)}</span>{/if}
            {#if locationLabel(node.location_id)}<span>{locationLabel(node.location_id)}</span>{/if}
            {#if node.model}<span>{node.model}</span>{/if}
            {#if node.medium}<span>{node.medium === 'fibre' ? 'Fibre' : 'Copper'}</span>{/if}
            {#if node.ip}<span class="mono">{node.ip}</span>{/if}
          </div>
          <div class="badges">
            {#if node.kind !== 'patch_panel'}
              <span class="badge" class:ok={node.enabled} class:off={!node.enabled}>{node.enabled ? 'Enabled' : 'Disabled'}</span>
            {/if}
            {#if node.locked}<span class="badge warn">Locked</span>{/if}
            {#if !node.in_scope}<span class="badge">Outside filter</span>{/if}
          </div>
        </div>
        <h4>
          {node.kind === 'server' ? 'Network ports' : 'Cabled ports'}
          {#if node.kind !== 'server'}<span class="muted">({ports.length} of {node.port_count})</span>{/if}
        </h4>
        {#if ports.length === 0}
          <p class="muted">{node.kind === 'server' ? 'No network ports recorded.' : 'No cabled ports in this view.'}</p>
        {:else if node.kind === 'patch_panel'}
          <ul class="ports">
            {#each ports as port (port.id)}
              <li class="patch-port">
                <span class="port-name mono">{port.name}</span>
                {#each ['front', 'rear'] as side}
                  {@const n = patchNeighbour(port, side)}
                  <button
                    type="button"
                    class="port-row patch-side"
                    disabled={!n}
                    on:click={() => n && selectCable(n.edge.cable_id, port.id)}
                  >
                    <span class="side-label">{side === 'front' ? 'Front' : 'Rear'}</span>
                    {#if n}
                      <span class="arrow">&rarr;</span>
                      <span class="peer">{n.node.name} <span class="mono">{portLabel(n.port, n.side)}</span></span>
                    {:else}
                      <span class="muted not-cabled">not cabled</span>
                    {/if}
                  </button>
                {/each}
              </li>
            {/each}
          </ul>
        {:else}
          <ul class="ports">
            {#each ports as port (port.id)}
              {@const peer = peerOf(port)}
              <li>
                <button
                  type="button"
                  class="port-row"
                  disabled={!peer}
                  on:click={() => peer && selectCable(peer.edge.cable_id, port.id)}
                >
                  <span class="port-name mono">{port.name}</span>
                  <span class="port-speed">{formatSpeed(port.speed_mbps)}</span>
                  {#if lldpOn && lldpByPort.get(port.id)}
                    {@const check = lldpByPort.get(port.id)}
                    <span class="lldp-badge lldp-{check.status}" title="LLDP sees: {lldpObservedText(check.observed)}">{LLDP_LABELS[check.status]}</span>
                  {/if}
                  {#if peer}
                    <span class="arrow">&rarr;</span>
                    <span class="peer" class:incomplete-text={!peer.complete}>
                      {peer.node.name} <span class="mono">{portLabel(peer.port, peer.side)}</span>
                      {#if peer.panels}<span class="via-count">via {peer.panels} panel{peer.panels === 1 ? '' : 's'}</span>{/if}
                    </span>
                  {:else}
                    <span class="muted not-cabled">not cabled</span>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      {:else if selection?.type === 'path' && pathSteps(selection)}
        {@const path = pathSteps(selection)}
        <div class="panel-head">
          <span class="kind">Cable path</span>
          <h3 class="path-title">
            {#each path.steps as step, i}
              {#if i}<span class="title-sep">&rarr;</span>{/if}
              {#if step.kind === 'cable'}
                cable
              {:else if step.kind === 'through'}
                {step.node.name} <span class="mono">{step.port.name}</span>
              {:else}
                {step.node.name} <span class="mono">{portLabel(step.port, step.node.kind === 'patch_panel' ? step.side : null)}</span>
              {/if}
            {/each}
          </h3>
          <div class="meta">
            <span>{path.path.hops.length} cable{path.path.hops.length === 1 ? '' : 's'}</span>
            <span>{formatSpeed(path.path.effective_speed_mbps)}</span>
            <span class="link-{path.path.link_status}">link {LINK_LABELS[path.path.link_status] || path.path.link_status}</span>
          </div>
          {#if !path.path.complete}
            <div class="badges"><span class="badge warn">Incomplete: the path stops at a patch panel with nothing cabled behind it</span></div>
          {/if}
        </div>
        <ol class="path">
          {#each path.steps as step}
            {#if step.kind === 'cable'}
              <li class="step cable" class:current={step.selected}>
                <button type="button" class="step-title step-link" on:click={() => selectCable(step.edge.cable_id, selection.fromPortId)}>Cable #{step.edge.cable_id}</button>
                <div class="step-meta">
                  <span>{step.edge.cable_type || 'type not set'}</span>
                  <span>{formatSpeed(step.edge.effective_speed_mbps)}</span>
                </div>
                {#if step.edge.description}<div class="step-desc">{step.edge.description}</div>{/if}
              </li>
            {:else if step.kind === 'through'}
              <li class="step through">
                <div class="step-title"><a href={deviceUrl(step.node)}>{step.node.name}</a> <span class="mono">{step.port.name}</span></div>
                <div class="step-meta"><span>in {step.inSide}, out {step.outSide}</span></div>
              </li>
            {:else}
              <li class="step" class:dangling={step.node.kind === 'patch_panel'}>
                <div class="step-title">
                  <a href={deviceUrl(step.node)}>{step.node.name}</a>
                  <span class="mono">{portLabel(step.port, step.node.kind === 'patch_panel' ? step.side : null)}</span>
                </div>
                <div class="step-meta">
                  {#if step.node.kind === 'patch_panel'}
                    <span>nothing cabled on the {step.side === 'front' ? 'rear' : 'front'}</span>
                  {:else}
                    <span>{formatSpeed(step.port.speed_mbps)}</span>
                  {/if}
                  {#if step.port.mac_address}<span class="mono">{step.port.mac_address}</span>{/if}
                  {#if portStatusText(step.port)}<span class:stale-text={step.port.kind === 'switch' && !step.port.status_fresh}>{portStatusText(step.port)}</span>{/if}
                  {#if step.port.lag_group}<span>LAG {step.port.lag_group}</span>{/if}
                  {#if step.port.description}<span>{step.port.description}</span>{/if}
                </div>
                {#if lldpOn && lldpByPort.get(step.port.id)}
                  {@const check = lldpByPort.get(step.port.id)}
                  <div class="step-meta">
                    <span class="lldp-badge lldp-{check.status}">LLDP: {LLDP_LABELS[check.status]}</span>
                    {#if check.observed}<span>sees {lldpObservedText(check.observed)}</span>{/if}
                  </div>
                {/if}
              </li>
            {/if}
          {/each}
        </ol>
        <div class="panel-actions">
          <button type="button" class="btn-secondary btn-sm" on:click={() => selectDevice(path.first.node.id, true)}>Show {path.first.node.name}</button>
          <button type="button" class="btn-secondary btn-sm" on:click={() => selectDevice(path.last.node.id, true)}>Show {path.last.node.name}</button>
        </div>
      {:else if selection?.type === 'lldp' && lldp?.results[selection.index]}
        {@const check = lldp.results[selection.index]}
        <div class="panel-head">
          <span class="kind">LLDP check</span>
          <h3>{lldpSwitchName(check)} <span class="mono">{check.switch_port_name || check.local_port_label || 'unknown port'}</span></h3>
          <div class="badges"><span class="lldp-badge lldp-{check.status}">{LLDP_LABELS[check.status]}</span></div>
        </div>
        <dl class="lldp-compare">
          <dt>Recorded</dt>
          <dd>{lldpRecordedText(check.recorded)}{#if check.cable_id}{' '}<span class="muted">(cable #{check.cable_id})</span>{/if}</dd>
          <dt>LLDP sees</dt>
          <dd>{lldpObservedText(check.observed)}</dd>
          {#if check.observed}
            {#if check.observed.sys_name}<dt>System name</dt><dd class="mono">{check.observed.sys_name}</dd>{/if}
            {#if check.observed.port_id}<dt>Port ID</dt><dd class="mono">{check.observed.port_id}</dd>{/if}
            {#if check.observed.port_desc}<dt>Port description</dt><dd>{check.observed.port_desc}</dd>{/if}
            {#if check.observed.chassis_id}<dt>Chassis ID</dt><dd class="mono">{check.observed.chassis_id}</dd>{/if}
            {#if check.observed.resolved_by}<dt>Matched by</dt><dd>{RESOLVED_BY_LABELS[check.observed.resolved_by]}</dd>{/if}
          {/if}
        </dl>
        <div class="panel-actions">
          {#if check.cable_id && edgesByCable.has(check.cable_id)}
            <button type="button" class="btn-secondary btn-sm" on:click={() => selectCable(check.cable_id, check.switch_port)}>Trace recorded cable</button>
          {/if}
          {#if check.observed?.resolved && nodesById.has(check.observed.resolved.device)}
            <button type="button" class="btn-secondary btn-sm" on:click={() => selectDevice(check.observed.resolved.device, true)}>Show {check.observed.resolved.device_name}</button>
          {/if}
          <button type="button" class="btn-secondary btn-sm" on:click={clearSelection}>Back to overview</button>
        </div>
      {:else}
        <div class="panel-head">
          <span class="kind">Overview</span>
          <h3>Network cabling</h3>
        </div>
        {#if stats}
          <dl class="stats">
            <dt>Servers</dt><dd>{stats.servers}</dd>
            <dt>Switches</dt><dd>{stats.switches}</dd>
            {#if stats.panels}<dt>Patch panels</dt><dd>{stats.panels}</dd>{/if}
            <dt>Cables</dt><dd>{stats.cables}</dd>
            {#if stats.links !== stats.cables}<dt>End-to-end links</dt><dd>{stats.links}</dd>{/if}
            {#if stats.incomplete}<dt>Incomplete paths</dt><dd>{stats.incomplete}</dd>{/if}
            <dt>Links up</dt><dd>{stats.up}</dd>
            <dt>Links down</dt><dd>{stats.down}</dd>
            {#if stats.stale}<dt>Stale status</dt><dd>{stats.stale}</dd>{/if}
            <dt>Not polled</dt><dd>{stats.unknown}</dd>
            {#if stats.external}<dt>Outside filter</dt><dd>{stats.external}</dd>{/if}
          </dl>
        {/if}
        <p class="muted hint">
          Click a server or switch to highlight its cables and neighbours. Click a cable or a port to trace the
          full path. Devices outside the filter are shown faded when they are cabled to devices inside it.
        </p>
        {#if lldpOn}
          <h4>LLDP check</h4>
          {#if lldpLoading && !lldp}
            <p class="muted">Loading LLDP data…</p>
          {:else if lldpError}
            <p class="error-text">{lldpError}</p>
          {:else if lldp}
            {#if lldp.switches.length === 0}
              <p class="muted">No switches in this view.</p>
            {:else}
              <ul class="lldp-switches">
                {#each lldp.switches as sw (sw.id)}
                  <li>
                    <span class="lldp-switch-name">{sw.name}</span>
                    <span class="muted lldp-switch-state">
                      {#if !sw.supports_lldp}
                        plugin cannot read LLDP
                      {:else if sw.lldp_error}
                        <span class="error-text" title={sw.lldp_error}>last read failed</span>
                      {:else if sw.lldp_collected_at}
                        {sw.neighbour_count} neighbour{sw.neighbour_count === 1 ? '' : 's'}, read {timeAgo(sw.lldp_collected_at)}
                      {:else}
                        never read
                      {/if}
                    </span>
                    {#if sw.supports_lldp}
                      <button
                        type="button"
                        class="btn-secondary btn-sm lldp-read"
                        disabled={lldpRefreshing !== null}
                        title="Read this switch's LLDP neighbour table now (read-only SNMP)"
                        on:click={() => refreshLldp(sw)}
                      >{lldpRefreshing === sw.id ? 'Reading…' : 'Read now'}</button>
                    {/if}
                  </li>
                {/each}
              </ul>
              {#if lldpRefreshMessage}<p class="muted small">{lldpRefreshMessage}</p>{/if}
            {/if}
            <dl class="stats">
              {#each Object.keys(LLDP_LABELS) as status}
                {#if lldp.summary[status]}<dt>{LLDP_LABELS[status]}</dt><dd>{lldp.summary[status]}</dd>{/if}
              {/each}
            </dl>
            {#if lldpProblems.length === 0 && lldp.results.length}
              <p class="muted">No discrepancies found.</p>
            {/if}
            {#if lldpProblems.length}
              <ul class="lldp-list">
                {#each lldpProblems as check}
                  <li>
                    <button type="button" class="port-row" on:click={() => selectLldp(check)}>
                      <span class="lldp-badge lldp-{check.status}">{LLDP_LABELS[check.status]}</span>
                      <span>{lldpSwitchName(check)} <span class="mono">{check.switch_port_name || check.local_port_label || '?'}</span></span>
                      <span class="lldp-detail">
                        <span class="muted">recorded</span> {lldpRecordedText(check.recorded)};
                        <span class="muted">LLDP sees</span> {lldpObservedText(check.observed)}
                      </span>
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if lldpQuiet.length}
              <button type="button" class="link-button" on:click={() => (showMatched = !showMatched)}>
                {showMatched ? 'Hide' : 'Show'} {lldpQuiet.length} matching or silent port{lldpQuiet.length === 1 ? '' : 's'}
              </button>
              {#if showMatched}
                <ul class="lldp-list">
                  {#each lldpQuiet as check}
                    <li>
                      <button type="button" class="port-row" on:click={() => selectLldp(check)}>
                        <span class="lldp-badge lldp-{check.status}">{LLDP_LABELS[check.status]}</span>
                        <span>{lldpSwitchName(check)} <span class="mono">{check.switch_port_name}</span></span>
                        <span class="lldp-detail">{lldpRecordedText(check.recorded)}</span>
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
            {/if}
            <p class="muted hint">
              "No LLDP seen" is common for servers that do not run an LLDP agent, so it is listed but not treated
              as a problem. LLDP data is read by the poller every 10 minutes.
            </p>
          {/if}
        {/if}
      {/if}
    </aside>
  </div>
</div>

<style>
  .cabling-page {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    gap: 10px;
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 10px 14px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-tertiary);
    margin: 0;
  }

  .field select,
  .field input {
    min-width: 150px;
    padding: 5px 8px;
    font-size: 13px;
    font-weight: 400;
    letter-spacing: normal;
    text-transform: none;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
  }

  .field.search input {
    min-width: 200px;
  }

  .field select:focus,
  .field input:focus {
    outline: none;
    box-shadow: var(--focus-ring);
    border-color: var(--accent-color);
  }

  .check {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: var(--text-secondary);
    padding-bottom: 6px;
  }

  .tools {
    display: flex;
    gap: 6px;
    margin-left: auto;
  }

  .search-message {
    font-size: 12px;
    color: var(--warning-text);
  }

  .workspace {
    flex: 1;
    display: flex;
    gap: 12px;
    min-height: 520px;
  }

  .canvas-wrap {
    position: relative;
    flex: 1;
    min-width: 0;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    box-shadow: var(--admin-surface-shadow);
    overflow: hidden;
  }

  .canvas {
    position: absolute;
    inset: 0;
  }

  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--bg-primary) 80%, transparent);
    pointer-events: none;
  }

  .overlay.error {
    color: var(--danger-text);
  }

  .legend {
    position: absolute;
    left: 10px;
    bottom: 10px;
    padding: 8px 10px;
    font-size: 11px;
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--bg-primary) 92%, transparent);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
  }

  .legend summary {
    cursor: pointer;
    font-weight: 700;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-tertiary);
  }

  .legend[open] summary {
    margin-bottom: 4px;
  }

  .legend-title {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 10px;
    color: var(--text-tertiary);
    margin-bottom: 3px;
  }

  .legend-gap {
    margin-top: 6px;
  }

  .legend-row {
    display: flex;
    align-items: center;
    gap: 6px;
    line-height: 1.6;
  }

  .swatch {
    width: 16px;
    height: 4px;
    border-radius: 2px;
  }

  .line {
    width: 16px;
    border-top: 2px solid var(--text-tertiary);
  }

  .line.dashed {
    border-top-style: dashed;
  }

  .line.dotted {
    border-top-style: dotted;
  }

  .box {
    width: 14px;
    height: 9px;
    border: 1.5px solid var(--border-color);
    border-radius: 2px;
    background: var(--bg-primary);
  }

  .box.switch {
    border-color: var(--accent-color);
    background: color-mix(in srgb, var(--accent-color) 14%, var(--bg-primary));
  }

  .box.panel-box {
    border-color: var(--text-tertiary);
    background: color-mix(in srgb, var(--text-tertiary) 12%, var(--bg-primary));
    clip-path: polygon(3px 0, calc(100% - 3px) 0, 100% 3px, 100% calc(100% - 3px), calc(100% - 3px) 100%, 3px 100%, 0 calc(100% - 3px), 0 3px);
  }

  .line.dangling {
    position: relative;
  }

  .line.dangling::after {
    content: '';
    position: absolute;
    right: 0;
    top: -5px;
    height: 8px;
    border-right: 2px solid var(--warning-color);
  }

  .box.locked {
    border-color: var(--warning-color);
    border-width: 2px;
  }

  .box.external {
    border-style: dashed;
    opacity: 0.6;
  }

  .panel {
    width: 320px;
    flex-shrink: 0;
    padding: 12px 14px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    box-shadow: var(--admin-surface-shadow);
    overflow-y: auto;
    font-size: 13px;
    color: var(--text-primary);
  }

  .panel-head {
    margin-bottom: 10px;
  }

  .kind {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }

  .panel h3 {
    margin: 2px 0 4px;
    font-size: 16px;
  }

  .panel h3.path-title {
    font-size: 14px;
    line-height: 1.4;
  }

  .panel h4 {
    margin: 12px 0 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-tertiary);
  }

  .panel a {
    color: var(--accent-color);
    text-decoration: none;
  }

  .panel a:hover {
    text-decoration: underline;
  }

  .meta,
  .step-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .badges {
    display: flex;
    gap: 6px;
    margin-top: 6px;
  }

  .badge {
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: 600;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .badge.ok {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .badge.off {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .badge.warn {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .ports {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .port-row {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    padding: 5px 6px;
    text-align: left;
    font: inherit;
    font-size: 12px;
    color: var(--text-primary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .port-row:hover:not(:disabled) {
    background: color-mix(in srgb, var(--accent-color) 8%, var(--bg-primary));
    border-color: color-mix(in srgb, var(--accent-color) 25%, transparent);
  }

  .port-row:disabled {
    cursor: default;
  }

  .port-name {
    font-weight: 600;
  }

  .port-speed,
  .arrow {
    color: var(--text-tertiary);
  }

  .not-cabled {
    font-style: italic;
  }

  .patch-port {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 6px;
    align-items: baseline;
    padding: 2px 0;
  }

  .patch-port .port-name {
    grid-row: span 2;
    font-size: 12px;
    padding-top: 5px;
  }

  .patch-side {
    padding: 2px 6px;
  }

  .side-label {
    width: 32px;
    color: var(--text-tertiary);
  }

  .via-count {
    margin-left: 4px;
    font-size: 11px;
    color: var(--text-tertiary);
  }

  .incomplete-text {
    color: var(--warning-text);
  }

  .path {
    list-style: none;
    margin: 0;
    padding: 0 0 0 12px;
    border-left: 2px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .step {
    position: relative;
  }

  .step::before {
    content: '';
    position: absolute;
    left: -18px;
    top: 5px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--bg-primary);
    border: 2px solid var(--accent-color);
  }

  .step.cable::before {
    border-radius: 2px;
    border-color: var(--text-tertiary);
  }

  .step.cable.current::before {
    border-color: var(--accent-color);
    background: var(--accent-color);
  }

  .step.through::before {
    border-radius: 2px;
    transform: rotate(45deg);
    border-color: var(--text-secondary);
  }

  .step.dangling::before {
    border-color: var(--warning-color);
  }

  .step-link {
    padding: 0;
    font: inherit;
    font-weight: 600;
    color: var(--text-primary);
    background: none;
    border: none;
    cursor: pointer;
  }

  .step-link:hover {
    color: var(--accent-color);
  }

  .title-sep {
    margin: 0 4px;
    font-weight: 400;
    color: var(--text-tertiary);
  }

  .step-title {
    font-weight: 600;
    margin-bottom: 2px;
  }

  .step-desc {
    margin-top: 3px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .link-up {
    color: var(--success-text);
  }

  .link-stale,
  .stale-text {
    color: var(--warning-text);
  }

  .link-down {
    color: var(--danger-text);
  }

  .panel-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 14px;
  }

  .stats {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 4px 12px;
    margin: 0 0 12px;
  }

  .stats dt {
    color: var(--text-secondary);
  }

  .stats dd {
    margin: 0;
    font-weight: 600;
    text-align: right;
  }

  .hint {
    font-size: 12px;
    line-height: 1.5;
  }

  .count-badge {
    min-width: 18px;
    padding: 0 5px;
    border-radius: 9px;
    font-size: 11px;
    font-weight: 700;
    line-height: 18px;
    text-align: center;
    color: #fff;
    background: var(--danger-color);
  }

  .line.lldp-mismatch {
    border-top: 3px solid var(--danger-color);
  }

  .line.lldp-seen {
    border-top-color: #2563eb;
  }

  .line.lldp-seen-bad {
    border-top-color: var(--danger-color);
  }

  .lldp-badge {
    padding: 0 6px;
    border-radius: 3px;
    font-size: 10.5px;
    font-weight: 600;
    line-height: 17px;
    white-space: nowrap;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .lldp-badge.lldp-match {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .lldp-badge.lldp-mismatch,
  .lldp-badge.lldp-unmapped_local_port {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .lldp-badge.lldp-missing_cable,
  .lldp-badge.lldp-unknown_neighbour {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .lldp-switches,
  .lldp-list {
    list-style: none;
    margin: 0 0 10px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .lldp-switches li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 8px;
    font-size: 12px;
  }

  .lldp-switch-name {
    font-weight: 600;
  }

  .lldp-switch-state {
    flex: 1;
    min-width: 0;
  }

  .lldp-read {
    height: 22px;
    padding: 0 8px;
    font-size: 11px;
  }

  .lldp-detail {
    flex-basis: 100%;
    font-size: 11.5px;
    color: var(--text-secondary);
  }

  .lldp-compare {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 0;
    font-size: 12.5px;
  }

  .lldp-compare dt {
    color: var(--text-tertiary);
  }

  .lldp-compare dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .link-button {
    padding: 0;
    margin-bottom: 8px;
    font: inherit;
    font-size: 12px;
    color: var(--accent-color);
    background: none;
    border: none;
    cursor: pointer;
  }

  .link-button:hover {
    text-decoration: underline;
  }

  .error-text {
    color: var(--danger-text);
  }

  .small {
    font-size: 12px;
  }

  .muted {
    color: var(--text-tertiary);
    font-weight: 400;
  }

  .mono {
    font-family: var(--font-mono);
    font-size: 0.95em;
  }

  @media (max-width: 1100px) {
    .workspace {
      flex-direction: column;
    }

    .canvas-wrap {
      min-height: 480px;
    }

    .panel {
      width: auto;
    }

    .tools {
      margin-left: 0;
    }
  }
</style>
