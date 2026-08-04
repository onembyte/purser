"use strict";
/* Hand-rolled inline SVG charts. No libraries: the page runs under a strict CSP with
   no network access.

   Mark conventions (from the dataviz method):
     * 2px SURFACE gap separates touching marks — never a stroke drawn around them.
     * Data-ends get a 4px radius; the baseline end stays square (anchored).
     * Grid/axes are recessive and solid — never dashed.
     * Colour comes from --series-N, assigned by entity (model), never by rank.
     * Every chart carries a hover tooltip; labels are selective, never one per point. */

const SVGNS = "http://www.w3.org/2000/svg";
const GAP = 2;        // surface gap between stacked segments / adjacent bars
const RADIUS = 4;     // data-end radius

function svgEl(name, attrs = {}) {
  const n = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

/** Rect with a rounded data-end and a square baseline end. */
function endRect(x, y, w, h, r, side) {
  if (h <= 0 || w <= 0) return null;
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  if (r < 0.75) return svgEl("rect", { x, y, width: w, height: h });
  let d;
  if (side === "top") {
    d = `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y}
         L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
  } else if (side === "right") {
    d = `M${x},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r}
         L${x + w},${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} L${x},${y + h} Z`;
  } else {
    return svgEl("rect", { x, y, width: w, height: h });
  }
  return svgEl("path", { d });
}

/* ------------------------------------------------------------------ tooltip */
const tip = (() => {
  const n = document.createElement("div");
  n.className = "tip";
  n.style.display = "none";
  document.body.appendChild(n);
  return {
    show(html, ev) {
      n.innerHTML = html;
      n.style.display = "block";
      const r = n.getBoundingClientRect();
      let x = ev.clientX + 12, y = ev.clientY - r.height - 10;
      if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - 12;
      if (y < 8) y = ev.clientY + 16;
      n.style.left = x + "px";
      n.style.top = y + "px";
    },
    hide() { n.style.display = "none"; },
  };
})();

function attachTip(node, htmlFn) {
  node.addEventListener("mousemove", (e) => tip.show(htmlFn(), e));
  node.addEventListener("mouseleave", () => tip.hide());
}

/* ------------------------------------------------------------------ legend */
function legend(series) {
  const l = document.createElement("div");
  l.className = "legend";
  series.forEach((s) => {
    const i = document.createElement("span");
    i.className = "legend-item";
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = `var(--series-${s.slot})`;
    // Text wears text tokens, never the series colour — the swatch carries identity.
    i.append(sw, document.createTextNode(s.name));
    l.append(i);
  });
  return l;
}

const money = (v) => (v >= 1000
  ? "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  : "$" + v.toFixed(2));

/** Ticks from 0 to a round value at or ABOVE max.
 *  The last tick must be >= max: if it stops below, the scale's top is under the
 *  tallest value and that bar renders outside the plot and over whatever is above it. */
function niceTicks(max, count = 4) {
  if (max <= 0) return [0, 1];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const out = [];
  for (let v = 0; v < max - step * 1e-9; v += step) out.push(v);
  out.push(out.length ? out[out.length - 1] + step : step);   // final tick >= max
  return out;
}

/* ============================================================ daily stacked bars */
function dailyStacked(container, daily, models, opts = {}) {
  const W = container.clientWidth || 720;
  const H = opts.height || 190;
  const M = { top: 8, right: 8, bottom: 22, left: 46 };   // bottom band holds the x-axis
  const iw = Math.max(10, W - M.left - M.right);
  const ih = Math.max(10, H - M.top - M.bottom);

  const svg = svgEl("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
                             preserveAspectRatio: "none", class: "chart" });
  const max = Math.max(...daily.map((d) => d.total), 0.0001);
  const ticks = niceTicks(max);
  const yMax = ticks[ticks.length - 1] || max;
  const y = (v) => M.top + ih - (v / yMax) * ih;

  // recessive grid (solid, never dashed) + y labels
  ticks.forEach((t) => {
    svg.append(svgEl("line", { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t),
                               class: "grid" }));
    const lb = svgEl("text", { x: M.left - 6, y: y(t) + 3, class: "axis",
                               "text-anchor": "end" });
    lb.textContent = t >= 1000 ? (t / 1000) + "k" : t;
    svg.append(lb);
  });

  const bw = Math.max(1, iw / daily.length - GAP);
  daily.forEach((d, i) => {
    const x = M.left + (i * iw) / daily.length;
    let acc = 0;
    // Stack in the fixed global model order so a series keeps its position and hue.
    const stack = models.filter((m) => d.byModel[m.id]);
    stack.forEach((m, j) => {
      const v = d.byModel[m.id];
      const y0 = y(acc), y1 = y(acc + v);
      let h = y0 - y1;
      // 2px surface gap between segments; the topmost segment gets the rounded end.
      const isTop = j === stack.length - 1;
      if (!isTop) h = Math.max(0.5, h - GAP);
      const r = endRect(x, y1, bw, h, RADIUS, isTop ? "top" : "square");
      if (r) {
        r.setAttribute("fill", `var(--series-${m.slot})`);
        svg.append(r);
      }
      acc += v;
    });

    // one invisible hit target per day, wider than the mark
    const hit = svgEl("rect", { x: x - GAP / 2, y: M.top, width: bw + GAP, height: ih,
                                fill: "transparent", class: "hit" });
    attachTip(hit, () => {
      const rows = models.filter((m) => d.byModel[m.id]).reverse()
        .map((m) => `<div class="tr"><span class="sw" style="background:var(--series-${m.slot})"></span>
                     <span class="tn">${m.name}</span><span class="tv">${money(d.byModel[m.id])}</span></div>`)
        .join("");
      return `<div class="th">${new Date(d.day + "T00:00").toLocaleDateString(undefined,
                { weekday: "short", month: "short", day: "numeric" })}</div>${rows}
              <div class="tr total"><span class="tn">Total</span>
              <span class="tv">${money(d.total)}</span></div>`;
    });
    svg.append(hit);
  });

  // Selective x labels only — never one per bar.
  const step = Math.max(1, Math.round(daily.length / 6));
  daily.forEach((d, i) => {
    if (i % step && i !== daily.length - 1) return;
    const x = M.left + (i * iw) / daily.length + bw / 2;
    const lb = svgEl("text", { x, y: H - 6, class: "axis", "text-anchor": "middle" });
    lb.textContent = new Date(d.day + "T00:00").toLocaleDateString(
      undefined, { month: "short", day: "numeric" });
    svg.append(lb);
  });

  container.replaceChildren(svg);
}

/* ================================================================ horizontal bars */
function hBars(container, items, opts = {}) {
  const rowH = 26;
  const labelW = opts.labelW || 150;
  const valueW = 74;
  const W = container.clientWidth || 720;
  const H = items.length * rowH;
  const iw = Math.max(10, W - labelW - valueW);
  const max = Math.max(...items.map((i) => i.value), 0.0001);

  const svg = svgEl("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
                             preserveAspectRatio: "none", class: "chart" });

  items.forEach((it, i) => {
    const y = i * rowH;
    const bh = rowH - 9;   // thin marks; the gap between rows is surface

    const name = svgEl("text", { x: labelW - 8, y: y + bh / 2 + 4, class: "axis strong",
                                 "text-anchor": "end" });
    name.textContent = it.name;
    svg.append(name);

    const w = Math.max(1, (it.value / max) * iw);
    // Single series => single hue. A value-ramp across nominal categories would encode
    // magnitude twice (length already does it) and is an anti-pattern.
    const r = endRect(labelW, y + 4, w, bh, RADIUS, "right");
    r.setAttribute("fill", `var(--series-${opts.slot || 1})`);
    svg.append(r);

    // Direct value labels: few enough rows that every one is legible and useful.
    const val = svgEl("text", { x: labelW + w + 8, y: y + bh / 2 + 4, class: "axis" });
    val.textContent = opts.format ? opts.format(it.value) : money(it.value);
    svg.append(val);

    const hit = svgEl("rect", { x: 0, y, width: W, height: rowH, fill: "transparent" });
    attachTip(hit, () => `<div class="th">${it.name}</div>
      <div class="tr"><span class="tn">${opts.valueLabel || "Cost"}</span>
      <span class="tv">${opts.format ? opts.format(it.value) : money(it.value)}</span></div>` +
      (it.note ? `<div class="tr"><span class="tn">${it.note}</span></div>` : ""));
    svg.append(hit);
  });

  container.replaceChildren(svg);
}

/* ========================================================= composition (one bar) */
function composition(container, segments, opts = {}) {
  const H = 34;
  const W = container.clientWidth || 720;
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;

  const svg = svgEl("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
                             preserveAspectRatio: "none", class: "chart" });
  let x = 0;
  segments.forEach((s, i) => {
    const raw = (s.value / total) * W;
    const isLast = i === segments.length - 1;
    const w = Math.max(0.5, isLast ? raw : raw - GAP);   // 2px surface gap
    const side = i === 0 ? "left" : isLast ? "right" : "square";
    const r = (side === "right")
      ? endRect(x, 0, w, H, RADIUS, "right")
      : svgEl("rect", { x, y: 0, width: w, height: H });
    r.setAttribute("fill", `var(--series-${s.slot})`);
    const shown = opts.format ? opts.format(s.value) : money(s.value);
    attachTip(r, () => `<div class="th">${s.name}</div>` +
      (opts.hideValue ? "" :
        `<div class="tr"><span class="tn">${opts.valueLabel || "Cost"}</span>
         <span class="tv">${shown}</span></div>`) +
      `<div class="tr"><span class="tn">Share</span>
       <span class="tv">${((s.value / total) * 100).toFixed(1)}%</span></div>`);
    svg.append(r);
    x += raw;
  });
  container.replaceChildren(svg);
}

window.Charts = { dailyStacked, hBars, composition, legend, money, niceTicks };
