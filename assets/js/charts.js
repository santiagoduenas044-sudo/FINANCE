/* ==========================================================================
   charts.js — small SVG chart set, built to one spec.
   --------------------------------------------------------------------------
   House rules: thin marks, 2px lines, 4px rounded data-ends, 2px surface gaps,
   hairline solid gridlines, selective direct labels, a legend whenever there
   is more than one series, and a hover layer on everything with a plot.
   Text never wears a series colour — a swatch beside it carries identity.
   ========================================================================== */

import { esc, moneyCompact, money, monthLabel } from './util.js';

const NS = 'http://www.w3.org/2000/svg';

/* --- scales --------------------------------------------------------------- */

/** Round a maximum up to a clean axis top and return tick values. */
export function niceScale(max, min = 0, count = 4) {
  if (!isFinite(max) || max <= 0) max = 1;
  const span = max - Math.min(0, min);
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const top = Math.ceil(max / step) * step;
  const bottom = min < 0 ? Math.floor(min / step) * step : 0;
  const ticks = [];
  for (let v = bottom; v <= top + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return { top, bottom, step, ticks };
}

/** Bar with a 4px rounded data-end, square where it meets the baseline. */
function barPath(x, y, w, hgt, r = 4) {
  const rr = Math.max(0, Math.min(r, w / 2, hgt));
  if (hgt <= 0.5) return '';
  return `M${x} ${y + hgt}L${x} ${y + rr}Q${x} ${y} ${x + rr} ${y}L${x + w - rr} ${y}Q${x + w} ${y} ${x + w} ${y + rr}L${x + w} ${y + hgt}Z`;
}

/** Smooth-ish polyline (monotone-safe: plain segments, round joins). */
function linePath(pts) {
  return pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join('');
}

function areaPath(pts, baseY) {
  if (!pts.length) return '';
  return `${linePath(pts)}L${pts[pts.length - 1].x.toFixed(1)} ${baseY}L${pts[0].x.toFixed(1)} ${baseY}Z`;
}

/* --- shared tooltip ------------------------------------------------------- */

let tipEl = null;
function tip() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'tip';
    tipEl.setAttribute('role', 'status');
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

export function showTip(html, x, y) {
  const t = tip();
  t.innerHTML = html;
  t.classList.add('on');
  const r = t.getBoundingClientRect();
  const pad = 12;
  let left = x + 14;
  if (left + r.width > innerWidth - pad) left = x - r.width - 14;
  left = Math.max(pad, left);
  let top = y - r.height - 12;
  if (top < pad) top = y + 18;
  t.style.left = `${left}px`;
  t.style.top = `${top}px`;
}

export function hideTip() { if (tipEl) tipEl.classList.remove('on'); }

const tipRows = (rows) => rows.map((r) => `
  <div class="tip-row">
    ${r.color ? `<span class="dot" style="--c:${r.color}"></span>` : ''}
    <span class="k">${esc(r.k)}</span><span class="v">${esc(r.v)}</span>
  </div>`).join('');

export function tipHTML(title, rows) {
  return `<div class="tip-title">${esc(title)}</div>${tipRows(rows)}`;
}

/* --- mounting ------------------------------------------------------------- */

const observers = new WeakMap();

/**
 * Render a chart into `host` at its measured width, and re-render on resize.
 * `draw(width)` returns an SVG string.
 */
export function mountChart(host, draw, minWidth = 240) {
  if (!host) return;
  const paint = () => {
    const w = Math.max(minWidth, Math.floor(host.clientWidth || host.getBoundingClientRect().width || minWidth));
    host.innerHTML = draw(w);
  };
  paint();
  if (!observers.has(host)) {
    let raf = null;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(paint);
    });
    ro.observe(host);
    observers.set(host, ro);
  }
  host._repaint = paint;
}

/* ==========================================================================
   Column chart with an optional overlaid line (same unit, one axis)
   ========================================================================== */

/**
 * @param {number} w
 * @param {object} o  { labels, columns:{label,color,values}, line:{label,color,values}, height }
 */
export function columnChart(w, o) {
  const h = o.height || 210;
  const padL = 44, padR = 10, padT = 12, padB = 26;
  const iw = Math.max(10, w - padL - padR);
  const ih = h - padT - padB;
  const n = o.labels.length;
  const colVals = o.columns?.values || [];
  const lineVals = o.line?.values || [];
  const max = Math.max(1, ...colVals, ...lineVals);
  const sc = niceScale(max);
  const y = (v) => padT + ih - (v / sc.top) * ih;
  const band = iw / Math.max(1, n);
  const barW = Math.min(24, Math.max(4, band - 10));

  let svg = `<svg class="chart" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
    aria-label="${esc(o.ariaLabel || 'Monthly chart')}">`;

  // gridlines + y ticks
  sc.ticks.forEach((t) => {
    const yy = y(t).toFixed(1);
    svg += `<line class="grid-line" x1="${padL}" y1="${yy}" x2="${w - padR}" y2="${yy}"/>`;
    svg += `<text x="${padL - 8}" y="${yy}" text-anchor="end" dominant-baseline="middle">${esc(moneyCompact(t))}</text>`;
  });

  // columns
  colVals.forEach((v, i) => {
    const cx = padL + band * i + band / 2;
    const hgt = Math.max(0, padT + ih - y(v));
    if (hgt > 0.5) {
      svg += `<path d="${barPath(cx - barW / 2, y(v), barW, hgt)}" fill="${o.columns.color}"/>`;
    }
  });

  // line over the columns, with a surface ring on the end marker
  if (lineVals.length) {
    const pts = lineVals.map((v, i) => ({ x: padL + band * i + band / 2, y: y(v) }));
    svg += `<path d="${linePath(pts)}" fill="none" stroke="${o.line.color}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>`;
    const last = pts[pts.length - 1];
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="6" fill="var(--surface)"/>`;
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="4" fill="${o.line.color}"/>`;
  }

  // x labels — thin out when tight
  const every = Math.ceil(n / Math.max(3, Math.floor(iw / 58)));
  o.labels.forEach((l, i) => {
    if (i !== n - 1 && (i % every !== 0 || n - 1 - i < every)) return;
    svg += `<text x="${(padL + band * i + band / 2).toFixed(1)}" y="${h - 8}" text-anchor="middle">${esc(l)}</text>`;
  });

  svg += `<line class="axis-line" x1="${padL}" y1="${padT + ih}" x2="${w - padR}" y2="${padT + ih}"/>`;

  // hover bands
  o.labels.forEach((l, i) => {
    const rows = [];
    if (o.columns) rows.push({ k: o.columns.label, v: money(colVals[i] || 0), color: o.columns.color });
    if (o.line) rows.push({ k: o.line.label, v: money(lineVals[i] || 0), color: o.line.color });
    svg += `<rect x="${(padL + band * i).toFixed(1)}" y="${padT}" width="${band.toFixed(1)}" height="${ih}"
      fill="transparent" tabindex="0" role="button"
      aria-label="${esc(l)}: ${rows.map((r) => `${r.k} ${money(r.v)}`).join(', ')}"
      data-tip="${esc(tipHTML(o.fullLabels?.[i] || l, rows))}"/>`;
  });

  svg += '</svg>';
  return svg;
}

/* ==========================================================================
   Line / area chart — one or more series, optional projection tail
   ========================================================================== */

/**
 * @param {object} o { labels, series:[{label,color,values,fill?,dashFrom?}], height, showLast }
 */
export function lineChart(w, o) {
  const h = o.height || 220;
  const padL = 46, padR = o.padR ?? 52, padT = 14, padB = 26;
  const iw = Math.max(10, w - padL - padR);
  const ih = h - padT - padB;
  const n = o.labels.length;
  const all = o.series.flatMap((s) => s.values.filter((v) => isFinite(v)));
  const max = Math.max(1, ...all);
  const min = Math.min(0, ...all);
  const sc = niceScale(max, min);
  const span = sc.top - sc.bottom || 1;
  const y = (v) => padT + ih - ((v - sc.bottom) / span) * ih;
  const x = (i) => padL + (n <= 1 ? iw / 2 : (iw * i) / (n - 1));

  let svg = `<svg class="chart" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
    aria-label="${esc(o.ariaLabel || 'Trend chart')}">`;

  sc.ticks.forEach((t) => {
    const yy = y(t).toFixed(1);
    svg += `<line class="grid-line" x1="${padL}" y1="${yy}" x2="${w - padR}" y2="${yy}"/>`;
    svg += `<text x="${padL - 8}" y="${yy}" text-anchor="end" dominant-baseline="middle">${esc(moneyCompact(t))}</text>`;
  });

  o.series.forEach((s) => {
    const pts = s.values.map((v, i) => ({ x: x(i), y: y(v || 0) }));
    if (!pts.length) return;
    const cut = s.dashFrom ?? -1;

    if (s.fill) {
      svg += `<path d="${areaPath(pts, padT + ih)}" fill="${s.color}" opacity="0.1"/>`;
    }
    if (cut >= 0 && cut < pts.length) {
      svg += `<path d="${linePath(pts.slice(0, cut + 1))}" fill="none" stroke="${s.color}" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>`;
      svg += `<path d="${linePath(pts.slice(cut))}" fill="none" stroke="${s.color}" stroke-width="2"
        stroke-dasharray="2 5" stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>`;
    } else {
      svg += `<path d="${linePath(pts)}" fill="none" stroke="${s.color}" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>`;
    }

    const last = pts[pts.length - 1];
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="6" fill="var(--surface)"/>`;
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="4" fill="${s.color}"/>`;
  });

  // one direct end-label per series when they do not collide
  if (o.showLast !== false && padR > 30) {
    const ends = o.series.map((s) => ({ s, y: y(s.values[s.values.length - 1] || 0) }))
      .sort((a, b) => a.y - b.y);
    const collides = ends.some((e, i) => i > 0 && Math.abs(e.y - ends[i - 1].y) < 13);
    if (!collides) {
      ends.forEach((e) => {
        svg += `<text class="lbl-strong" x="${w - padR + 8}" y="${e.y.toFixed(1)}" dominant-baseline="middle">${
          esc(moneyCompact(e.s.values[e.s.values.length - 1] || 0))}</text>`;
      });
    }
  }

  const every = Math.ceil(n / Math.max(3, Math.floor(iw / 62)));
  o.labels.forEach((l, i) => {
    if (i !== n - 1 && (i % every !== 0 || n - 1 - i < every)) return;
    let anchor = 'middle';
    if (i === 0) anchor = 'start';
    if (i === n - 1) anchor = 'end';
    svg += `<text x="${x(i).toFixed(1)}" y="${h - 8}" text-anchor="${anchor}">${esc(l)}</text>`;
  });

  svg += `<line class="axis-line" x1="${padL}" y1="${(padT + ih).toFixed(1)}" x2="${w - padR}" y2="${(padT + ih).toFixed(1)}"/>`;

  // crosshair hit bands
  const bw = n > 1 ? iw / (n - 1) : iw;
  o.labels.forEach((l, i) => {
    const rows = o.series.map((s) => ({ k: s.label, v: money(s.values[i] || 0), color: s.color }));
    svg += `<rect x="${(x(i) - bw / 2).toFixed(1)}" y="${padT}" width="${bw.toFixed(1)}" height="${ih}"
      fill="transparent" tabindex="0" role="button"
      aria-label="${esc(l)}: ${rows.map((r) => `${r.k} ${r.v}`).join(', ')}"
      data-tip="${esc(tipHTML(o.fullLabels?.[i] || l, rows))}"
      data-cx="${x(i).toFixed(1)}"/>`;
  });
  svg += `<line class="crosshair" x1="0" y1="${padT}" x2="0" y2="${padT + ih}" stroke="var(--axis)"
    stroke-width="1" opacity="0" pointer-events="none"/>`;

  svg += '</svg>';
  return svg;
}

/* ==========================================================================
   Small multiples — one compact area chart per measure
   ========================================================================== */

export function miniArea(w, o) {
  const h = o.height || 62;
  const padT = 6, padB = 4;
  const ih = h - padT - padB;
  const vals = o.values.map((v) => (isFinite(v) ? v : 0));
  const max = Math.max(1, ...vals);
  const min = Math.min(0, ...vals);
  const span = max - min || 1;
  const n = vals.length;
  const x = (i) => (n <= 1 ? w / 2 : (w * i) / (n - 1));
  const y = (v) => padT + ih - ((v - min) / span) * ih;
  const pts = vals.map((v, i) => ({ x: x(i), y: y(v) }));
  let svg = `<svg class="chart" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">`;
  svg += `<path d="${areaPath(pts, h - padB)}" fill="${o.color}" opacity="0.1"/>`;
  svg += `<path d="${linePath(pts)}" fill="none" stroke="${o.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const last = pts[pts.length - 1];
  if (last) {
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="5.5" fill="var(--surface)"/>`;
    svg += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="3.5" fill="${o.color}"/>`;
  }
  // hover bands
  const bw = n > 1 ? w / (n - 1) : w;
  o.labels?.forEach((l, i) => {
    svg += `<rect x="${(x(i) - bw / 2).toFixed(1)}" y="0" width="${bw.toFixed(1)}" height="${h}" fill="transparent"
      data-tip="${esc(tipHTML(l, [{ k: o.label, v: money(vals[i]), color: o.color }]))}"/>`;
  });
  svg += '</svg>';
  return svg;
}

/* ==========================================================================
   Sparkline
   ========================================================================== */

export function sparkline(values, color = 'var(--ink-3)', w = 78, h = 24) {
  const vals = values.map((v) => (isFinite(v) ? v : 0));
  if (!vals.length) return '';
  const max = Math.max(...vals, 1), min = Math.min(...vals, 0);
  const span = max - min || 1;
  const x = (i) => (vals.length <= 1 ? w / 2 : (w * i) / (vals.length - 1));
  const y = (v) => h - 3 - ((v - min) / span) * (h - 6);
  const pts = vals.map((v, i) => ({ x: x(i), y: y(v) }));
  const last = pts[pts.length - 1];
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true" style="--c:${color}">
    <path class="area" d="${areaPath(pts, h)}"/>
    <path class="line" d="${linePath(pts)}"/>
    <circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="2.4"/>
  </svg>`;
}

/* ==========================================================================
   Progress ring
   ========================================================================== */

export function ring(pctValue, color = 'var(--ink)', size = 68, stroke = 6, centerHTML = '') {
  const p = Math.max(0, Math.min(1, isFinite(pctValue) ? pctValue : 0));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const off = c * (1 - p);
  return `<span class="ring-wrap" style="--c:${color}">
    <svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}"/>
      <circle class="val" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}"
        stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${off.toFixed(2)}"
        transform="rotate(-90 ${size / 2} ${size / 2})"/>
    </svg>
    ${centerHTML ? `<span class="ring-center">${centerHTML}</span>` : ''}
  </span>`;
}

/* ==========================================================================
   Allocation bar — one dollar, divided. HTML so labels stay selectable.
   ========================================================================== */

export function allocationBar(segments, { slim = false, showEmpty = true, total = null } = {}) {
  const sumV = segments.reduce((a, s) => a + Math.max(0, s.value), 0);
  const whole = total ?? sumV;
  const rest = Math.max(0, whole - sumV);
  const parts = segments.filter((s) => s.value > 0).map((s) => `
    <div class="allocbar-seg" style="--c:${s.color};flex:${(s.value / (whole || 1)).toFixed(5)} 1 0"
      data-tip="${esc(tipHTML(s.label, [
        { k: 'Amount', v: money(s.value), color: s.color },
        { k: 'Share', v: `${((s.value / (whole || 1)) * 100).toFixed(1)}%` },
      ]))}" role="img" aria-label="${esc(s.label)}: ${esc(money(s.value))}"></div>`).join('');
  const empty = showEmpty && rest > 0.004 ? `
    <div class="allocbar-seg empty" style="flex:${(rest / (whole || 1)).toFixed(5)} 1 0"
      data-tip="${esc(tipHTML('Not yet divided', [{ k: 'Amount', v: money(rest) }]))}"
      role="img" aria-label="Not yet divided: ${esc(money(rest))}"></div>` : '';
  return `<div class="allocbar${slim ? ' slim' : ''}">${parts}${empty}</div>`;
}

/* ==========================================================================
   Ranked bar list — names always visible, so colour never carries meaning alone
   ========================================================================== */

export function barList(rows, { max = null, valueFmt = money } = {}) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 1);
  return `<div class="ledger">${rows.map((r) => `
    <div class="ledger-row" style="display:block;padding:9px 0">
      <div style="display:flex;align-items:baseline;gap:8px">
        <span class="dot" style="--c:${r.color}"></span>
        <span class="k" style="flex:0 1 auto">${esc(r.label)}</span>
        <span class="leader"></span>
        <span class="v">${esc(valueFmt(r.value))}</span>
        ${r.note ? `<span class="v dim" style="width:44px;text-align:right">${esc(r.note)}</span>` : ''}
      </div>
      <div class="meter" style="margin-top:7px;--c:${r.color}">
        <div class="meter-fill" style="width:${((r.value / top) * 100).toFixed(1)}%"></div>
      </div>
    </div>`).join('')}</div>`;
}

/* ==========================================================================
   Legend
   ========================================================================== */

export function legend(items, { square = false } = {}) {
  return `<div class="legend">${items.map((i) => `
    <span class="legend-item"><span class="legend-key${square ? ' sq' : ''}" style="--c:${i.color}"></span>${esc(i.label)}</span>
  `).join('')}</div>`;
}

/* ==========================================================================
   Hover wiring — one delegated listener for every chart on the page
   ========================================================================== */

export function initChartHover(root = document) {
  const move = (e) => {
    const target = e.target.closest?.('[data-tip]');
    if (!target) { hideTip(); clearCrosshair(root); return; }
    showTip(target.dataset.tip, e.clientX ?? 0, e.clientY ?? 0);
    const cx = target.getAttribute('data-cx');
    if (cx) {
      const svg = target.closest('svg');
      const line = svg?.querySelector('.crosshair');
      if (line) { line.setAttribute('x1', cx); line.setAttribute('x2', cx); line.setAttribute('opacity', '1'); }
    }
  };
  root.addEventListener('pointermove', move, { passive: true });
  root.addEventListener('pointerleave', () => { hideTip(); clearCrosshair(root); }, { passive: true });
  root.addEventListener('pointerdown', move, { passive: true });
  root.addEventListener('focusin', (e) => {
    const t = e.target.closest?.('[data-tip]');
    if (!t) return;
    const r = t.getBoundingClientRect();
    showTip(t.dataset.tip, r.left + r.width / 2, r.top + 8);
  });
  root.addEventListener('focusout', () => hideTip());
  window.addEventListener('scroll', () => hideTip(), { passive: true });
}

function clearCrosshair(root) {
  root.querySelectorAll?.('.crosshair[opacity="1"]').forEach((l) => l.setAttribute('opacity', '0'));
}

export { monthLabel };
