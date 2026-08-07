/* ==========================================================================
   model.js — every financial calculation, as pure functions.
   --------------------------------------------------------------------------
   How money moves (deliberately simple — four kinds of transaction):

     income        money arrives            → lands in a bucket (Unallocated by default)
     transfer      bucket → bucket          → this is what "dividing income" creates
     expense       bucket → gone            → money spent
     contribution  bucket → an investment   → money leaves cash, becomes portfolio value

   A bucket's balance is just: what came in, minus what went out.
   Net worth is: cash in buckets + what your investments are worth − debts.
   ========================================================================== */

import {
  round2, sum, clamp, monthKey, thisMonth, addMonths, monthRange, todayISO,
  fromISO, toISO, monthsUntil, daysInMonth, pad2,
} from './util.js';
import { UNALLOCATED_ID, activeBuckets, spendableBuckets, bucketById } from './store.js';

/* ==========================================================================
   Bucket balances
   ========================================================================== */

/** Balance of one bucket, optionally as of the end of a given date. */
export function bucketBalance(s, bucketId, endISO = null) {
  let v = 0;
  for (const t of s.transactions) {
    if (endISO && t.date > endISO) continue;
    const a = Number(t.amount) || 0;
    if (t.type === 'income' && t.bucketId === bucketId) v += a;
    else if (t.type === 'transfer') {
      if (t.toBucketId === bucketId) v += a;
      if (t.bucketId === bucketId) v -= a;
    } else if ((t.type === 'expense' || t.type === 'contribution') && t.bucketId === bucketId) v -= a;
  }
  return round2(v);
}

/** All bucket balances at once — one pass over transactions. */
export function allBalances(s, endISO = null) {
  const map = Object.create(null);
  for (const b of s.buckets) map[b.id] = 0;
  for (const t of s.transactions) {
    if (endISO && t.date > endISO) continue;
    const a = Number(t.amount) || 0;
    if (t.type === 'income') { if (t.bucketId in map) map[t.bucketId] += a; }
    else if (t.type === 'transfer') {
      if (t.toBucketId in map) map[t.toBucketId] += a;
      if (t.bucketId in map) map[t.bucketId] -= a;
    } else if (t.type === 'expense' || t.type === 'contribution') {
      if (t.bucketId in map) map[t.bucketId] -= a;
    }
  }
  for (const k in map) map[k] = round2(map[k]);
  return map;
}

export const unallocatedBalance = (s) => bucketBalance(s, UNALLOCATED_ID);

/** Total cash held across every bucket. */
export function totalCash(s, endISO = null) {
  const bal = allBalances(s, endISO);
  return round2(sum(Object.values(bal)));
}

/* ==========================================================================
   Months
   ========================================================================== */

/** Every month that has any activity, oldest first. Always includes this month. */
export function activeMonths(s) {
  const set = new Set(s.transactions.map((t) => monthKey(t.date)));
  set.add(thisMonth());
  const keys = [...set].filter(Boolean).sort();
  if (keys.length < 2) return keys;
  return monthRange(keys[0], keys[keys.length - 1]);
}

export function txnsInMonth(s, mk) {
  return s.transactions.filter((t) => monthKey(t.date) === mk);
}

/**
 * Everything worth knowing about one month.
 * Definitions are the plain-language ones shown in the UI.
 */
export function monthStats(s, mk) {
  const txns = txnsInMonth(s, mk);
  const buckets = s.buckets;
  const kind = Object.fromEntries(buckets.map((b) => [b.id, b.kind]));

  const flowIn = Object.create(null);
  const flowOut = Object.create(null);
  const spentBy = Object.create(null);
  const allocatedTo = Object.create(null);
  for (const b of buckets) { flowIn[b.id] = 0; flowOut[b.id] = 0; spentBy[b.id] = 0; allocatedTo[b.id] = 0; }

  const contributedBy = Object.create(null);
  for (const b of buckets) contributedBy[b.id] = 0;

  let income = 0, spent = 0, allocated = 0, contributed = 0, contributedFromNonInvest = 0;

  for (const t of txns) {
    const a = Number(t.amount) || 0;
    if (t.type === 'income') {
      income += a;
      if (t.bucketId in flowIn) flowIn[t.bucketId] += a;
      if (t.bucketId !== UNALLOCATED_ID && t.bucketId in allocatedTo) allocatedTo[t.bucketId] += a;
    } else if (t.type === 'transfer') {
      if (t.toBucketId in flowIn) flowIn[t.toBucketId] += a;
      if (t.bucketId in flowOut) flowOut[t.bucketId] += a;
      if (t.bucketId === UNALLOCATED_ID) allocated += a;
      if (t.toBucketId in allocatedTo) allocatedTo[t.toBucketId] += a;
    } else if (t.type === 'expense') {
      spent += a;
      if (t.bucketId in flowOut) flowOut[t.bucketId] += a;
      if (t.bucketId in spentBy) spentBy[t.bucketId] += a;
    } else if (t.type === 'contribution') {
      contributed += a;
      if (t.bucketId in contributedBy) contributedBy[t.bucketId] += a;
      // money leaving an investing bucket is still invested, so it is not an outflow there
      if (kind[t.bucketId] !== 'invest') {
        if (t.bucketId in flowOut) flowOut[t.bucketId] += a;
        contributedFromNonInvest += a;
      }
    }
  }

  const netOf = (k) => sum(buckets.filter((b) => b.kind === k), (b) => flowIn[b.id] - flowOut[b.id]);
  const saved = round2(netOf('save'));
  const invested = round2(netOf('invest') + contributedFromNonInvest);

  const essentialSpend = round2(sum(buckets.filter((b) => b.essential), (b) => spentBy[b.id]));
  const discretionarySpend = round2(sum(buckets.filter((b) => b.discretionary), (b) => spentBy[b.id]));
  const householdSpend = round2(sum(buckets.filter((b) => b.essential && b.kind === 'spend'), (b) => spentBy[b.id]));

  return {
    monthKey: mk,
    income: round2(income),
    spent: round2(spent),
    allocated: round2(allocated),
    contributed: round2(contributed),
    saved,
    invested,
    essentialSpend,
    discretionarySpend,
    householdSpend,
    leftover: round2(income - spent),
    savingsRate: income > 0 ? saved / income : 0,
    investmentRate: income > 0 ? invested / income : 0,
    spentBy,
    allocatedTo,
    contributedBy,
    flowIn,
    flowOut,
    txnCount: txns.length,
  };
}

/** monthStats for a list of months (oldest first). */
export function monthSeries(s, months) {
  return months.map((mk) => monthStats(s, mk));
}

/** Last `n` months ending with (and including) `endKey`. */
export function lastMonths(n, endKey = thisMonth()) {
  return Array.from({ length: n }, (_, i) => addMonths(endKey, -(n - 1 - i)));
}

/** Average of a field across the last n *complete* months (this month excluded). */
export function recentAverage(s, field, n = 3) {
  const months = lastMonths(n, addMonths(thisMonth(), -1));
  const vals = months.map((mk) => monthStats(s, mk)[field]).filter((v) => isFinite(v));
  const withActivity = vals.filter((v, i) => monthStats(s, months[i]).txnCount > 0);
  const use = withActivity.length ? withActivity : vals;
  return use.length ? round2(sum(use) / use.length) : 0;
}

/* ==========================================================================
   Buckets — targets, progress, history
   ========================================================================== */

/** The monthly target amount for a bucket, given an income figure. */
export function bucketTarget(bucket, income) {
  if (!bucket || bucket.kind === 'system') return 0;
  if (bucket.targetMode === 'fixed') return round2(bucket.targetValue || 0);
  return round2((income || 0) * ((bucket.targetValue || 0) / 100));
}

/** The full picture for one bucket in one month. */
export function bucketMonth(s, bucket, mk, stats = null, balances = null) {
  const st = stats || monthStats(s, mk);
  const bal = balances || allBalances(s);
  const income = st.income;
  const target = bucketTarget(bucket, income);
  const allocated = round2(st.allocatedTo[bucket.id] || 0);
  const spent = round2(st.spentBy[bucket.id] || 0);
  const contributed = round2((st.contributedBy || {})[bucket.id] || 0);
  const balance = round2(bal[bucket.id] || 0);
  // Money that left the bucket this month. For an investing bucket that is a
  // contribution, not spending — it is still your money, just working elsewhere.
  const outflow = round2(spent + contributed);
  const remaining = round2(allocated - outflow);
  return {
    bucket,
    target,
    allocated,
    spent,
    contributed,
    outflow,
    remaining,
    balance,
    pctOfIncome: income > 0 ? allocated / income : 0,
    fundedPct: target > 0 ? clamp(allocated / target, 0, 2) : (allocated > 0 ? 1 : 0),
    spentPct: allocated > 0 ? outflow / allocated : 0,
    overspent: spent > allocated + 0.004 && bucket.kind === 'spend',
    overspentBy: round2(Math.max(0, spent - allocated)),
    underfunded: target > 0 && allocated < target - 0.004,
  };
}

/** Closing balance of a bucket at the end of each month in `months`. */
export function bucketHistory(s, bucketId, months) {
  return months.map((mk) => {
    const end = `${mk}-${pad2(daysInMonth(mk))}`;
    return { monthKey: mk, value: bucketBalance(s, bucketId, end) };
  });
}

/** Spend per month for one bucket. */
export function bucketSpendHistory(s, bucketId, months) {
  return months.map((mk) => ({
    monthKey: mk,
    value: round2(sum(
      s.transactions.filter((t) => t.type === 'expense' && t.bucketId === bucketId && monthKey(t.date) === mk),
      (t) => t.amount,
    )),
  }));
}

/* ==========================================================================
   Investments
   ========================================================================== */

/** Latest valuation of a holding on or before a date. */
export function holdingValue(s, holdingId, atISO = null) {
  const vs = s.valuations
    .filter((v) => v.holdingId === holdingId && (!atISO || v.date <= atISO))
    .sort((a, b) => (a.date < b.date ? -1 : 1));
  if (vs.length) return round2(vs[vs.length - 1].value);
  return holdingContributions(s, holdingId, atISO);
}

export function holdingContributions(s, holdingId, atISO = null) {
  return round2(sum(
    s.transactions.filter((t) => t.type === 'contribution' && t.holdingId === holdingId && (!atISO || t.date <= atISO)),
    (t) => t.amount,
  ));
}

export function portfolioValue(s, atISO = null) {
  return round2(sum(s.holdings, (hd) => holdingValue(s, hd.id, atISO)));
}

export function totalContributions(s, atISO = null) {
  return round2(sum(
    s.transactions.filter((t) => t.type === 'contribution' && (!atISO || t.date <= atISO)),
    (t) => t.amount,
  ));
}

export function investmentStats(s) {
  const value = portfolioValue(s);
  const cost = totalContributions(s);
  const investCash = round2(sum(
    s.buckets.filter((b) => b.kind === 'invest'),
    (b) => bucketBalance(s, b.id),
  ));
  const pl = round2(value - cost);
  return {
    value, cost, investCash, pl,
    plPct: cost > 0 ? pl / cost : 0,
    totalInvested: round2(cost + investCash),
    holdings: s.holdings.map((hd) => {
      const v = holdingValue(s, hd.id);
      const c = holdingContributions(s, hd.id);
      return { ...hd, value: v, cost: c, pl: round2(v - c), plPct: c > 0 ? (v - c) / c : 0 };
    }).sort((a, b) => b.value - a.value),
  };
}

/** Cumulative contributions vs portfolio value, month by month. */
export function investmentSeries(s, months) {
  return months.map((mk) => {
    const end = `${mk}-${pad2(daysInMonth(mk))}`;
    return {
      monthKey: mk,
      contributed: totalContributions(s, end),
      value: portfolioValue(s, end),
    };
  });
}

/* ==========================================================================
   Net worth
   ========================================================================== */

export function totalDebt(s) {
  return round2(sum(s.debts || [], (d) => d.balance));
}

export function netWorth(s, atISO = null) {
  return round2(totalCash(s, atISO) + portfolioValue(s, atISO) - (atISO ? 0 : totalDebt(s)));
}

export function netWorthSeries(s, months) {
  const debt = totalDebt(s);
  return months.map((mk, i) => {
    const end = `${mk}-${pad2(daysInMonth(mk))}`;
    const cash = totalCash(s, end);
    const invest = portfolioValue(s, end);
    return {
      monthKey: mk,
      cash, invest,
      value: round2(cash + invest - (i === months.length - 1 ? debt : 0)),
    };
  });
}

/* ==========================================================================
   Recurring expenses
   ========================================================================== */

/** Occurrences of recurring expenses between two dates (inclusive). */
export function recurringBetween(s, fromISO_, toISO_) {
  const out = [];
  for (const r of s.recurring || []) {
    if (!r.active) continue;
    let d = r.nextDate || todayISO();
    let guard = 0;
    while (d <= toISO_ && guard++ < 60) {
      if (d >= fromISO_) out.push({ ...r, date: d });
      d = advanceRecurring(d, r.cadence);
    }
  }
  return out.sort((a, b) => (a.date < b.date ? -1 : 1));
}

export function advanceRecurring(iso, cadence) {
  const d = fromISO(iso);
  if (cadence === 'weekly') d.setDate(d.getDate() + 7);
  else if (cadence === 'biweekly') d.setDate(d.getDate() + 14);
  else if (cadence === 'yearly') d.setFullYear(d.getFullYear() + 1);
  else d.setMonth(d.getMonth() + 1);
  return toISO(d);
}

/** Recurring cost per month (normalised across cadences). */
export function recurringMonthlyTotal(s) {
  const per = { weekly: 4.33, biweekly: 2.17, monthly: 1, yearly: 1 / 12 };
  return round2(sum((s.recurring || []).filter((r) => r.active), (r) => (r.amount || 0) * (per[r.cadence] || 1)));
}

/** Bills still to come between today and the end of this month. */
export function upcomingThisMonth(s) {
  const mk = thisMonth();
  return recurringBetween(s, todayISO(), `${mk}-${pad2(daysInMonth(mk))}`);
}

/* ==========================================================================
   Safe to spend
   ========================================================================== */

/**
 * Safe to spend = money in your everyday buckets + money not yet divided,
 * minus any bill due before the end of the month that its bucket cannot cover.
 */
export function safeToSpend(s) {
  const bal = allBalances(s);
  const buckets = activeBuckets(s);
  const unallocated = round2(bal[UNALLOCATED_ID] || 0);
  const everyday = buckets.filter((b) => b.discretionary && b.kind !== 'system');
  const everydayTotal = round2(sum(everyday, (b) => Math.max(0, bal[b.id] || 0)));

  const upcoming = upcomingThisMonth(s);
  const dueByBucket = Object.create(null);
  for (const r of upcoming) dueByBucket[r.bucketId] = (dueByBucket[r.bucketId] || 0) + (r.amount || 0);

  let shortfall = 0;
  for (const [bid, due] of Object.entries(dueByBucket)) {
    const have = bal[bid] || 0;
    // everyday buckets are already counted above, so their bills come straight off the top
    const covered = everyday.some((b) => b.id === bid) ? 0 : have;
    shortfall += Math.max(0, due - covered);
  }
  shortfall = round2(shortfall + sum(everyday, (b) => Math.min(dueByBucket[b.id] || 0, Math.max(0, bal[b.id] || 0))));

  const amount = round2(unallocated + everydayTotal - shortfall);
  return {
    amount,
    unallocated,
    everydayTotal,
    everyday,
    upcomingTotal: round2(sum(upcoming, (r) => r.amount)),
    shortfall: round2(shortfall),
    daysLeft: Math.max(0, daysInMonth(thisMonth()) - new Date().getDate()),
    perDay: null,
  };
}

/* ==========================================================================
   Goals
   ========================================================================== */

/** Current amount saved toward a goal: a linked bucket's balance, or a manual figure. */
export function goalCurrent(s, goal, balances = null) {
  if (goal.bucketId) {
    const bal = balances || allBalances(s);
    return round2(Math.max(0, bal[goal.bucketId] || 0) + (goal.manualAmount || 0));
  }
  return round2(goal.manualAmount || 0);
}

export function goalStats(s, goal, balances = null) {
  const current = goalCurrent(s, goal, balances);
  const target = round2(goal.targetAmount || 0);
  const needed = round2(Math.max(0, target - current));
  const monthsLeft = goal.deadline ? monthsUntil(goal.deadline) : Infinity;
  const monthsLeftWhole = Math.max(0, Math.ceil(monthsLeft));
  const requiredMonthly = needed <= 0 ? 0
    : (monthsLeft > 0 ? round2(needed / Math.max(monthsLeft, 0.5)) : needed);

  // Pace: how fast has this goal actually been growing?
  let pace = 0;
  if (goal.bucketId) {
    const months = lastMonths(4, addMonths(thisMonth(), -1));
    const flows = months.map((mk) => {
      const st = monthStats(s, mk);
      return (st.flowIn[goal.bucketId] || 0) - (st.flowOut[goal.bucketId] || 0);
    }).filter((v) => v !== 0);
    pace = flows.length ? round2(sum(flows) / flows.length) : 0;
  } else {
    const age = Math.max(1, -monthsUntil(goal.createdAt || todayISO()));
    pace = round2(current / age);
  }

  const complete = target > 0 ? clamp(current / target, 0, 1) : 0;
  const done = needed <= 0.004;
  const monthsAtPace = pace > 0 ? needed / pace : Infinity;
  const eta = done ? todayISO() : (isFinite(monthsAtPace) ? etaDate(monthsAtPace) : null);
  const onTrack = done || (pace > 0 && pace >= requiredMonthly - 0.5) || !goal.deadline;

  return {
    goal, current, target, needed, complete, done,
    monthsLeft: isFinite(monthsLeft) ? monthsLeft : Infinity,
    monthsLeftWhole,
    requiredMonthly, pace, monthsAtPace, eta, onTrack,
    overdue: goal.deadline ? monthsLeft < 0 && !done : false,
  };
}

function etaDate(months) {
  const d = new Date();
  d.setMonth(d.getMonth() + Math.ceil(months));
  return toISO(d);
}

/* ==========================================================================
   Emergency fund
   ========================================================================== */

export function emergencyFund(s) {
  const bucket = activeBuckets(s).find((b) => b.emergency);
  const monthsTarget = s.settings.emergencyMonths || 6;
  const months = lastMonths(3, addMonths(thisMonth(), -1));
  const stats = months.map((mk) => monthStats(s, mk)).filter((st) => st.txnCount > 0);
  const avgEssential = stats.length
    ? round2(sum(stats, (st) => st.essentialSpend) / stats.length) : 0;
  const avgSpend = stats.length ? round2(sum(stats, (st) => st.spent) / stats.length) : 0;
  const monthlyNeed = avgEssential > 0 ? avgEssential : avgSpend;
  const target = round2(monthlyNeed * monthsTarget);
  const current = bucket ? bucketBalance(s, bucket.id) : 0;
  return {
    bucket, current, target, monthsTarget, monthlyNeed,
    monthsCovered: monthlyNeed > 0 ? current / monthlyNeed : 0,
    progress: target > 0 ? clamp(current / target, 0, 1) : 0,
  };
}

/* ==========================================================================
   Savings streak
   ========================================================================== */

/** Consecutive months (most recent first) where more money was saved than taken out. */
export function savingsStreak(s) {
  let streak = 0;
  let k = thisMonth();
  const cur = monthStats(s, k);
  if (cur.saved <= 0) k = addMonths(k, -1);
  for (let i = 0; i < 120; i++) {
    const st = monthStats(s, k);
    if (st.txnCount === 0) break;
    if (st.saved > 0) { streak++; k = addMonths(k, -1); } else break;
  }
  return streak;
}

/* ==========================================================================
   Financial health score
   ========================================================================== */

export function healthScore(s) {
  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const prevKey = addMonths(mk, -1);
  const prev = monthStats(s, prevKey);
  // use last complete month if this one has barely started
  const base = cur.income > 0 ? cur : prev;

  const savT = s.settings.savingsRateTarget || 0.2;
  const invT = s.settings.investmentRateTarget || 0.1;
  const ef = emergencyFund(s);

  const balances = allBalances(s);
  const goals = (s.goals || []).filter((g) => !g.archived).map((g) => goalStats(s, g, balances));
  const spendB = spendableBuckets(s).filter((b) => b.kind === 'spend');
  const bm = spendB.map((b) => bucketMonth(s, b, mk, cur, balances));
  const overspending = bm.filter((x) => x.overspent).length;

  const parts = [
    {
      key: 'savings', label: 'Savings rate', weight: 25,
      score: clamp(savT > 0 ? base.savingsRate / savT : 0, 0, 1),
      detail: `${(base.savingsRate * 100).toFixed(0)}% saved · target ${(savT * 100).toFixed(0)}%`,
    },
    {
      key: 'investing', label: 'Investment rate', weight: 20,
      score: clamp(invT > 0 ? base.investmentRate / invT : 0, 0, 1),
      detail: `${(base.investmentRate * 100).toFixed(0)}% invested · target ${(invT * 100).toFixed(0)}%`,
    },
    {
      key: 'emergency', label: 'Emergency fund', weight: 20,
      score: ef.progress,
      detail: ef.monthlyNeed > 0
        ? `${ef.monthsCovered.toFixed(1)} of ${ef.monthsTarget} months covered`
        : 'Add some spending history to measure this',
    },
    {
      key: 'budget', label: 'Staying inside buckets', weight: 15,
      score: spendB.length ? clamp(1 - overspending / spendB.length, 0, 1) : 1,
      detail: overspending === 0 ? 'No bucket overspent' : `${overspending} bucket${overspending === 1 ? '' : 's'} over`,
    },
    {
      key: 'allocated', label: 'Money given a job', weight: 10,
      score: base.income > 0 ? clamp(base.allocated / base.income, 0, 1) : 0,
      detail: base.income > 0
        ? `${((base.allocated / base.income) * 100).toFixed(0)}% of income divided up`
        : 'No income recorded yet',
    },
    {
      key: 'goals', label: 'Goals on track', weight: 10,
      score: goals.length ? goals.filter((g) => g.onTrack).length / goals.length : 1,
      detail: goals.length ? `${goals.filter((g) => g.onTrack).length} of ${goals.length} on track` : 'No goals set',
    },
  ];

  const total = Math.round(sum(parts, (p) => p.score * p.weight));
  const grade = total >= 85 ? 'Excellent' : total >= 70 ? 'Strong' : total >= 55 ? 'Steady' : total >= 35 ? 'Needs work' : 'Fragile';
  const tone = total >= 70 ? 'good' : total >= 45 ? 'warn' : 'bad';
  return { total, grade, tone, parts };
}

/* ==========================================================================
   Suggested allocation
   ========================================================================== */

/**
 * How to divide a given amount. Uses each bucket's own target; if no targets
 * are set at all, falls back to an even split so there is always a starting point.
 */
export function suggestSplit(s, amount, monthIncomeSoFar = null) {
  const buckets = spendableBuckets(s);
  const amt = round2(amount || 0);
  const out = {};
  if (amt <= 0 || !buckets.length) { buckets.forEach((b) => { out[b.id] = 0; }); return out; }

  const incomeBasis = monthIncomeSoFar == null ? amt : monthIncomeSoFar;
  const wants = buckets.map((b) => {
    if (b.targetMode === 'fixed') return { b, want: round2(b.targetValue || 0) };
    return { b, want: round2(incomeBasis * ((b.targetValue || 0) / 100)) };
  });

  const totalWant = sum(wants, (w) => w.want);
  if (totalWant <= 0) {
    const each = round2(amt / buckets.length);
    let left = amt;
    buckets.forEach((b, i) => {
      const v = i === buckets.length - 1 ? round2(left) : each;
      out[b.id] = v; left = round2(left - v);
    });
    return out;
  }

  // Scale to fit the money actually available, largest-remainder rounded to cents.
  const scale = Math.min(1, amt / totalWant);
  let left = amt;
  wants.forEach(({ b, want }, i) => {
    const v = i === wants.length - 1 ? round2(Math.max(0, left)) : round2(Math.min(left, want * scale));
    out[b.id] = v;
    left = round2(left - v);
  });
  return out;
}

/** A gentle nudge when targets look unbalanced. */
export function allocationAdvice(s) {
  const buckets = spendableBuckets(s);
  const pctBuckets = buckets.filter((b) => b.targetMode === 'percent');
  const totalPct = round2(sum(pctBuckets, (b) => b.targetValue || 0));
  const savePct = round2(sum(pctBuckets.filter((b) => b.kind === 'save'), (b) => b.targetValue || 0));
  const invPct = round2(sum(pctBuckets.filter((b) => b.kind === 'invest'), (b) => b.targetValue || 0));
  const fixedTotal = round2(sum(buckets.filter((b) => b.targetMode === 'fixed'), (b) => b.targetValue || 0));
  return { totalPct, savePct, invPct, fixedTotal, over: totalPct > 100.01, under: totalPct < 99.99 };
}

/* ==========================================================================
   Forecast
   ========================================================================== */

/**
 * Straight-line projection from your recent averages, with investments
 * compounding at the assumed annual return. Assumptions are all editable.
 */
export function forecast(s, monthsAhead = 12, overrides = {}) {
  // Before there is a full month of history, this month is the best estimate there is.
  const now = monthStats(s, thisMonth());
  const avg = (field) => recentAverage(s, field, 3) || now[field];
  const avgIncome = overrides.income ?? avg('income');
  const avgSaved = overrides.saved ?? avg('saved');
  const avgInvested = overrides.invested ?? avg('invested');
  const avgSpent = overrides.spent ?? avg('spent');
  const annual = overrides.annualReturn ?? (s.settings.annualReturn || 0.07);
  const r = Math.pow(1 + annual, 1 / 12) - 1;

  let cash = totalCash(s);
  let portfolio = portfolioValue(s);
  const debt = totalDebt(s);
  const rows = [];
  let mk = thisMonth();
  for (let i = 1; i <= monthsAhead; i++) {
    mk = addMonths(mk, 1);
    cash = round2(cash + avgSaved);
    portfolio = round2(portfolio * (1 + r) + avgInvested);
    rows.push({
      monthKey: mk, month: i,
      cash, portfolio,
      netWorth: round2(cash + portfolio - debt),
      income: avgIncome, saved: avgSaved, invested: avgInvested, spent: avgSpent,
    });
  }
  return {
    rows,
    assumptions: { income: avgIncome, saved: avgSaved, invested: avgInvested, spent: avgSpent, annualReturn: annual },
    at: (n) => rows[Math.min(rows.length, n) - 1],
  };
}

/* ==========================================================================
   Trends & insights
   ========================================================================== */

/** This month's spend per bucket vs its recent average. */
export function spendingTrends(s, mk = thisMonth()) {
  const months = lastMonths(6, addMonths(mk, -1));
  const cur = monthStats(s, mk);
  return spendableBuckets(s)
    .filter((b) => b.kind === 'spend')
    .map((b) => {
      const hist = bucketSpendHistory(s, b.id, months);
      const active = hist.filter((h) => h.value > 0);
      const avg = active.length ? round2(sum(active, (h) => h.value) / active.length) : 0;
      const now = round2(cur.spentBy[b.id] || 0);
      return {
        bucket: b, now, avg,
        change: avg > 0 ? (now - avg) / avg : (now > 0 ? 1 : 0),
        series: hist.map((h) => h.value),
      };
    })
    .sort((a, b) => b.now - a.now);
}

/** Where the money actually went this month, biggest first. */
export function spendingBreakdown(s, mk = thisMonth()) {
  const st = monthStats(s, mk);
  const rows = spendableBuckets(s)
    .map((b) => ({ bucket: b, value: round2(st.spentBy[b.id] || 0) }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value);
  const total = round2(sum(rows, (r) => r.value));
  return { rows: rows.map((r) => ({ ...r, share: total > 0 ? r.value / total : 0 })), total };
}

/** Month-over-month comparison. */
export function compareMonths(s, aKey, bKey) {
  const a = monthStats(s, aKey);
  const b = monthStats(s, bKey);
  const d = (f) => ({
    now: a[f], prev: b[f],
    diff: round2(a[f] - b[f]),
    pct: b[f] !== 0 ? (a[f] - b[f]) / Math.abs(b[f]) : (a[f] ? 1 : 0),
  });
  return {
    a, b,
    income: d('income'), spent: d('spent'), saved: d('saved'),
    invested: d('invested'), householdSpend: d('householdSpend'),
    savingsRate: { now: a.savingsRate, prev: b.savingsRate, diff: a.savingsRate - b.savingsRate },
  };
}

/**
 * Plain-language observations. Ordered by how much they matter.
 * Each returns { tone, icon, title, text, action? }
 */
export function insights(s) {
  const out = [];
  const mk = thisMonth();
  const cur = monthStats(s, mk);
  const balances = allBalances(s);
  const stsp = safeToSpend(s);

  // Overspending
  const overs = spendableBuckets(s)
    .filter((b) => b.kind === 'spend')
    .map((b) => bucketMonth(s, b, mk, cur, balances))
    .filter((x) => x.overspent)
    .sort((a, b) => b.overspentBy - a.overspentBy);
  overs.slice(0, 2).forEach((x) => {
    out.push({
      tone: 'bad', icon: 'alert', priority: 1,
      title: `${x.bucket.name} is over its plan`,
      text: `You have spent ${fmtM(x.spent)} against ${fmtM(x.allocated)} allocated — ${fmtM(x.overspentBy)} over. Move money in from another bucket, or ease off for the rest of the month.`,
      view: 'buckets',
    });
  });

  // Money sitting undivided
  const unalloc = balances[UNALLOCATED_ID] || 0;
  if (unalloc > 0.5) {
    out.push({
      tone: cur.income > 0 && unalloc / cur.income > 0.25 ? 'warn' : 'info',
      icon: 'wallet', priority: 2,
      title: `${fmtM(unalloc)} has not been given a job`,
      text: 'Dividing it now is what keeps the rest of these numbers honest.',
      action: 'divide',
    });
  }

  // Streak
  const streak = savingsStreak(s);
  if (streak >= 2) {
    out.push({
      tone: 'good', icon: 'flame', priority: 3,
      title: `${streak}-month saving streak`,
      text: `You have put money away every month for ${streak} months running. Keep it going.`,
    });
  }

  // Emergency fund
  const ef = emergencyFund(s);
  if (ef.bucket && ef.target > 0) {
    if (ef.progress >= 1) {
      out.push({
        tone: 'good', icon: 'shield', priority: 4,
        title: 'Emergency fund is fully stocked',
        text: `${fmtM(ef.current)} covers about ${ef.monthsCovered.toFixed(1)} months of essentials. Extra money here could work harder invested.`,
      });
    } else if (ef.monthsCovered < 3) {
      out.push({
        tone: 'warn', icon: 'shield', priority: 2,
        title: `Emergency fund covers ${ef.monthsCovered.toFixed(1)} months`,
        text: `Getting to ${ef.monthsTarget} months needs ${fmtM(ef.target - ef.current)} more. That is the cushion that makes everything else safe.`,
        view: 'buckets',
      });
    }
  }

  // Biggest spending category
  const bd = spendingBreakdown(s, mk);
  if (bd.rows.length) {
    const top = bd.rows[0];
    out.push({
      tone: 'info', icon: 'trend', priority: 6,
      title: `${top.bucket.name} took ${(top.share * 100).toFixed(0)}% of your spending`,
      text: `${fmtM(top.value)} of ${fmtM(bd.total)} spent this month.`,
      view: 'history',
    });
  }

  // Trend spikes
  spendingTrends(s, mk)
    .filter((t) => t.avg > 20 && t.change > 0.35 && t.now > t.avg)
    .slice(0, 1)
    .forEach((t) => {
      out.push({
        tone: 'warn', icon: 'trend', priority: 5,
        title: `${t.bucket.name} is running ${(t.change * 100).toFixed(0)}% above normal`,
        text: `${fmtM(t.now)} this month against a ${fmtM(t.avg)} average.`,
        view: 'history',
      });
    });

  // Goals
  const goals = (s.goals || []).filter((g) => !g.archived).map((g) => goalStats(s, g, balances));
  goals.filter((g) => !g.onTrack && !g.done).slice(0, 1).forEach((g) => {
    out.push({
      tone: 'warn', icon: 'goal', priority: 3,
      title: `${g.goal.name} is behind schedule`,
      text: g.pace > 0
        ? `You are adding about ${fmtM(g.pace)} a month; hitting the deadline needs ${fmtM(g.requiredMonthly)}.`
        : `Nothing has gone in recently. It needs ${fmtM(g.requiredMonthly)} a month to land on time.`,
      view: 'goals',
    });
  });
  goals.filter((g) => g.done).slice(0, 1).forEach((g) => {
    out.push({
      tone: 'good', icon: 'check', priority: 4,
      title: `${g.goal.name} is fully funded`,
      text: `${fmtM(g.current)} of ${fmtM(g.target)}. Time to spend it or set a new target.`,
      view: 'goals',
    });
  });

  // Safe to spend running low
  if (stsp.amount < 0) {
    out.push({
      tone: 'bad', icon: 'alert', priority: 1,
      title: 'Your everyday money is in the red',
      text: `Bills due before month end come to ${fmtM(stsp.upcomingTotal)} and the buckets do not cover them. Move money in from savings or hold off on spending.`,
    });
  } else if (stsp.daysLeft > 3 && stsp.amount > 0 && stsp.amount / Math.max(1, stsp.daysLeft) < 8) {
    out.push({
      tone: 'warn', icon: 'alert', priority: 2,
      title: 'Everyday money is running thin',
      text: `${fmtM(stsp.amount)} left across ${stsp.daysLeft} days — about ${fmtM(stsp.amount / stsp.daysLeft)} a day.`,
    });
  }

  // Getting started — only ever shown while there is little else to say
  if (!goals.length) {
    out.push({
      tone: 'info', icon: 'goal', priority: 20,
      title: 'Give your saving a destination',
      text: 'A goal with an amount and a date turns "saving something" into "on track for this".',
      view: 'goals',
    });
  }
  if (!s.holdings.length) {
    out.push({
      tone: 'info', icon: 'invest', priority: 21,
      title: 'Track what you invest',
      text: 'Add an account, log contributions, update the value now and then — profit and growth follow on their own.',
      view: 'invest',
    });
  }
  if (!(s.recurring || []).some((r) => r.active)) {
    out.push({
      tone: 'info', icon: 'repeat', priority: 22,
      title: 'Add the bills that come round every month',
      text: 'Once they are listed, your safe-to-spend figure knows what is still coming.',
      view: 'settings',
    });
  }
  if (activeMonths(s).length < 3) {
    out.push({
      tone: 'info', icon: 'history', priority: 23,
      title: 'Trends need a little history',
      text: 'Keep recording for a few more weeks and averages, streaks and forecasts get much sharper.',
    });
  }

  // Investment growth
  const inv = investmentStats(s);
  if (inv.cost > 0) {
    out.push({
      tone: inv.pl >= 0 ? 'good' : 'info', icon: 'invest', priority: 7,
      title: inv.pl >= 0
        ? `Investments are up ${fmtM(inv.pl)}`
        : `Investments are down ${fmtM(Math.abs(inv.pl))}`,
      text: `${fmtM(inv.cost)} put in is worth ${fmtM(inv.value)} — a ${(inv.plPct * 100).toFixed(1)}% return so far.`,
      view: 'invest',
    });
  }

  return out.sort((a, b) => a.priority - b.priority);
}

/* Local money formatter for insight copy (avoids a circular import). */
let _fmt = (n) => `$${Math.round(n).toLocaleString('en-US')}`;
export function setInsightFormatter(fn) { _fmt = fn; }
const fmtM = (n) => _fmt(n);

/* ==========================================================================
   End-of-month summary
   ========================================================================== */

export function monthSummary(s, mk) {
  const st = monthStats(s, mk);
  const prev = monthStats(s, addMonths(mk, -1));
  const bd = spendingBreakdown(s, mk);
  const buckets = spendableBuckets(s);
  const balances = allBalances(s);
  const rows = buckets.map((b) => bucketMonth(s, b, mk, st, balances));
  return {
    stats: st, prev, breakdown: bd,
    overspent: rows.filter((r) => r.overspent),
    bestBucket: rows.filter((r) => r.bucket.kind === 'spend' && r.allocated > 0)
      .sort((a, b) => a.spentPct - b.spentPct)[0] || null,
    change: {
      income: st.income - prev.income,
      spent: st.spent - prev.spent,
      saved: st.saved - prev.saved,
      invested: st.invested - prev.invested,
    },
  };
}
