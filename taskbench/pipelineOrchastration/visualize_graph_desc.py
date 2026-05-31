from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_INPUT = Path("taskbench/data_multimedia/graph_desc.json")
DEFAULT_OUTPUT = Path("taskbench/data_multimedia/graph_desc_visualization.html")

TYPE_COLORS = {
    "url": "#6b7280",
    "text": "#2563eb",
    "image": "#16a34a",
    "audio": "#d97706",
    "video": "#dc2626",
    "other": "#7c3aed",
}


def _norm_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in TYPE_COLORS else "other"


def _primary_output_type(node: Dict[str, Any]) -> str:
    output_types = node.get("output-type", [])
    if isinstance(output_types, list) and output_types:
        return _norm_type(output_types[0])
    return "other"


def _node_group_positions(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    groups: Dict[str, List[Dict[str, Any]]] = {key: [] for key in TYPE_COLORS}
    for node in nodes:
        groups.setdefault(_primary_output_type(node), []).append(node)

    centers: Dict[str, Tuple[float, float]] = {
        "url": (220.0, 180.0),
        "text": (650.0, 270.0),
        "image": (1080.0, 220.0),
        "audio": (980.0, 720.0),
        "video": (1450.0, 470.0),
        "other": (320.0, 760.0),
    }
    positions: Dict[str, Dict[str, float]] = {}
    for group, items in groups.items():
        if not items:
            continue
        items = sorted(items, key=lambda item: str(item.get("id", "")))
        cx, cy = centers.get(group, centers["other"])
        radius = max(80.0, min(220.0, 42.0 * len(items) / math.pi))
        if len(items) == 1:
            node_id = str(items[0].get("id", ""))
            positions[node_id] = {"x": cx, "y": cy}
            continue
        for idx, item in enumerate(items):
            angle = (2.0 * math.pi * idx / len(items)) - math.pi / 2.0
            node_id = str(item.get("id", ""))
            positions[node_id] = {
                "x": cx + radius * math.cos(angle),
                "y": cy + radius * math.sin(angle),
            }
    return positions


def _stats(nodes: List[Dict[str, Any]], links: List[Dict[str, Any]]) -> Dict[str, Any]:
    in_degree = Counter(str(link.get("target", "")) for link in links)
    out_degree = Counter(str(link.get("source", "")) for link in links)
    edge_types = Counter(_norm_type(link.get("type")) for link in links)
    node_types = Counter(_primary_output_type(node) for node in nodes)
    return {
        "node_count": len(nodes),
        "edge_count": len(links),
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
        "top_out_degree": out_degree.most_common(8),
        "top_in_degree": in_degree.most_common(8),
    }


def _build_html(
    *,
    source_path: Path,
    nodes: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    positions: Dict[str, Dict[str, float]],
    stats: Dict[str, Any],
) -> str:
    payload = {
        "sourcePath": str(source_path.as_posix()),
        "nodes": nodes,
        "links": links,
        "positions": positions,
        "stats": stats,
        "typeColors": TYPE_COLORS,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>graph_desc visualization</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #111827;
      --muted: #6b7280;
      --border: #d1d5db;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .app {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--border);
      background: var(--panel);
      padding: 18px 16px;
      overflow: auto;
    }
    main {
      min-width: 0;
      overflow: hidden;
      position: relative;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .source {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
      margin-bottom: 18px;
    }
    .section {
      border-top: 1px solid var(--border);
      padding-top: 14px;
      margin-top: 14px;
    }
    .section h2 {
      margin: 0 0 10px;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0;
      color: #374151;
    }
    .stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .stat {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      background: #f9fafb;
    }
    .stat .value {
      font-size: 22px;
      font-weight: 700;
      line-height: 1;
    }
    .stat .label {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    button.chip {
      border: 1px solid var(--border);
      background: #ffffff;
      color: #111827;
      border-radius: 8px;
      padding: 7px 9px;
      font-size: 12px;
      cursor: pointer;
    }
    button.chip.active {
      border-color: #111827;
      background: #111827;
      color: #ffffff;
    }
    .legend-row {
      display: grid;
      grid-template-columns: 14px 1fr auto;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      margin: 7px 0;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 999px;
    }
    input[type="search"] {
      width: 100%;
      height: 36px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0 10px;
      font-size: 13px;
    }
    ol {
      margin: 0;
      padding-left: 20px;
      color: #374151;
      font-size: 12px;
      line-height: 1.5;
    }
    #graph {
      width: 100%;
      height: 100vh;
      display: block;
      background:
        linear-gradient(#e5e7eb 1px, transparent 1px),
        linear-gradient(90deg, #e5e7eb 1px, transparent 1px);
      background-size: 40px 40px;
      cursor: grab;
    }
    #graph:active { cursor: grabbing; }
    .link {
      fill: none;
      stroke-width: 1.5;
      stroke-opacity: 0.2;
      transition: stroke-opacity 120ms ease, stroke-width 120ms ease;
    }
    .node circle {
      stroke: #ffffff;
      stroke-width: 2.5;
      filter: drop-shadow(0 3px 5px rgba(15, 23, 42, 0.18));
      transition: r 120ms ease, stroke-width 120ms ease, opacity 120ms ease;
    }
    .node text {
      font-size: 12px;
      paint-order: stroke;
      stroke: #ffffff;
      stroke-width: 4px;
      stroke-linejoin: round;
      fill: #111827;
      pointer-events: none;
    }
    .dim { opacity: 0.12; }
    .hidden { display: none; }
    .node.focus circle {
      r: 13;
      stroke: #111827;
      stroke-width: 3;
    }
    .node.matched circle {
      stroke: #facc15;
      stroke-width: 4;
    }
    .link.focus {
      stroke-opacity: 0.95;
      stroke-width: 3;
    }
    .tooltip {
      position: absolute;
      top: 18px;
      right: 18px;
      width: min(430px, calc(100% - 36px));
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: var(--shadow);
      padding: 14px;
      pointer-events: none;
      display: none;
    }
    .tooltip h3 {
      margin: 0 0 8px;
      font-size: 15px;
    }
    .tooltip p {
      margin: 6px 0;
      font-size: 12px;
      line-height: 1.45;
      color: #374151;
    }
    .toolbar {
      position: absolute;
      left: 18px;
      top: 18px;
      display: flex;
      gap: 8px;
    }
    .tool {
      width: 34px;
      height: 34px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 3px 12px rgba(15, 23, 42, 0.12);
      cursor: pointer;
      font-size: 16px;
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>graph_desc visualization</h1>
      <div class="source" id="sourcePath"></div>

      <div class="stat-grid">
        <div class="stat"><div class="value" id="nodeCount"></div><div class="label">nodes</div></div>
        <div class="stat"><div class="value" id="edgeCount"></div><div class="label">edges</div></div>
      </div>

      <div class="section">
        <h2>Search Nodes</h2>
        <input id="search" type="search" placeholder="Type a tool name">
      </div>

      <div class="section">
        <h2>Edge Type Filter</h2>
        <div class="chips" id="filters"></div>
      </div>

      <div class="section">
        <h2>Legend</h2>
        <div id="legend"></div>
      </div>

      <div class="section">
        <h2>Top Out Degree</h2>
        <ol id="topOut"></ol>
      </div>

      <div class="section">
        <h2>Top In Degree</h2>
        <ol id="topIn"></ol>
      </div>
    </aside>

    <main>
      <div class="toolbar">
        <button class="tool" id="zoomIn" title="Zoom in">+</button>
        <button class="tool" id="zoomOut" title="Zoom out">-</button>
        <button class="tool" id="reset" title="Reset view">R</button>
      </div>
      <svg id="graph" viewBox="0 0 1700 980" role="img" aria-label="Tool compatibility graph"></svg>
      <div class="tooltip" id="tooltip"></div>
    </main>
  </div>

  <script id="graph-data" type="application/json">__PAYLOAD__</script>
  <script>
    const data = JSON.parse(document.getElementById("graph-data").textContent);
    const nodes = data.nodes;
    const links = data.links;
    const positions = data.positions;
    const colors = data.typeColors;
    const nodeById = new Map(nodes.map(node => [node.id, node]));
    const inEdges = new Map();
    const outEdges = new Map();
    for (const link of links) {
      if (!outEdges.has(link.source)) outEdges.set(link.source, []);
      if (!inEdges.has(link.target)) inEdges.set(link.target, []);
      outEdges.get(link.source).push(link);
      inEdges.get(link.target).push(link);
    }

    const svg = document.getElementById("graph");
    const tooltip = document.getElementById("tooltip");
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    svg.appendChild(g);

    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    for (const [type, color] of Object.entries(colors)) {
      const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
      marker.setAttribute("id", `arrow-${type}`);
      marker.setAttribute("viewBox", "0 0 10 10");
      marker.setAttribute("refX", "8");
      marker.setAttribute("refY", "5");
      marker.setAttribute("markerWidth", "5");
      marker.setAttribute("markerHeight", "5");
      marker.setAttribute("orient", "auto-start-reverse");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
      path.setAttribute("fill", color);
      marker.appendChild(path);
      defs.appendChild(marker);
    }
    svg.appendChild(defs);

    let selectedType = "all";
    let searchTerm = "";
    let scale = 0.82;
    let tx = 46;
    let ty = 70;
    let isDragging = false;
    let dragStart = null;

    function normType(value) {
      const text = String(value || "").trim().toLowerCase();
      return colors[text] ? text : "other";
    }

    function outputType(node) {
      return normType((node["output-type"] || [])[0]);
    }

    function pathFor(link) {
      const s = positions[link.source];
      const t = positions[link.target];
      if (!s || !t) return "";
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const offset = 18;
      const sx = s.x + dx / len * offset;
      const sy = s.y + dy / len * offset;
      const tx2 = t.x - dx / len * offset;
      const ty2 = t.y - dy / len * offset;
      const curve = Math.min(140, Math.max(35, len * 0.18));
      const mx = (sx + tx2) / 2 - dy / len * curve;
      const my = (sy + ty2) / 2 + dx / len * curve;
      return `M ${sx} ${sy} Q ${mx} ${my} ${tx2} ${ty2}`;
    }

    function setTransform() {
      g.setAttribute("transform", `translate(${tx}, ${ty}) scale(${scale})`);
    }

    function edgeKey(link) {
      return `${link.source}|||${link.target}|||${normType(link.type)}`;
    }

    const linkElements = links.map(link => {
      const type = normType(link.type);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "link");
      path.setAttribute("d", pathFor(link));
      path.setAttribute("stroke", colors[type]);
      path.setAttribute("marker-end", `url(#arrow-${type})`);
      path.dataset.source = link.source;
      path.dataset.target = link.target;
      path.dataset.type = type;
      path.dataset.key = edgeKey(link);
      g.appendChild(path);
      return path;
    });

    const nodeElements = nodes.map(node => {
      const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      const pos = positions[node.id] || {x: 0, y: 0};
      const type = outputType(node);
      group.setAttribute("class", "node");
      group.setAttribute("transform", `translate(${pos.x}, ${pos.y})`);
      group.dataset.id = node.id;
      group.dataset.type = type;

      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("r", "10");
      circle.setAttribute("fill", colors[type]);
      group.appendChild(circle);

      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", "14");
      label.setAttribute("y", "4");
      label.textContent = node.id;
      group.appendChild(label);

      group.addEventListener("mouseenter", () => focusNode(node.id));
      group.addEventListener("mouseleave", clearFocus);
      g.appendChild(group);
      return group;
    });

    function updateFilters() {
      for (const el of linkElements) {
        const visible = selectedType === "all" || el.dataset.type === selectedType;
        el.classList.toggle("hidden", !visible);
      }
      for (const el of nodeElements) {
        const id = el.dataset.id.toLowerCase();
        const matched = searchTerm && id.includes(searchTerm);
        el.classList.toggle("matched", Boolean(matched));
      }
    }

    function focusNode(id) {
      const relevant = new Set();
      for (const link of outEdges.get(id) || []) relevant.add(edgeKey(link));
      for (const link of inEdges.get(id) || []) relevant.add(edgeKey(link));
      for (const el of linkElements) {
        const isVisible = selectedType === "all" || el.dataset.type === selectedType;
        const isFocus = relevant.has(el.dataset.key) && isVisible;
        el.classList.toggle("focus", isFocus);
        el.classList.toggle("dim", !isFocus);
      }
      for (const el of nodeElements) {
        const isNeighbor = el.dataset.id === id
          || (outEdges.get(id) || []).some(link => link.target === el.dataset.id)
          || (inEdges.get(id) || []).some(link => link.source === el.dataset.id);
        el.classList.toggle("focus", el.dataset.id === id);
        el.classList.toggle("dim", !isNeighbor);
      }
      showTooltip(id);
    }

    function clearFocus() {
      for (const el of linkElements) el.classList.remove("focus", "dim");
      for (const el of nodeElements) el.classList.remove("focus", "dim");
      tooltip.style.display = "none";
      updateFilters();
    }

    function showTooltip(id) {
      const node = nodeById.get(id);
      if (!node) return;
      const incoming = inEdges.get(id) || [];
      const outgoing = outEdges.get(id) || [];
      tooltip.innerHTML = `
        <h3>${node.id}</h3>
        <p>${node.desc || ""}</p>
        <p><strong>Input:</strong> ${(node["input-type"] || []).join(", ") || "none"}</p>
        <p><strong>Output:</strong> ${(node["output-type"] || []).join(", ") || "none"}</p>
        <p><strong>In / Out degree:</strong> ${incoming.length} / ${outgoing.length}</p>
      `;
      tooltip.style.display = "block";
    }

    function renderSidebar() {
      document.getElementById("sourcePath").textContent = data.sourcePath;
      document.getElementById("nodeCount").textContent = data.stats.node_count;
      document.getElementById("edgeCount").textContent = data.stats.edge_count;

      const filters = document.getElementById("filters");
      const types = ["all", ...Object.keys(data.stats.edge_types)];
      for (const type of types) {
        const button = document.createElement("button");
        button.className = "chip" + (type === selectedType ? " active" : "");
        button.textContent = type === "all" ? "all" : `${type} ${data.stats.edge_types[type] || ""}`;
        button.addEventListener("click", () => {
          selectedType = type;
          for (const child of filters.children) child.classList.remove("active");
          button.classList.add("active");
          updateFilters();
        });
        filters.appendChild(button);
      }

      const legend = document.getElementById("legend");
      for (const [type, color] of Object.entries(colors)) {
        const row = document.createElement("div");
        row.className = "legend-row";
        row.innerHTML = `<span class="swatch" style="background:${color}"></span><span>${type}</span><span>${data.stats.node_types[type] || 0}</span>`;
        legend.appendChild(row);
      }

      function fillList(id, rows) {
        const list = document.getElementById(id);
        for (const [name, count] of rows) {
          const li = document.createElement("li");
          li.textContent = `${name} (${count})`;
          list.appendChild(li);
        }
      }
      fillList("topOut", data.stats.top_out_degree);
      fillList("topIn", data.stats.top_in_degree);
    }

    document.getElementById("search").addEventListener("input", event => {
      searchTerm = event.target.value.trim().toLowerCase();
      updateFilters();
    });
    document.getElementById("zoomIn").addEventListener("click", () => { scale *= 1.18; setTransform(); });
    document.getElementById("zoomOut").addEventListener("click", () => { scale /= 1.18; setTransform(); });
    document.getElementById("reset").addEventListener("click", () => { scale = 0.82; tx = 46; ty = 70; setTransform(); });

    svg.addEventListener("wheel", event => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.08 : 0.92;
      scale = Math.max(0.25, Math.min(2.5, scale * factor));
      setTransform();
    }, {passive: false});
    svg.addEventListener("pointerdown", event => {
      isDragging = true;
      dragStart = {x: event.clientX, y: event.clientY, tx, ty};
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", event => {
      if (!isDragging || !dragStart) return;
      tx = dragStart.tx + event.clientX - dragStart.x;
      ty = dragStart.ty + event.clientY - dragStart.y;
      setTransform();
    });
    svg.addEventListener("pointerup", () => { isDragging = false; dragStart = null; });
    svg.addEventListener("pointercancel", () => { isDragging = false; dragStart = null; });

    renderSidebar();
    setTransform();
    updateFilters();
  </script>
</body>
</html>
""".replace("__PAYLOAD__", payload_json.replace("</script>", "<\\/script>"))


def build_visualization(input_path: Path, output_path: Path) -> None:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    links = data.get("links", [])
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("graph_desc.json must contain list fields: nodes and links")
    positions = _node_group_positions(nodes)
    html = _build_html(
        source_path=input_path,
        nodes=nodes,
        links=links,
        positions=positions,
        stats=_stats(nodes, links),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a standalone HTML visualization for graph_desc.json.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_visualization(args.input, args.output)
    print(f"[DONE] wrote {args.output}")


if __name__ == "__main__":
    main()
