/* Local SVG graph explorer. The adapter uses only returned projections and the
   validated plan; a reachability result never becomes an invented direct edge. */
(() => {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const COLORS = ["#b7f34a", "#78d9b8", "#81b8ff", "#d6a1f5", "#f3b95b", "#fa929d", "#80d6ed"];
  const hasId = (value) => (typeof value === "number" && Number.isFinite(value)) || (typeof value === "string" && value.trim() !== "");
  const display = (value) => value !== null && typeof value === "object" ? JSON.stringify(value) : String(value == null ? "" : value);
  const nodeKey = (label, id) => JSON.stringify([label, String(id)]);

  function buildExpandedEvidenceGraph(records, evidence) {
    const nodes = new Map();
    const edges = new Map();
    const count = Array.isArray(records) ? records.length : 0;
    const rows = (values) => Array.isArray(values) ? values.filter((row) => Number.isInteger(row) && row >= 0 && row < count) : [];
    const mergeRows = (item, values) => rows(values).forEach((row) => { if (!item.records.includes(row)) item.records.push(row); });
    evidence.nodes.forEach((item) => {
      if (!item || typeof item.label !== "string" || !item.label || !hasId(item.id)) return;
      const key = nodeKey(item.label, item.id);
      if (!nodes.has(key)) nodes.set(key, { key, id: item.id, label: item.label, properties: {}, records: [] });
      const result = nodes.get(key);
      if (item.properties && typeof item.properties === "object" && !Array.isArray(item.properties)) Object.assign(result.properties, item.properties);
      result.properties.id = item.id;
      mergeRows(result, item.records);
    });
    evidence.edges.forEach((item) => {
      if (!item || !item.source || !item.target || typeof item.label !== "string" || !item.label) return;
      const source = nodeKey(item.source.label, item.source.id);
      const target = nodeKey(item.target.label, item.target.id);
      if (!nodes.has(source) || !nodes.has(target)) return;
      const properties = item.properties && typeof item.properties === "object" && !Array.isArray(item.properties) ? item.properties : {};
      const key = JSON.stringify([source, target, item.label, false, properties]);
      if (!edges.has(key)) edges.set(key, { key, source, target, label: item.label, caption: item.label,
        path: false, properties, records: [] });
      mergeRows(edges.get(key), item.records);
    });
    return { nodes: [...nodes.values()], edges: [...edges.values()] };
  }

  function buildEvidenceGraph(records, graph, plan = {}, evidence = null) {
    if (evidence && Array.isArray(evidence.nodes) && Array.isArray(evidence.edges)) {
      return buildExpandedEvidenceGraph(records, evidence);
    }
    const nodes = new Map();
    const edges = new Map();
    function node(record, prefix, label, row) {
      const id = record[`${prefix}_id`];
      if (!hasId(id)) return null;
      const key = nodeKey(label, id);
      const properties = { id };
      Object.entries(record).forEach(([name, value]) => {
        if (name.startsWith(`${prefix}_`) && value != null) properties[name.slice(prefix.length + 1)] = value;
      });
      if (graph === "callgraph") {
        if (prefix === "function") {
          ["fun_obj_var_id", "source_loc"].forEach((name) => {
            if (record[name] != null) properties[name] = record[name];
          });
        }
        if (record[prefix] != null) properties.name = record[prefix];
      }
      if (graph === "pag" && prefix === "value") {
        if (record.value_kind) properties.kind = record.value_kind;
        if (record.source_location) properties.source_loc = record.source_location;
      }
      if (!nodes.has(key)) nodes.set(key, { key, id, label, properties: {}, records: [] });
      const item = nodes.get(key);
      Object.assign(item.properties, properties);
      if (!item.records.includes(row)) item.records.push(row);
      return item;
    }
    function link(source, target, label, record, row, path = false) {
      if (!source || !target) return;
      const hops = record.path_length == null ? record.call_distance : record.path_length;
      const properties = {};
      ["edge_kind", "flow_kind"].forEach((name) => {
        if (record[name] != null) properties.kind = record[name];
      });
      if (graph === "pag" && !path) {
        ["id", "icfg_node_id", "bb_id"].forEach((name) => {
          if (record[`flow_${name}`] != null) properties[name] = record[`flow_${name}`];
        });
      }
      if (path) {
        if (hops != null) properties.hops = hops;
        else if (plan.depth === null) properties.hop_limit = "Unlimited";
        else if (plan.depth) properties.max_hops = plan.depth;
      }
      const key = JSON.stringify([source.key, target.key, label, path, properties]);
      if (!edges.has(key)) {
        const hopLabel = hops != null ? `${hops} hop${Number(hops) === 1 ? "" : "s"}` : plan.depth === null ? "1+ hops" : `1–${plan.depth || "?"} hops`;
        edges.set(key, { key, source: source.key, target: target.key, label,
          caption: path ? hopLabel : label, path, properties, records: [] });
      }
      edges.get(key).records.push(row);
    }
    (Array.isArray(records) ? records : []).forEach((record, row) => {
      if (!record || typeof record !== "object" || Array.isArray(record)) return;
      if (graph === "icfg") {
        const seed = node(record, "seed", plan.seed_label || "ICFGNode", row);
        const result = node(record, "result", plan.result_label || "ICFGNode", row);
        const incoming = (plan.intent || "").includes("predecessors");
        link(incoming ? result : seed, incoming ? seed : result, plan.edge_label || "CFG edge", record, row,
          (plan.intent || "").includes("reachable"));
      } else if (graph === "callgraph") {
        const roles = {};
        ["function", "caller", "target", "source", "callee"].forEach((role) => {
          roles[role] = node(record, role, "CallGraphNode", row);
        });
        const source = roles.caller || roles.source;
        const target = roles.target || roles.callee;
        link(source, target, "CallGraphEdge", record, row,
          (plan.intent || "").includes("reachable") || Number(record.call_distance) > 1);
      } else if (graph === "pag") {
        if ("seed_id" in record || "result_id" in record) {
          const seed = node(record, "seed", plan.seed_label || "PAGNode", row);
          const result = node(record, "result", plan.result_label || "PAGNode", row);
          const incoming = (plan.intent || "").includes("predecessors");
          link(incoming ? result : seed, incoming ? seed : result, plan.edge_label || "PAG statement", record, row,
            (plan.intent || "").includes("reachable"));
        } else {
          const argument = node(record, "argument", "ArgValVar", row);
          const value = node(record, "value", record.value_kind || "PAGNode", row);
          link(argument, value, "Value-flow path", record, row, true);
        }
      }
    });
    return { nodes: [...nodes.values()], edges: [...edges.values()] };
  }

  function svgElement(tag, attrs = {}, value) {
    const element = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs).forEach(([name, content]) => element.setAttribute(name, content));
    if (value != null) element.textContent = value;
    return element;
  }

  function evidenceForPath(evidence, path) {
    const keys = new Set(path.nodes.map((node) => nodeKey(node.label, node.id)));
    return { nodes: evidence.nodes.filter((node) => keys.has(nodeKey(node.label, node.id))),
      edges: path.edge_indices.map((index) => evidence.edges[index]).filter(Boolean) };
  }

  class EvidenceGraphView {
    constructor() {
      this.svg = document.getElementById("graphSvg");
      this.canvas = document.getElementById("graphCanvas");
      this.root = document.getElementById("evidenceGraph");
      this.pathRoot = document.getElementById("evidencePaths");
      this.paths = [];
      this.nodes = [];
      this.edges = [];
      this.camera = { x: 0, y: 0, scale: 1 };
      this.svg.addEventListener("wheel", (event) => {
        event.preventDefault();
        this.zoom(Math.exp(-event.deltaY * 0.0015), this.localPoint(event));
      }, { passive: false });
      this.svg.addEventListener("pointerdown", (event) => this.startDrag(event));
      this.svg.addEventListener("pointermove", (event) => this.moveDrag(event));
      this.svg.addEventListener("pointerup", (event) => this.endDrag(event));
      this.svg.addEventListener("pointercancel", () => { this.drag = null; });
      this.svg.addEventListener("lostpointercapture", () => { this.drag = null; });
      this.svg.addEventListener("keydown", (event) => {
        if (event.target !== this.svg) return;
        if (event.key === "+" || event.key === "=") this.zoom(1.2);
        else if (event.key === "-") this.zoom(1 / 1.2);
        else if (event.key === "0") this.fit();
        else if (event.key === "Escape") this.select(null);
        else return;
        event.preventDefault();
      });
      document.getElementById("graphZoomIn").addEventListener("click", () => this.zoom(1.25));
      document.getElementById("graphZoomOut").addEventListener("click", () => this.zoom(0.8));
      document.getElementById("graphFit").addEventListener("click", () => this.fit());
      document.getElementById("graphLayout").addEventListener("click", () => { this.layout(); this.drawPositions(); this.fit(); });
      document.getElementById("graphClearSelection").addEventListener("click", () => this.select(null));
      this.resizeObserver = new ResizeObserver(() => { if (this.nodes.length) this.fit(); });
      this.resizeObserver.observe(this.canvas);
    }

    clearGraph() {
      this.nodes = [];
      this.edges = [];
      this.drag = null;
      this.viewport = null;
      this.svg.replaceChildren();
      this.root.classList.add("hidden");
      document.getElementById("pathSteps").classList.add("hidden");
      document.getElementById("pathHopList").replaceChildren();
      this.select(null);
    }

    clear() {
      this.clearGraph();
      // The single graph viewer moves below the expanded path. Restore it
      // before removing old entries so the controls and listeners survive.
      this.pathRoot.after(this.root);
      this.root.removeAttribute("aria-labelledby");
      document.getElementById("pathList").replaceChildren();
      this.pathRoot.classList.add("hidden");
      this.paths = [];
      this.pathEntries = [];
      this.activePath = null;
      this.context = null;
    }

    render(records, graph, plan, evidence = null) {
      this.clear();
      if (evidence && Array.isArray(evidence.paths) && evidence.paths.length) {
        this.context = { records, graph, plan, evidence };
        this.paths = evidence.paths;
        this.pathRoot.classList.remove("hidden");
        document.getElementById("pathCount").textContent = `${this.paths.length} path${this.paths.length === 1 ? "" : "s"}`;
        const status = evidence.complete === false
          ? evidence.stop_reason === "timeout"
            ? "The query time limit was reached. The paths below are partial results."
            : evidence.stop_reason === "result_limit"
              ? "The result limit was reached. More paths may exist; increase the limit to retrieve more."
              : "The analysis reached a scope or traversal limit. The paths below are partial results; see the analysis details."
          : evidence.enumeration === "cycle_witnesses"
            ? "Representative cycle witnesses are listed; this does not enumerate every cycle."
            : "All matching paths within the selected depth are listed.";
        document.getElementById("pathListNote").textContent = `${status} Select a path to see its complete graph and every hop. A path does not revisit a node, except to close a cycle at its starting node.`;
        const list = document.getElementById("pathList");
        this.pathEntries = this.paths.map((path, index) => {
          const entry = document.createElement("div");
          entry.className = "evidence-path";
          const button = document.createElement("button");
          button.type = "button";
          button.className = "path-toggle";
          button.id = `pathToggle${index}`;
          button.setAttribute("aria-expanded", "false");
          button.setAttribute("aria-controls", "evidenceGraph");
          const label = document.createElement("b");
          label.textContent = `Path ${index + 1}`;
          const endpoints = document.createElement("span");
          endpoints.className = "path-endpoints";
          const describe = (node) => `${node.label} #${node.id}`;
          endpoints.textContent = `${describe(path.nodes[0])} → ${describe(path.nodes[path.nodes.length - 1])}`;
          const hops = document.createElement("span");
          hops.className = "path-hop-count";
          hops.textContent = `${path.hop_count} hop${path.hop_count === 1 ? "" : "s"}`;
          const arrow = document.createElement("span");
          arrow.className = "path-chevron";
          arrow.setAttribute("aria-hidden", "true");
          arrow.textContent = "+";
          button.append(label, endpoints, hops, arrow);
          button.addEventListener("click", () => this.showPath(index));
          entry.append(button);
          list.append(entry);
          return { entry, button, arrow };
        });
        return;
      }
      if (evidence && Array.isArray(evidence.nodes) && !evidence.nodes.length) return;
      document.getElementById("graphViewTitle").textContent = "Graph view";
      this.renderGraph(records, graph, plan, evidence);
    }

    showPath(index) {
      const collapse = this.activePath === index;
      this.clearGraph();
      this.activePath = collapse ? null : index;
      this.pathEntries.forEach(({ entry, button, arrow }, i) => {
        const expanded = this.activePath === i;
        button.setAttribute("aria-expanded", String(expanded));
        entry.classList.toggle("expanded", expanded);
        arrow.textContent = expanded ? "−" : "+";
      });
      if (collapse) return;
      const path = this.paths[index];
      const { records, graph, plan, evidence } = this.context;
      this.pathEntries[index].entry.append(this.root);
      this.root.setAttribute("aria-labelledby", `pathToggle${index}`);
      document.getElementById("graphViewTitle").textContent = `Path ${index + 1} · ${path.hop_count} hop${path.hop_count === 1 ? "" : "s"}`;
      this.renderGraph(records, graph, plan, evidenceForPath(evidence, path));
      document.getElementById("graphEvidenceNote").textContent = "This graph shows the selected path, including every intermediate node and relationship. Arrows follow the stored edge direction.";
      const hops = document.getElementById("pathHopList");
      path.edge_indices.forEach((edgeIndex, hop) => {
        const edge = evidence.edges[edgeIndex];
        const item = document.createElement("li");
        const source = path.nodes[hop], target = path.nodes[hop + 1];
        const properties = Object.keys(edge.properties || {}).length ? ` · ${display(edge.properties)}` : "";
        item.textContent = `Hop ${hop + 1}: ${source.label} #${source.id} — ${edge.label} → ${target.label} #${target.id}${properties}`;
        hops.append(item);
      });
      document.getElementById("pathSteps").classList.remove("hidden");
    }

    renderGraph(records, graph, plan, evidence) {
      const data = buildEvidenceGraph(records, graph, plan, evidence);
      this.nodes = data.nodes;
      this.edges = data.edges;
      this.nodeMap = new Map(this.nodes.map((node) => [node.key, node]));
      this.root.classList.remove("hidden");
      const paths = this.edges.filter((edge) => edge.path).length;
      document.getElementById("graphStats").textContent = `${this.nodes.length} nodes · ${this.edges.length - paths} edges${paths ? ` · ${paths} path summaries` : ""}`;
      document.getElementById("graphUnavailable").classList.toggle("hidden", this.nodes.length > 0);
      document.getElementById("graphEvidenceNote").textContent = paths
        ? "Dashed links summarize paths between returned endpoints. Intermediate nodes and edges were not returned."
        : "Only nodes and relationships in the returned records are shown. Repeated relationships are grouped; select one to see matching records.";
      const legend = document.getElementById("graphLegend");
      legend.replaceChildren();
      const labels = [...new Set(this.nodes.map((node) => node.label))].sort();
      labels.forEach((label, index) => {
        const item = document.createElement("span");
        item.style.setProperty("--node-color", COLORS[index % COLORS.length]);
        item.textContent = label;
        legend.append(item);
      });
      this.nodes.forEach((node) => { node.color = COLORS[labels.indexOf(node.label) % COLORS.length]; });
      const defs = svgElement("defs");
      const marker = svgElement("marker", { id: "evidenceArrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
      marker.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#88a698" }));
      defs.append(marker);
      this.viewport = svgElement("g");
      this.svg.append(defs, this.viewport);
      const pairs = new Map();
      this.edges.forEach((edge) => {
        const pair = JSON.stringify([edge.source, edge.target].sort());
        if (!pairs.has(pair)) pairs.set(pair, []);
        pairs.get(pair).push(edge);
      });
      pairs.forEach((group) => group.forEach((edge, index) => {
        edge.curve = (index - (group.length - 1) / 2) * 44;
        edge.loopSize = 90 + index * 24;
        const item = svgElement("g", { class: `evidence-edge${edge.path ? " path-summary" : ""}`, tabindex: 0, role: "button",
          "aria-label": `${edge.label}: ${this.nodeMap.get(edge.source).id} to ${this.nodeMap.get(edge.target).id}${edge.path ? `, ${edge.caption}, path summary` : ""}` });
        edge.element = item;
        edge.line = svgElement("path", { class: "edge-line", "marker-end": "url(#evidenceArrow)" });
        edge.hit = svgElement("path", { class: "edge-hit" });
        edge.text = svgElement("text", { class: "edge-caption", "text-anchor": "middle" }, edge.caption);
        item.append(edge.line, edge.hit, edge.text);
        this.bindSelection(item, edge);
        this.viewport.append(item);
      }));
      this.nodes.forEach((node) => {
        const name = display(node.properties.name || node.properties.val_name || node.label);
        const caption = node.label === "CallGraphNode" ? name.split("::").pop() : name;
        const item = svgElement("g", { class: "evidence-node", tabindex: 0, role: "button", "aria-label": `${node.label} ${node.id}: ${name}` });
        item.style.setProperty("--node-color", node.color);
        node.element = item;
        item.append(svgElement("title", {}, `${node.label} #${node.id}\n${name}`),
          svgElement("circle", { r: 25 }),
          svgElement("text", { class: "node-id", "text-anchor": "middle", dy: "0.35em" }, String(node.id)),
          svgElement("text", { class: "node-caption", "text-anchor": "middle", y: 44 }, caption.length > 26 ? `${caption.slice(0, 23)}…` : caption));
        this.bindSelection(item, node);
        this.viewport.append(item);
      });
      this.layout();
      this.drawPositions();
      this.fit();
    }

    bindSelection(element, item) {
      element.addEventListener("click", () => {
        if (!this.suppressClick) this.select(item);
      });
      element.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this.select(item);
        }
      });
    }

    layout() {
      const count = this.nodes.length;
      // Complete paths can contain many more nodes than result rows. Keep
      // their initial layout linear in size so rendering never drops hops or
      // spends quadratic time settling thousands of intermediate nodes.
      if (count > 200) {
        const columns = Math.ceil(Math.sqrt(count));
        const rows = Math.ceil(count / columns);
        this.nodes.forEach((node, index) => {
          node.x = (index % columns - (columns - 1) / 2) * 170;
          node.y = (Math.floor(index / columns) - (rows - 1) / 2) * 120;
        });
        return;
      }
      const radius = Math.max(100, Math.sqrt(count) * 48);
      this.nodes.forEach((node, index) => {
        const angle = index * Math.PI * 2 / Math.max(count, 1);
        node.x = Math.cos(angle) * radius;
        node.y = Math.sin(angle) * radius;
      });
      // Bounded query results are small; settle once so the graph stays still
      // while reading or inspecting it and needs no animation loop or CDN.
      for (let step = 0; step < 220; step += 1) {
        const forces = new Map(this.nodes.map((node) => [node.key, { x: -node.x * 0.012, y: -node.y * 0.012 }]));
        this.nodes.forEach((a, index) => {
          this.nodes.slice(index + 1).forEach((b) => {
            const dx = a.x - b.x || 0.01;
            const dy = a.y - b.y || 0.01;
            const distance = Math.max(10, Math.hypot(dx, dy));
            const force = 6500 / (distance * distance);
            forces.get(a.key).x += dx / distance * force;
            forces.get(a.key).y += dy / distance * force;
            forces.get(b.key).x -= dx / distance * force;
            forces.get(b.key).y -= dy / distance * force;
          });
        });
        this.edges.forEach((edge) => {
          if (edge.source === edge.target) return;
          const a = this.nodeMap.get(edge.source);
          const b = this.nodeMap.get(edge.target);
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          const force = (distance - 175) * 0.035;
          forces.get(a.key).x += dx / distance * force;
          forces.get(a.key).y += dy / distance * force;
          forces.get(b.key).x -= dx / distance * force;
          forces.get(b.key).y -= dy / distance * force;
        });
        this.nodes.forEach((node) => {
          const force = forces.get(node.key);
          const cooling = 1 - step / 240;
          node.x += Math.max(-12, Math.min(12, force.x)) * cooling;
          node.y += Math.max(-12, Math.min(12, force.y)) * cooling;
        });
      }
    }

    drawPositions() {
      this.nodes.forEach((node) => node.element.setAttribute("transform", `translate(${node.x},${node.y})`));
      this.edges.forEach((edge) => {
        const a = this.nodeMap.get(edge.source);
        const b = this.nodeMap.get(edge.target);
        let path, tx, ty, angle = 0;
        if (a === b) {
          const height = edge.loopSize;
          path = `M ${a.x - 18} ${a.y - 18} C ${a.x - height} ${a.y - height}, ${a.x + height} ${a.y - height}, ${a.x + 18} ${a.y - 18}`;
          tx = a.x;
          ty = a.y - height * 0.75;
        } else {
          const distance = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
          const nx = -(b.y - a.y) / distance;
          const ny = (b.x - a.x) / distance;
          const curve = edge.source < edge.target ? edge.curve : -edge.curve;
          const cx = (a.x + b.x) / 2 + nx * curve;
          const cy = (a.y + b.y) / 2 + ny * curve;
          const startDistance = Math.max(1, Math.hypot(cx - a.x, cy - a.y));
          const endDistance = Math.max(1, Math.hypot(b.x - cx, b.y - cy));
          const sx = a.x + (cx - a.x) / startDistance * 29;
          const sy = a.y + (cy - a.y) / startDistance * 29;
          const ex = b.x - (b.x - cx) / endDistance * 32;
          const ey = b.y - (b.y - cy) / endDistance * 32;
          path = `M ${sx} ${sy} Q ${cx} ${cy} ${ex} ${ey}`;
          tx = (sx + 2 * cx + ex) / 4;
          ty = (sy + 2 * cy + ey) / 4;
          angle = Math.atan2(ey - sy, ex - sx) * 180 / Math.PI;
          if (angle > 90) angle -= 180;
          if (angle < -90) angle += 180;
        }
        edge.line.setAttribute("d", path);
        edge.hit.setAttribute("d", path);
        edge.text.setAttribute("x", tx);
        edge.text.setAttribute("y", ty);
        edge.text.setAttribute("dy", -7);
        edge.text.setAttribute("transform", `rotate(${angle},${tx},${ty})`);
      });
    }

    fit() {
      if (!this.nodes.length || !this.viewport) return;
      const bounds = this.viewport.getBBox();
      const width = this.canvas.clientWidth;
      const height = this.canvas.clientHeight;
      if (!width || !height) return;
      const scale = Math.min(1.5, (width - 64) / Math.max(1, bounds.width), (height - 80) / Math.max(1, bounds.height));
      this.camera = { scale, x: width / 2 - (bounds.x + bounds.width / 2) * scale,
        y: height / 2 - (bounds.y + bounds.height / 2) * scale };
      this.transform();
    }

    transform() {
      if (this.viewport) this.viewport.setAttribute("transform", `translate(${this.camera.x},${this.camera.y}) scale(${this.camera.scale})`);
      document.getElementById("graphZoom").textContent = `${Math.round(this.camera.scale * 100)}%`;
    }

    zoom(factor, point = { x: this.canvas.clientWidth / 2, y: this.canvas.clientHeight / 2 }) {
      if (!this.nodes.length) return;
      const old = this.camera.scale;
      const scale = Math.min(4, Math.max(0.02, old * factor));
      this.camera.x = point.x - (point.x - this.camera.x) * scale / old;
      this.camera.y = point.y - (point.y - this.camera.y) * scale / old;
      this.camera.scale = scale;
      this.transform();
    }

    localPoint(event) {
      const rect = this.svg.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    startDrag(event) {
      if (event.button !== 0 || !this.nodes.length || this.drag) return;
      const nodeElement = event.target.closest(".evidence-node");
      const node = this.nodes.find((item) => item.element === nodeElement);
      const point = this.localPoint(event);
      this.suppressClick = false;
      this.drag = { pointer: event.pointerId, node, point, moved: false,
        x: node ? node.x : this.camera.x, y: node ? node.y : this.camera.y,
        background: !event.target.closest(".evidence-node, .evidence-edge") };
      this.svg.setPointerCapture(event.pointerId);
    }

    moveDrag(event) {
      const drag = this.drag;
      if (!drag || drag.pointer !== event.pointerId) return;
      const point = this.localPoint(event);
      const dx = point.x - drag.point.x;
      const dy = point.y - drag.point.y;
      if (Math.hypot(dx, dy) > 3) drag.moved = true;
      if (!drag.moved) return;
      if (drag.node) {
        drag.node.x = drag.x + dx / this.camera.scale;
        drag.node.y = drag.y + dy / this.camera.scale;
        this.drawPositions();
      } else {
        this.camera.x = drag.x + dx;
        this.camera.y = drag.y + dy;
        this.transform();
      }
    }

    endDrag(event) {
      if (!this.drag || this.drag.pointer !== event.pointerId) return;
      const drag = this.drag;
      this.drag = null;
      this.suppressClick = drag.moved;
      this.svg.releasePointerCapture(event.pointerId);
      // Pointer capture retargets click to the SVG, so select from the saved
      // gesture target as well as supporting keyboard and synthetic clicks.
      if (!drag.moved) {
        const element = document.elementFromPoint(event.clientX, event.clientY);
        const edgeElement = element && element.closest(".evidence-edge");
        const edge = this.edges.find((item) => item.element === edgeElement);
        this.select(drag.node || edge || (drag.background ? null : this.selected));
      }
      setTimeout(() => { this.suppressClick = false; }, 0);
    }

    select(item) {
      this.selected = item;
      this.nodes.forEach((node) => {
        const related = !item || node === item || node.key === item.source || node.key === item.target
          || this.edges.some((edge) => (edge.source === item.key && edge.target === node.key) || (edge.target === item.key && edge.source === node.key));
        if (node.element) {
          node.element.classList.toggle("selected", node === item);
          node.element.classList.toggle("muted", !related);
        }
      });
      this.edges.forEach((edge) => {
        if (edge.element) {
          edge.element.classList.toggle("selected", edge === item);
          edge.element.classList.toggle("muted", !!item && edge !== item && edge.source !== item.key && edge.target !== item.key);
        }
      });
      document.getElementById("graphSelectionTitle").textContent = item ? item.path ? "Path summary" : item.source ? "Relationship" : "Node properties" : "Properties";
      document.getElementById("graphSelectionHint").classList.toggle("hidden", !!item);
      document.getElementById("graphClearSelection").classList.toggle("hidden", !item);
      const list = document.getElementById("graphProperties");
      list.replaceChildren();
      if (!item) return;
      const properties = { label: item.label };
      if (item.source) {
        properties.source = this.nodeMap.get(item.source).id;
        properties.target = this.nodeMap.get(item.target).id;
      }
      Object.assign(properties, item.properties);
      properties.records = item.records.map((row) => row + 1).join(", ");
      Object.entries(properties).forEach(([name, value]) => {
        const term = document.createElement("dt");
        const detail = document.createElement("dd");
        term.textContent = name;
        detail.textContent = display(value);
        list.append(term, detail);
      });
    }
  }

  const api = { buildEvidenceGraph, evidenceForPath, EvidenceGraphView };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.GraphEvidence = api;
})();
