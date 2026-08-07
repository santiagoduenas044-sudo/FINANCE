/* ==========================================================================
   views/goals.js — targets with deadlines, and the arithmetic that tells you
   whether you will actually get there.
   ========================================================================== */

import {
  esc, icon, money, pct, dateLabel, durationLabel, monthLabel, monthName,
  round2, clamp, sum, todayISO,
} from '../util.js';
import { getState } from '../store.js';
import { goalStats, allBalances } from '../model.js';
import { ring, mountChart, lineChart, allocationBar } from '../charts.js';
import { editGoal } from '../flows.js';
import { openModal } from '../ui.js';

const cvar = (id) => `var(--${id || 's0'})`;

/** A goal wears its linked bucket's colour, or the next categorical slot. */
function goalColor(s, goal, i) {
  const b = goal.bucketId ? s.buckets.find((x) => x.id === goal.bucketId) : null;
  return cvar(b ? b.color : `s${(i % 8) + 1}`);
}

export function render() {
  const s = getState();
  const balances = allBalances(s);
  const goals = (s.goals || []).filter((g) => !g.archived).map((g) => goalStats(s, g, balances));

  if (!goals.length) {
    return `<div class="card c12" style="grid-column:1/-1">
      <div class="empty">
        <span class="insight-ico" style="width:44px;height:44px">${icon('goal')}</span>
        <h3>No goals yet</h3>
        <p>Name something you are saving for, put a number and a date on it, and the rest — how much a month,
           whether you are on track, when you will actually get there — is worked out for you.</p>
        <button type="button" class="btn btn-primary" data-newgoal style="margin-top:6px">${icon('plus')} Create a goal</button>
      </div>
    </div>`;
  }

  const totalTarget = round2(sum(goals, (g) => g.target));
  const totalSaved = round2(sum(goals, (g) => Math.min(g.current, g.target)));
  const onTrack = goals.filter((g) => g.onTrack).length;
  const monthlyNeed = round2(sum(goals, (g) => g.requiredMonthly));

  const summary = `
    <section class="hero" style="--sheen:var(--s3)">
      <div class="hero-sheen"></div>
      <div class="hero-inner">
        <div class="hero-main">
          <span class="lbl">Saved toward your goals</span>
          <div class="hero-value">${money(totalSaved)}</div>
          <p class="hero-note">
            of ${money(totalTarget)} across ${goals.length} goal${goals.length === 1 ? '' : 's'} —
            ${onTrack === goals.length ? 'all of them on track' : `${onTrack} on track, ${goals.length - onTrack} behind`}.
          </p>
        </div>
        <div class="hero-side">
          <div class="hero-mini"><span class="lbl">Still needed</span><div class="hero-mini-v">${money(totalTarget - totalSaved)}</div></div>
          <div class="hero-mini"><span class="lbl">Needed each month</span><div class="hero-mini-v">${money(monthlyNeed)}</div></div>
          <div class="hero-mini"><span class="lbl">Overall progress</span><div class="hero-mini-v">${pct(totalTarget > 0 ? totalSaved / totalTarget : 0)}</div></div>
        </div>
      </div>
      <div style="position:relative;margin-top:20px">
        ${allocationBar(goals.map((g, i) => ({
          label: g.goal.name,
          value: Math.min(g.current, g.target),
          color: goalColor(s, g.goal, i),
        })), { total: totalTarget || 1 })}
      </div>
    </section>`;

  const cards = goals.map((g, i) => {
    const color = goalColor(s, g.goal, i);
    const tone = g.done ? 'good' : g.overdue ? 'bad' : g.onTrack ? 'good' : 'warn';
    return `
      <div class="bucket" role="button" tabindex="0" data-goal="${esc(g.goal.id)}"
        aria-label="${esc(g.goal.name)}, ${pct(g.complete)} complete">
        <div class="bucket-top">
          ${ring(g.complete, g.done ? 'var(--good)' : color, 54, 5,
            `<span class="rv" style="font-size:12px">${Math.round(g.complete * 100)}%</span>`)}
          <div style="min-width:0;flex:1">
            <div class="bucket-name">${esc(g.goal.name)}</div>
            <div class="bucket-kind">
              <span class="chip ${tone}">
                ${icon(g.done ? 'check' : g.onTrack ? 'trend' : 'alert')}
                ${g.done ? 'funded' : g.overdue ? 'past deadline' : g.onTrack ? 'on track' : 'behind'}
              </span>
            </div>
          </div>
        </div>

        <div style="display:flex;align-items:baseline;gap:6px">
          <span style="font-size:20px;font-weight:600;letter-spacing:-.03em">${money(g.current)}</span>
          <span class="dim" style="font-size:13px">of ${money(g.target)}</span>
        </div>

        <div class="bucket-figs">
          <div class="bucket-fig"><div class="lbl">Still needed</div><div class="v">${money(g.needed, { cents: false })}</div></div>
          <div class="bucket-fig"><div class="lbl">Time left</div><div class="v">${
            g.goal.deadline ? (g.overdue ? 'overdue' : durationLabel(g.monthsLeft)) : 'none set'}</div></div>
          <div class="bucket-fig"><div class="lbl">Per month</div><div class="v">${money(g.requiredMonthly, { cents: false })}</div></div>
        </div>

        <div class="bucket-foot" style="border-top:1px solid var(--hairline)">
          <span class="lbl">Likely finish</span>
          <span class="num ${g.done ? 'pos' : ''}" style="margin-left:auto;font-size:12px">${
            g.done ? 'done' : (g.eta ? dateLabel(g.eta) : 'not at this pace')}</span>
        </div>
      </div>`;
  }).join('');

  return `
    ${summary}
    <div class="section-head"><h2>Your goals</h2><span class="lbl">${goals.length} active</span></div>
    <div class="bucket-grid stagger">${cards}</div>`;
}

/* --- detail --------------------------------------------------------------- */

function openGoal(id) {
  const s = getState();
  const goal = s.goals.find((g) => g.id === id);
  if (!goal) return;
  const g = goalStats(s, goal);
  const bucket = goal.bucketId ? s.buckets.find((b) => b.id === goal.bucketId) : null;
  const color = goalColor(s, goal, s.goals.indexOf(goal));

  // Two futures: what the deadline demands, and where the current pace lands.
  const horizon = clamp(Math.ceil(isFinite(g.monthsLeft) ? Math.max(g.monthsLeft, 1) : 12), 3, 36);
  const labels = ['Now'];
  const required = [g.current];
  const atPace = [g.current];
  for (let i = 1; i <= horizon; i++) {
    const d = new Date(); d.setMonth(d.getMonth() + i);
    labels.push(monthLabel(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`));
    required.push(round2(Math.min(g.target, g.current + g.requiredMonthly * i)));
    atPace.push(round2(Math.min(g.target * 1.1, g.current + g.pace * i)));
  }

  const body = `
    <div style="display:flex;align-items:center;gap:16px">
      ${ring(g.complete, g.done ? 'var(--good)' : color, 84, 7,
        `<span class="rv" style="font-size:17px">${Math.round(g.complete * 100)}%</span>`)}
      <div style="min-width:0">
        <div style="font-size:27px;font-weight:600;letter-spacing:-.035em;line-height:1">${money(g.current)}</div>
        <div style="font-size:13px;color:var(--ink-2);margin-top:5px">of ${money(g.target)} · ${money(g.needed)} to go</div>
        <div style="margin-top:8px">
          <span class="chip ${g.done ? 'good' : g.onTrack ? 'good' : 'warn'}">
            ${icon(g.done ? 'check' : g.onTrack ? 'trend' : 'alert')}
            ${g.done ? 'fully funded' : g.onTrack ? 'on track' : 'behind schedule'}
          </span>
        </div>
      </div>
    </div>

    ${goal.note ? `<p style="margin:0;font-size:13px;color:var(--ink-2)">${esc(goal.note)}</p>` : ''}

    <div class="ledger">
      <div class="ledger-row"><span class="k">Target amount</span><span class="leader"></span><span class="v">${money(g.target)}</span></div>
      <div class="ledger-row"><span class="k">Still needed</span><span class="leader"></span><span class="v">${money(g.needed)}</span></div>
      <div class="ledger-row"><span class="k">Deadline</span><span class="leader"></span><span class="v">${goal.deadline ? esc(dateLabel(goal.deadline)) : 'none set'}</span></div>
      <div class="ledger-row"><span class="k">Time remaining</span><span class="leader"></span><span class="v">${
        goal.deadline ? (g.overdue ? 'past deadline' : esc(durationLabel(g.monthsLeft))) : '—'}</span></div>
      <div class="ledger-row"><span class="k">Needed each month</span><span class="leader"></span><span class="v">${money(g.requiredMonthly)}</span></div>
      <div class="ledger-row"><span class="k">Your recent pace</span><span class="leader"></span><span class="v ${
        g.pace >= g.requiredMonthly ? 'pos' : 'neg'}">${money(g.pace)} a month</span></div>
      <div class="ledger-row"><span class="k">Estimated completion</span><span class="leader"></span><span class="v">${
        g.done ? 'done' : (g.eta ? esc(dateLabel(g.eta)) : 'not at this pace')}</span></div>
      <div class="ledger-row"><span class="k">Tracked by</span><span class="leader"></span><span class="v">${
        bucket ? esc(bucket.name) : 'manual amount'}</span></div>
    </div>

    <div>
      <span class="lbl" style="display:block;margin-bottom:8px">Two ways this could go</span>
      <div class="chart-host" data-chart="goal"></div>
      <div class="legend">
        <span class="legend-item"><span class="legend-key" style="--c:${color}"></span>At your current pace</span>
        <span class="legend-item"><span class="legend-key" style="--c:var(--ink-3)"></span>What the deadline needs</span>
      </div>
    </div>`;

  const m = openModal({
    title: goal.name,
    wide: true,
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn btn-primary" data-editg>${icon('edit')} Edit goal</button>`,
  });

  mountChart(m.el.querySelector('[data-chart="goal"]'), (w) => lineChart(w, {
    labels, height: 180, padR: 46, showLast: false,
    series: [
      { label: 'What the deadline needs', color: 'var(--ink-3)', values: required },
      { label: 'At your current pace', color, values: atPace, fill: true },
    ],
    ariaLabel: 'Projected goal progress',
  }));

  m.el.querySelector('[data-editg]').addEventListener('click', () => { m.close(); editGoal(goal); });
}

export function mount(root) {
  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-newgoal]')) { editGoal(null); return; }
    const c = e.target.closest('[data-goal]');
    if (c) openGoal(c.dataset.goal);
  });
  root.addEventListener('keydown', (e) => {
    const c = e.target.closest('[data-goal]');
    if (c && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openGoal(c.dataset.goal); }
  });
}

export const actions = () => `
  <button type="button" class="btn btn-primary" data-newgoal>${icon('plus')} New goal</button>`;

export function mountActions(bar) {
  bar.querySelector('[data-newgoal]')?.addEventListener('click', () => editGoal(null));
}
