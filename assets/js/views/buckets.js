/* ==========================================================================
   views/buckets.js — the money buckets: add, rename, reorder, retarget,
   and see exactly what each one holds.
   ========================================================================== */

import {
  esc, icon, money, pct, monthLabel, monthName, thisMonth, sum, round2, clamp, dateLabel,
} from '../util.js';
import { getState, spendableBuckets, UNALLOCATED_ID, bucketById } from '../store.js';
import {
  monthStats, allBalances, bucketMonth, bucketHistory, lastMonths, allocationAdvice, bucketTarget,
} from '../model.js';
import { allocationBar, sparkline, mountChart, lineChart, legend, miniArea } from '../charts.js';
import { editBucket, reorderBucket, reorderBucketTo, divideIncome, moveMoney, addExpense } from '../flows.js';
import { openModal } from '../ui.js';

const cvar = (b) => `var(--${b.color || 's0'})`;

/* Card figures round to whole currency — cents belong in the tables, not here. */
const whole = (n) => money(n, { cents: false });

const KIND_LABEL = { save: 'Saving', invest: 'Investing', spend: 'Spending', system: 'Unallocated' };

export function render() {
  const s = getState();
  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const balances = allBalances(s);
  const buckets = spendableBuckets(s);
  const advice = allocationAdvice(s);
  const unalloc = round2(balances[UNALLOCATED_ID] || 0);
  const months = lastMonths(8);

  const rows = buckets.map((b) => bucketMonth(s, b, mk, cur, balances));
  const totalAllocated = round2(sum(rows, (r) => r.allocated));
  const totalSpent = round2(sum(rows, (r) => r.spent));
  const totalTarget = round2(sum(rows, (r) => r.target));

  const summary = `
    <section class="hero" style="--sheen:var(--s1)">
      <div class="hero-sheen"></div>
      <div class="hero-inner">
        <div class="hero-main">
          <span class="lbl">Divided up this month</span>
          <div class="hero-value">${money(totalAllocated)}</div>
          <p class="hero-note">
            out of ${money(cur.income)} that came in${
              unalloc > 0.004 ? ` — <strong style="font-weight:600;color:var(--ink)">${money(unalloc)}</strong> is still undivided` : ''}.
          </p>
          ${unalloc > 0.004 ? `<button type="button" class="btn btn-primary" style="margin-top:14px" data-divide>${icon('swap')} Divide it now</button>` : ''}
        </div>
        <div class="hero-side">
          <div class="hero-mini"><span class="lbl">Spent so far</span><div class="hero-mini-v">${money(totalSpent)}</div></div>
          <div class="hero-mini"><span class="lbl">Left in buckets</span><div class="hero-mini-v">${money(round2(totalAllocated - totalSpent))}</div></div>
          <div class="hero-mini"><span class="lbl">Targets add to</span>
            <div class="hero-mini-v ${advice.over ? 'neg' : ''}">${advice.totalPct}%</div></div>
        </div>
      </div>
      <div style="position:relative;margin-top:20px">
        ${allocationBar(rows.map((r) => ({ label: r.bucket.name, value: r.allocated, color: cvar(r.bucket) })),
          { total: Math.max(cur.income, totalAllocated) || 1 })}
      </div>
    </section>`;

  const cards = rows.map((r, i) => {
    const b = r.bucket;
    const hist = bucketHistory(s, b.id, months).map((x) => x.value);
    return `
      <div class="bucket" role="button" tabindex="0" data-bucket="${esc(b.id)}" draggable="true" data-index="${i}"
        aria-label="${esc(b.name)}, ${esc(money(r.balance))}">
        <div class="bucket-top">
          <span class="insight-ico" style="background:color-mix(in srgb, ${cvar(b)} 16%, transparent);color:${cvar(b)}">${icon(b.icon || 'wallet')}</span>
          <div style="min-width:0">
            <div class="bucket-name">${esc(b.name)}</div>
            <div class="bucket-kind">
              <span class="chip">${esc(KIND_LABEL[b.kind] || b.kind)}</span>
              ${r.overspent ? '<span class="chip bad">over budget</span>' : ''}
              ${b.emergency ? `<span class="chip">${icon('shield')} emergency</span>` : ''}
            </div>
          </div>
          <div class="bucket-amt">
            <div class="v">${money(r.balance)}</div>
            <div class="lbl" style="margin-top:3px">balance</div>
          </div>
        </div>

        <div style="display:flex;align-items:center;gap:9px">
          <div class="meter tall" style="flex:1;--c:${r.overspent ? 'var(--bad)' : cvar(b)}">
            <div class="meter-fill" style="width:${clamp(r.allocated > 0 ? r.spentPct * 100 : 0, 0, 100).toFixed(1)}%"></div>
          </div>
          <span class="lbl" style="font-size:9px;white-space:nowrap">${
            r.allocated > 0 ? `${Math.round(r.spentPct * 100)}% used` : 'nothing in yet'}</span>
        </div>

        <div class="bucket-figs">
          <div class="bucket-fig"><div class="lbl">Target</div><div class="v">${whole(r.target)}</div></div>
          <div class="bucket-fig"><div class="lbl">Put in</div><div class="v">${whole(r.allocated)}</div></div>
          <div class="bucket-fig">
            <div class="lbl">${b.kind === 'invest' ? 'Invested' : 'Spent'}</div>
            <div class="v">${whole(b.kind === 'invest' ? r.contributed : r.spent)}</div>
          </div>
          <div class="bucket-fig"><div class="lbl">Left</div><div class="v ${r.remaining < 0 ? 'neg' : ''}">${whole(r.remaining)}</div></div>
        </div>

        <div class="bucket-foot">
          <span class="lbl">${r.pctOfIncome > 0 ? `${(r.pctOfIncome * 100).toFixed(0)}% of income` : 'balance trend'}</span>
          <span class="spark-slot">${sparkline(hist, cvar(b), 84, 22)}</span>
        </div>
      </div>`;
  }).join('');

  const unallocCard = `
    <div class="card c12" style="border-style:${unalloc > 0.004 ? 'solid' : 'dashed'}">
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
        <span class="insight-ico" style="background:var(--wash)">${icon('wallet')}</span>
        <div style="min-width:0">
          <div style="font-size:14px;font-weight:600;letter-spacing:-.015em">Unallocated Money</div>
          <div style="font-size:12.5px;color:var(--ink-2);margin-top:2px">
            Money that has arrived but has not been given a job yet. It is never lost — it just sits here.
          </div>
        </div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:12px">
          <div style="text-align:right">
            <div style="font-size:21px;font-weight:600;letter-spacing:-.03em">${money(unalloc)}</div>
          </div>
          ${unalloc > 0.004 ? '<button type="button" class="btn btn-primary btn-sm" data-divide>Divide it</button>' : ''}
        </div>
      </div>
    </div>`;

  const targetsCard = `
    <div class="card c12">
      <div class="card-head">
        <span class="lbl">Monthly targets</span>
        <div class="card-head-actions">
          <span class="chip ${advice.over ? 'bad' : advice.totalPct > 99.99 ? 'good' : ''}">
            ${advice.totalPct}% of income assigned
          </span>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Bucket</th><th>Kind</th><th class="r">Target</th><th class="r">On ${esc(money(cur.income))} income</th>
            <th class="r">Actually put in</th><th class="r">Difference</th><th></th>
          </tr></thead>
          <tbody>
            ${rows.map((r) => `
              <tr>
                <td><span class="txn-desc"><span class="dot" style="--c:${cvar(r.bucket)}"></span><span>${esc(r.bucket.name)}</span></span></td>
                <td class="dim nowrap">${esc(KIND_LABEL[r.bucket.kind])}</td>
                <td class="r">${r.bucket.targetMode === 'percent' ? `${r.bucket.targetValue || 0}%` : money(r.bucket.targetValue || 0)}</td>
                <td class="r">${money(r.target)}</td>
                <td class="r">${money(r.allocated)}</td>
                <td class="r ${r.allocated - r.target < -0.5 ? 'neg' : r.allocated - r.target > 0.5 ? 'pos' : 'dim'}">
                  ${r.target > 0 ? money(r.allocated - r.target, { sign: true }) : '—'}</td>
                <td class="r"><button type="button" class="icon-btn sm bare" data-edit="${esc(r.bucket.id)}" aria-label="Edit ${esc(r.bucket.name)}">${icon('edit')}</button></td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <p style="margin:12px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        Targets are yours to change — nothing here is fixed. They do not have to add up to 100%;
        whatever you leave over simply stays unallocated until you decide.
      </p>
    </div>`;

  return `
    ${summary}
    <div class="section-head">
      <h2>Your buckets</h2>
      <span class="lbl">drag to reorder</span>
    </div>
    <div class="bucket-grid stagger" id="bucket-grid">${cards}</div>
    <div class="grid" style="margin-top:14px">${unallocCard}</div>
    <div class="section-head"><h2>Targets</h2></div>
    <div class="grid">${targetsCard}</div>`;
}

/* --- bucket detail -------------------------------------------------------- */

function openBucket(id, ctx) {
  const s = getState();
  const b = bucketById(id);
  if (!b) return;
  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const balances = allBalances(s);
  const r = bucketMonth(s, b, mk, cur, balances);
  const months = lastMonths(12);
  const hist = bucketHistory(s, b.id, months);

  const txns = s.transactions
    .filter((t) => t.bucketId === b.id || t.toBucketId === b.id)
    .slice(0, 12);

  const body = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
      <span class="insight-ico" style="background:color-mix(in srgb, ${cvar(b)} 16%, transparent);color:${cvar(b)};width:34px;height:34px">${icon(b.icon || 'wallet')}</span>
      <div>
        <div style="font-size:26px;font-weight:600;letter-spacing:-.035em;line-height:1">${money(r.balance)}</div>
        <div class="lbl" style="margin-top:4px">balance right now</div>
      </div>
      <span class="chip" style="margin-left:auto">${esc(KIND_LABEL[b.kind])}</span>
    </div>

    ${b.note ? `<p style="margin:0;font-size:13px;color:var(--ink-2)">${esc(b.note)}</p>` : ''}

    <div>
      <span class="lbl" style="display:block;margin-bottom:8px">Balance over the last 12 months</span>
      <div class="chart-host" data-chart="bhist"></div>
    </div>

    <div>
      <span class="lbl" style="display:block;margin-bottom:6px">${esc(monthName(mk))}</span>
      <div class="ledger">
        <div class="ledger-row"><span class="k">Monthly target</span><span class="leader"></span><span class="v">${money(r.target)}${
          b.targetMode === 'percent' ? ` <span class="dim">(${b.targetValue || 0}%)</span>` : ''}</span></div>
        <div class="ledger-row"><span class="k">Put in this month</span><span class="leader"></span><span class="v">${money(r.allocated)}</span></div>
        <div class="ledger-row"><span class="k">Spent this month</span><span class="leader"></span><span class="v">${money(r.spent)}</span></div>
        <div class="ledger-row"><span class="k">Remaining</span><span class="leader"></span><span class="v ${r.remaining < 0 ? 'neg' : ''}">${money(r.remaining)}</span></div>
        <div class="ledger-row"><span class="k">Share of income</span><span class="leader"></span><span class="v">${r.pctOfIncome > 0 ? pct(r.pctOfIncome, 1) : '—'}</span></div>
      </div>
    </div>

    <div>
      <span class="lbl" style="display:block;margin-bottom:6px">Recent activity</span>
      ${txns.length ? `<div class="ledger">${txns.map((t) => {
        const out = (t.type === 'expense' || t.type === 'contribution' || (t.type === 'transfer' && t.bucketId === b.id));
        return `<div class="ledger-row">
          <span class="k" style="color:var(--ink)">${esc(t.description)}</span>
          <span class="leader"></span>
          <span class="v dim">${esc(dateLabel(t.date, 'short'))}</span>
          <span class="v ${out ? '' : 'pos'}">${out ? '−' : '+'}${money(t.amount)}</span>
        </div>`;
      }).join('')}</div>` : '<p class="dim" style="margin:0;font-size:13px">Nothing yet.</p>'}
    </div>`;

  const m = openModal({
    title: b.name,
    wide: true,
    body,
    foot: `<button type="button" class="btn btn-sm" data-move>${icon('swap')} Move money</button>
      <button type="button" class="btn btn-sm" data-spend>${icon('upload')} Spend</button>
      <span class="grow"></span>
      <button type="button" class="btn btn-primary" data-editb>${icon('edit')} Edit bucket</button>`,
  });

  mountChart(m.el.querySelector('[data-chart="bhist"]'), (w) => lineChart(w, {
    labels: months.map((x) => monthLabel(x)),
    fullLabels: months.map((x) => monthName(x)),
    height: 170, padR: 46,
    series: [{ label: b.name, color: cvar(b), values: hist.map((x) => x.value), fill: true }],
    ariaLabel: `${b.name} balance over 12 months`,
  }));

  m.el.querySelector('[data-editb]').addEventListener('click', () => { m.close(); editBucket(b); });
  m.el.querySelector('[data-move]').addEventListener('click', () => { m.close(); moveMoney({ to: b.id }); });
  m.el.querySelector('[data-spend]').addEventListener('click', () => { m.close(); addExpense({ bucketId: b.id }); });
}

/* --- mount ---------------------------------------------------------------- */

export function mount(root, ctx) {
  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-divide]')) { divideIncome(); return; }
    const ed = e.target.closest('[data-edit]');
    if (ed) { editBucket(bucketById(ed.dataset.edit)); return; }
    const card = e.target.closest('[data-bucket]');
    if (card) openBucket(card.dataset.bucket, ctx);
  });

  root.addEventListener('keydown', (e) => {
    const card = e.target.closest('[data-bucket]');
    if (!card) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openBucket(card.dataset.bucket, ctx); }
    if ((e.metaKey || e.altKey) && (e.key === 'ArrowLeft' || e.key === 'ArrowUp')) { e.preventDefault(); reorderBucket(card.dataset.bucket, -1); }
    if ((e.metaKey || e.altKey) && (e.key === 'ArrowRight' || e.key === 'ArrowDown')) { e.preventDefault(); reorderBucket(card.dataset.bucket, 1); }
  });

  // drag to reorder
  const grid = root.querySelector('#bucket-grid');
  if (!grid) return;
  let dragId = null;
  grid.addEventListener('dragstart', (e) => {
    const card = e.target.closest('[data-bucket]');
    if (!card) return;
    dragId = card.dataset.bucket;
    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', dragId); } catch {}
  });
  grid.addEventListener('dragend', (e) => {
    e.target.closest('[data-bucket]')?.classList.remove('dragging');
    grid.querySelectorAll('.drag-over').forEach((n) => n.classList.remove('drag-over'));
  });
  grid.addEventListener('dragover', (e) => {
    const card = e.target.closest('[data-bucket]');
    if (!card || !dragId || card.dataset.bucket === dragId) return;
    e.preventDefault();
    grid.querySelectorAll('.drag-over').forEach((n) => n.classList.remove('drag-over'));
    card.classList.add('drag-over');
  });
  grid.addEventListener('drop', (e) => {
    const card = e.target.closest('[data-bucket]');
    if (!card || !dragId) return;
    e.preventDefault();
    reorderBucketTo(dragId, card.dataset.bucket);
    dragId = null;
  });
}

export const actions = () => `
  <button type="button" class="btn btn-primary" data-newbucket>${icon('plus')} New bucket</button>`;

export function mountActions(bar) {
  bar.querySelector('[data-newbucket]')?.addEventListener('click', () => editBucket(null));
}
