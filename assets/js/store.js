/* ==========================================================================
   store.js — state, persistence, defaults, demo data
   --------------------------------------------------------------------------
   Nothing about the user's money is hard-coded. Buckets ship with *starting
   suggestions* for target percentages that are editable everywhere, and income
   is only ever derived from income transactions the user enters.
   ========================================================================== */

import { uid, round2, todayISO, monthKey, addMonths, thisMonth, pad2, daysInMonth } from './util.js';

const KEY = 'ledger.finance.v1';
export const SCHEMA_VERSION = 1;

/* --- colour slots --------------------------------------------------------- */
export const SLOTS = [
  { id: 's1', name: 'Blue' },
  { id: 's2', name: 'Orange' },
  { id: 's3', name: 'Aqua' },
  { id: 's4', name: 'Yellow' },
  { id: 's5', name: 'Magenta' },
  { id: 's6', name: 'Green' },
  { id: 's7', name: 'Violet' },
  { id: 's8', name: 'Red' },
  { id: 's0', name: 'Grey' },
];

export const BUCKET_KINDS = [
  { id: 'save', name: 'Saving', hint: 'Counts toward your savings rate' },
  { id: 'invest', name: 'Investing', hint: 'Counts toward your investment rate' },
  { id: 'spend', name: 'Spending', hint: 'Money you expect to spend this month' },
  { id: 'system', name: 'System', hint: 'Unallocated money' },
];

export const UNALLOCATED_ID = 'b_unallocated';

/* --- starter template ----------------------------------------------------- */
/* These percentages are suggestions to get started — every one is editable. */
function starterBuckets() {
  const b = (name, kind, color, icon, targetValue, opts = {}) => ({
    id: uid('b'), name, kind, color, icon,
    targetMode: 'percent', targetValue,
    essential: !!opts.essential,
    discretionary: !!opts.discretionary,
    emergency: !!opts.emergency,
    rollover: opts.rollover !== false,
    note: opts.note || '',
    archived: false,
  });
  const list = [
    b('Travel Fund', 'save', 's1', 'plane', 10),
    b('Emergency / General Savings', 'save', 's3', 'shield', 15, { emergency: true }),
    b('Investments', 'invest', 's7', 'invest', 15),
    b('Help Mom / Household', 'spend', 's5', 'heart', 20, { essential: true, rollover: false }),
    b('Groceries', 'spend', 's6', 'cart', 15, { essential: true, rollover: false }),
    b('Family Cinema / Entertainment', 'spend', 's2', 'film', 5, { discretionary: true, rollover: false }),
    b('Personal Spending', 'spend', 's4', 'user', 20, { discretionary: true, rollover: false }),
  ];
  list.push({
    id: UNALLOCATED_ID, name: 'Unallocated Money', kind: 'system', color: 's0', icon: 'wallet',
    targetMode: 'percent', targetValue: 0, essential: false, discretionary: false,
    emergency: false, rollover: true, note: 'Money that has arrived but has not been given a job yet.',
    archived: false, system: true,
  });
  return list.map((x, i) => ({ ...x, order: i }));
}

function starterAccounts() {
  return [
    { id: uid('a'), name: 'Checking', kind: 'checking', archived: false },
    { id: uid('a'), name: 'Savings', kind: 'savings', archived: false },
    { id: uid('a'), name: 'Brokerage', kind: 'investment', archived: false },
    { id: uid('a'), name: 'Cash', kind: 'cash', archived: false },
  ];
}

export function blankState() {
  return {
    version: SCHEMA_VERSION,
    createdAt: todayISO(),
    settings: {
      name: '',
      currency: 'USD',
      locale: 'en-US',
      theme: 'system',
      savingsRateTarget: 0.20,
      investmentRateTarget: 0.10,
      emergencyMonths: 6,
      annualReturn: 0.07,
      onboarded: false,
    },
    buckets: starterBuckets(),
    accounts: starterAccounts(),
    transactions: [],
    goals: [],
    holdings: [],
    valuations: [],
    recurring: [],
    debts: [],
  };
}

/* --- persistence ---------------------------------------------------------- */

let state = blankState();
const listeners = new Set();
let saveTimer = null;
let undoStack = [];

export function getState() { return state; }

export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

function notify() { listeners.forEach((fn) => fn(state)); }

function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { localStorage.setItem(KEY, JSON.stringify(state)); }
    catch (e) { console.warn('Could not save data', e); }
  }, 120);
}

/**
 * Apply a mutation to state, then save and re-render.
 * `label` makes the change undoable from the toast.
 */
export function commit(fn, label) {
  const before = label ? JSON.stringify(state) : null;
  fn(state);
  if (before) {
    undoStack.push({ label, snapshot: before });
    if (undoStack.length > 20) undoStack.shift();
  }
  persist();
  notify();
}

export function canUndo() { return undoStack.length > 0; }

export function undo() {
  const last = undoStack.pop();
  if (!last) return null;
  state = JSON.parse(last.snapshot);
  persist();
  notify();
  return last.label;
}

export function load() {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      state = migrate(JSON.parse(raw));
      return true;
    }
  } catch (e) {
    console.warn('Stored data could not be read; starting fresh.', e);
  }
  state = blankState();
  return false;
}

export function replaceState(next) {
  state = migrate(next);
  persist();
  notify();
}

export function resetAll() {
  undoStack = [];
  state = blankState();
  persist();
  notify();
}

function migrate(data) {
  const base = blankState();
  const s = { ...base, ...data };
  s.settings = { ...base.settings, ...(data.settings || {}) };
  // guarantee the system bucket exists and stays last
  if (!Array.isArray(s.buckets) || !s.buckets.length) s.buckets = base.buckets;
  if (!s.buckets.some((b) => b.id === UNALLOCATED_ID)) {
    s.buckets.push(base.buckets[base.buckets.length - 1]);
  }
  s.buckets = s.buckets.map((b, i) => ({ order: i, rollover: true, ...b }));
  ['transactions', 'goals', 'holdings', 'valuations', 'recurring', 'accounts', 'debts']
    .forEach((k) => { if (!Array.isArray(s[k])) s[k] = base[k] || []; });
  s.version = SCHEMA_VERSION;
  return s;
}

export function exportJSON() {
  return JSON.stringify({ ...state, exportedAt: new Date().toISOString() }, null, 2);
}

export function importJSON(text) {
  const data = JSON.parse(text);
  if (!data || typeof data !== 'object' || !Array.isArray(data.buckets)) {
    throw new Error('That file does not look like a Ledger backup.');
  }
  replaceState(data);
}

/* --- convenience accessors ------------------------------------------------ */

export const activeBuckets = (s = state) =>
  s.buckets.filter((b) => !b.archived).sort((a, b) => a.order - b.order);

export const spendableBuckets = (s = state) =>
  activeBuckets(s).filter((b) => b.kind !== 'system');

export const bucketById = (id, s = state) => s.buckets.find((b) => b.id === id);

export const accountById = (id, s = state) => s.accounts.find((a) => a.id === id);

/* --- demo data ------------------------------------------------------------ */
/* A seeded generator so the sample month history is stable between loads. */

function rng(seed) {
  let t = seed >>> 0;
  return () => {
    t += 0x6D2B79F5;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

const DEMO_SPEND = {
  'Groceries': ['Whole Foods', 'Corner market', 'Trader Joe\'s', 'Farmers market', 'Costco run'],
  'Help Mom / Household': ['Mom — rent help', 'Electric bill', 'Internet', 'Home repairs', 'Water bill'],
  'Family Cinema / Entertainment': ['Cinema — family night', 'Streaming', 'Concert tickets', 'Bowling'],
  'Personal Spending': ['Coffee', 'Running shoes', 'Books', 'Dinner out', 'Barber', 'App subscription'],
  'Travel Fund': ['Flight deposit', 'Hostel booking'],
};

export function makeDemoState() {
  const s = blankState();
  s.settings.name = '';
  s.settings.onboarded = true;
  const rand = rng(20260807);
  const buckets = s.buckets;
  const byName = (n) => buckets.find((b) => b.name === n);
  const checking = s.accounts[0].id;
  const savings = s.accounts[1].id;
  const brokerage = s.accounts[2].id;

  const months = [];
  let k = addMonths(thisMonth(), -13);
  for (let i = 0; i < 14; i++) { months.push(k); k = addMonths(k, 1); }

  const T = [];
  const push = (o) => T.push({ id: uid('t'), createdAt: new Date().toISOString(), ...o });

  const holding = { id: uid('h'), name: 'Index Fund (VT)', kind: 'fund', note: 'Monthly auto-invest' };
  const holding2 = { id: uid('h'), name: 'Retirement (Roth)', kind: 'retirement', note: '' };
  s.holdings = [holding, holding2];

  let portfolio1 = 0, portfolio2 = 0;
  const today = todayISO();

  months.forEach((mk, mi) => {
    const isCurrent = mi === months.length - 1;
    const dim = daysInMonth(mk);
    const dayCap = isCurrent ? Number(today.slice(8, 10)) : dim;

    /* --- income: a main paycheck, plus occasional side income --- */
    const base = 4200 + Math.round(rand() * 900);
    const bump = mi > 8 ? 350 : 0;                 // a raise part-way through
    const incomes = [{ day: 1, amt: round2(base * 0.55 + bump), desc: 'Paycheck' }];
    if (dayCap >= 15) incomes.push({ day: 15, amt: round2(base * 0.45), desc: 'Paycheck' });
    if (rand() > 0.62 && dayCap >= 20) {
      incomes.push({ day: 20, amt: round2(180 + rand() * 520), desc: 'Freelance project' });
    }

    let monthIncome = 0;
    incomes.forEach((inc) => {
      if (inc.day > dayCap) return;
      monthIncome += inc.amt;
      push({
        date: `${mk}-${pad2(inc.day)}`, description: inc.desc, amount: inc.amt,
        type: 'income', bucketId: UNALLOCATED_ID, accountId: checking,
      });
    });
    if (!monthIncome) return;

    /* --- divide it up, following the targets with a little human drift --- */
    const allocDay = Math.min(dayCap, 2);
    let leftToAllocate = monthIncome;
    const alloc = {};
    buckets.filter((b) => b.kind !== 'system').forEach((b) => {
      const drift = 0.88 + rand() * 0.24;
      let amt = round2(monthIncome * (b.targetValue / 100) * drift);
      amt = Math.min(amt, leftToAllocate);
      alloc[b.id] = amt;
      leftToAllocate = round2(leftToAllocate - amt);
      if (amt > 0) {
        push({
          date: `${mk}-${pad2(allocDay)}`, description: `Allocated to ${b.name}`, amount: amt,
          type: 'transfer', bucketId: UNALLOCATED_ID, toBucketId: b.id,
          accountId: b.kind === 'save' ? savings : checking,
        });
      }
    });

    /* --- spending through the month --- */
    Object.entries(DEMO_SPEND).forEach(([name, descs]) => {
      const b = byName(name);
      if (!b) return;
      const budget = alloc[b.id] || 0;
      if (budget <= 0) return;
      // discretionary buckets sometimes overshoot; essentials rarely do
      const ceiling = b.discretionary ? 1.22 : (b.essential ? 1.04 : 0.55);
      const spendTotal = round2(budget * (0.55 + rand() * (ceiling - 0.55)));
      const n = 2 + Math.floor(rand() * 4);
      let left = spendTotal;
      for (let i = 0; i < n; i++) {
        const last = i === n - 1;
        const amt = last ? round2(left) : round2(left * (0.2 + rand() * 0.45));
        left = round2(left - amt);
        const day = Math.min(dayCap, 2 + Math.floor(rand() * (dim - 3)));
        if (amt <= 0) continue;
        push({
          date: `${mk}-${pad2(day)}`, description: descs[Math.floor(rand() * descs.length)],
          amount: amt, type: 'expense', bucketId: b.id, accountId: checking,
        });
      }
    });

    /* --- investing --- */
    const inv = byName('Investments');
    const contribution = round2((alloc[inv.id] || 0) * 0.9);
    if (contribution > 0) {
      const split = round2(contribution * 0.65);
      push({
        date: `${mk}-${pad2(Math.min(dayCap, 5))}`, description: 'Auto-invest — index fund',
        amount: split, type: 'contribution', bucketId: inv.id, holdingId: holding.id, accountId: brokerage,
      });
      push({
        date: `${mk}-${pad2(Math.min(dayCap, 5))}`, description: 'Retirement contribution',
        amount: round2(contribution - split), type: 'contribution', bucketId: inv.id,
        holdingId: holding2.id, accountId: brokerage,
      });
      // market movement: drift up with monthly noise
      portfolio1 = round2((portfolio1 + split) * (1 + (rand() - 0.38) * 0.036));
      portfolio2 = round2((portfolio2 + (contribution - split)) * (1 + (rand() - 0.38) * 0.03));
      const vday = isCurrent ? dayCap : dim;
      s.valuations.push({ id: uid('v'), holdingId: holding.id, date: `${mk}-${pad2(vday)}`, value: portfolio1 });
      s.valuations.push({ id: uid('v'), holdingId: holding2.id, date: `${mk}-${pad2(vday)}`, value: portfolio2 });
    }
  });

  s.transactions = T.sort((a, b) => (a.date < b.date ? 1 : -1));

  /* --- goals --- */
  const travel = byName('Travel Fund');
  const emergency = byName('Emergency / General Savings');
  s.goals = [
    {
      id: uid('g'), name: 'Japan trip', targetAmount: 6500,
      deadline: `${addMonths(thisMonth(), 11)}-01`, bucketId: travel.id,
      manualAmount: 0, createdAt: `${months[0]}-01`, note: 'Two weeks, spring.', archived: false,
    },
    {
      id: uid('g'), name: '6-month emergency fund', targetAmount: 16800,
      deadline: `${addMonths(thisMonth(), 20)}-01`, bucketId: emergency.id,
      manualAmount: 0, createdAt: `${months[0]}-01`, note: '', archived: false,
    },
    {
      id: uid('g'), name: 'New laptop', targetAmount: 2400,
      deadline: `${addMonths(thisMonth(), 5)}-15`, bucketId: null,
      manualAmount: 900, createdAt: `${months[6]}-01`, note: '', archived: false,
    },
  ];

  /* --- recurring --- */
  const nextOn = (day) => {
    const t = new Date();
    const d = new Date(t.getFullYear(), t.getMonth(), day);
    if (d < t) d.setMonth(d.getMonth() + 1);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  };
  s.recurring = [
    { id: uid('r'), name: 'Rent help for Mom', amount: 600, bucketId: byName('Help Mom / Household').id, cadence: 'monthly', dayOfMonth: 1, nextDate: nextOn(1), active: true },
    { id: uid('r'), name: 'Internet', amount: 62, bucketId: byName('Help Mom / Household').id, cadence: 'monthly', dayOfMonth: 8, nextDate: nextOn(8), active: true },
    { id: uid('r'), name: 'Phone', amount: 45, bucketId: byName('Personal Spending').id, cadence: 'monthly', dayOfMonth: 12, nextDate: nextOn(12), active: true },
    { id: uid('r'), name: 'Streaming bundle', amount: 34, bucketId: byName('Family Cinema / Entertainment').id, cadence: 'monthly', dayOfMonth: 18, nextDate: nextOn(18), active: true },
    { id: uid('r'), name: 'Gym', amount: 39, bucketId: byName('Personal Spending').id, cadence: 'monthly', dayOfMonth: 25, nextDate: nextOn(25), active: true },
  ];

  s.debts = [];
  return s;
}

export function loadDemo() {
  undoStack = [];
  state = makeDemoState();
  persist();
  notify();
}
