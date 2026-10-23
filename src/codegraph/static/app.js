/* codegraph dashboard — offline, dependency-free front end.
 *
 * Charts are hand-rolled SVG:
 *   - force-directed call graph (Verlet-ish spring layout, custom)
 *   - token trend line/area chart over analysis runs
 *   - symbol-kind donut (arc path generator)
 *   - resolution-strategy bars (CSS widths)
 *
 * No network access other than same-origin /api/* endpoints.
 */
"use strict";

// ---------------------------------------------------------------------------
// config / tiny helpers
// ---------------------------------------------------------------------------

let REPO = new URLSearchParams(location.search).get("repo") || "";
const POLL_MS = 15000;

const $ = (id) => document.getElementById(id);

const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(name, attrs) {
  const el = document.createElementNS(SVGNS, name);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) el.setAttribute(k, String(v));
  }
  return el;
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmt(n) {
  const v = Number(n) || 0;
  return v.toLocaleString("en-US");
}

function compact(n) {
  const v = Number(n) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(abs >= 1e10 ? 0 : 1) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + "M";
  if (abs >= 1e4) return (v / 1e3).toFixed(0) + "k";
  if (abs >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
}

function bytes(n) {
  let v = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
}

function pct(part, whole) {
  if (!whole) return "0.0%";
  return ((part / whole) * 100).toFixed(1) + "%";
}

function shortTime(s) {
  // "2026-10-23 07:18:49" -> "10-23 07:18"
  if (!s) return "—";
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return String(s);
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

function truncate(s, n) {
  s = String(s === null || s === undefined ? "" : s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

const PALETTE = [
  "#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#39c5cf",
  "#f778ba", "#f85149", "#e3b341", "#79c0ff", "#56d364",
  "#ffa657", "#a5d6ff", "#7ee787", "#ff7b72", "#d2a8ff",
];

function colorFor(i) { return PALETTE[i % PALETTE.length]; }

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

function apiUrl(path) {
  const sep = path.includes("?") ? "&" : "?";
  return REPO ? `${path}${sep}repo=${encodeURIComponent(REPO)}` : path;
}

async function getJSON(path) {
  const res = await fetch(apiUrl(path), { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.status + " " + res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(`${path} → ${detail}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// tooltip
// ---------------------------------------------------------------------------

const Tip = (() => {
  let el = null;
  function node() {
    if (!el) {
      el = document.createElement("div");
      el.id = "tip";
      document.body.appendChild(el);
    }
    return el;
  }
  function show(evt, html) {
    const t = node();
    t.innerHTML = html;
    t.style.display = "block";
    move(evt);
  }
  function move(evt) {
    if (!el || el.style.display === "none") return;
    const pad = 14;
    const r = el.getBoundingClientRect();
    let x = evt.clientX + pad;
    let y = evt.clientY + pad;
    if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
    el.style.left = Math.max(6, x) + "px";
    el.style.top = Math.max(6, y) + "px";
  }
  function hide() { if (el) el.style.display = "none"; }
  function bind(target, html) {
    target.addEventListener("mouseenter", (e) => show(e, html));
    target.addEventListener("mousemove", move);
    target.addEventListener("mouseleave", hide);
  }
  return { show, hide, bind };
})();

// ---------------------------------------------------------------------------
// 1. force-directed call graph (pure SVG)
// ---------------------------------------------------------------------------

const CallGraph = (() => {
  const state = {
    nodes: [],
    links: [],
    alpha: 1,
    rootId: null,
    width: 900,
    height: 520,
    lastTraceKey: "",
  };

  const KIND_COLOR = {
    function: "#58a6ff",
    method: "#3fb950",
    class: "#bc8cff",
    struct: "#bc8cff",
    interface: "#39c5cf",
    trait: "#39c5cf",
    enum: "#d29922",
    const: "#e3b341",
    macro: "#f778ba",
    module: "#8b949e",
    impl: "#79c0ff",
    type: "#ffa657",
    unresolved: "#f85149",
  };

  function kindColor(kind) { return KIND_COLOR[kind] || "#8b949e"; }

  // Build graph model from /api/trace payload.
  //
  // /api/trace returns flat BFS lists where every non-root entry carries
  // `depth` and `via` (the name of its immediate parent). We rebuild the tree
  // from those two fields; anything that cannot be attached (shouldn't happen)
  // falls back to the root so no node is ever orphaned.
  function fromTrace(trace) {
    const nodes = new Map();
    const links = [];
    const root = trace.symbol;
    const rootId = "s" + root.id;

    const add = (sym, depth, unresolved) => {
      const id = unresolved ? "u:" + sym.name + ":" + (sym.start_line || 0) : "s" + sym.id;
      if (nodes.has(id)) {
        const n = nodes.get(id);
        if (depth < n.depth) n.depth = depth;
        return id;
      }
      nodes.set(id, {
        id,
        symId: unresolved ? null : (sym.id || null),
        name: sym.name || sym.qualname || "?",
        qualname: sym.qualname || sym.name || "?",
        kind: unresolved ? "unresolved" : (sym.kind || "unknown"),
        path: sym.path || "",
        line: sym.start_line || 0,
        depth: depth,
        unresolved: !!unresolved,
        x: 0, y: 0, vx: 0, vy: 0,
      });
      return id;
    };

    add(root, 0, false);
    state.rootId = rootId;

    const seenLink = new Set();
    const link = (a, b) => {
      if (!a || !b || a === b) return;
      const key = a + "→" + b;
      if (seenLink.has(key)) return;
      seenLink.add(key);
      links.push({ source: a, target: b, kind: "call" });
    };

    // index nodes by depth+name so `via` resolves to the right parent
    const byKey = new Map(); // "depth:name" -> node id
    const index = (sym, depth) => {
      if (sym && sym.name) byKey.set(depth + ":" + sym.name, "s" + sym.id);
    };
    index(root, 0);

    const attach = (entry, dir) => {
      const s = entry.symbol || {};
      const depth = entry.depth || 1;
      const id = add(s, depth, false);
      byKey.set(depth + ":" + (s.name || ""), id);
      if (depth === 0) return;
      let parent = entry.via ? byKey.get((depth - 1) + ":" + entry.via) : null;
      if (!parent) {
        // any node one level shallower, preferring the root
        const shallower = [...nodes.values()].filter((n) => n.depth === depth - 1);
        parent = shallower.length ? shallower[shallower.length - 1].id : rootId;
      }
      if (dir === "out") link(parent, id);
      else link(id, parent);
    };

    for (const c of trace.callees || []) attach(c, "out");
    for (const c of trace.callers || []) attach(c, "in");

    // unresolved callee names as hollow red nodes hanging off the root
    for (const u of trace.unresolved_calls || []) {
      const id = add({ name: u.name, qualname: u.name, start_line: u.line }, 1, true);
      link(rootId, id);
    }

    return { nodes: [...nodes.values()], links };
  }

  // Scale/translate node positions so the layout fills the canvas instead of
  // huddling around the centre when the traced subgraph is small.
  function fitPositions() {
    const { nodes, width, height } = state;
    if (nodes.length < 2) {
      if (nodes[0]) { nodes[0].x = width / 2; nodes[0].y = height / 2; }
      return;
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    }
    const pad = 78;
    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY, 2.4);
    const midX = (minX + maxX) / 2;
    const midY = (minY + maxY) / 2;
    for (const n of nodes) {
      n.x = width / 2 + (n.x - midX) * scale;
      n.y = height / 2 + (n.y - midY) * scale;
    }
  }

  function seedPositions(nodes, width, height) {
    const cx = width / 2;
    const cy = height / 2;
    const byDepth = new Map();
    for (const n of nodes) {
      const d = n.depth || 0;
      if (!byDepth.has(d)) byDepth.set(d, []);
      byDepth.get(d).push(n);
    }
    const maxDepth = Math.max(1, ...[...byDepth.keys()]);
    for (const [d, list] of byDepth) {
      const r = d === 0 ? 0 : 58 + (d / maxDepth) * Math.min(width, height) * 0.34;
      list.forEach((n, i) => {
        const a = (i / Math.max(1, list.length)) * Math.PI * 2 + d * 0.7;
        n.x = cx + Math.cos(a) * r + (Math.random() - 0.5) * 12;
        n.y = cy + Math.sin(a) * r + (Math.random() - 0.5) * 12;
        n.vx = 0; n.vy = 0;
      });
    }
  }

  function radius(n) {
    if (n.depth === 0) return 11;
    if (n.unresolved) return 5;
    return Math.max(4.5, 8 - (n.depth || 1) * 0.8);
  }

  function simulate(iterations) {
    const { nodes, links } = state;
    const n = nodes.length;
    if (n === 0) return;
    const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
    const cx = state.width / 2;
    const cy = state.height / 2;
    const ideal = Math.max(46, Math.min(120, 700 / Math.sqrt(n)));

    // degree for spring strength
    const deg = new Float64Array(n);
    for (const l of links) {
      const a = idx.get(l.source), b = idx.get(l.target);
      if (a !== undefined) deg[a]++;
      if (b !== undefined) deg[b]++;
    }

    const repulsion = 1400 + n * 26;

    for (let it = 0; it < iterations; it++) {
      const k = state.alpha;

      // repulsion (all pairs; n stays small because trace caps at 200)
      for (let i = 0; i < n; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < n; j++) {
          const b = nodes[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
          if (d2 > 160000) continue;
          const d = Math.sqrt(d2);
          const f = (repulsion / d2) * k;
          const ux = (dx / d) * f;
          const uy = (dy / d) * f;
          a.vx += ux; a.vy += uy;
          b.vx -= ux; b.vy -= uy;
        }
      }

      // springs
      for (const l of links) {
        const a = nodes[idx.get(l.source)];
        const b = nodes[idx.get(l.target)];
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const strength = 0.055 * k;
        const f = (d - ideal) * strength;
        const ux = (dx / d) * f;
        const uy = (dy / d) * f;
        a.vx += ux; a.vy += uy;
        b.vx -= ux; b.vy -= uy;
      }

      // gravity toward centre + integrate
      for (let i = 0; i < n; i++) {
        const nd = nodes[i];
        nd.vx += (cx - nd.x) * 0.0022 * k;
        nd.vy += (cy - nd.y) * 0.0022 * k;
        const damp = 0.82;
        nd.vx *= damp;
        nd.vy *= damp;
        const maxV = 26;
        if (nd.vx > maxV) nd.vx = maxV; else if (nd.vx < -maxV) nd.vx = -maxV;
        if (nd.vy > maxV) nd.vy = maxV; else if (nd.vy < -maxV) nd.vy = -maxV;
        nd.x += nd.vx;
        nd.y += nd.vy;
        // soft walls
        const pad = 26;
        if (nd.x < pad) { nd.x = pad; nd.vx = Math.abs(nd.vx) * 0.5; }
        if (nd.x > state.width - pad) { nd.x = state.width - pad; nd.vx = -Math.abs(nd.vx) * 0.5; }
        if (nd.y < pad) { nd.y = pad; nd.vy = Math.abs(nd.vy) * 0.5; }
        if (nd.y > state.height - pad) { nd.y = state.height - pad; nd.vy = -Math.abs(nd.vy) * 0.5; }
      }

      state.alpha = Math.max(0.06, state.alpha * 0.985);
      if (state.alpha <= 0.061) break;
    }
  }

  function emptyMessage(text) {
    const host = $("graph-svg");
    if (!host) return;
    const w = Math.max(320, host.clientWidth || 900);
    const h = host.clientHeight || 520;
    host.setAttribute("viewBox", `0 0 ${w} ${h}`);
    while (host.firstChild) host.removeChild(host.firstChild);
    const t = svgEl("text", {
      x: w / 2, y: h / 2, "text-anchor": "middle", fill: "#8b949e", "font-size": 12,
    });
    t.textContent = text || "no call-graph data";
    host.appendChild(t);
  }

  function render() {
    const host = $("graph-svg");
    if (!host) return;
    const w = Math.max(320, host.clientWidth || 900);
    const h = host.clientHeight || 520;
    state.width = w;
    state.height = h;
    host.setAttribute("viewBox", `0 0 ${w} ${h}`);
    while (host.firstChild) host.removeChild(host.firstChild);

    const { nodes, links } = state;
    if (!nodes.length) {
      emptyMessage("no call-graph data — trace a symbol above");
      return;
    }

    const idx = new Map(nodes.map((nd) => [nd.id, nd]));
    const g = svgEl("g", {});
    host.appendChild(g);

    // arrow marker
    const defs = svgEl("defs", {});
    const marker = svgEl("marker", {
      id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse",
    });
    marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#3d444d" }));
    defs.appendChild(marker);
    g.appendChild(defs);

    const linkLayer = svgEl("g", { stroke: "#3d444d", "stroke-width": 1, fill: "none" });
    const nodeLayer = svgEl("g", {});
    g.appendChild(linkLayer);
    g.appendChild(nodeLayer);

    for (const l of links) {
      const a = idx.get(l.source);
      const b = idx.get(l.target);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const r1 = radius(a) + 2;
      const r2 = radius(b) + 6;
      const x1 = a.x + (dx / d) * r1;
      const y1 = a.y + (dy / d) * r1;
      const x2 = b.x - (dx / d) * r2;
      const y2 = b.y - (dy / d) * r2;
      const line = svgEl("line", {
        x1, y1, x2, y2,
        "stroke-opacity": 0.55,
        "marker-end": "url(#arrow)",
      });
      if (b.unresolved) line.setAttribute("stroke-dasharray", "3 3");
      linkLayer.appendChild(line);
    }

    for (const nd of nodes) {
      const grp = svgEl("g", {
        class: "gnode",
        transform: `translate(${nd.x.toFixed(1)},${nd.y.toFixed(1)})`,
      });
      const r = radius(nd);
      const c = svgEl("circle", {
        r,
        fill: nd.unresolved ? "#0d1117" : kindColor(nd.kind),
        "fill-opacity": nd.unresolved ? 1 : 0.9,
        stroke: nd.unresolved ? "#f85149" : (nd.depth === 0 ? "#ffffff" : "#0d1117"),
        "stroke-width": nd.depth === 0 ? 2 : 1.2,
      });
      if (nd.unresolved) c.setAttribute("stroke-dasharray", "2 2");
      grp.appendChild(c);

      if (nd.depth === 0) {
        const ring = svgEl("circle", {
          r: r + 5, fill: "none", stroke: "#58a6ff", "stroke-opacity": 0.45, "stroke-width": 1,
        });
        grp.appendChild(ring);
      }

      const label = truncate(nd.name, nd.depth === 0 ? 26 : 20);
      const t = svgEl("text", {
        x: 0,
        y: r + 11,
        "text-anchor": "middle",
        fill: nd.depth === 0 ? "#e6edf3" : "#8b949e",
        "font-size": nd.depth === 0 ? 12 : 10,
        "font-weight": nd.depth === 0 ? 600 : 400,
      });
      t.textContent = label;
      grp.appendChild(t);

      const tip = `<b>${esc(nd.qualname)}</b><br>` +
        `kind: ${esc(nd.kind)}<br>` +
        (nd.path ? `file: ${esc(nd.path)}:${nd.line}<br>` : "") +
        `depth: ${nd.depth}`;
      Tip.bind(grp, tip);

      if (nd.symId) {
        grp.style.cursor = "pointer";
        grp.addEventListener("click", () => {
          $("trace-symbol").value = nd.name;
          traceCurrent(nd.name);
        });
      }
      nodeLayer.appendChild(grp);
    }
  }

  function layout(iterations) {
    simulate(iterations);
    fitPositions();
    render();
  }

  function setData(trace) {
    const { nodes, links } = fromTrace(trace);
    state.nodes = nodes;
    state.links = links;
    state.alpha = 1;
    const host = $("graph-svg");
    const w = Math.max(320, (host && host.clientWidth) || 900);
    const h = (host && host.clientHeight) || 520;
    state.width = w;
    state.height = h;
    seedPositions(nodes, w, h);
    // run in a few chunks so the UI stays responsive
    layout(160);
    state.lastTraceKey = traceKey(trace);
  }

  function traceKey(trace) {
    const ids = [];
    for (const c of trace.callees || []) ids.push("o" + c.symbol.id);
    for (const c of trace.callers || []) ids.push("i" + c.symbol.id);
    for (const u of trace.unresolved_calls || []) ids.push("u" + u.name + u.line);
    return trace.symbol.id + "|" + ids.sort().join(",");
  }

  // Re-fetch-free refresh: if the graph shape did not change, keep the layout
  // and only re-render (avoids the graph "jumping" every 15 s).
  function updateData(trace) {
    const key = traceKey(trace);
    if (key === state.lastTraceKey && state.nodes.length) return false;
    setData(trace);
    return true;
  }

  function relayout() {
    if (!state.nodes.length) return;
    state.alpha = 0.85;
    seedPositions(state.nodes, state.width, state.height);
    layout(140);
  }

  function stats() { return { nodes: state.nodes.length, links: state.links.length }; }

  function clear() {
    state.nodes = [];
    state.links = [];
    state.lastTraceKey = "";
    render();
  }

  return { setData, updateData, relayout, stats, render, clear, emptyMessage, kindColor };
})();

// ---------------------------------------------------------------------------
// 2. token trend (pure SVG line + area)
// ---------------------------------------------------------------------------

const TokenTrend = (() => {
  let lastData = null;

  function niceMax(v) {
    if (v <= 0) return 10;
    const exp = Math.floor(Math.log10(v));
    const base = Math.pow(10, exp);
    const f = v / base;
    const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nice * base;
  }

  function render(series, opts) {
    const host = $("trend-svg-host");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);

    if (!series.length) {
      host.innerHTML = '<div class="empty">no analysis runs recorded yet</div>';
      return;
    }

    const W = 900;
    const H = 260;
    const M = { top: 16, right: 16, bottom: 34, left: 62 };
    const iw = W - M.left - M.right;
    const ih = H - M.top - M.bottom;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`,
      class: "trend-svg",
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
      "aria-label": "token consumption per analysis run",
    });

    const keys = ["input", "output"];
    const labels = { input: "input tokens", output: "output tokens", avoided: "avoided (cache)" };
    const colors = { input: "#58a6ff", output: "#3fb950", avoided: "#d29922" };
    const active = opts && opts.keys ? opts.keys : keys;

    let maxV = 0;
    for (const p of series) for (const k of active) maxV = Math.max(maxV, p[k] || 0);
    const yMax = niceMax(maxV || 10);

    // grid + y axis
    const grid = svgEl("g", {});
    svg.appendChild(grid);
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const y = M.top + (ih * i) / steps;
      const val = yMax * (1 - i / steps);
      grid.appendChild(svgEl("line", {
        x1: M.left, x2: W - M.right, y1: y, y2: y,
        stroke: "#21262d", "stroke-width": 1,
      }));
      const t = svgEl("text", {
        x: M.left - 8, y: y + 3.5, "text-anchor": "end",
        fill: "#8b949e", "font-size": 10,
      });
      t.textContent = compact(val);
      grid.appendChild(t);
    }

    const n = series.length;
    const xAt = (i) => (n === 1 ? M.left + iw / 2 : M.left + (iw * i) / (n - 1));
    const yAt = (v) => M.top + ih - (ih * Math.min(v, yMax)) / yMax;

    // x labels (subsample if many runs)
    const labelEvery = Math.max(1, Math.ceil(n / 8));
    for (let i = 0; i < n; i += labelEvery) {
      const t = svgEl("text", {
        x: xAt(i), y: H - 12, "text-anchor": "middle", fill: "#8b949e", "font-size": 10,
      });
      t.textContent = series[i].label;
      svg.appendChild(t);
    }

    // series
    for (const k of active) {
      const pts = series.map((p, i) => [xAt(i), yAt(p[k] || 0)]);

      // a single run has no "trend" — draw a dashed reference level instead of
      // an invisible one-point line
      if (pts.length === 1) {
        svg.appendChild(svgEl("line", {
          x1: M.left, x2: W - M.right, y1: pts[0][1], y2: pts[0][1],
          stroke: colors[k], "stroke-width": 1.5, "stroke-dasharray": "5 4",
          "stroke-opacity": 0.8,
        }));
      }

      // area
      if (pts.length > 1) {
        const areaD = `M ${pts[0][0]} ${M.top + ih} ` +
          pts.map((p) => `L ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ") +
          ` L ${pts[pts.length - 1][0]} ${M.top + ih} Z`;
        const area = svgEl("path", {
          d: areaD, fill: colors[k], "fill-opacity": 0.10, stroke: "none",
        });
        svg.appendChild(area);
      }

      const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
      svg.appendChild(svgEl("path", {
        d, fill: "none", stroke: colors[k], "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }));

      for (let i = 0; i < pts.length; i++) {
        const c = svgEl("circle", {
          cx: pts[i][0], cy: pts[i][1], r: 3.2,
          fill: "#0d1117", stroke: colors[k], "stroke-width": 1.8,
        });
        const p = series[i];
        const tip = `<b>${esc(p.fullLabel || p.label)}</b><br>` +
          `run: ${esc(p.run_id || "—")}<br>` +
          `kind: ${esc(p.kind || "—")}<br>` +
          `input: ${fmt(p.input)}<br>output: ${fmt(p.output)}<br>` +
          `total: ${fmt((p.input || 0) + (p.output || 0))}<br>` +
          `files: ${fmt(p.files_called)} called / ${fmt(p.files_cached)} cached`;
        Tip.bind(c, tip);
        svg.appendChild(c);
      }
    }

    // axis lines
    svg.appendChild(svgEl("line", {
      x1: M.left, x2: W - M.right, y1: M.top + ih, y2: M.top + ih,
      stroke: "#30363d", "stroke-width": 1,
    }));
    svg.appendChild(svgEl("line", {
      x1: M.left, x2: M.left, y1: M.top, y2: M.top + ih,
      stroke: "#30363d", "stroke-width": 1,
    }));

    host.appendChild(svg);

    // legend
    const legend = $("trend-legend");
    if (legend) {
      legend.innerHTML = active.map((k) =>
        `<span class="lg"><i style="background:${colors[k]}"></i>${esc(labels[k])}</span>`
      ).join("") + `<span class="lg" style="margin-left:auto">peak <b>${compact(maxV)}</b> tokens</span>`;
    }
  }

  function setRuns(runs) {
    // runs come newest-first from the API; chart oldest -> newest
    const rows = (runs || []).slice().reverse();
    const series = rows.map((r) => {
      const input = r.input_tokens || 0;
      const output = r.output_tokens || 0;
      return {
        run_id: r.run_id,
        kind: r.kind,
        label: shortTime(r.started_at),
        fullLabel: shortTime(r.started_at),
        input,
        output,
        avoided: (r.avoided_input_tokens || 0) + (r.avoided_output_tokens || 0),
        files_called: r.files_called || 0,
        files_cached: r.files_cached || 0,
      };
    });
    lastData = series;
    render(series);
    return series;
  }

  function toggleAvoided(on) {
    if (!lastData) return;
    render(lastData, { keys: on ? ["input", "output", "avoided"] : ["input", "output"] });
  }

  return { setRuns, toggleAvoided };
})();

// ---------------------------------------------------------------------------
// 3. symbol-kind donut (pure SVG arcs)
// ---------------------------------------------------------------------------

const Donut = (() => {
  function arcPath(cx, cy, rOuter, rInner, a0, a1) {
    // angles in radians, clockwise from 12 o'clock.
    // Sweeps > 180° are split so the SVG large-arc flags stay unambiguous and a
    // single-slice donut (a0 ≈ 0, a1 ≈ 2π) still renders a visible ring.
    const p = (r, a) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    const segs = [];
    const total = a1 - a0;
    const nSeg = Math.max(1, Math.ceil(total / (Math.PI * 0.999)));
    for (let s = 0; s < nSeg; s++) {
      const s0 = a0 + (total * s) / nSeg;
      const s1 = a0 + (total * (s + 1)) / nSeg;
      const large = s1 - s0 > Math.PI ? 1 : 0;
      const [x0, y0] = p(rOuter, s0);
      const [x1, y1] = p(rOuter, s1);
      segs.push(
        `${s === 0 ? "M" : "L"} ${x0.toFixed(2)} ${y0.toFixed(2)}`,
        `A ${rOuter} ${rOuter} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`
      );
    }
    for (let s = nSeg - 1; s >= 0; s--) {
      const s0 = a0 + (total * s) / nSeg;
      const s1 = a0 + (total * (s + 1)) / nSeg;
      const large = s1 - s0 > Math.PI ? 1 : 0;
      const [x0, y0] = p(rInner, s1);
      const [x1, y1] = p(rInner, s0);
      segs.push(
        `L ${x0.toFixed(2)} ${y0.toFixed(2)}`,
        `A ${rInner} ${rInner} 0 ${large} 0 ${x1.toFixed(2)} ${y1.toFixed(2)}`
      );
    }
    segs.push("Z");
    return segs.join(" ");
  }

  function render(byKind) {
    const host = $("donut-host");
    const legend = $("donut-legend");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);

    const entries = Object.entries(byKind || {}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((s, [, v]) => s + v, 0);
    if (!total) {
      host.innerHTML = '<div class="empty">no symbols indexed</div>';
      if (legend) legend.innerHTML = "";
      return;
    }

    const W = 240;
    const H = 240;
    const cx = W / 2;
    const cy = H / 2;
    const rOuter = 104;
    const rInner = 66;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, class: "donut-svg", role: "img",
      "aria-label": "symbol kind distribution",
    });

    // subtle track
    svg.appendChild(svgEl("circle", {
      cx, cy, r: (rOuter + rInner) / 2, fill: "none", stroke: "#21262d", "stroke-width": rOuter - rInner,
    }));

    let a = 0;
    const gap = 0.012; // radians between slices
    entries.forEach(([kind, count], i) => {
      const frac = count / total;
      const sweep = frac * Math.PI * 2;
      const a0 = a + gap / 2;
      const a1 = Math.max(a0 + 0.0001, a + sweep - gap / 2);
      const color = CallGraph.kindColor(kind);
      const path = svgEl("path", {
        d: arcPath(cx, cy, rOuter, rInner, a0, a1),
        fill: color,
        "fill-opacity": 0.92,
        stroke: "#0d1117",
        "stroke-width": 1,
      });
      const tip = `<b>${esc(kind)}</b><br>${fmt(count)} symbols<br>${pct(count, total)} of all symbols`;
      Tip.bind(path, tip);
      svg.appendChild(path);
      a += sweep;
    });

    // centre label
    const t1 = svgEl("text", {
      x: cx, y: cy - 3, "text-anchor": "middle", fill: "#e6edf3",
      "font-size": 22, "font-weight": 600,
    });
    t1.textContent = compact(total);
    svg.appendChild(t1);
    const t2 = svgEl("text", {
      x: cx, y: cy + 15, "text-anchor": "middle", fill: "#8b949e", "font-size": 10,
    });
    t2.textContent = "symbols";
    svg.appendChild(t2);

    host.appendChild(svg);

    if (legend) {
      legend.innerHTML = entries.map(([kind, count]) =>
        `<span class="lg"><i style="background:${CallGraph.kindColor(kind)}"></i>` +
        `${esc(kind)} <b>${fmt(count)}</b></span>`
      ).join("");
    }
  }

  return { render, arcPath };
})();

// ---------------------------------------------------------------------------
// 4. resolution strategy bars (CSS, fed by /api/resolution)
// ---------------------------------------------------------------------------

const ResolutionBars = (() => {
  function render(data) {
    const host = $("resolution-bars");
    if (!host) return;
    host.innerHTML = "";

    const via = (data && data.via) || {};
    const entries = Object.entries(via)
      .filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1]);

    if (!entries.length) {
      host.innerHTML = '<div class="empty">no edge resolution data</div>';
      return;
    }

    const total = entries.reduce((s, [, v]) => s + v, 0);
    const max = entries[0][1];

    for (const [name, count] of entries) {
      const unresolved = name === "unresolved" || name === "(unresolved)";
      const row = document.createElement("div");
      row.className = "bar-row" + (unresolved ? " unresolved" : "");
      const width = Math.max(0.8, (count / max) * 100);
      row.innerHTML =
        `<span class="bar-name">${esc(name)}</span>` +
        `<span class="bar-track"><span class="bar-fill" style="width:${width.toFixed(2)}%"></span></span>` +
        `<span class="bar-val">${fmt(count)}</span>` +
        `<span class="bar-pct">${pct(count, total)}</span>`;
      Tip.bind(row, `<b>${esc(name)}</b><br>${fmt(count)} edges<br>${pct(count, total)} of all edges`);
      host.appendChild(row);
    }
  }

  return { render };
})();

// ---------------------------------------------------------------------------
// page wiring
// ---------------------------------------------------------------------------

let lastRuns = [];
let lastStats = null;
let trendShowAvoided = false;

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function renderKPIs(stats, tokens) {
  const s = stats || {};
  const t = tokens || {};
  const byPhase = t.by_phase || {};
  let totalTok = 0;
  let calls = 0;
  let cacheHits = 0;
  let avoided = 0;
  for (const p of Object.values(byPhase)) {
    totalTok += (p.input_tokens || 0) + (p.output_tokens || 0);
    calls += p.calls || 0;
    cacheHits += p.cache_hits || 0;
    avoided += (p.avoided_input_tokens || 0) + (p.avoided_output_tokens || 0);
  }
  const resolvedRate = s.edges ? (s.resolved_edges / s.edges) * 100 : 0;

  setHTML("kpis", [
    ["Files", fmt(s.files), "v-a", `${Object.keys(s.by_language || {}).length} languages`],
    ["Symbols", fmt(s.symbols), "v-a", `${Object.keys(s.by_kind || {}).length} kinds`],
    ["Edges", fmt(s.edges), "v-m", `${fmt(s.call_edges)} call edges`],
    ["Resolved", resolvedRate.toFixed(1) + "%", resolvedRate >= 50 ? "v-g" : "v-o",
      `${fmt(s.resolved_edges)} / ${fmt(s.edges)}`],
    ["Summaries", fmt(s.summaries), "v-g", `${fmt(totalTok)} tokens spent`],
    ["DB size", bytes(s.db_bytes), "v-p", `${fmt(calls)} LLM calls`],
  ].map(([label, value, cls, note]) =>
    `<div class="kpi"><div class="label">${esc(label)}</div>` +
    `<div class="value ${cls}">${value}</div>` +
    `<div class="note">${esc(note)}</div></div>`
  ).join(""));

  // header pills
  setText("repo-pill", s.repo || REPO || "default repo");
  const live = $("live-pill");
  if (live) live.textContent = "updated " + new Date().toLocaleTimeString();

  // token summary rows
  const per = t.summary_per_file || {};
  setHTML("token-summary", [
    ["Total tokens", fmt(totalTok), "a"],
    ["LLM calls", fmt(calls), ""],
    ["Cache hits", `${fmt(cacheHits)} / ${fmt(cacheHits + calls)}`, "g"],
    ["Avoided tokens", fmt(avoided), "g"],
    ["Summaries", fmt(per.n), ""],
    ["Mean / file", fmt(Math.round(per.mean || 0)), ""],
    ["P50 / file", fmt(per.p50), ""],
    ["P95 / file", fmt(per.p95), "o"],
  ].map(([k, v, c]) =>
    `<div class="row"><span class="k">${esc(k)}</span><span class="v ${c}">${v}</span></div>`
  ).join(""));

  // phase table
  const phases = Object.entries(byPhase);
  setHTML("phase-table", phases.length
    ? `<div class="table-wrap"><table><thead><tr><th>Phase</th>` +
      `<th style="text-align:right">Calls</th><th style="text-align:right">Cache</th>` +
      `<th style="text-align:right">In tok</th><th style="text-align:right">Out tok</th>` +
      `<th style="text-align:right">Avg ms</th></tr></thead><tbody>` +
      phases.map(([phase, p]) =>
        `<tr><td>${esc(phase)}</td>` +
        `<td class="num">${fmt(p.calls)}</td>` +
        `<td class="num">${fmt(p.cache_hits)}</td>` +
        `<td class="num">${fmt(p.input_tokens)}</td>` +
        `<td class="num">${fmt(p.output_tokens)}</td>` +
        `<td class="num">${fmt(p.calls ? Math.round((p.latency_ms || 0) / p.calls) : 0)}</td></tr>`
      ).join("") + "</tbody></table></div>"
    : '<div class="empty">no LLM calls recorded yet</div>');

  // edge composition
  setHTML("edge-rows", [
    ["Call edges", fmt(s.call_edges), "a"],
    ["Import edges", fmt(s.import_edges), ""],
    ["Inherit edges", fmt(s.inherit_edges), ""],
    ["Resolved", fmt(s.resolved_edges), "g"],
    ["Unresolved", fmt(s.unresolved_edges), "o"],
    ["Parse errors", fmt(s.parse_errors), s.parse_errors ? "r" : ""],
  ].map(([k, v, c]) =>
    `<div class="row"><span class="k">${esc(k)}</span><span class="v ${c}">${v}</span></div>`
  ).join(""));
}

function renderRunsTable(runs) {
  const rows = runs || [];
  setHTML("runs-table", rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Run</th><th>Kind</th>` +
      `<th style="text-align:right">Called</th><th style="text-align:right">Cached</th>` +
      `<th style="text-align:right">In tok</th><th style="text-align:right">Out tok</th>` +
      `<th style="text-align:right">Total</th><th>Started</th></tr></thead><tbody>` +
      rows.map((r) =>
        `<tr><td class="mono-ellipsis" title="${esc(r.run_id)}">${esc(r.run_id)}</td>` +
        `<td>${esc(r.kind)}</td>` +
        `<td class="num">${fmt(r.files_called)}</td>` +
        `<td class="num">${fmt(r.files_cached)}</td>` +
        `<td class="num">${fmt(r.input_tokens)}</td>` +
        `<td class="num">${fmt(r.output_tokens)}</td>` +
        `<td class="num">${fmt((r.input_tokens || 0) + (r.output_tokens || 0))}</td>` +
        `<td class="k">${esc(shortTime(r.started_at))}</td></tr>`
      ).join("") + "</tbody></table></div>"
    : '<div class="empty">no runs recorded yet</div>');
}

function renderFilesTable(files) {
  const rows = (files || []).slice(0, 400);
  setHTML("files-table", rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>File</th><th>Lang</th>` +
      `<th style="text-align:right">Lines</th><th style="text-align:right">Size</th><th>Parse</th>` +
      `</tr></thead><tbody>` +
      rows.map((f) =>
        `<tr><td class="mono-ellipsis" title="${esc(f.path)}">${esc(f.path)}</td>` +
        `<td>${esc(f.language)}</td>` +
        `<td class="num">${fmt(f.line_count)}</td>` +
        `<td class="num">${bytes(f.size_bytes)}</td>` +
        `<td class="${f.parse_ok ? "v-g" : "v-r"}">${f.parse_ok ? "ok" : "error"}</td></tr>`
      ).join("") + "</tbody></table></div>"
    : '<div class="empty">no files indexed</div>');
}

// ---------------------------------------------------------------------------
// trace / call graph interaction
// ---------------------------------------------------------------------------

async function traceCurrent(symbol, opts) {
  const sym = (symbol || $("trace-symbol").value || "").trim();
  const dir = $("trace-dir").value;
  const depth = $("trace-depth").value;
  const status = $("graph-status");
  const silent = opts && opts.silent;
  if (!sym) {
    if (status) status.textContent = "enter a symbol to trace";
    return;
  }
  if (status && !silent) status.textContent = `tracing ${sym}…`;
  try {
    const data = await getJSON(
      `/api/trace?symbol=${encodeURIComponent(sym)}&direction=${encodeURIComponent(dir)}` +
      `&depth=${encodeURIComponent(depth)}`
    );
    if (silent) CallGraph.updateData(data);
    else CallGraph.setData(data);
    const st = CallGraph.stats();
    if (status) {
      status.textContent = `${data.symbol.qualname} · ${st.nodes} nodes / ${st.links} edges`;
    }
    const tj = $("trace-json");
    if (tj) {
      tj.textContent = JSON.stringify({
        root: data.symbol.qualname,
        kind: data.symbol.kind,
        path: `${data.symbol.path}:${data.symbol.start_line}`,
        callees: (data.callees || []).length,
        callers: (data.callers || []).length,
        unresolved_calls: (data.unresolved_calls || []).length,
      }, null, 2);
    }
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    if (!silent) CallGraph.emptyMessage(String(e.message || e));
  }
}

async function doSearch() {
  const q = ($("search-input").value || "").trim();
  const host = $("search-results");
  if (!q) return;
  host.innerHTML = '<div class="empty">searching…</div>';
  try {
    const data = await getJSON(`/api/search?q=${encodeURIComponent(q)}&limit=30`);
    const results = data.results || [];
    if (!results.length) {
      host.innerHTML = '<div class="empty">no symbols matched</div>';
      return;
    }
    host.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Kind</th>` +
      `<th>Symbol</th><th>Location</th><th>Signature</th><th></th></tr></thead><tbody>` +
      results.map((r, i) =>
        `<tr><td>${esc(r.kind)}</td>` +
        `<td class="mono-ellipsis" title="${esc(r.qualname)}">${esc(r.qualname)}</td>` +
        `<td class="k mono-ellipsis" title="${esc(r.path)}">${esc(r.path)}:${r.start_line}</td>` +
        `<td class="k mono-ellipsis" title="${esc(r.signature || "")}">${esc(truncate(r.signature || "", 70))}</td>` +
        `<td><button data-sym="${esc(r.name)}" data-idx="${i}">trace</button></td></tr>`
      ).join("") + "</tbody></table></div>";
    host.querySelectorAll("button[data-sym]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sym = btn.getAttribute("data-sym");
        $("trace-symbol").value = sym;
        traceCurrent(sym);
        const card = $("graph-card");
        if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  } catch (e) {
    host.innerHTML = `<div class="empty">${esc(e.message || e)}</div>`;
  }
}

async function runAction(path, body, label) {
  const out = $("action-output");
  out.textContent = `${label}: running…`;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ repo: REPO || undefined }, body || {})),
    });
    const data = await res.json();
    out.textContent = `${label}: HTTP ${res.status}\n` +
      JSON.stringify(data, null, 2).slice(0, 6000);
    await refreshAll();
  } catch (e) {
    out.textContent = `${label}: ${e.message || e}`;
  }
}

// ---------------------------------------------------------------------------
// main refresh loop
// ---------------------------------------------------------------------------

let refreshing = false;

async function refreshAll() {
  if (refreshing) return;
  refreshing = true;
  const errEl = $("err");
  try {
    const [statsRes, runsRes, filesRes, resRes] = await Promise.all([
      getJSON("/api/stats"),
      getJSON("/api/runs"),
      getJSON("/api/files"),
      getJSON("/api/resolution"),
    ]);
    if (errEl) errEl.textContent = "";

    lastStats = statsRes.stats || {};
    const tokens = statsRes.tokens || {};

    renderKPIs(lastStats, tokens);

    lastRuns = runsRes.runs || [];
    renderRunsTable(lastRuns);
    TokenTrend.setRuns(lastRuns);

    renderFilesTable(filesRes.files || []);

    Donut.render(lastStats.by_kind || {});
    ResolutionBars.render(resRes || {});

    const resolvedRate = lastStats.edges
      ? ((lastStats.resolved_edges / lastStats.edges) * 100).toFixed(1)
      : "0.0";
    setText("resolution-hint",
      `${fmt(resRes.total || 0)} edges · ${resolvedRate}% resolved · ${Object.keys(resRes.via || {}).length} strategies`);
    setText("err", "");
    setText("repo-pill", lastStats.repo || REPO || "default repo");
  } catch (e) {
    const msg = String(e.message || e);
    if (errEl) errEl.textContent = msg;
    setText("repo-pill", REPO ? truncate(REPO, 48) : "default repo");
    // paint an explicit "no data" state rather than leaving stale panels blank
    renderOfflineState(msg);
  } finally {
    refreshing = false;
  }
}

function renderOfflineState(msg) {
  const notice = `<div class="empty">${esc(msg)}<br>` +
    `<span class="k">check the repository path — it must be indexed by codegraph</span></div>`;
  setHTML("kpis", "");
  setHTML("token-summary", "");
  setHTML("edge-rows", "");
  setHTML("phase-table", notice);
  setHTML("runs-table", notice);
  setHTML("files-table", notice);
  setHTML("trend-svg-host", notice);
  setHTML("trend-legend", "");
  setHTML("donut-host", notice);
  setHTML("donut-legend", "");
  setHTML("resolution-bars", notice);
  setText("resolution-hint", "unavailable");
  CallGraph.clear();
}

function wireEvents() {
  $("search-btn").addEventListener("click", doSearch);
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  $("trace-btn").addEventListener("click", () => traceCurrent());
  $("trace-symbol").addEventListener("keydown", (e) => {
    if (e.key === "Enter") traceCurrent();
  });
  $("trace-dir").addEventListener("change", () => traceCurrent());
  $("trace-depth").addEventListener("change", () => traceCurrent());
  $("relayout-btn").addEventListener("click", () => CallGraph.relayout());
  $("trend-avoided").addEventListener("change", (e) => {
    trendShowAvoided = !!e.target.checked;
    TokenTrend.toggleAvoided(trendShowAvoided);
  });
  // quick-pick chips for common entry symbols
  document.querySelectorAll("[data-symbol]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const sym = chip.getAttribute("data-symbol");
      $("trace-symbol").value = sym;
      traceCurrent(sym);
    });
  });
  // repository switcher (blank = server default)
  const repoInput = $("repo-input");
  if (repoInput) {
    repoInput.value = REPO;
    repoInput.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      REPO = repoInput.value.trim();
      const url = new URL(location.href);
      if (REPO) url.searchParams.set("repo", REPO);
      else url.searchParams.delete("repo");
      history.replaceState(null, "", url.toString());
      refreshAll().then(() => traceCurrent(undefined, { silent: false }));
    });
  }
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.getAttribute("data-action");
      const body = JSON.parse(btn.getAttribute("data-body") || "{}");
      runAction(action, body, btn.textContent.trim());
    });
  });
  window.addEventListener("resize", () => {
    clearTimeout(wireEvents._t);
    wireEvents._t = setTimeout(() => {
      if (CallGraph.stats().nodes) CallGraph.render();
    }, 220);
  });
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  // surface any uncaught error in the header instead of failing silently
  window.addEventListener("error", (e) => {
    const el = $("err");
    if (el) el.textContent = "JS: " + (e.message || e.error || "error");
  });
  window.addEventListener("unhandledrejection", (e) => {
    const el = $("err");
    if (el) el.textContent = "JS: " + ((e.reason && e.reason.message) || e.reason || "rejection");
  });

  wireEvents();
  await refreshAll();
  // initial call graph: trace a sensible default entry point
  const first = $("trace-symbol");
  const symbol = (first && first.value) || "main";
  await traceCurrent(symbol);
  // one timer drives everything: panels re-fetch, call graph only re-lays-out
  // when the traced subgraph actually changed (no visual jumping on idle)
  setInterval(async () => {
    if (document.hidden) return;
    await refreshAll();
    await traceCurrent(undefined, { silent: true });
  }, POLL_MS);
});
