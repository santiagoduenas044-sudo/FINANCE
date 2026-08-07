/* ==========================================================================
   views/transactions.js — every movement of money, searchable and editable.
   ========================================================================== */

import {
  esc, icon, money, dateLabel, monthLabel, monthName, monthKey, thisMonth, sum, round2,
} from '../util.js';
import { getState, activeBuckets, bucketById, accountById } from '../store.js';
import { activeMonths, monthStats } from '../model.js';
import { editTransaction, quickAdd, divideIncome, addExpense } from '../flows.js';

const cvar = (b) => `var(--${b?.color || 's0'})`;

const TYPE_LABEL = {
  income: 'Income', expense: 'Spending', transfer: 'Moved', contribution: 'Invested',
};
const TYPE_ICON = {
  income: 'download', expense: 'upload', transfer: 'swap', contribution: 'invest',
};

/* Filter state lives with the view so it survives re-renders. */
const filters = { q: '', month: 'all', bucket: 'all', type: 'all' };

export function render() {
  const s = getState();
  const buckets = activeBuckets(s);
  const months = activeMonths(s).slice().reverse();

  let list = s.transactions.slice();
  if (filters.month !== 'all') list = list.filter((t) => monthKey(t.date) === filters.month);
  if (filters.bucket !== 'all') list = list.filter((t) => t.bucketId === filters.bucket || t.toBucketId === filters.bucket);
  if (filters.type !== 'all') list = list.filter((t) => t.type === filters.type);
  if (filters.q.trim()) {
    const q = filters.q.trim().toLowerCase();
    list = list.filter((t) => t.description?.toLowerCase().includes(q));
  }
  list.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : (a.createdAt < b.createdAt ? 1 : -1)));

  const inTotal = round2(sum(list.filter((t) => t.type === 'income'), (t) => t.amount));
  const outTotal = round2(sum(list.filter((t) => t.type === 'expense'), (t) => t.amount));
  const invTotal = round2(sum(list.filter((t) => t.type === 'contribution'), (t) => t.amount));

  const filterBar = `
    <div class="card c12" style="padding:12px">
      <div style="display:flex;flex-wrap:wrap;gap:9px;align-items:center">
        <label style="position:relative;flex:1 1 200px;min-width:0">
          <span class="sr-only">Search descriptions</span>
          <span style="position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--ink-3);pointer-events:none;display:grid">${icon('search')}</span>
          <input class="input" id="t-q" style="padding-left:33px" placeholder="Search descriptions…" value="${esc(filters.q)}">
        </label>
        <select class="select" id="t-month" style="width:auto;min-width:132px" aria-label="Month">
          <option value="all" ${filters.month === 'all' ? 'selected' : ''}>All months</option>
          ${months.map((m) => `<option value="${m}" ${filters.month === m ? 'selected' : ''}>${esc(monthName(m))}</option>`).join('')}
        </select>
        <select class="select" id="t-bucket" style="width:auto;min-width:140px" aria-label="Bucket">
          <option value="all" ${filters.bucket === 'all' ? 'selected' : ''}>All buckets</option>
          ${buckets.map((b) => `<option value="${b.id}" ${filters.bucket === b.id ? 'selected' : ''}>${esc(b.name)}</option>`).join('')}
        </select>
        <select class="select" id="t-type" style="width:auto;min-width:120px" aria-label="Type">
          <option value="all" ${filters.type === 'all' ? 'selected' : ''}>All types</option>
          ${Object.entries(TYPE_LABEL).map(([k, v]) => `<option value="${k}" ${filters.type === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}
        </select>
        ${(filters.q || filters.month !== 'all' || filters.bucket !== 'all' || filters.type !== 'all')
          ? '<button type="button" class="btn btn-sm btn-ghost" id="t-clear">Clear</button>' : ''}
      </div>
    </div>`;

  const table = list.length ? `
    <div class="card c12">
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Date</th><th>Description</th><th>Category</th><th>Account</th><th>Type</th><th class="r">Amount</th><th></th>
          </tr></thead>
          <tbody>
            ${list.slice(0, 400).map((t) => {
              const b = bucketById(t.bucketId);
              const to = t.toBucketId ? bucketById(t.toBucketId) : null;
              const acc = accountById(t.accountId);
              const isOut = t.type === 'expense' || t.type === 'contribution';
              const isIn = t.type === 'income';
              return `<tr data-txn="${esc(t.id)}" tabindex="0">
                <td class="nowrap dim">${esc(dateLabel(t.date, 'short'))}</td>
                <td>
                  <span class="txn-desc">
                    <span class="insight-ico" style="width:22px;height:22px;border-radius:7px">${icon(TYPE_ICON[t.type] || 'list')}</span>
                    <span>${esc(t.description)}</span>
                  </span>
                </td>
                <td class="nowrap">
                  <span class="txn-desc">
                    <span class="dot" style="--c:${cvar(b)}"></span>
                    <span>${esc(to ? `${b?.name || '—'} → ${to.name}` : (b?.name || '—'))}</span>
                  </span>
                </td>
                <td class="nowrap dim">${esc(acc?.name || '—')}</td>
                <td class="nowrap"><span class="chip">${esc(TYPE_LABEL[t.type] || t.type)}</span></td>
                <td class="r ${isIn ? 'pos' : ''}">${isIn ? '+' : isOut ? '−' : ''}${money(t.amount)}</td>
                <td class="r"><button type="button" class="icon-btn sm bare" data-edit="${esc(t.id)}" aria-label="Edit">${icon('edit')}</button></td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
      ${list.length > 400 ? `<p style="margin:12px 0 0;font-size:12px;color:var(--ink-3)">Showing the 400 most recent of ${list.length}. Narrow the filters to see the rest.</p>` : ''}
    </div>` : `
    <div class="card c12">
      <div class="empty">
        <span class="insight-ico" style="width:44px;height:44px">${icon('list')}</span>
        <h3>${s.transactions.length ? 'Nothing matches those filters' : 'No transactions yet'}</h3>
        <p>${s.transactions.length
          ? 'Try clearing the search or picking a different month.'
          : 'Start by recording income and dividing it into buckets — everything else follows from that.'}</p>
        ${s.transactions.length ? '' : '<button type="button" class="btn btn-primary" data-divide style="margin-top:6px">Add income</button>'}
      </div>
    </div>`;

  const summary = `
    <div class="grid stagger">
      <div class="card c4"><div class="stat">
        <span class="lbl">Money in</span><span class="stat-value pos">${money(inTotal)}</span>
        <div class="stat-foot"><span>${list.filter((t) => t.type === 'income').length} deposits</span></div>
      </div></div>
      <div class="card c4"><div class="stat">
        <span class="lbl">Money out</span><span class="stat-value">${money(outTotal)}</span>
        <div class="stat-foot"><span>${list.filter((t) => t.type === 'expense').length} purchases</span></div>
      </div></div>
      <div class="card c4"><div class="stat">
        <span class="lbl">Invested</span><span class="stat-value">${money(invTotal)}</span>
        <div class="stat-foot"><span>${list.filter((t) => t.type === 'contribution').length} contributions</span></div>
      </div></div>
    </div>`;

  return `
    ${summary}
    <div class="grid" style="margin-top:14px">${filterBar}</div>
    <div class="grid" style="margin-top:14px">${table}</div>`;
}

export function mount(root, ctx) {
  const rerender = () => ctx.rerender();

  const q = root.querySelector('#t-q');
  if (q) {
    let timer = null;
    q.addEventListener('input', () => {
      clearTimeout(timer);
      const v = q.value;
      timer = setTimeout(() => { filters.q = v; rerender(); requestAnimationFrame(() => {
        const el = document.querySelector('#t-q');
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      }); }, 260);
    });
  }
  root.querySelector('#t-month')?.addEventListener('change', (e) => { filters.month = e.target.value; rerender(); });
  root.querySelector('#t-bucket')?.addEventListener('change', (e) => { filters.bucket = e.target.value; rerender(); });
  root.querySelector('#t-type')?.addEventListener('change', (e) => { filters.type = e.target.value; rerender(); });
  root.querySelector('#t-clear')?.addEventListener('click', () => {
    filters.q = ''; filters.month = 'all'; filters.bucket = 'all'; filters.type = 'all'; rerender();
  });

  const open = (id) => {
    const t = getState().transactions.find((x) => x.id === id);
    if (t) editTransaction(t);
  };

  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-divide]')) { divideIncome(); return; }
    const ed = e.target.closest('[data-edit]');
    if (ed) { open(ed.dataset.edit); return; }
    const row = e.target.closest('[data-txn]');
    if (row) open(row.dataset.txn);
  });
  root.addEventListener('keydown', (e) => {
    const row = e.target.closest('[data-txn]');
    if (row && e.key === 'Enter') { e.preventDefault(); open(row.dataset.txn); }
  });
}

export const actions = () => `
  <button type="button" class="btn btn-primary" data-add>${icon('plus')} Add</button>`;

export function mountActions(bar) {
  bar.querySelector('[data-add]')?.addEventListener('click', () => quickAdd());
}

export function resetFilters() {
  filters.q = ''; filters.month = 'all'; filters.bucket = 'all'; filters.type = 'all';
}
