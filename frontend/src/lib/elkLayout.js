// ELK layout for Cytoscape, computed in a web worker so large graphs do not block the page.
// The result is applied with Cytoscape's preset layout.
import ELK from 'elkjs/lib/elk-api.js';
import elkWorkerUrl from 'elkjs/lib/elk-worker.min.js?url';

let elk = null;

function getElk() {
  if (!elk) elk = new ELK({ workerUrl: elkWorkerUrl });
  return elk;
}

/** Stop the layout worker; the next layout starts a fresh one. */
export function stopElkWorker() {
  if (elk) {
    elk.terminateWorker();
    elk = null;
  }
}

function toElkGraph(nodes, edges, layoutOptions, nodeLayoutOptions) {
  const byId = new Map();
  const root = { id: '__root', layoutOptions, children: [], edges: [] };
  nodes.forEach((n) => {
    const entry = { id: n.id() };
    const extra = nodeLayoutOptions ? nodeLayoutOptions(n) : null;
    if (extra && Object.keys(extra).length) entry.layoutOptions = extra;
    if (!n.isParent()) {
      const dims = n.layoutDimensions({ nodeDimensionsIncludeLabels: false });
      entry.width = dims.w;
      entry.height = dims.h;
    }
    byId.set(entry.id, entry);
  });
  nodes.forEach((n) => {
    const entry = byId.get(n.id());
    const parent = n.isChild() ? byId.get(n.parent().id()) : null;
    if (parent) (parent.children ||= []).push(entry);
    else root.children.push(entry);
  });
  const nodeIds = new Set(byId.keys());
  edges.forEach((e) => {
    const source = e.data('source');
    const target = e.data('target');
    if (nodeIds.has(source) && nodeIds.has(target)) root.edges.push({ id: e.id(), sources: [source], targets: [target] });
  });
  return root;
}

// Centre positions of leaf nodes. ELK gives top-left corners relative to the parent node.
function centres(graph) {
  const out = {};
  const walk = (container, ox, oy) => {
    for (const child of container.children || []) {
      const x = ox + (child.x || 0);
      const y = oy + (child.y || 0);
      if (child.children?.length) walk(child, x, y);
      else out[child.id] = { x: x + (child.width || 0) / 2, y: y + (child.height || 0) / 2 };
    }
  };
  walk(graph, 0, 0);
  return out;
}

/**
 * Lay out `eles` (nodes and the edges ELK should consider) and return centre positions by node id.
 * `layoutOptions` go on the root graph; `nodeLayoutOptions(node)` adds per-node options.
 */
export async function computeElkPositions(eles, { layoutOptions, nodeLayoutOptions } = {}) {
  const graph = toElkGraph(eles.nodes(), eles.edges(), layoutOptions || {}, nodeLayoutOptions);
  const result = await getElk().layout(graph);
  return centres(result);
}
