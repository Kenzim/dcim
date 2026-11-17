<script>
  import { onMount, onDestroy, tick } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { getCablingGraph, getLocations, getRacks } from '../lib/api.js';
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

  let cyContainer;
  let cy = null;
  let cytoscapeLib = null;
  let loadingLib = true;
  let loading = false;
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

  let colourMode = 'speed';
  let showPortLabels = false;
  let search = '';
  let searchMessage = '';

  let selection = null;
  let unsubscribeTheme = null;
  let resizeObserver = null;
  let loadSeq = 0;
  let destroyed = false;

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
  }

  function writeParams() {
    const params = new URLSearchParams();
    if (locationId) params.set('location', locationId);
    if (rackId) params.set('rack', rackId);
    if (selection?.type === 'device') params.set('focus', selection.id);
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
    for (const e of g.edges) counts[e.link_status] = (counts[e.link_status] || 0) + 1;
    return {
      servers: g.nodes.filter((n) => n.kind === 'server' && n.in_scope).length,
      switches: g.nodes.filter((n) => n.kind === 'switch' && n.in_scope).length,
      external: g.nodes.filter((n) => !n.in_scope).length,
      cables: g.edges.length,
      ...counts,
    };
  }

  function deviceUrl(node) {
    return node.kind === 'server' ? `/admin/servers/${node.device_id}` : `/admin/switches/${node.device_id}`;
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
      const sub = n.kind === 'switch' ? placement(n) || n.model || '' : unitLabel(n);
      const data = {
        id: n.id,
        kind: n.kind,
        rackUnit: n.rack_unit,
        label: sub ? `${n.name}\n${sub}` : n.name,
      };
      if (n.kind === 'server') data.parent = n.rack_id ? `rack:${n.rack_id}` : `norack:${n.location_id}`;
      elements.push({ group: 'nodes', classes: classes.join(' '), data });
    }

    const groupOf = (node) => (node.kind === 'server' ? (node.rack_id ? `rack:${node.rack_id}` : `norack:${node.location_id}`) : null);
    const proxies = new Set();
    for (const e of g.edges) {
      const ends = [nodesById.get(e.source), nodesById.get(e.target)];
      const sw = ends.find((n) => n.kind === 'switch');
      const srv = ends.find((n) => n.kind === 'server');
      if (sw && srv) proxies.add(`${sw.id}|${groupOf(srv)}`);
    }
    for (const key of proxies) {
      const [source, target] = key.split('|');
      elements.push({ group: 'edges', classes: 'proxy', data: { id: `proxy:${key}`, source, target } });
    }

    for (const e of g.edges) {
      const a = portsById.get(e.source_port);
      const b = portsById.get(e.target_port);
      const aNode = nodesById.get(e.source);
      const bNode = nodesById.get(e.target);
      let flip = aNode.kind === 'server' && bNode.kind === 'switch';
      if (aNode.kind === bNode.kind) flip = aNode.name.localeCompare(bNode.name) > 0;
      const [src, dst, srcPort, dstPort] = flip ? [e.target, e.source, b, a] : [e.source, e.target, a, b];
      elements.push({
        group: 'edges',
        classes: LINK_COLOURS[e.link_status] ? e.link_status : 'unknown',
        data: {
          id: e.id,
          cableId: e.cable_id,
          source: src,
          target: dst,
          sourceLabel: srcPort.name,
          targetLabel: dstPort.name,
          speedColour: speedColour(e.effective_speed_mbps),
          statusColour: LINK_COLOURS[e.link_status] || LINK_COLOURS.unknown,
        },
      });
    }
    return elements;
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
      { selector: 'node.server, node.switch', style: { 'z-compound-depth': 'top' } },
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
      { selector: 'edge.down', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
      { selector: 'edge.unknown, edge.stale', style: { 'line-style': 'dotted' } },
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
      name: 'elk',
      fit: true,
      padding: 30,
      animate: false,
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
          const incoming = n.incomers('edge').not('.proxy').length;
          return incoming === 0 ? { 'elk.layered.layering.layerConstraint': 'FIRST' } : {};
        }
        return { 'elk.priority': String(n.data('rackUnit') ?? 0) };
      },
      elk: {
        algorithm: 'layered',
        'elk.direction': 'DOWN',
        'elk.hierarchyHandling': 'SEPARATE_CHILDREN',
        'elk.layered.layering.strategy': 'LONGEST_PATH',
        'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
        'elk.spacing.nodeNode': 36,
        'elk.layered.spacing.nodeNodeBetweenLayers': 90,
        'elk.spacing.componentComponent': 48,
      },
    };
  }

  async function loadLibrary() {
    const [{ default: cytoscape }, { default: elk }] = await Promise.all([
      import('cytoscape'),
      import('cytoscape-elk'),
    ]);
    if (!cytoscape.__rackflowElk) {
      cytoscape.use(elk);
      cytoscape.__rackflowElk = true;
    }
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
    } catch (err) {
      if (seq === loadSeq) error = err.message;
    } finally {
      if (seq === loadSeq) loading = false;
    }
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
    cy.on('tap', 'edge', (evt) => selectCable(evt.target.data('cableId')));
    cy.on('tap', (evt) => {
      if (evt.target === cy) clearSelection();
    });
    if (showPortLabels) cy.edges().addClass('ports');
    const layout = cy.layout(layoutOptions());
    layout.one('layoutstop', () => {
      cy.resize();
      cy.fit(undefined, 30);
      if (wanted && nodesById.has(wanted)) {
        selectDevice(wanted, true);
      }
      focusId = null;
    });
    layout.run();
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

  function selectDevice(id, centre = false) {
    if (!cy) return;
    const ele = cy.getElementById(id);
    if (ele.empty()) return;
    const cables = ele.connectedEdges().not('.proxy');
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
    highlight(ele.union(ele.connectedNodes()), ele);
    selection = { type: 'path', cableId, fromPortId };
    writeParams();
  }

  function pathSteps(sel) {
    const edge = edgesByCable.get(sel.cableId);
    if (!edge) return null;
    let first = portsById.get(edge.source_port);
    let second = portsById.get(edge.target_port);
    const startHere = sel.fromPortId
      ? sel.fromPortId === second.id
      : nodesById.get(second.device)?.kind === 'server' && nodesById.get(first.device)?.kind !== 'server';
    if (startHere) [first, second] = [second, first];
    return {
      edge,
      a: { port: first, node: nodesById.get(first.device) },
      b: { port: second, node: nodesById.get(second.device) },
    };
  }

  function peerOf(port) {
    if (!port.cable_id) return null;
    const edge = edgesByCable.get(port.cable_id);
    if (!edge) return null;
    const otherId = edge.source_port === port.id ? edge.target_port : edge.source_port;
    const other = portsById.get(otherId);
    return other ? { port: other, node: nodesById.get(other.device), edge } : null;
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
    cy?.layout(layoutOptions()).run();
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
      {#if loadingLib || loading}
        <div class="overlay">{loadingLib ? 'Loading graph library…' : 'Loading cabling…'}</div>
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
        <div class="legend-row"><span class="box"></span>Server</div>
        <div class="legend-row"><span class="box locked"></span>Locked</div>
        <div class="legend-row"><span class="box external"></span>Outside filter</div>
      </details>
    </div>

    <aside class="panel">
      {#if selection?.type === 'device' && nodesById.get(selection.id)}
        {@const node = nodesById.get(selection.id)}
        {@const ports = portsByDevice.get(node.id) || []}
        <div class="panel-head">
          <span class="kind">{node.kind === 'switch' ? 'Switch' : 'Server'}</span>
          <h3><a href={deviceUrl(node)}>{node.name}</a></h3>
          <div class="meta">
            {#if placement(node)}<span>{placement(node)}</span>{/if}
            {#if locationLabel(node.location_id)}<span>{locationLabel(node.location_id)}</span>{/if}
            {#if node.model}<span>{node.model}</span>{/if}
            {#if node.ip}<span class="mono">{node.ip}</span>{/if}
          </div>
          <div class="badges">
            <span class="badge" class:ok={node.enabled} class:off={!node.enabled}>{node.enabled ? 'Enabled' : 'Disabled'}</span>
            {#if node.locked}<span class="badge warn">Locked</span>{/if}
            {#if !node.in_scope}<span class="badge">Outside filter</span>{/if}
          </div>
        </div>
        <h4>
          {node.kind === 'switch' ? 'Cabled ports' : 'Network ports'}
          {#if node.kind === 'switch'}<span class="muted">({ports.length} of {node.port_count})</span>{/if}
        </h4>
        {#if ports.length === 0}
          <p class="muted">{node.kind === 'switch' ? 'No cabled ports in this view.' : 'No network ports recorded.'}</p>
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
                  {#if peer}
                    <span class="arrow">&rarr;</span>
                    <span class="peer">{peer.node.name} <span class="mono">{peer.port.name}</span></span>
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
            {path.a.node.name} <span class="mono">{path.a.port.name}</span>
            &rarr; cable &rarr;
            {path.b.node.name} <span class="mono">{path.b.port.name}</span>
          </h3>
        </div>
        <ol class="path">
          {#each [path.a, path.b] as end, i}
            <li class="step">
              <div class="step-title"><a href={deviceUrl(end.node)}>{end.node.name}</a> <span class="mono">{end.port.name}</span></div>
              <div class="step-meta">
                <span>{formatSpeed(end.port.speed_mbps)}</span>
                {#if end.port.mac_address}<span class="mono">{end.port.mac_address}</span>{/if}
                {#if portStatusText(end.port)}<span class:stale-text={end.port.kind === 'switch' && !end.port.status_fresh}>{portStatusText(end.port)}</span>{/if}
                {#if end.port.lag_group}<span>LAG {end.port.lag_group}</span>{/if}
                {#if end.port.description}<span>{end.port.description}</span>{/if}
              </div>
            </li>
            {#if i === 0}
              <li class="step cable">
                <div class="step-title">Cable #{path.edge.cable_id}</div>
                <div class="step-meta">
                  <span>{path.edge.cable_type || 'type not set'}</span>
                  <span>{formatSpeed(path.edge.effective_speed_mbps)}</span>
                  <span class="link-{path.edge.link_status}">link {LINK_LABELS[path.edge.link_status] || path.edge.link_status}</span>
                </div>
                {#if path.edge.description}<div class="step-desc">{path.edge.description}</div>{/if}
              </li>
            {/if}
          {/each}
        </ol>
        <div class="panel-actions">
          <button type="button" class="btn-secondary btn-sm" on:click={() => selectDevice(path.a.node.id, true)}>Show {path.a.node.name}</button>
          <button type="button" class="btn-secondary btn-sm" on:click={() => selectDevice(path.b.node.id, true)}>Show {path.b.node.name}</button>
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
            <dt>Cables</dt><dd>{stats.cables}</dd>
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
