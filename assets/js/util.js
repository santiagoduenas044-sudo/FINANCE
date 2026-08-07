/* ==========================================================================
   util.js — formatting, dates, DOM helpers, icons
   ========================================================================== */

/* --- numbers & money ------------------------------------------------------ */

export const round2 = (n) => Math.round((Number(n) || 0) * 100) / 100;

export const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

export const sum = (arr, fn = (x) => x) => arr.reduce((a, b) => a + (Number(fn(b)) || 0), 0);

let _currency = 'USD';
let _locale = 'en-US';

export function setMoneyFormat(currency, locale) {
  _currency = currency || 'USD';
  _locale = locale || 'en-US';
}

export function currencySymbol() {
  try {
    return (0).toLocaleString(_locale, { style: 'currency', currency: _currency, maximumFractionDigits: 0 })
      .replace(/[\d\s.,]/g, '') || '$';
  } catch { return '$'; }
}

/** Full money string, e.g. $1,240.50 */
export function money(n, { cents = 'auto', sign = false } = {}) {
  const v = Number(n) || 0;
  const showCents = cents === true || (cents === 'auto' && Math.abs(v % 1) > 0.004);
  let s;
  try {
    s = Math.abs(v).toLocaleString(_locale, {
      style: 'currency', currency: _currency,
      minimumFractionDigits: showCents ? 2 : 0,
      maximumFractionDigits: showCents ? 2 : 0,
    });
  } catch {
    s = '$' + Math.abs(v).toFixed(showCents ? 2 : 0);
  }
  const neg = v < -0.004;
  if (neg) return '-' + s;
  if (sign && v > 0.004) return '+' + s;
  return s;
}

/** Compact money for axis ticks and tight tiles: $4.2K, $1.3M */
export function moneyCompact(n) {
  const v = Number(n) || 0;
  const a = Math.abs(v);
  const sgn = v < 0 ? '-' : '';
  const sym = currencySymbol();
  if (a >= 1e6) return `${sgn}${sym}${trimZero(a / 1e6)}M`;
  if (a >= 1e4) return `${sgn}${sym}${trimZero(a / 1e3)}K`;
  if (a >= 1000) return `${sgn}${sym}${Math.round(a).toLocaleString(_locale)}`;
  return `${sgn}${sym}${a >= 100 ? Math.round(a) : trimZero(a)}`;
}

const trimZero = (n) => String(Math.round(n * 10) / 10).replace(/\.0$/, '');

export function pct(n, digits = 0) {
  if (!isFinite(n)) return '—';
  return `${(n * 100).toFixed(digits)}%`;
}

/** Parse a user-typed amount: "1,200.50", "1 200", "$40" */
export function parseAmount(str) {
  if (typeof str === 'number') return round2(str);
  const cleaned = String(str ?? '').replace(/[^0-9.\-]/g, '');
  const v = parseFloat(cleaned);
  return isFinite(v) ? round2(v) : 0;
}

/* --- dates ---------------------------------------------------------------- */

export const pad2 = (n) => String(n).padStart(2, '0');

/** Today as YYYY-MM-DD in local time */
export function todayISO() { return toISO(new Date()); }

export function toISO(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** Parse YYYY-MM-DD as a *local* date (avoids UTC off-by-one) */
export function fromISO(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  return new Date(y || 1970, (m || 1) - 1, d || 1);
}

/** Month key YYYY-MM */
export const monthKey = (iso) => String(iso || '').slice(0, 7);
export const thisMonth = () => monthKey(todayISO());

export function monthLabel(key, style = 'short') {
  const [y, m] = key.split('-').map(Number);
  const d = new Date(y, m - 1, 1);
  try {
    return d.toLocaleDateString(_locale, { month: style, year: style === 'short' ? '2-digit' : 'numeric' })
      .replace(',', '');
  } catch { return key; }
}

export function monthName(key) {
  const [y, m] = key.split('-').map(Number);
  try { return new Date(y, m - 1, 1).toLocaleDateString(_locale, { month: 'long', year: 'numeric' }); }
  catch { return key; }
}

export function addMonths(key, n) {
  const [y, m] = key.split('-').map(Number);
  const d = new Date(y, m - 1 + n, 1);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
}

/** Inclusive list of month keys between two keys */
export function monthRange(from, to) {
  const out = [];
  let k = from;
  let guard = 0;
  while (k <= to && guard++ < 600) { out.push(k); k = addMonths(k, 1); }
  return out;
}

export function daysInMonth(key) {
  const [y, m] = key.split('-').map(Number);
  return new Date(y, m, 0).getDate();
}

export function dateLabel(iso, style = 'med') {
  const d = fromISO(iso);
  try {
    if (style === 'short') return d.toLocaleDateString(_locale, { month: 'short', day: 'numeric' });
    if (style === 'long') return d.toLocaleDateString(_locale, { month: 'long', day: 'numeric', year: 'numeric' });
    return d.toLocaleDateString(_locale, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch { return iso; }
}

/** Whole + fractional months from today until an ISO date (can be negative) */
export function monthsUntil(iso) {
  if (!iso) return Infinity;
  const now = new Date();
  const end = fromISO(iso);
  const months = (end.getFullYear() - now.getFullYear()) * 12 + (end.getMonth() - now.getMonth());
  const dayFrac = (end.getDate() - now.getDate()) / 30.44;
  return months + dayFrac;
}

export function relativeTime(iso) {
  const days = Math.round((fromISO(iso) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  if (days === -1) return 'yesterday';
  if (days < 0) return `${Math.abs(days)} days ago`;
  if (days < 31) return `in ${days} days`;
  const m = Math.round(days / 30.44);
  if (m < 12) return `in ${m} month${m === 1 ? '' : 's'}`;
  const y = days / 365.25;
  return `in ${y.toFixed(y < 10 ? 1 : 0)} years`;
}

/** Human duration for a month count */
export function durationLabel(months) {
  if (!isFinite(months) || months < 0) return '—';
  if (months < 1) return 'under a month';
  const m = Math.round(months);
  if (m < 12) return `${m} month${m === 1 ? '' : 's'}`;
  const y = Math.floor(m / 12), r = m % 12;
  return r ? `${y}y ${r}m` : `${y} year${y === 1 ? '' : 's'}`;
}

/* --- ids ------------------------------------------------------------------ */

export function uid(prefix = 'id') {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

/* --- DOM ------------------------------------------------------------------ */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Create an element from an HTML string */
export function h(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** Escape for interpolation into HTML */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Delegated event binding */
export function on(root, evt, sel, fn) {
  root.addEventListener(evt, (e) => {
    const t = e.target.closest(sel);
    if (t && root.contains(t)) fn(e, t);
  });
}

/** Animate a number from its current value to a target */
export function countUp(el, to, fmt = money, ms = 620) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { el.textContent = fmt(to); return; }
  const from = Number(el.dataset.v || 0);
  el.dataset.v = to;
  const t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / ms);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(from + (to - from) * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function download(filename, text, mime = 'application/json') {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/* --- icons ---------------------------------------------------------------- */
/* Stroke icons on a 24 grid, 1.7 weight, round caps — one consistent family. */

const P = {
  home: '<path d="M3.5 10.4 12 3.8l8.5 6.6V19a1.6 1.6 0 0 1-1.6 1.6h-3.3v-6H8.4v6H5.1A1.6 1.6 0 0 1 3.5 19z"/>',
  buckets: '<path d="M4 6.5h16M5.5 6.5 7 19.2a1.4 1.4 0 0 0 1.4 1.3h7.2a1.4 1.4 0 0 0 1.4-1.3L18.5 6.5M9.2 6.5V4.8a1.3 1.3 0 0 1 1.3-1.3h3a1.3 1.3 0 0 1 1.3 1.3v1.7M10 11v5M14 11v5"/>',
  goal: '<circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
  list: '<path d="M8.5 6.5h11M8.5 12h11M8.5 17.5h11M4.4 6.5h.01M4.4 12h.01M4.4 17.5h.01"/>',
  invest: '<path d="M3.5 16.8 9 11.2l3.6 3.6 7.9-8.3M20.5 11.4V6.5h-4.9"/>',
  history: '<rect x="3.4" y="4.8" width="17.2" height="15.8" rx="2.4"/><path d="M3.4 9.6h17.2M8.2 3.4v3M15.8 3.4v3M8 14h2.4M8 17.4h6.6M13.6 14h2.6"/>',
  settings: '<path d="M4 7.5h5M13 7.5h7M4 16.5h7M15 16.5h5"/><circle cx="11" cy="7.5" r="2.1"/><circle cx="13" cy="16.5" r="2.1"/>',
  plus: '<path d="M12 5.2v13.6M5.2 12h13.6"/>',
  minus: '<path d="M5.2 12h13.6"/>',
  close: '<path d="M6.2 6.2 17.8 17.8M17.8 6.2 6.2 17.8"/>',
  chevronRight: '<path d="M9.5 5.5 16 12l-6.5 6.5"/>',
  chevronLeft: '<path d="M14.5 5.5 8 12l6.5 6.5"/>',
  chevronDown: '<path d="M5.5 9.5 12 16l6.5-6.5"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14M18 13l-6 6-6-6"/>',
  arrowRight: '<path d="M4.5 12h15M13.5 6l6 6-6 6"/>',
  edit: '<path d="M15.6 4.6a2 2 0 0 1 2.8 2.8L8.2 17.6l-3.7 1 1-3.7z"/>',
  trash: '<path d="M4.6 6.6h14.8M9.4 6.6V4.9a1.2 1.2 0 0 1 1.2-1.2h2.8a1.2 1.2 0 0 1 1.2 1.2v1.7M6.4 6.6l.9 12.5a1.4 1.4 0 0 0 1.4 1.3h6.6a1.4 1.4 0 0 0 1.4-1.3l.9-12.5"/>',
  grip: '<path d="M9 6.5h.01M15 6.5h.01M9 12h.01M15 12h.01M9 17.5h.01M15 17.5h.01" stroke-width="2.4"/>',
  lock: '<rect x="4.8" y="10.4" width="14.4" height="9.8" rx="2.2"/><path d="M8.2 10.4V7.8a3.8 3.8 0 0 1 7.6 0v2.6"/>',
  unlock: '<rect x="4.8" y="10.4" width="14.4" height="9.8" rx="2.2"/><path d="M8.2 10.4V7.8a3.8 3.8 0 0 1 7.3-1.4"/>',
  check: '<path d="M4.8 12.6 9.6 17.4 19.2 6.6"/>',
  alert: '<path d="M12 4.6 2.9 20.2h18.2z"/><path d="M12 10v4.2M12 17.4h.01"/>',
  info: '<circle cx="12" cy="12" r="8.4"/><path d="M12 11.4v5M12 8.2h.01"/>',
  spark: '<path d="M12 3.4 13.9 9l5.6 1.9-5.6 1.9L12 18.4 10.1 12.8 4.5 10.9 10.1 9z"/><path d="M18.6 3.4v3.2M20.2 5h-3.2"/>',
  flame: '<path d="M12 20.4c3.3 0 5.6-2.2 5.6-5.2 0-3.6-3-5-4.2-8.8-.3-.9-.3-2-.3-2.8-2.5 1.4-4.4 4-4.4 6.4 0 1.2.4 2 .4 2s-1.3-.6-1.9-1.8c-.7 1.3-1.2 2.9-1.2 4.6 0 3.2 2.4 5.6 6 5.6z"/>',
  shield: '<path d="M12 3.4 4.8 6.2v5.3c0 4.3 2.9 7.6 7.2 9.1 4.3-1.5 7.2-4.8 7.2-9.1V6.2z"/><path d="M9.2 12.1 11.3 14.2l3.6-4"/>',
  repeat: '<path d="M4.5 8.6h11.4a3.6 3.6 0 0 1 3.6 3.6v.4M7.6 5.4 4.4 8.6l3.2 3.2M19.5 15.4H8.1a3.6 3.6 0 0 1-3.6-3.6v-.4M16.4 18.6l3.2-3.2-3.2-3.2"/>',
  wallet: '<path d="M3.6 8.4a2.4 2.4 0 0 1 2.4-2.4h11.2A2.4 2.4 0 0 1 19.6 8.4M3.6 8.4v8.8a2.4 2.4 0 0 0 2.4 2.4h12a2.4 2.4 0 0 0 2.4-2.4v-2.6M3.6 8.4h14.8a2 2 0 0 1 2 2v1.8h-4.2a2 2 0 0 0 0 4h4.2"/>',
  download: '<path d="M12 3.8v11.4M7.4 10.8 12 15.4l4.6-4.6M4.4 19.4h15.2"/>',
  upload: '<path d="M12 15.4V4M7.4 8.6 12 4l4.6 4.6M4.4 19.4h15.2"/>',
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.4 5.4l1.6 1.6M17 17l1.6 1.6M18.6 5.4 17 7M7 17l-1.6 1.6"/>',
  moon: '<path d="M20 13.6A8.4 8.4 0 0 1 10.4 4a8.4 8.4 0 1 0 9.6 9.6z"/>',
  more: '<circle cx="5.4" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="18.6" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.4"/><path d="M15.5 15.5 20.4 20.4"/>',
  plane: '<path d="M10.5 19.4 12 20.6l1.8-4.6 5.2-1.4a1.6 1.6 0 0 0 1.2-1.6V11l-6 1.6-1.9-5.2 2-1.4V3.4L12 4.7 8.7 3.4v2.6l2 1.4-1.9 5.2L2.8 11v1a1.6 1.6 0 0 0 1.2 1.6l5.2 1.4L11 20.6z"/>',
  cart: '<circle cx="9.6" cy="19.4" r="1.4"/><circle cx="17.4" cy="19.4" r="1.4"/><path d="M2.8 3.8h2.6l2.4 11.2h10.4l2-8H6.4"/>',
  film: '<rect x="3.2" y="5.2" width="17.6" height="13.6" rx="2.2"/><path d="M8 5.2v13.6M16 5.2v13.6M3.2 12h17.6M3.2 8.6h4.8M3.2 15.4h4.8M16 8.6h4.8M16 15.4h4.8"/>',
  heart: '<path d="M12 20.2S3.6 15.2 3.6 9.5A4.5 4.5 0 0 1 12 7a4.5 4.5 0 0 1 8.4 2.5c0 5.7-8.4 10.7-8.4 10.7z"/>',
  user: '<circle cx="12" cy="8" r="3.8"/><path d="M4.8 20.4a7.6 7.6 0 0 1 14.4 0"/>',
  house: '<path d="M3.6 10.6 12 4l8.4 6.6V19a1.6 1.6 0 0 1-1.6 1.6H5.2A1.6 1.6 0 0 1 3.6 19z"/><path d="M9.6 20.6v-6h4.8v6"/>',
  car: '<path d="M4.6 16.4v2.2a1 1 0 0 0 1 1h1.6a1 1 0 0 0 1-1v-2.2M15.8 16.4v2.2a1 1 0 0 0 1 1h1.6a1 1 0 0 0 1-1v-2.2"/><path d="M4.2 16.4h15.6v-4.2l-2-5.2a1.6 1.6 0 0 0-1.5-1H7.7a1.6 1.6 0 0 0-1.5 1l-2 5.2z"/><path d="M4.2 12.2h15.6M7.6 14.4h.01M16.4 14.4h.01"/>',
  book: '<path d="M4.4 4.6h5.2A2.8 2.8 0 0 1 12 6.9v12.5a2.2 2.2 0 0 0-2.2-1.7H4.4z"/><path d="M19.6 4.6h-5.2A2.8 2.8 0 0 0 12 6.9v12.5a2.2 2.2 0 0 1 2.2-1.7h5.4z"/>',
  gift: '<rect x="3.4" y="9.2" width="17.2" height="4.2" rx="1"/><path d="M4.8 13.4v5.8a1.4 1.4 0 0 0 1.4 1.4h11.6a1.4 1.4 0 0 0 1.4-1.4v-5.8M12 9.2v11.4"/><path d="M12 9.2S10.6 4 8.2 4a2.2 2.2 0 0 0 0 5.2zM12 9.2S13.4 4 15.8 4a2.2 2.2 0 0 1 0 5.2z"/>',
  coffee: '<path d="M4.4 8.4h12v6.2a4.6 4.6 0 0 1-9.2 0z" /><path d="M16.4 9.6h1.4a2.6 2.6 0 0 1 0 5.2h-1.4M4.4 20.2h12"/>',
  phone: '<rect x="6.6" y="2.8" width="10.8" height="18.4" rx="2.4"/><path d="M10.6 18.2h2.8"/>',
  pig: '<path d="M20.4 11.4h1.2M3.6 12.4a5.6 5.6 0 0 1 5.6-5.6h3.6a5.6 5.6 0 0 1 5.4 4.2l1.8 1.2v2.6l-1.9.5a5.6 5.6 0 0 1-2.1 2.7v1.6a1 1 0 0 1-1 1h-1.4a1 1 0 0 1-1-1v-.7H9.6v.7a1 1 0 0 1-1 1H7.2a1 1 0 0 1-1-1v-1.9a5.6 5.6 0 0 1-2.6-4.6z"/><path d="M14.6 11.6h.01M9.6 6.9 8.4 3.8l3.4 1.2"/>',
  wrench: '<path d="M15.4 3.6a5.4 5.4 0 0 0-4.9 7.6L3.8 17.9a1.8 1.8 0 0 0 2.5 2.5l6.7-6.7a5.4 5.4 0 0 0 7.2-6.3l-3 3-2.8-.7-.7-2.8 3-3a5.4 5.4 0 0 0-1.3-.3z"/>',
  bank: '<path d="M3.4 9.6 12 4.4l8.6 5.2M5 9.6v8.2M9.6 9.6v8.2M14.4 9.6v8.2M19 9.6v8.2M3 20.4h18"/>',
  scale: '<path d="M12 4v16M7 20h10M12 6.6l-6.4 1.8M12 6.6l6.4 1.8"/><path d="M2.4 14.4a3.2 3.2 0 0 0 6.4 0L5.6 8.4zM15.2 14.4a3.2 3.2 0 0 0 6.4 0L18.4 8.4z"/>',
  bolt: '<path d="M13.4 3.4 4.8 13.6h6l-.6 7 8.6-10.2h-6z"/>',
  filter: '<path d="M3.6 5.6h16.8l-6.6 7.8v5.4l-3.6 1.8v-7.2z"/>',
  calendar: '<rect x="3.4" y="4.8" width="17.2" height="15.8" rx="2.4"/><path d="M3.4 9.6h17.2M8.2 3.4v3M15.8 3.4v3"/>',
  eye: '<path d="M2.4 12S5.9 5.6 12 5.6 21.6 12 21.6 12 18.1 18.4 12 18.4 2.4 12 2.4 12z"/><circle cx="12" cy="12" r="3"/>',
  trend: '<path d="M3.5 16.8 9 11.2l3.6 3.6 7.9-8.3M20.5 11.4V6.5h-4.9"/>',
  swap: '<path d="M7.4 4.6 3.8 8.2l3.6 3.6M3.8 8.2h13.4M16.6 19.4l3.6-3.6-3.6-3.6M20.2 15.8H6.8"/>',
};

/** Render an icon. `name` must exist in the set. */
export function icon(name, cls = '') {
  const p = P[name] || P.info;
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

export const ICON_NAMES = Object.keys(P);

/** Icons offered in the bucket editor */
export const BUCKET_ICONS = [
  'plane', 'shield', 'invest', 'heart', 'cart', 'film', 'user', 'house',
  'car', 'book', 'gift', 'coffee', 'phone', 'pig', 'wrench', 'bank',
  'wallet', 'flame', 'bolt', 'goal',
];
