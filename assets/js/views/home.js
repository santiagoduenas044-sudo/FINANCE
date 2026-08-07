/* ==========================================================================
   views/home.js — the dashboard. Answers, in order:
   How much do I have? Where is it? What can I safely spend?
   How much am I saving and investing? Am I on track?
   ========================================================================== */

import {
  esc, icon, money, moneyCompact, pct, monthLabel, monthName, thisMonth, addMonths,
  dateLabel, todayISO, sum, round2, clamp, durationLabel, countUp,
} from '../util.js';
import { getState, spendableBuckets, activeBuckets, UNALLOCATED_ID } from '../store.js';
import {
  monthStats, allBalances, totalCash, netWorth, netWorthSeries, safeToSpend, lastMonths,
  bucketMonth, healthScore, emergencyFund, savingsStreak, insights, investmentStats,
  forecast, upcomingThisMonth, spendingBreakdown, recentAverage, activeMonths,
  recurringMonthlyTotal,
} from '../model.js';
import { columnChart, lineChart, allocationBar, ring, sparkline, legend, mountChart, barList } from '../charts.js';
import { divideIncome, quickAdd, payRecurring } from '../flows.js';

const cvar = (b) => `var(--${b.color || 's0'})`;

/* --- small building blocks ------------------------------------------------ */

function statTile({ label, value, sub, delta, series, color, cls = 'c3' }) {
  return `<div class="card ${cls}">
    <div class="stat">
      <span class="lbl">${esc(label)}</span>
      <div class="stat-row">
        <span class="stat-value">${esc(value)}</span>
        ${series ? `<span class="spark">${sparkline(series, color || 'var(--ink-3)')}</span>` : ''}
      </div>
      <div class="stat-foot">
        ${delta ? deltaChip(delta.value, delta.goodWhen, delta.label) : ''}
        ${sub ? `<span>${esc(sub)}</span>` : ''}
      </div>
    </div>
  </div>`;
}

function deltaChip(v, goodWhen = 'up', label = 'vs last month') {
  if (!isFinite(v) || Math.abs(v) < 0.005) {
    return `<span class="delta dim">— ${esc(label)}</span>`;
  }
  const up = v > 0;
  const good = goodWhen === 'up' ? up : !up;
  return `<span class="delta ${good ? 'pos' : 'neg'}">
    ${icon(up ? 'arrowUp' : 'arrowDown')}${esc(money(Math.abs(v)))}</span>
    <span class="dim">${esc(label)}</span>`;
}

/* ========================================================================== */

export function render() {
  const s = getState();
  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const prev = monthStats(s, addMonths(mk, -1));
  const balances = allBalances(s);
  const cash = totalCash(s);
  const inv = investmentStats(s);
  const nw = netWorth(s);
  const sts = safeToSpend(s);
  const health = healthScore(s);
  const ef = emergencyFund(s);
  const streak = savingsStreak(s);
  const buckets = spendableBuckets(s);
  const months = lastMonths(12);
  const monthsWithData = activeMonths(s);
  const insightList = insights(s);
  const upcoming = upcomingThisMonth(s);
  const avgIncome = recentAverage(s, 'income', 3) || cur.income;

  if (!s.transactions.length) return emptyHome();

  /* ---- hero: how much money do I have, and where is it? ---- */
  const held = buckets
    .map((b) => ({ b, value: Math.max(0, balances[b.id] || 0) }))
    .filter((x) => x.value > 0)
    .sort((a, b) => b.value - a.value);
  const unalloc = round2(balances[UNALLOCATED_ID] || 0);
  const dominant = held[0]?.b;

  const heroSegments = held.map((x) => ({ label: x.b.name, value: x.value, color: cvar(x.b) }))
    .concat(unalloc > 0 ? [{ label: 'Unallocated Money', value: unalloc, color: 'var(--s0)' }] : []);

  const hero = `
    <section class="hero" style="--sheen:${dominant ? cvar(dominant) : 'var(--s1)'}">
      <div class="hero-sheen"></div>
      <div class="hero-inner">
        <div class="hero-main">
          <span class="lbl">Your money right now</span>
          <div class="hero-value" id="hero-cash" data-v="0">${money(cash)}</div>
          <p class="hero-note">
            Sitting across ${held.length} bucket${held.length === 1 ? '' : 's'}${
              unalloc > 0.004 ? `, with <strong style="font-weight:600;color:var(--ink)">${money(unalloc)}</strong> still waiting for a job` : ' — every dollar has a job'}.
          </p>
        </div>
        <div class="hero-side">
          <div class="hero-mini">
            <span class="lbl">Safe to spend</span>
            <div class="hero-mini-v ${sts.amount < 0 ? 'neg' : ''}">${money(sts.amount)}</div>
          </div>
          <div class="hero-mini">
            <span class="lbl">Net worth</span>
            <div class="hero-mini-v">${money(nw)}</div>
          </div>
          <div class="hero-mini">
            <span class="lbl">Invested</span>
            <div class="hero-mini-v">${money(inv.value)}</div>
          </div>
        </div>
      </div>
      <div style="position:relative;margin-top:20px">
        ${allocationBar(heroSegments, { showEmpty: false })}
        ${legend(heroSegments.slice(0, 8).map((x) => ({ label: x.label, color: x.color })), { square: true })}
      </div>
    </section>`;

  /* ---- this month ---- */
  const incSeries = months.map((m) => monthStats(s, m).income);
  const spendSeries = months.map((m) => monthStats(s, m).spent);
  const saveSeries = months.map((m) => monthStats(s, m).saved);
  const investSeries = months.map((m) => monthStats(s, m).invested);

  const tiles = `
    <div class="grid stagger">
      ${statTile({
        label: 'Income this month', value: money(cur.income),
        delta: { value: cur.income - prev.income, goodWhen: 'up' },
        series: incSeries, color: 'var(--s3)',
      })}
      ${statTile({
        label: 'Spent this month', value: money(cur.spent),
        delta: { value: cur.spent - prev.spent, goodWhen: 'down' },
        series: spendSeries, color: 'var(--s2)',
      })}
      ${statTile({
        label: 'Saved this month', value: money(cur.saved),
        sub: `${pct(cur.savingsRate)} of income`,
        series: saveSeries, color: 'var(--s1)',
      })}
      ${statTile({
        label: 'Invested this month', value: money(cur.invested),
        sub: `${pct(cur.investmentRate)} of income`,
        series: investSeries, color: 'var(--s7)',
      })}
    </div>`;

  /* ---- spending chart + health ---- */
  const spendCard = `
    <div class="card c8">
      <div class="card-head">
        <span class="lbl">Spending and income · last 12 months</span>
      </div>
      <div class="chart-host" data-chart="spend"></div>
      ${legend([
        { label: 'Spent', color: 'var(--s2)' },
        { label: 'Income', color: 'var(--s3)' },
      ], { square: true })}
    </div>`;

  const healthCard = `
    <div class="card c4">
      <div class="card-head"><span class="lbl">Financial health</span></div>
      <div style="display:flex;align-items:center;gap:16px">
        ${ring(health.total / 100, `var(--${health.tone === 'good' ? 's6' : health.tone === 'warn' ? 's4' : 's8'})`, 76, 7,
          `<span class="rv" style="font-size:19px">${health.total}</span>`)}
        <div style="min-width:0">
          <div style="font-size:16px;font-weight:600;letter-spacing:-.025em">${esc(health.grade)}</div>
          <div style="font-size:12.5px;color:var(--ink-2);margin-top:3px">out of 100, from six habits</div>
        </div>
      </div>
      <div class="ledger" style="margin-top:14px">
        <div class="ledger-row" style="padding-bottom:4px">
          <span class="lbl">Habit</span><span class="leader" style="opacity:0"></span><span class="lbl">Score</span>
        </div>
        ${health.parts.map((p) => `
          <div class="ledger-row" style="display:block;padding:8px 0">
            <div style="display:flex;align-items:baseline;gap:8px">
              <span class="k" style="color:var(--ink)">${esc(p.label)}</span>
              <span class="leader"></span>
              <span class="v ${p.score >= 0.9 ? 'pos' : p.score < 0.4 ? 'neg' : ''}">${Math.round(p.score * 100)}</span>
            </div>
            <div style="font-size:11.5px;color:var(--ink-3);margin-top:2px">${esc(p.detail)}</div>
          </div>`).join('')}
      </div>
    </div>`;

  /* ---- buckets at a glance + net worth ---- */
  const bucketRows = buckets.map((b) => {
    const bm = bucketMonth(s, b, mk, cur, balances);
    return { b, bm };
  });

  const bucketsCard = `
    <div class="card c5">
      <div class="card-head">
        <span class="lbl">Buckets this month</span>
        <div class="card-head-actions">
          <button type="button" class="btn btn-sm" data-go="buckets">Manage</button>
        </div>
      </div>
      <div class="ledger">
        ${bucketRows.map(({ b, bm }) => `
          <div class="ledger-row" style="display:block;padding:10px 0">
            <div style="display:flex;align-items:baseline;gap:8px">
              <span class="dot" style="--c:${cvar(b)}"></span>
              <span class="k" style="flex:0 1 auto;color:var(--ink)">${esc(b.name)}</span>
              ${bm.overspent ? '<span class="chip bad">over</span>' : ''}
              <span class="leader"></span>
              <span class="v">${money(bm.balance)}</span>
            </div>
            <div style="display:flex;align-items:center;gap:9px;margin-top:7px">
              <div class="meter" style="flex:1;--c:${bm.overspent ? 'var(--bad)' : cvar(b)}">
                <div class="meter-fill" style="width:${clamp(bm.allocated > 0 ? (bm.spent / bm.allocated) * 100 : 0, 0, 100).toFixed(1)}%"></div>
              </div>
              <span class="lbl" style="font-size:9px;white-space:nowrap">${money(bm.spent)} of ${money(bm.allocated)}</span>
            </div>
          </div>`).join('')}
      </div>
    </div>`;

  const nwSeries = netWorthSeries(s, months);
  const nwCard = `
    <div class="card c7">
      <div class="card-head">
        <span class="lbl">Net worth · last 12 months</span>
        <div class="card-head-actions">
          <span class="chip ${nwSeries[11].value >= nwSeries[0].value ? 'good' : 'bad'}">
            ${nwSeries[11].value >= nwSeries[0].value ? '+' : ''}${moneyCompact(nwSeries[11].value - nwSeries[0].value)} this year
          </span>
        </div>
      </div>
      <div class="chart-host" data-chart="networth"></div>
      ${legend([
        { label: 'Cash in buckets', color: 'var(--s1)' },
        { label: 'Investments', color: 'var(--s7)' },
      ], { square: true })}
      <div class="ledger" style="margin-top:12px;padding-top:4px;border-top:1px solid var(--hairline)">
        <div class="ledger-row"><span class="k">Cash across buckets</span><span class="leader"></span><span class="v">${money(cash)}</span></div>
        <div class="ledger-row"><span class="k">Investments at today's value</span><span class="leader"></span><span class="v">${money(inv.value)}</span></div>
        ${s.debts?.length ? `<div class="ledger-row"><span class="k">Debts</span><span class="leader"></span><span class="v neg">−${money(sum(s.debts, (d) => d.balance))}</span></div>` : ''}
        <div class="ledger-row"><span class="k" style="color:var(--ink);font-weight:550">Net worth</span><span class="leader"></span>
          <span class="v" style="font-weight:500">${money(nw)}</span></div>
      </div>
    </div>`;

  /* ---- rates, emergency fund, safe to spend ---- */
  const savT = s.settings.savingsRateTarget || 0.2;
  const invT = s.settings.investmentRateTarget || 0.1;

  const ratesCard = `
    <div class="card c4">
      <div class="card-head"><span class="lbl">Savings &amp; investment rate</span></div>
      <div style="display:flex;gap:20px;align-items:center;justify-content:space-around;padding:4px 0 2px">
        <div style="text-align:center">
          ${ring(clamp(cur.savingsRate / savT, 0, 1), 'var(--s1)', 82, 7,
            `<span class="rv">${pct(cur.savingsRate)}</span>`)}
          <div class="lbl" style="margin-top:9px">Saved</div>
          <div style="font-size:11.5px;color:var(--ink-3);margin-top:2px">target ${pct(savT)}</div>
        </div>
        <div style="text-align:center">
          ${ring(clamp(cur.investmentRate / invT, 0, 1), 'var(--s7)', 82, 7,
            `<span class="rv">${pct(cur.investmentRate)}</span>`)}
          <div class="lbl" style="margin-top:9px">Invested</div>
          <div style="font-size:11.5px;color:var(--ink-3);margin-top:2px">target ${pct(invT)}</div>
        </div>
      </div>
      <div class="ledger" style="margin-top:10px">
        <div class="ledger-row"><span class="k">Saved all time</span><span class="leader"></span><span class="v">${money(sum(buckets.filter((b) => b.kind === 'save'), (b) => balances[b.id] || 0))}</span></div>
        <div class="ledger-row"><span class="k">Invested all time</span><span class="leader"></span><span class="v">${money(inv.totalInvested)}</span></div>
      </div>
    </div>`;

  const efCard = `
    <div class="card c4">
      <div class="card-head"><span class="lbl">Emergency fund</span></div>
      ${ef.bucket ? `
        <div class="stat">
          <span class="stat-value">${money(ef.current)}</span>
          <div class="stat-foot">
            <span>${ef.target > 0
              ? `${ef.monthsCovered.toFixed(1)} of ${ef.monthsTarget} months of essentials`
              : `sitting in ${esc(ef.bucket.name)}`}</span>
          </div>
        </div>
        ${ef.target > 0 ? `
          <div class="meter tall" style="margin-top:12px;--c:${ef.progress >= 1 ? 'var(--good)' : cvar(ef.bucket)}">
            <div class="meter-fill" style="width:${(ef.progress * 100).toFixed(1)}%"></div>
          </div>
          <div class="ledger" style="margin-top:8px">
            <div class="ledger-row"><span class="k">Target</span><span class="leader"></span><span class="v">${money(ef.target)}</span></div>
            <div class="ledger-row"><span class="k">Still needed</span><span class="leader"></span><span class="v">${money(Math.max(0, ef.target - ef.current))}</span></div>
          </div>` : `
          <p style="margin:12px 0 0;font-size:12.5px;color:var(--ink-2);line-height:1.5">
            The target here is ${ef.monthsTarget} months of your essential spending. Record a month of
            groceries and household costs and it will size itself.
          </p>`}` : `
        <p style="margin:0;font-size:13px;color:var(--ink-2)">
          Mark one of your buckets as the emergency fund and it will be tracked here.
        </p>
        <button type="button" class="btn btn-sm" style="margin-top:12px" data-go="buckets">Choose a bucket</button>`}
    </div>`;

  const safeCard = `
    <div class="card c4">
      <div class="card-head"><span class="lbl">Safe to spend</span></div>
      <div class="stat">
        <span class="stat-value ${sts.amount < 0 ? 'neg' : ''}">${money(sts.amount)}</span>
        <div class="stat-foot">
          <span>${sts.daysLeft} day${sts.daysLeft === 1 ? '' : 's'} left${
            sts.daysLeft > 0 && sts.amount > 0 ? ` · about ${money(sts.amount / sts.daysLeft)} a day` : ''}</span>
        </div>
      </div>
      <div class="ledger" style="margin-top:12px">
        <div class="ledger-row"><span class="k">Everyday buckets</span><span class="leader"></span><span class="v">${money(sts.everydayTotal)}</span></div>
        <div class="ledger-row"><span class="k">Not yet divided</span><span class="leader"></span><span class="v">${money(sts.unallocated)}</span></div>
        <div class="ledger-row"><span class="k">Bills still due</span><span class="leader"></span><span class="v">${
          sts.shortfall > 0.004 ? `−${money(sts.shortfall)}` : money(0)}</span></div>
      </div>
      ${streak >= 2 ? `<div style="display:flex;align-items:center;gap:7px;margin-top:12px;padding-top:11px;border-top:1px solid var(--hairline)">
        <span class="chip good">${icon('flame')} ${streak}-month saving streak</span>
      </div>` : ''}
    </div>`;

  /* ---- insights + coming up ---- */
  const insightCard = `
    <div class="card c6">
      <div class="card-head"><span class="lbl">What I notice</span></div>
      <div>
        ${insightList.slice(0, 5).map((i) => `
          <div class="insight">
            <span class="insight-ico ${i.tone === 'info' ? '' : i.tone}">${icon(i.icon)}</span>
            <div class="insight-body">
              <div class="insight-title">${esc(i.title)}</div>
              <div class="insight-text">${esc(i.text)}</div>
              ${i.action === 'divide' ? '<button type="button" class="btn btn-sm" style="margin-top:8px" data-divide>Divide it now</button>' : ''}
              ${i.view ? `<button type="button" class="btn btn-sm btn-ghost" style="margin-top:6px;padding-left:0" data-go="${i.view}">Take a look ${icon('arrowRight')}</button>` : ''}
            </div>
          </div>`).join('') || '<p class="dim" style="margin:0;font-size:13px">Nothing to flag — things look steady.</p>'}
      </div>
    </div>`;

  const comingCard = `
    <div class="card c6 card-fill">
      <div class="card-head">
        <span class="lbl">Coming up before month end</span>
        <div class="card-head-actions"><button type="button" class="btn btn-sm" data-go="settings">Manage</button></div>
      </div>
      ${upcoming.length ? `
        <div class="ledger">
          ${upcoming.slice(0, 7).map((r) => {
            const b = s.buckets.find((x) => x.id === r.bucketId);
            return `<div class="ledger-row">
              <span class="dot" style="--c:${b ? cvar(b) : 'var(--s0)'}"></span>
              <span class="k">${esc(r.name)}</span>
              <span class="leader"></span>
              <span class="v dim">${esc(dateLabel(r.date, 'short'))}</span>
              <span class="v">${money(r.amount)}</span>
              <button type="button" class="icon-btn sm bare" data-pay="${esc(r.id)}" aria-label="Record ${esc(r.name)} as paid">${icon('check')}</button>
            </div>`;
          }).join('')}
        </div>
        <div style="display:flex;align-items:baseline;gap:8px;margin-top:12px;padding-top:11px;border-top:1px solid var(--hairline)">
          <span class="lbl">Total due</span><span class="leader"></span>
          <span class="num" style="font-weight:500">${money(sum(upcoming, (r) => r.amount))}</span>
        </div>` : `
        <p style="margin:0;font-size:13px;color:var(--ink-2)">No recurring expenses left this month.</p>`}
      <div class="ledger" style="margin-top:auto;padding-top:12px">
        <div class="ledger-row"><span class="k">Recurring cost each month</span><span class="leader"></span>
          <span class="v">${money(recurringMonthlyTotal(s))}</span></div>
        <div class="ledger-row"><span class="k">Share of a typical month's income</span><span class="leader"></span>
          <span class="v">${avgIncome > 0 ? pct(recurringMonthlyTotal(s) / avgIncome) : '—'}</span></div>
      </div>
      <p style="margin:11px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        Nothing is charged automatically. Tick one off and it becomes a real transaction.
      </p>
    </div>`;

  /* ---- forecast ---- */
  const fc = forecast(s, 12);
  const fcMonths = ['Now'].concat(fc.rows.map((r) => monthLabel(r.monthKey)));
  const forecastCard = `
    <div class="card c12">
      <div class="card-head">
        <span class="lbl">If things carry on like this</span>
        <div class="card-head-actions">
          <span class="chip">${icon('info')} based on your last 3 months</span>
        </div>
      </div>
      <div class="grid" style="gap:12px">
        ${[3, 6, 12].map((n) => {
          const r = fc.at(n);
          return `<div class="c4" style="background:var(--surface-2);border:1px solid var(--hairline);border-radius:var(--r);padding:13px">
            <span class="lbl">In ${n} months</span>
            <div style="font-size:22px;font-weight:600;letter-spacing:-.03em;margin-top:6px">${money(r.netWorth)}</div>
            <div class="ledger" style="margin-top:8px">
              <div class="ledger-row"><span class="k">Cash</span><span class="leader"></span><span class="v">${money(r.cash)}</span></div>
              <div class="ledger-row"><span class="k">Investments</span><span class="leader"></span><span class="v">${money(r.portfolio)}</span></div>
            </div>
          </div>`;
        }).join('')}
      </div>
      <div class="chart-host" data-chart="forecast" style="margin-top:14px"></div>
      <p style="margin:10px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        Assumes ${money(fc.assumptions.saved)} saved and ${money(fc.assumptions.invested)} invested each month,
        with investments growing ${pct(fc.assumptions.annualReturn, 1)} a year. Change those in Settings.
      </p>
    </div>`;

  return `
    ${hero}
    <div class="section-head"><h2>This month</h2><span class="lbl">${esc(monthName(mk))}</span></div>
    ${tiles}
    <div class="grid" style="margin-top:14px">
      ${spendCard}
      ${healthCard}
    </div>
    <div class="section-head"><h2>Where your money is</h2></div>
    <div class="grid">
      ${bucketsCard}
      ${nwCard}
    </div>
    <div class="grid" style="margin-top:14px">
      ${ratesCard}
      ${efCard}
      ${safeCard}
    </div>
    <div class="section-head"><h2>What happens next</h2></div>
    <div class="grid">
      ${insightCard}
      ${comingCard}
    </div>
    <div class="grid" style="margin-top:14px">
      ${forecastCard}
    </div>`;
}

/* --- empty state ---------------------------------------------------------- */

function emptyHome() {
  return `
    <section class="hero" style="--sheen:var(--s1)">
      <div class="hero-sheen"></div>
      <div class="hero-inner">
        <div class="hero-main">
          <span class="lbl">Let's begin</span>
          <div class="hero-value">${money(0)}</div>
          <p class="hero-note">
            Nothing has come in yet. Record your first income and decide, dollar by dollar, where it goes —
            every number on this dashboard is built from that one habit.
          </p>
          <button type="button" class="btn btn-primary btn-lg" style="margin-top:18px" data-divide>
            ${icon('plus')} Add income
          </button>
        </div>
      </div>
    </section>
    <div class="grid" style="margin-top:16px">
      ${[
        { icon: 'buckets', t: 'Give every dollar a job', d: 'Your buckets are already set up with starting targets. Rename them, change the percentages, add your own.' },
        { icon: 'goal', t: 'Set a goal or two', d: 'A target amount and a deadline is all it takes. The maths — required monthly, on track or not, likely finish date — is handled.' },
        { icon: 'invest', t: 'Track what you invest', d: 'Log contributions and update the value now and then, and profit, loss and growth appear on their own.' },
      ].map((c) => `
        <div class="card c4">
          <span class="insight-ico">${icon(c.icon)}</span>
          <h3 style="font-size:14.5px;margin-top:12px">${esc(c.t)}</h3>
          <p style="margin:6px 0 0;font-size:13px;color:var(--ink-2);line-height:1.5">${esc(c.d)}</p>
        </div>`).join('')}
    </div>`;
}

/* --- mount ---------------------------------------------------------------- */

export function mount(root, ctx) {
  const s = getState();
  if (!s.transactions.length) return;

  const months = lastMonths(12);
  const labels = months.map((m) => monthLabel(m));
  const fullLabels = months.map((m) => monthName(m));
  const stats = months.map((m) => monthStats(s, m));

  const spendHost = root.querySelector('[data-chart="spend"]');
  mountChart(spendHost, (w) => columnChart(w, {
    labels, fullLabels, height: 218,
    columns: { label: 'Spent', color: 'var(--s2)', values: stats.map((x) => x.spent) },
    line: { label: 'Income', color: 'var(--s3)', values: stats.map((x) => x.income) },
    ariaLabel: 'Spending columns with an income line, last 12 months',
  }));

  const nwHost = root.querySelector('[data-chart="networth"]');
  const nws = netWorthSeries(s, months);
  mountChart(nwHost, (w) => lineChart(w, {
    labels, fullLabels, height: 200,
    series: [
      { label: 'Cash in buckets', color: 'var(--s1)', values: nws.map((x) => x.cash), fill: true },
      { label: 'Investments', color: 'var(--s7)', values: nws.map((x) => x.invest), fill: true },
    ],
    ariaLabel: 'Net worth split into cash and investments over the last 12 months',
  }));

  const fcHost = root.querySelector('[data-chart="forecast"]');
  if (fcHost) {
    const fc = forecast(s, 12);
    const past = lastMonths(6);
    const pastNW = netWorthSeries(s, past);
    const labels2 = past.map((m) => monthLabel(m)).concat(fc.rows.map((r) => monthLabel(r.monthKey)));
    const full2 = past.map((m) => monthName(m)).concat(fc.rows.map((r) => monthName(r.monthKey)));
    const values = pastNW.map((x) => x.value).concat(fc.rows.map((r) => r.netWorth));
    mountChart(fcHost, (w) => lineChart(w, {
      labels: labels2, fullLabels: full2, height: 190,
      series: [{ label: 'Net worth', color: 'var(--s1)', values, fill: true, dashFrom: past.length - 1 }],
      ariaLabel: 'Net worth so far, with a dashed projection for the next 12 months',
    }));
  }

  const heroEl = root.querySelector('#hero-cash');
  if (heroEl) {
    heroEl.dataset.v = 0;
    countUp(heroEl, totalCash(s));
  }

  root.addEventListener('click', (e) => {
    const pay = e.target.closest('[data-pay]');
    if (pay) {
      const r = getState().recurring.find((x) => x.id === pay.dataset.pay);
      if (r) payRecurring(r);
      return;
    }
    if (e.target.closest('[data-divide]')) { divideIncome(); return; }
    const go = e.target.closest('[data-go]');
    if (go) ctx.navigate(go.dataset.go);
  });
}
