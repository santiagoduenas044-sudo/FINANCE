/* ==========================================================================
   app.js — shell, routing, theme, onboarding.
   ========================================================================== */

import {
  $, h, esc, icon, money, monthName, thisMonth, setMoneyFormat, todayISO, dateLabel,
} from './util.js';
import { load, getState, subscribe, loadDemo, commit } from './store.js';
import { setInsightFormatter, monthStats, safeToSpend } from './model.js';
import { initChartHover, hideTip } from './charts.js';
import { openModal, toast, actionSheet, closeAllModals } from './ui.js';
import { divideIncome, quickAdd, addExpense } from './flows.js';

import * as home from './views/home.js';
import * as buckets from './views/buckets.js';
import * as goals from './views/goals.js';
import * as transactions from './views/transactions.js';
import * as investments from './views/investments.js';
import * as history from './views/history.js';
import * as settings from './views/settings.js';

const VIEWS = {
  home: { mod: home, label: 'Home', icon: 'home', title: 'Dashboard' },
  buckets: { mod: buckets, label: 'Buckets', icon: 'buckets', title: 'Money buckets' },
  goals: { mod: goals, label: 'Goals', icon: 'goal', title: 'Goals' },
  transactions: { mod: transactions, label: 'Transactions', icon: 'list', title: 'Transactions' },
  invest: { mod: investments, label: 'Invest', icon: 'invest', title: 'Investments' },
  history: { mod: history, label: 'History', icon: 'history', title: 'Monthly history' },
  settings: { mod: settings, label: 'Settings', icon: 'settings', title: 'Settings' },
};

const RAIL_ORDER = ['home', 'buckets', 'goals', 'transactions', 'invest', 'history'];
const TAB_ORDER = ['home', 'buckets', null, 'goals', 'more'];
const MORE_VIEWS = ['transactions', 'invest', 'history', 'settings'];

let current = 'home';

/* --- theme ---------------------------------------------------------------- */

function applyTheme() {
  const t = getState().settings.theme || 'system';
  if (t === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    meta.setAttribute('content', dark ? '#0B0C0E' : '#F1F1F4');
  }
}

function cycleTheme() {
  const order = ['system', 'dark', 'light'];
  const cur = getState().settings.theme || 'system';
  const next = order[(order.indexOf(cur) + 1) % order.length];
  commit((s) => { s.settings.theme = next; });
  applyTheme();
  toast(`Appearance: ${next === 'system' ? 'matching your device' : next}`);
}

function applySettings() {
  const s = getState();
  setMoneyFormat(s.settings.currency, s.settings.locale);
  setInsightFormatter((n) => money(n));
  applyTheme();
}

/* --- greeting ------------------------------------------------------------- */

function greeting() {
  const hr = new Date().getHours();
  const name = getState().settings.name?.trim();
  const part = hr < 5 ? 'Late night' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
  return name ? `${part}, ${name}` : part;
}

/* --- shell ---------------------------------------------------------------- */

function buildShell() {
  const app = $('#app');
  app.innerHTML = `
    <div class="shell">
      <aside class="rail">
        <div class="brand">
          <span class="brand-mark">L</span>
          <span class="brand-text">
            <span class="brand-name">Ledger</span>
            <span class="brand-sub">Personal finance</span>
          </span>
        </div>
        <nav id="rail-nav" aria-label="Main"></nav>
        <div class="rail-spacer"></div>
        <div class="rail-foot">
          <button type="button" class="btn btn-primary btn-block" id="rail-add">${icon('plus')} Add money</button>
          <div style="display:flex;gap:6px">
            <button type="button" class="nav-item" id="rail-settings" style="flex:1">${icon('settings')} Settings</button>
            <button type="button" class="icon-btn bare" id="rail-theme" aria-label="Change appearance">${icon('moon')}</button>
          </div>
        </div>
      </aside>

      <main class="main">
        <div class="wrap">
          <header class="topbar">
            <div class="topbar-meta">
              <span class="lbl" id="top-eyebrow"></span>
              <h1 id="top-title">Dashboard</h1>
            </div>
            <div class="topbar-actions" id="top-actions"></div>
          </header>
          <div id="view-host"></div>
        </div>
      </main>
    </div>

    <nav class="tabbar" id="tabbar" aria-label="Main"></nav>`;

  const nav = $('#rail-nav');
  nav.innerHTML = RAIL_ORDER.map((k) => `
    <button type="button" class="nav-item" data-nav="${k}">${icon(VIEWS[k].icon)}<span>${esc(VIEWS[k].label)}</span></button>`).join('');

  const tabbar = $('#tabbar');
  tabbar.innerHTML = TAB_ORDER.map((k) => {
    if (k === null) return `<button type="button" class="tab add" id="tab-add" aria-label="Add money">
      <span class="tab-plus">${icon('plus')}</span></button>`;
    if (k === 'more') return `<button type="button" class="tab" data-more>${icon('more')}<span>More</span></button>`;
    return `<button type="button" class="tab" data-nav="${k}">${icon(VIEWS[k].icon)}<span>${esc(VIEWS[k].label)}</span></button>`;
  }).join('');

  app.addEventListener('click', (e) => {
    const n = e.target.closest('[data-nav]');
    if (n) { navigate(n.dataset.nav); return; }
    if (e.target.closest('#rail-settings')) { navigate('settings'); return; }
    if (e.target.closest('#rail-theme')) { cycleTheme(); return; }
    if (e.target.closest('#rail-add') || e.target.closest('#tab-add')) { quickAdd(); return; }
    if (e.target.closest('[data-more]')) { openMore(); }
  });
}

async function openMore() {
  const pick = await actionSheet('More', MORE_VIEWS.map((k) => ({
    id: k, label: VIEWS[k].label, icon: VIEWS[k].icon,
    hint: {
      transactions: 'Every movement of money',
      invest: 'Contributions, value and growth',
      history: 'Compare month against month',
      settings: 'Targets, recurring bills, backups',
    }[k],
  })));
  if (pick) navigate(pick);
}

/* --- routing -------------------------------------------------------------- */

const ctx = {
  navigate,
  rerender: () => renderView(),
  applySettings: () => { applySettings(); renderView(); },
};

function navigate(name) {
  if (!VIEWS[name]) name = 'home';
  current = name;
  if (location.hash.slice(1) !== name) {
    history_replace(name);
  }
  renderView();
  document.querySelector('.main')?.scrollTo?.({ top: 0 });
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
}

function history_replace(name) {
  try { location.hash = name; } catch { /* file:// edge cases */ }
}

function renderView() {
  const v = VIEWS[current];
  const s = getState();

  document.querySelectorAll('[data-nav]').forEach((b) => {
    b.setAttribute('aria-current', b.dataset.nav === current ? 'page' : 'false');
    if (b.dataset.nav !== current) b.removeAttribute('aria-current');
  });
  document.querySelectorAll('[data-more]').forEach((b) => {
    if (MORE_VIEWS.includes(current)) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });

  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const narrow = window.innerWidth < 760;
  $('#top-title').textContent = current === 'home' ? greeting() : v.title;
  $('#top-eyebrow').textContent = current === 'home' && !narrow
    ? `${monthName(mk)} · ${money(cur.income)} in, ${money(cur.spent)} out`
    : monthName(mk);

  const actionsBar = $('#top-actions');
  const extra = v.mod.actions ? v.mod.actions() : '';
  actionsBar.innerHTML = `
    ${extra}
    <button type="button" class="btn btn-primary desktop-hide" data-quickadd>${icon('plus')}<span class="sr-only">Add</span></button>`;
  v.mod.mountActions?.(actionsBar);
  actionsBar.querySelector('[data-quickadd]')?.addEventListener('click', () => quickAdd());

  const host = $('#view-host');
  const el = document.createElement('div');
  el.className = 'view-enter';
  el.innerHTML = v.mod.render(ctx);
  host.replaceChildren(el);
  v.mod.mount?.(el, ctx);
  hideTip();
}

/* --- onboarding ----------------------------------------------------------- */

function onboarding() {
  const s = getState();
  if (s.settings.onboarded || s.transactions.length) return;

  // The shareable preview build opens on sample data so the dashboard is alive
  // straight away. The real app always starts from the welcome screen below.
  if (window.__LEDGER_PREVIEW__) {
    loadDemo();
    applySettings();
    renderView();
    toast('Preview — loaded with sample data. Everything here is editable.', { ms: 6500 });
    return;
  }

  const bucketNames = s.buckets.filter((b) => b.kind !== 'system');

  openModal({
    title: 'Welcome to Ledger',
    wide: true,
    dismissable: false,
    body: `
      <p style="margin:0;font-size:14.5px;line-height:1.55;color:var(--ink-2)">
        One habit runs this whole app: <strong style="color:var(--ink);font-weight:600">when money comes in,
        you decide where every dollar goes</strong>. Everything else — savings rate, goals, forecasts,
        the health score — is worked out from that.
      </p>

      <div>
        <span class="lbl" style="display:block;margin-bottom:9px">Your buckets, ready to edit</span>
        <div class="ledger">
          ${bucketNames.map((b) => `
            <div class="ledger-row">
              <span class="dot" style="--c:var(--${b.color})"></span>
              <span class="k" style="color:var(--ink)">${esc(b.name)}</span>
              <span class="leader"></span>
              <span class="v dim">${b.targetValue}% suggested</span>
            </div>`).join('')}
        </div>
        <p style="margin:12px 0 0;font-size:12.5px;color:var(--ink-3);line-height:1.5">
          Those percentages are only a starting point — rename, reorder, delete or re-target any of them,
          and add your own. Nothing about your income is assumed.
        </p>
      </div>

      <div style="padding:13px;border-radius:var(--r);background:var(--surface-2);border:1px solid var(--hairline)">
        <div style="display:flex;gap:9px;align-items:flex-start">
          <span style="color:var(--ink-3);flex:none;margin-top:1px">${icon('shield')}</span>
          <p style="margin:0;font-size:12.5px;color:var(--ink-2);line-height:1.5">
            Your data never leaves this device. It is stored in this browser only — so keep a backup
            from Settings, and add Ledger to your home screen for one-tap access.
          </p>
        </div>
      </div>`,
    foot: `<button type="button" class="btn" data-demo>Explore with sample data</button>
      <span class="grow"></span>
      <button type="button" class="btn btn-primary" data-start>Add my first income</button>`,
    onMount: ({ el, close }) => {
      el.querySelector('[data-start]').addEventListener('click', () => {
        commit((st) => { st.settings.onboarded = true; });
        close();
        divideIncome();
      });
      el.querySelector('[data-demo]').addEventListener('click', () => {
        loadDemo();
        applySettings();
        close();
        toast('Sample data loaded — clear it any time from Settings.');
      });
    },
  });
}

/* --- keyboard ------------------------------------------------------------- */

function keyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea, select')) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const map = { 1: 'home', 2: 'buckets', 3: 'goals', 4: 'transactions', 5: 'invest', 6: 'history', 7: 'settings' };
    if (map[e.key]) { e.preventDefault(); navigate(map[e.key]); return; }
    if (e.key === 'i' || e.key === 'I') { e.preventDefault(); divideIncome(); }
    if (e.key === 'e' || e.key === 'E') { e.preventDefault(); addExpense(); }
    if (e.key === 'n' || e.key === 'N') { e.preventDefault(); quickAdd(); }
  });
}

/* --- boot ----------------------------------------------------------------- */

function boot() {
  load();
  applySettings();
  buildShell();
  initChartHover(document);
  keyboard();

  const hash = location.hash.slice(1);
  current = VIEWS[hash] ? hash : 'home';
  renderView();

  window.addEventListener('hashchange', () => {
    const nextName = location.hash.slice(1);
    if (VIEWS[nextName] && nextName !== current) { current = nextName; renderView(); }
  });

  subscribe(() => { applyTheme(); renderView(); });

  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', applyTheme);

  onboarding();

  if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    });
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
