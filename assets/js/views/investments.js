/* ==========================================================================
   views/investments.js — what you put in, what it is worth, and the gap
   between the two.
   ========================================================================== */

import {
  esc, icon, money, pct, monthLabel, monthName, dateLabel, round2, sum, thisMonth, addMonths,
} from '../util.js';
import { getState } from '../store.js';
import {
  investmentStats, investmentSeries, lastMonths, monthStats, totalContributions, recentAverage, forecast,
} from '../model.js';
import { lineChart, columnChart, mountChart, legend, sparkline, ring } from '../charts.js';
import { editHolding, updateValue, addContribution } from '../flows.js';

export function render() {
  const s = getState();
  const inv = investmentStats(s);
  const months = lastMonths(12);

  if (!s.holdings.length) {
    return `<div class="card c12">
      <div class="empty">
        <span class="insight-ico" style="width:44px;height:44px">${icon('invest')}</span>
        <h3>Nothing tracked yet</h3>
        <p>Add an investment — a fund, a retirement account, anything. Log what you put in, update its
           value now and then, and profit, loss and growth over time appear on their own.</p>
        <button type="button" class="btn btn-primary" data-newholding style="margin-top:6px">${icon('plus')} Track an investment</button>
      </div>
    </div>`;
  }

  const series = investmentSeries(s, months);
  const monthlyContrib = months.map((m) => {
    const t = s.transactions.filter((x) => x.type === 'contribution' && x.date.slice(0, 7) === m);
    return round2(sum(t, (x) => x.amount));
  });
  const avgContrib = round2(sum(monthlyContrib.filter((v) => v > 0)) / Math.max(1, monthlyContrib.filter((v) => v > 0).length));

  const hero = `
    <section class="hero" style="--sheen:var(--s7)">
      <div class="hero-sheen"></div>
      <div class="hero-inner">
        <div class="hero-main">
          <span class="lbl">Portfolio value</span>
          <div class="hero-value">${money(inv.value)}</div>
          <p class="hero-note">
            You have put in ${money(inv.cost)}. That is
            <strong style="font-weight:600;color:var(--${inv.pl >= 0 ? 'good-ink' : 'bad-ink'})">${
              inv.pl >= 0 ? 'up' : 'down'} ${money(Math.abs(inv.pl))}</strong>
            — a ${pct(Math.abs(inv.plPct), 1)} ${inv.pl >= 0 ? 'return' : 'loss'} so far.
          </p>
        </div>
        <div class="hero-side">
          <div class="hero-mini"><span class="lbl">Total put in</span><div class="hero-mini-v">${money(inv.cost)}</div></div>
          <div class="hero-mini"><span class="lbl">Profit / loss</span>
            <div class="hero-mini-v ${inv.pl >= 0 ? 'pos' : 'neg'}">${money(inv.pl, { sign: true })}</div></div>
          <div class="hero-mini"><span class="lbl">Waiting in buckets</span><div class="hero-mini-v">${money(inv.investCash)}</div></div>
        </div>
      </div>
    </section>`;

  const growth = `
    <div class="card c8">
      <div class="card-head"><span class="lbl">Growth · what you put in vs what it is worth</span></div>
      <div class="chart-host" data-chart="growth"></div>
      ${legend([
        { label: 'Portfolio value', color: 'var(--s7)' },
        { label: 'Money you put in', color: 'var(--s1)' },
      ], { square: true })}
      <p style="margin:10px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        The gap between the two lines is your profit or loss. Nothing is estimated — the value line only
        moves when you update it.
      </p>
    </div>`;

  const contribCard = `
    <div class="card c4">
      <div class="card-head"><span class="lbl">Monthly contributions</span></div>
      <div class="stat">
        <span class="stat-value">${money(avgContrib)}</span>
        <div class="stat-foot"><span>average over the months you invested</span></div>
      </div>
      <div class="chart-host" data-chart="contrib" style="margin-top:10px"></div>
    </div>`;

  const holdingsCard = `
    <div class="card c12">
      <div class="card-head">
        <span class="lbl">Your investments</span>
        <div class="card-head-actions">
          <button type="button" class="btn btn-sm" data-newholding>${icon('plus')} Add</button>
        </div>
      </div>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Investment</th><th class="r">Put in</th><th class="r">Worth now</th>
            <th class="r">Profit / loss</th><th class="r">Return</th><th class="r">Share</th><th></th>
          </tr></thead>
          <tbody>
            ${inv.holdings.map((hd, i) => `
              <tr>
                <td>
                  <span class="txn-desc">
                    <span class="dot" style="--c:var(--s${(i % 8) + 1})"></span>
                    <span>${esc(hd.name)}</span>
                  </span>
                  ${hd.note ? `<div class="dim" style="font-size:11.5px;margin-top:2px;padding-left:17px">${esc(hd.note)}</div>` : ''}
                </td>
                <td class="r">${money(hd.cost)}</td>
                <td class="r">${money(hd.value)}</td>
                <td class="r ${hd.pl >= 0 ? 'pos' : 'neg'}">${money(hd.pl, { sign: true })}</td>
                <td class="r ${hd.pl >= 0 ? 'pos' : 'neg'}">${hd.cost > 0 ? pct(hd.plPct, 1) : '—'}</td>
                <td class="r dim">${inv.value > 0 ? pct(hd.value / inv.value) : '—'}</td>
                <td class="r">
                  <span class="row-actions">
                    <button type="button" class="btn btn-sm" data-value="${esc(hd.id)}">Update value</button>
                    <button type="button" class="icon-btn sm bare" data-edith="${esc(hd.id)}" aria-label="Edit ${esc(hd.name)}">${icon('edit')}</button>
                  </span>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;

  // Projection using the same simple assumptions as the dashboard forecast
  const fc = forecast(s, 12);
  const projCard = `
    <div class="card c12">
      <div class="card-head">
        <span class="lbl">Where this could be</span>
        <div class="card-head-actions"><span class="chip">${icon('info')} ${pct(fc.assumptions.annualReturn, 1)} a year assumed</span></div>
      </div>
      <div class="grid">
        ${[3, 6, 12].map((n) => {
          const r = fc.at(n);
          return `<div class="c4" style="background:var(--surface-2);border:1px solid var(--hairline);border-radius:var(--r);padding:13px">
            <span class="lbl">In ${n} months</span>
            <div style="font-size:22px;font-weight:600;letter-spacing:-.03em;margin-top:6px">${money(r.portfolio)}</div>
            <div style="font-size:12px;color:var(--ink-2);margin-top:4px">
              ${money(round2(r.portfolio - inv.value), { sign: true })} from here
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>`;

  return `
    ${hero}
    <div class="section-head"><h2>Over time</h2></div>
    <div class="grid">
      ${growth}
      ${contribCard}
    </div>
    <div class="section-head"><h2>Holdings</h2></div>
    <div class="grid">${holdingsCard}</div>
    <div class="grid" style="margin-top:14px">${projCard}</div>`;
}

export function mount(root) {
  const s = getState();
  if (!s.holdings.length) {
    root.addEventListener('click', (e) => {
      if (e.target.closest('[data-newholding]')) editHolding(null);
    });
    return;
  }

  const months = lastMonths(12);
  const labels = months.map((m) => monthLabel(m));
  const full = months.map((m) => monthName(m));
  const series = investmentSeries(s, months);

  mountChart(root.querySelector('[data-chart="growth"]'), (w) => lineChart(w, {
    labels, fullLabels: full, height: 224,
    series: [
      { label: 'Portfolio value', color: 'var(--s7)', values: series.map((x) => x.value), fill: true },
      { label: 'Money you put in', color: 'var(--s1)', values: series.map((x) => x.contributed) },
    ],
    ariaLabel: 'Portfolio value against total contributions over 12 months',
  }));

  const monthlyContrib = months.map((m) => round2(sum(
    s.transactions.filter((x) => x.type === 'contribution' && x.date.slice(0, 7) === m), (x) => x.amount)));
  mountChart(root.querySelector('[data-chart="contrib"]'), (w) => columnChart(w, {
    labels, fullLabels: full, height: 150,
    columns: { label: 'Contributed', color: 'var(--s7)', values: monthlyContrib },
    ariaLabel: 'Monthly investment contributions',
  }));

  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-newholding]')) { editHolding(null); return; }
    const v = e.target.closest('[data-value]');
    if (v) {
      const hd = getState().holdings.find((x) => x.id === v.dataset.value);
      const cur = investmentStats(getState()).holdings.find((x) => x.id === v.dataset.value);
      if (hd) updateValue(hd, cur?.value || 0);
      return;
    }
    const ed = e.target.closest('[data-edith]');
    if (ed) {
      const hd = getState().holdings.find((x) => x.id === ed.dataset.edith);
      if (hd) editHolding(hd);
    }
  });
}

export const actions = () => `
  <button type="button" class="btn btn-primary" data-invest>${icon('plus')} Invest money</button>`;

export function mountActions(bar) {
  bar.querySelector('[data-invest]')?.addEventListener('click', () => addContribution());
}
