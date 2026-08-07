/* ==========================================================================
   views/history.js — month against month, and the end-of-month recap.
   ========================================================================== */

import {
  esc, icon, money, pct, monthLabel, monthName, thisMonth, addMonths, round2, sum, dateLabel,
} from '../util.js';
import { getState, spendableBuckets } from '../store.js';
import {
  activeMonths, monthStats, monthSeries, compareMonths, spendingBreakdown, spendingTrends,
  monthSummary, bucketMonth, allBalances, lastMonths,
} from '../model.js';
import { miniArea, mountChart, columnChart, barList, sparkline, allocationBar, legend } from '../charts.js';
import { openModal } from '../ui.js';

const cvar = (b) => `var(--${b?.color || 's0'})`;

let selected = null;

function pickMonth(s) {
  const months = activeMonths(s);
  if (selected && months.includes(selected)) return selected;
  return months[months.length - 1] || thisMonth();
}

export function render() {
  const s = getState();
  const months = activeMonths(s);
  const mk = pickMonth(s);
  const prevKey = addMonths(mk, -1);
  const cmp = compareMonths(s, mk, prevKey);
  const window12 = months.slice(-12);
  const stats = monthSeries(s, window12);
  const bd = spendingBreakdown(s, mk);
  const trends = spendingTrends(s, mk);

  if (!s.transactions.length) {
    return `<div class="card c12"><div class="empty">
      <span class="insight-ico" style="width:44px;height:44px">${icon('history')}</span>
      <h3>No history yet</h3>
      <p>Once you have a month or two of income and spending recorded, this page compares them side by side.</p>
    </div></div>`;
  }

  const metric = (label, d, goodWhen = 'up', asPct = false) => {
    const diff = d.diff;
    const up = diff > 0.0001;
    const flat = Math.abs(diff) < (asPct ? 0.0005 : 0.005);
    // 'flat' means the direction is not inherently good or bad — helping at
    // home less is not a win, it is just a different month.
    const neutral = goodWhen === 'neutral';
    const good = goodWhen === 'up' ? up : !up;
    return `<div class="c4" style="background:var(--surface-2);border:1px solid var(--hairline);border-radius:var(--r);padding:14px">
      <span class="lbl">${esc(label)}</span>
      <div style="font-size:23px;font-weight:600;letter-spacing:-.032em;margin-top:6px">${
        asPct ? pct(d.now) : money(d.now)}</div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:6px;font-size:12px">
        ${flat ? '<span class="delta dim">— no change</span>' : `
          <span class="delta ${neutral ? 'dim' : good ? 'pos' : 'neg'}">${icon(up ? 'arrowUp' : 'arrowDown')}${
            asPct ? pct(Math.abs(diff), 1) : money(Math.abs(diff))}</span>`}
        <span class="dim">vs ${esc(monthLabel(prevKey, 'long'))}</span>
      </div>
    </div>`;
  };

  const compareCard = `
    <section class="hero" style="--sheen:var(--s1)">
      <div class="hero-sheen"></div>
      <div class="hero-inner" style="align-items:center">
        <div class="hero-main">
          <span class="lbl">Month in focus</span>
          <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
            <button type="button" class="icon-btn" data-step="-1" aria-label="Previous month"
              ${months.indexOf(mk) <= 0 ? 'disabled' : ''}>${icon('chevronLeft')}</button>
            <div style="font-size:clamp(26px,4.4vw,34px);font-weight:600;letter-spacing:-.035em;min-width:min(230px,52vw)">
              ${esc(monthName(mk))}
            </div>
            <button type="button" class="icon-btn" data-step="1" aria-label="Next month"
              ${months.indexOf(mk) >= months.length - 1 ? 'disabled' : ''}>${icon('chevronRight')}</button>
          </div>
        </div>
        <div class="hero-side">
          <div class="hero-mini"><span class="lbl">Savings rate</span>
            <div class="hero-mini-v">${pct(cmp.a.savingsRate)}</div></div>
          <div class="hero-mini"><span class="lbl">Left over</span>
            <div class="hero-mini-v ${cmp.a.leftover < 0 ? 'neg' : ''}">${money(cmp.a.leftover)}</div></div>
          <div class="hero-mini">
            <button type="button" class="btn btn-primary" data-review>${icon('spark')} Month in review</button>
          </div>
        </div>
      </div>
    </section>`;

  const metrics = `
    <div class="card c12">
      <div class="card-head"><span class="lbl">Compared with the month before</span></div>
      <div class="grid">
        ${metric('Income', cmp.income, 'up')}
        ${metric('Spending', cmp.spent, 'down')}
        ${metric('Saved', cmp.saved, 'up')}
        ${metric('Invested', cmp.invested, 'up')}
        ${metric('Household contribution', cmp.householdSpend, 'neutral')}
        ${metric('Savings rate', cmp.savingsRate, 'up', true)}
      </div>
    </div>`;

  const smallMultiples = `
    <div class="card c12">
      <div class="card-head"><span class="lbl">Last ${window12.length} months at a glance</span></div>
      <div class="grid">
        ${[
          { k: 'income', label: 'Income', color: 'var(--s3)' },
          { k: 'spent', label: 'Spending', color: 'var(--s2)' },
          { k: 'saved', label: 'Saved', color: 'var(--s1)' },
          { k: 'invested', label: 'Invested', color: 'var(--s7)' },
        ].map((m) => `
          <div class="c3" style="background:var(--surface-2);border:1px solid var(--hairline);border-radius:var(--r);padding:13px">
            <span class="lbl">${esc(m.label)}</span>
            <div style="font-size:19px;font-weight:600;letter-spacing:-.03em;margin-top:5px">
              ${money(stats[stats.length - 1][m.k])}
            </div>
            <div class="chart-host" data-mini="${m.k}" data-color="${m.color}" style="margin-top:8px"></div>
          </div>`).join('')}
      </div>
    </div>`;

  const whereCard = `
    <div class="card c5">
      <div class="card-head"><span class="lbl">Where the money went in ${esc(monthLabel(mk, 'long'))}</span></div>
      ${bd.rows.length ? barList(bd.rows.map((r) => ({
        label: r.bucket.name, value: r.value, color: cvar(r.bucket), note: pct(r.share),
      }))) : '<p class="dim" style="margin:0;font-size:13px">Nothing spent this month.</p>'}
    </div>`;

  const trendCard = `
    <div class="card c7">
      <div class="card-head"><span class="lbl">Spending trends · this month vs your normal</span></div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th>Bucket</th><th class="r">This month</th><th class="r">6-month average</th><th class="r">Change</th><th></th></tr></thead>
          <tbody>
            ${trends.filter((t) => t.now > 0 || t.avg > 0).map((t) => `
              <tr>
                <td><span class="txn-desc"><span class="dot" style="--c:${cvar(t.bucket)}"></span><span>${esc(t.bucket.name)}</span></span></td>
                <td class="r">${money(t.now)}</td>
                <td class="r dim">${money(t.avg)}</td>
                <td class="r ${t.change > 0.08 ? 'neg' : t.change < -0.08 ? 'pos' : 'dim'}">
                  ${t.avg > 0 ? `${t.change > 0 ? '+' : ''}${(t.change * 100).toFixed(0)}%` : 'new'}
                </td>
                <td class="r" style="width:88px">${sparkline(t.series, cvar(t.bucket), 78, 22)}</td>
              </tr>`).join('') || '<tr><td colspan="5" class="dim">No spending recorded.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>`;

  const tableCard = `
    <div class="card c12">
      <div class="card-head"><span class="lbl">Every month</span></div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Month</th><th class="r">Income</th><th class="r">Spending</th><th class="r">Saved</th>
            <th class="r">Invested</th><th class="r">Household</th><th class="r">Savings rate</th><th class="r">vs previous</th>
          </tr></thead>
          <tbody>
            ${months.slice().reverse().map((m) => {
              const st = monthStats(s, m);
              const pv = monthStats(s, addMonths(m, -1));
              const d = round2(st.saved - pv.saved);
              return `<tr data-month="${m}" tabindex="0" style="cursor:pointer;${m === mk ? 'background:var(--wash)' : ''}">
                <td class="nowrap" style="font-weight:${m === mk ? '600' : '400'}">${esc(monthName(m))}</td>
                <td class="r">${money(st.income)}</td>
                <td class="r">${money(st.spent)}</td>
                <td class="r">${money(st.saved)}</td>
                <td class="r">${money(st.invested)}</td>
                <td class="r">${money(st.householdSpend)}</td>
                <td class="r">${st.income > 0 ? pct(st.savingsRate) : '—'}</td>
                <td class="r ${d > 0.5 ? 'pos' : d < -0.5 ? 'neg' : 'dim'}">${
                  Math.abs(d) < 0.5 ? '—' : `${d > 0 ? '+' : '−'}${money(Math.abs(d))} saved`}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>`;

  return `
    ${compareCard}
    <div class="grid" style="margin-top:16px">${metrics}</div>
    <div class="grid" style="margin-top:14px">${smallMultiples}</div>
    <div class="section-head"><h2>Spending</h2></div>
    <div class="grid">${whereCard}${trendCard}</div>
    <div class="section-head"><h2>All months</h2></div>
    <div class="grid">${tableCard}</div>`;
}

/* --- month in review ------------------------------------------------------ */

function openReview(mk) {
  const s = getState();
  const sm = monthSummary(s, mk);
  const st = sm.stats;
  const bd = sm.breakdown;
  const buckets = spendableBuckets(s);
  const balances = allBalances(s);

  const line = (k, v, cls = '') => `<div class="ledger-row"><span class="k">${esc(k)}</span><span class="leader"></span><span class="v ${cls}">${v}</span></div>`;

  const body = `
    <div style="text-align:center;padding:4px 0 8px">
      <span class="lbl">${esc(monthName(mk))}</span>
      <div style="font-size:38px;font-weight:600;letter-spacing:-.04em;margin-top:6px">${money(st.saved)}</div>
      <div style="font-size:13px;color:var(--ink-2);margin-top:4px">put away — ${pct(st.savingsRate)} of everything that came in</div>
    </div>

    <div style="position:relative">
      ${allocationBar(bd.rows.map((r) => ({ label: r.bucket.name, value: r.value, color: cvar(r.bucket) })),
        { total: st.income || bd.total || 1 })}
      ${legend(bd.rows.slice(0, 6).map((r) => ({ label: r.bucket.name, color: cvar(r.bucket) })), { square: true })}
    </div>

    <div class="ledger">
      ${line('Money in', money(st.income))}
      ${line('Money out', money(st.spent))}
      ${line('Saved', money(st.saved), st.saved > 0 ? 'pos' : '')}
      ${line('Invested', money(st.invested))}
      ${line('Helped at home', money(st.householdSpend))}
      ${line('Left over', money(st.leftover), st.leftover < 0 ? 'neg' : '')}
    </div>

    <div>
      <span class="lbl" style="display:block;margin-bottom:7px">Compared with the month before</span>
      <div class="ledger">
        ${line('Income', money(sm.change.income, { sign: true }), sm.change.income >= 0 ? 'pos' : 'neg')}
        ${line('Spending', money(sm.change.spent, { sign: true }), sm.change.spent <= 0 ? 'pos' : 'neg')}
        ${line('Saved', money(sm.change.saved, { sign: true }), sm.change.saved >= 0 ? 'pos' : 'neg')}
        ${line('Invested', money(sm.change.invested, { sign: true }), sm.change.invested >= 0 ? 'pos' : 'neg')}
      </div>
    </div>

    ${sm.overspent.length ? `
      <div class="divide-status over">
        <span>${icon('alert')}</span>
        <span class="k">${sm.overspent.map((o) => esc(o.bucket.name)).join(', ')} went over budget</span>
        <span class="v">${money(sum(sm.overspent, (o) => o.overspentBy))}</span>
      </div>` : `
      <div class="divide-status done">
        <span>${icon('check')}</span>
        <span class="k">Every bucket stayed inside its plan</span>
      </div>`}

    ${sm.bestBucket ? `<p style="margin:0;font-size:12.5px;color:var(--ink-3);line-height:1.5">
      Most disciplined bucket: <strong style="color:var(--ink-2);font-weight:550">${esc(sm.bestBucket.bucket.name)}</strong>,
      which used only ${pct(sm.bestBucket.spentPct)} of what you put in it.
    </p>` : ''}`;

  openModal({ title: 'Month in review', body, wide: true, foot: '<span class="grow"></span><button type="button" class="btn btn-primary" data-close>Done</button>' });
}

/* --- mount ---------------------------------------------------------------- */

export function mount(root, ctx) {
  const s = getState();
  if (!s.transactions.length) return;
  const months = activeMonths(s);
  const mk = pickMonth(s);
  const window12 = months.slice(-12);
  const stats = monthSeries(s, window12);
  const labels = window12.map((m) => monthName(m));

  ['income', 'spent', 'saved', 'invested'].forEach((k) => {
    const host = root.querySelector(`[data-mini="${k}"]`);
    if (!host) return;
    mountChart(host, (w) => miniArea(w, {
      values: stats.map((x) => x[k]),
      labels, label: k[0].toUpperCase() + k.slice(1),
      color: host.dataset.color, height: 56,
    }), 80);
  });

  root.addEventListener('click', (e) => {
    const step = e.target.closest('[data-step]');
    if (step) {
      const i = months.indexOf(mk) + Number(step.dataset.step);
      if (i >= 0 && i < months.length) { selected = months[i]; ctx.rerender(); }
      return;
    }
    if (e.target.closest('[data-review]')) { openReview(mk); return; }
    const row = e.target.closest('[data-month]');
    if (row) { selected = row.dataset.month; ctx.rerender(); }
  });

  root.addEventListener('keydown', (e) => {
    const row = e.target.closest('[data-month]');
    if (row && e.key === 'Enter') { selected = row.dataset.month; ctx.rerender(); }
  });
}

export const actions = () => '';
export function mountActions() {}
export function setMonth(m) { selected = m; }
