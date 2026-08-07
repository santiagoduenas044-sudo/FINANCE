/* ==========================================================================
   flows.js — the things you actually *do*: receive income and divide it,
   record spending, move money, edit buckets, goals and investments.
   ========================================================================== */

import {
  uid, round2, money, esc, icon, todayISO, parseAmount, $$, sum, clamp,
  BUCKET_ICONS, currencySymbol, dateLabel, monthKey, thisMonth,
} from './util.js';
import {
  getState, commit, SLOTS, BUCKET_KINDS, UNALLOCATED_ID,
  activeBuckets, spendableBuckets, bucketById,
} from './store.js';
import {
  suggestSplit, monthStats, allBalances, bucketTarget, allocationAdvice, advanceRecurring,
} from './model.js';
import {
  openModal, toast, confirmDialog, field, inputHTML, selectHTML, checkHTML,
  readForm, colorPicker, iconPicker, wirePickers, actionSheet, amountField,
} from './ui.js';
import { allocationBar } from './charts.js';

const cvar = (slot) => `var(--${slot || 's0'})`;

/* ==========================================================================
   Receive income → divide every dollar
   ========================================================================== */

export function divideIncome(prefill = {}) {
  const s = getState();
  const buckets = spendableBuckets(s);
  const accounts = s.accounts.filter((a) => !a.archived);

  if (!buckets.length) {
    toast('Add at least one bucket first.');
    return;
  }

  const state = {
    amount: prefill.amount ? round2(prefill.amount) : 0,
    split: Object.fromEntries(buckets.map((b) => [b.id, 0])),
    locked: new Set(),
    touched: new Set(),
  };

  const rows = buckets.map((b) => `
    <div class="split-row" data-bid="${b.id}">
      <span class="dot lg" style="--c:${cvar(b.color)}"></span>
      <span class="split-name">
        ${esc(b.name)}
        <span class="lbl" style="display:block;font-size:9px;margin-top:1px">${
          b.targetMode === 'percent' ? `target ${b.targetValue || 0}%` : `target ${money(b.targetValue || 0)}`}</span>
      </span>
      <input type="number" step="0.01" min="0" inputmode="decimal" data-amt aria-label="Amount for ${esc(b.name)}" value="">
      <span class="split-pct" data-pct>—</span>
      <button type="button" class="icon-btn sm bare split-lock" data-lock aria-pressed="false"
        aria-label="Lock ${esc(b.name)}">${icon('unlock')}</button>
    </div>`).join('');

  const body = `
    ${amountField('__amount', state.amount || '', 'Income amount', { id: 'div-amount' })}

    <div class="row-2">
      ${field('Date', inputHTML('date', { type: 'date', value: prefill.date || todayISO() }))}
      ${field('Where it landed', selectHTML('accountId', accounts.map((a) => ({ value: a.id, label: a.name })), accounts[0]?.id))}
    </div>
    ${field('Description', inputHTML('description', { value: prefill.description || '', placeholder: 'Paycheck, freelance, gift…' }))}

    <div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="lbl">Preview</span>
      </div>
      <div id="div-bar"></div>
    </div>

    <div class="divide-status" id="div-status">
      <span>${icon('wallet', '')}</span>
      <span class="k">Left to divide</span>
      <span class="v" id="div-left">${money(0)}</span>
    </div>

    <div style="display:flex;flex-wrap:wrap;gap:6px">
      <button type="button" class="btn btn-sm" data-act="targets">Use my targets</button>
      <button type="button" class="btn btn-sm" data-act="even">Even split</button>
      <button type="button" class="btn btn-sm" data-act="rest">Give the rest out</button>
      <button type="button" class="btn btn-sm" data-act="clear">Clear</button>
    </div>

    <div>
      <span class="lbl" style="display:block;margin-bottom:2px">Divide it up</span>
      <div id="div-rows">${rows}</div>
      <p style="margin:12px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        Anything you leave undivided stays in <strong style="color:var(--ink-2);font-weight:550">Unallocated Money</strong>
        so it never disappears. Targets are only suggestions — change them any time in Buckets.
      </p>
    </div>`;

  const m = openModal({
    title: 'Money came in',
    wide: true,
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Save &amp; divide</button>`,
  });

  const root = m.el;
  const amountEl = root.querySelector('#div-amount');
  const leftEl = root.querySelector('#div-left');
  const statusEl = root.querySelector('#div-status');
  const barEl = root.querySelector('#div-bar');

  const rowFor = (bid) => root.querySelector(`.split-row[data-bid="${bid}"]`);

  function allocatedTotal() {
    return round2(sum(Object.values(state.split)));
  }

  function paint() {
    const amt = state.amount;
    const total = allocatedTotal();
    const left = round2(amt - total);

    buckets.forEach((b) => {
      const row = rowFor(b.id);
      const input = row.querySelector('[data-amt]');
      const v = state.split[b.id] || 0;
      if (document.activeElement !== input) input.value = v ? v.toFixed(2).replace(/\.00$/, '') : '';
      row.querySelector('[data-pct]').textContent = amt > 0 && v > 0 ? `${((v / amt) * 100).toFixed(0)}%` : '—';
    });

    leftEl.textContent = money(Math.abs(left));
    statusEl.classList.toggle('done', Math.abs(left) < 0.005 && amt > 0);
    statusEl.classList.toggle('over', left < -0.005);
    statusEl.querySelector('.k').textContent =
      amt <= 0 ? 'Enter an amount to divide'
        : left < -0.005 ? 'Over by'
          : Math.abs(left) < 0.005 ? 'Every dollar has a job'
            : 'Left to divide';

    barEl.innerHTML = allocationBar(
      buckets.map((b) => ({ label: b.name, value: state.split[b.id] || 0, color: cvar(b.color) })),
      { total: Math.max(amt, total) || 1 },
    );
  }

  function applySuggestion() {
    const amt = state.amount;
    const lockedTotal = round2(sum([...state.locked], (id) => state.split[id] || 0));
    const pool = round2(Math.max(0, amt - lockedTotal));
    const st = monthStats(getState(), monthKey(root.querySelector('[name=date]').value || todayISO()));
    const basis = round2(st.income + amt);
    const sug = suggestSplit(getState(), pool, basis);
    buckets.forEach((b) => {
      if (state.locked.has(b.id)) return;
      state.split[b.id] = round2(sug[b.id] || 0);
    });
    paint();
  }

  function evenSplit() {
    const amt = state.amount;
    const open = buckets.filter((b) => !state.locked.has(b.id));
    const lockedTotal = round2(sum([...state.locked], (id) => state.split[id] || 0));
    let pool = round2(Math.max(0, amt - lockedTotal));
    const each = round2(pool / (open.length || 1));
    open.forEach((b, i) => {
      const v = i === open.length - 1 ? round2(pool) : each;
      state.split[b.id] = v;
      pool = round2(pool - v);
    });
    paint();
  }

  function giveRest() {
    const left = round2(state.amount - allocatedTotal());
    if (left <= 0.004) { toast('Nothing left to give out.'); return; }
    const open = buckets.filter((b) => !state.locked.has(b.id));
    if (!open.length) { toast('Every bucket is locked.'); return; }
    const weights = open.map((b) => (b.targetMode === 'percent' ? (b.targetValue || 0) : (b.targetValue || 0)));
    const wTotal = sum(weights) || open.length;
    let pool = left;
    open.forEach((b, i) => {
      const w = (weights[i] || (wTotal === open.length ? 1 : 0)) / wTotal;
      const v = i === open.length - 1 ? round2(pool) : round2(left * w);
      state.split[b.id] = round2((state.split[b.id] || 0) + v);
      pool = round2(pool - v);
    });
    paint();
  }

  amountEl.addEventListener('input', () => {
    state.amount = parseAmount(amountEl.value);
    if (!state.touched.size) applySuggestion(); else paint();
  });
  amountEl.addEventListener('change', () => {
    state.amount = parseAmount(amountEl.value);
    if (!state.touched.size) applySuggestion(); else paint();
  });

  root.querySelector('#div-rows').addEventListener('input', (e) => {
    const input = e.target.closest('[data-amt]');
    if (!input) return;
    const bid = input.closest('.split-row').dataset.bid;
    state.split[bid] = Math.max(0, parseAmount(input.value));
    state.touched.add(bid);
    paint();
  });

  root.querySelector('#div-rows').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-lock]');
    if (!btn) return;
    const bid = btn.closest('.split-row').dataset.bid;
    const on = !state.locked.has(bid);
    if (on) state.locked.add(bid); else state.locked.delete(bid);
    btn.setAttribute('aria-pressed', String(on));
    btn.innerHTML = icon(on ? 'lock' : 'unlock');
  });

  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'targets') { state.touched.clear(); applySuggestion(); }
    if (act === 'even') evenSplit();
    if (act === 'rest') giveRest();
    if (act === 'clear') { buckets.forEach((x) => { state.split[x.id] = 0; }); state.touched.clear(); paint(); }
  });

  root.querySelector('[data-save]').addEventListener('click', () => {
    const form = readForm(root);
    const amt = round2(state.amount);
    if (amt <= 0) { toast('Enter how much came in.'); amountEl.focus(); return; }
    const total = allocatedTotal();
    if (total > amt + 0.005) { toast('You have divided up more than came in.'); return; }

    const date = form.date || todayISO();
    const desc = (form.description || '').trim() || 'Income';
    const accountId = form.accountId || null;

    commit((st) => {
      st.transactions.unshift({
        id: uid('t'), date, description: desc, amount: amt, type: 'income',
        bucketId: UNALLOCATED_ID, accountId, createdAt: new Date().toISOString(),
      });
      buckets.forEach((b) => {
        const v = round2(state.split[b.id] || 0);
        if (v <= 0) return;
        st.transactions.unshift({
          id: uid('t'), date, description: `Allocated to ${b.name}`, amount: v,
          type: 'transfer', bucketId: UNALLOCATED_ID, toBucketId: b.id, accountId,
          createdAt: new Date().toISOString(),
        });
      });
      st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    }, 'income');

    const left = round2(amt - total);
    m.close();
    toast(left > 0.004
      ? `${money(amt)} recorded — ${money(left)} still to divide.`
      : `${money(amt)} recorded and fully divided.`);
  });

  paint();
}

/* ==========================================================================
   Spending, transfers, contributions
   ========================================================================== */

export function addExpense(prefill = {}) {
  const s = getState();
  const buckets = spendableBuckets(s).filter((b) => b.kind !== 'invest');
  const accounts = s.accounts.filter((a) => !a.archived);
  const bal = allBalances(s);

  const body = `
    ${amountField('amount', prefill.amount || '', 'Amount spent')}
    ${field('What was it?', inputHTML('description', { value: prefill.description || '', placeholder: 'Groceries, cinema tickets…', required: true }))}
    ${field('Which bucket does it come out of?', selectHTML('bucketId',
      buckets.map((b) => ({ value: b.id, label: `${b.name} — ${money(bal[b.id] || 0)} left` })), prefill.bucketId || buckets[0]?.id))}
    <div class="row-2">
      ${field('Date', inputHTML('date', { type: 'date', value: prefill.date || todayISO() }))}
      ${field('Account', selectHTML('accountId', accounts.map((a) => ({ value: a.id, label: a.name })), accounts[0]?.id))}
    </div>
    <div id="exp-warn"></div>`;

  const m = openModal({
    title: 'Money went out',
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Save</button>`,
  });

  const warn = m.el.querySelector('#exp-warn');
  const check = () => {
    const f = readForm(m.el);
    const b = bucketById(f.bucketId);
    const have = bal[f.bucketId] || 0;
    const amt = round2(f.amount || 0);
    if (b && amt > have + 0.004) {
      warn.innerHTML = `<div class="divide-status over">
        <span>${icon('alert')}</span>
        <span class="k">This takes ${esc(b.name)} ${money(amt - have)} below zero</span>
      </div>`;
    } else warn.innerHTML = '';
  };
  m.el.addEventListener('input', check);
  m.el.addEventListener('change', check);

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    const amt = round2(f.amount || 0);
    if (amt <= 0) { toast('Enter an amount.'); return; }
    if (!f.description?.trim()) { toast('Give it a description so future you remembers.'); return; }
    commit((st) => {
      st.transactions.unshift({
        id: uid('t'), date: f.date || todayISO(), description: f.description.trim(),
        amount: amt, type: 'expense', bucketId: f.bucketId, accountId: f.accountId,
        createdAt: new Date().toISOString(),
      });
      st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    }, 'expense');
    m.close();
    toast(`${money(amt)} out of ${bucketById(f.bucketId)?.name || 'bucket'}.`);
  });
}

export function moveMoney(prefill = {}) {
  const s = getState();
  const buckets = activeBuckets(s);
  const bal = allBalances(s);
  const opts = buckets.map((b) => ({ value: b.id, label: `${b.name} — ${money(bal[b.id] || 0)}` }));

  const body = `
    ${amountField('amount', prefill.amount || '', 'Amount to move')}
    ${field('From', selectHTML('bucketId', opts, prefill.from || UNALLOCATED_ID))}
    <div style="display:grid;place-items:center;color:var(--ink-3);margin:-4px 0">${icon('arrowDown')}</div>
    ${field('To', selectHTML('toBucketId', opts, prefill.to || buckets.find((b) => b.id !== UNALLOCATED_ID)?.id))}
    <div class="row-2">
      ${field('Date', inputHTML('date', { type: 'date', value: todayISO() }))}
      ${field('Note', inputHTML('description', { placeholder: 'Optional' }))}
    </div>`;

  const m = openModal({
    title: 'Move money between buckets',
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Move it</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    const amt = round2(f.amount || 0);
    if (amt <= 0) { toast('Enter an amount.'); return; }
    if (f.bucketId === f.toBucketId) { toast('Pick two different buckets.'); return; }
    const from = bucketById(f.bucketId), to = bucketById(f.toBucketId);
    commit((st) => {
      st.transactions.unshift({
        id: uid('t'), date: f.date || todayISO(),
        description: f.description?.trim() || `${from.name} → ${to.name}`,
        amount: amt, type: 'transfer', bucketId: f.bucketId, toBucketId: f.toBucketId,
        createdAt: new Date().toISOString(),
      });
      st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    }, 'transfer');
    m.close();
    toast(`${money(amt)} moved to ${to.name}.`);
  });
}

export function addContribution(prefill = {}) {
  const s = getState();
  if (!s.holdings.length) {
    toast('Add an investment first.');
    editHolding(null);
    return;
  }
  const buckets = spendableBuckets(s);
  const bal = allBalances(s);
  const investFirst = buckets.filter((b) => b.kind === 'invest').concat(buckets.filter((b) => b.kind !== 'invest'));

  const body = `
    ${amountField('amount', prefill.amount || '', 'Amount invested')}
    ${field('Into which investment?', selectHTML('holdingId', s.holdings.map((hd) => ({ value: hd.id, label: hd.name })), prefill.holdingId || s.holdings[0].id))}
    ${field('Paid from which bucket?', selectHTML('bucketId',
      investFirst.map((b) => ({ value: b.id, label: `${b.name} — ${money(bal[b.id] || 0)} left` })), investFirst[0]?.id))}
    <div class="row-2">
      ${field('Date', inputHTML('date', { type: 'date', value: todayISO() }))}
      ${field('Note', inputHTML('description', { placeholder: 'Monthly contribution' }))}
    </div>`;

  const m = openModal({
    title: 'Invest money',
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Save</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    const amt = round2(f.amount || 0);
    if (amt <= 0) { toast('Enter an amount.'); return; }
    const hd = s.holdings.find((x) => x.id === f.holdingId);
    commit((st) => {
      st.transactions.unshift({
        id: uid('t'), date: f.date || todayISO(),
        description: f.description?.trim() || `Invested in ${hd.name}`,
        amount: amt, type: 'contribution', bucketId: f.bucketId, holdingId: f.holdingId,
        createdAt: new Date().toISOString(),
      });
      st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    }, 'contribution');
    m.close();
    toast(`${money(amt)} invested in ${hd.name}.`);
  });
}

/* ==========================================================================
   Editing a transaction
   ========================================================================== */

export function editTransaction(txn) {
  const s = getState();
  const buckets = activeBuckets(s);
  const accounts = s.accounts.filter((a) => !a.archived);
  const typeLabels = {
    income: 'Income', expense: 'Spending', transfer: 'Move between buckets', contribution: 'Investment',
  };

  const body = `
    ${amountField('amount', txn.amount, 'Amount')}
    ${field('Description', inputHTML('description', { value: txn.description }))}
    <div class="row-2">
      ${field('Date', inputHTML('date', { type: 'date', value: txn.date }))}
      ${field('Type', `<input class="input" value="${esc(typeLabels[txn.type] || txn.type)}" disabled>`)}
    </div>
    ${field(txn.type === 'transfer' ? 'From bucket' : 'Bucket',
      selectHTML('bucketId', buckets.map((b) => ({ value: b.id, label: b.name })), txn.bucketId))}
    ${txn.type === 'transfer'
      ? field('To bucket', selectHTML('toBucketId', buckets.map((b) => ({ value: b.id, label: b.name })), txn.toBucketId))
      : ''}
    ${txn.type === 'contribution' && s.holdings.length
      ? field('Investment', selectHTML('holdingId', s.holdings.map((hd) => ({ value: hd.id, label: hd.name })), txn.holdingId))
      : ''}
    ${field('Account', selectHTML('accountId', [{ value: '', label: '—' }].concat(accounts.map((a) => ({ value: a.id, label: a.name }))), txn.accountId || ''))}`;

  const m = openModal({
    title: 'Edit transaction',
    body,
    foot: `<button type="button" class="btn btn-danger" data-del>${icon('trash')} Delete</button>
      <span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Save</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    const amt = round2(f.amount || 0);
    if (amt <= 0) { toast('Enter an amount.'); return; }
    commit((st) => {
      const t = st.transactions.find((x) => x.id === txn.id);
      if (!t) return;
      Object.assign(t, {
        amount: amt,
        description: (f.description || '').trim() || t.description,
        date: f.date || t.date,
        bucketId: f.bucketId || t.bucketId,
        toBucketId: f.toBucketId ?? t.toBucketId,
        holdingId: f.holdingId ?? t.holdingId,
        accountId: f.accountId || null,
      });
      st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    }, 'edit');
    m.close();
    toast('Transaction updated.');
  });

  m.el.querySelector('[data-del]').addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: 'Delete this transaction?',
      text: `“${txn.description}” for ${money(txn.amount)} will be removed and every total will be recalculated.`,
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return;
    commit((st) => { st.transactions = st.transactions.filter((x) => x.id !== txn.id); }, 'delete');
    m.close();
    toast('Transaction deleted.');
  });
}

/* ==========================================================================
   Buckets
   ========================================================================== */

export function editBucket(bucket) {
  const isNew = !bucket;
  const s = getState();
  const b = bucket || {
    id: uid('b'), name: '', kind: 'spend', color: nextFreeSlot(s), icon: 'wallet',
    targetMode: 'percent', targetValue: 0, essential: false, discretionary: false,
    emergency: false, rollover: true, note: '', archived: false,
    order: spendableBuckets(s).length,
  };

  const advice = allocationAdvice(s);

  const body = `
    ${field('Name', inputHTML('name', { value: b.name, placeholder: 'e.g. Travel Fund', required: true, autofocus: true }))}
    ${field('What is this money for?', selectHTML('kind', BUCKET_KINDS.filter((k) => k.id !== 'system').map((k) => ({ value: k.id, label: `${k.name} — ${k.hint.toLowerCase()}` })), b.kind))}
    <div class="row-2">
      ${field('Monthly target', selectHTML('targetMode', [
        { value: 'percent', label: '% of income' },
        { value: 'fixed', label: 'Fixed amount' },
      ], b.targetMode))}
      ${field('Amount', inputHTML('targetValue', { type: 'number', step: '0.01', min: 0, value: b.targetValue || '', inputmode: 'decimal' }))}
    </div>
    <div style="font-size:12px;color:var(--ink-3);margin-top:-6px">
      Percentages across all buckets currently add up to <strong style="color:${advice.over ? 'var(--bad-ink)' : 'var(--ink-2)'};font-weight:600">${advice.totalPct}%</strong>.
      They do not have to reach 100% — whatever is left over simply stays unallocated.
    </div>
    ${colorPicker('color', SLOTS.filter((x) => x.id !== 's0'), b.color)}
    ${iconPicker('icon', BUCKET_ICONS, b.icon)}
    <div style="display:flex;flex-direction:column;gap:9px;padding-top:4px;border-top:1px solid var(--hairline)">
      ${checkHTML('discretionary', 'Everyday money — counts toward “safe to spend”', b.discretionary)}
      ${checkHTML('essential', 'Essential — used to size the emergency fund', b.essential)}
      ${checkHTML('emergency', 'This is my emergency fund', b.emergency)}
      ${checkHTML('rollover', 'Unspent money rolls into next month', b.rollover !== false)}
    </div>
    ${field('Note', `<textarea class="textarea" name="note" placeholder="Optional">${esc(b.note || '')}</textarea>`)}`;

  const m = openModal({
    title: isNew ? 'New bucket' : 'Edit bucket',
    body,
    foot: `${isNew ? '' : `<button type="button" class="btn btn-danger" data-del>${icon('trash')} Delete</button>`}
      <span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>${isNew ? 'Create bucket' : 'Save'}</button>`,
  });
  wirePickers(m.el);

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    if (!f.name?.trim()) { toast('Give the bucket a name.'); return; }
    commit((st) => {
      const target = isNew ? { ...b } : st.buckets.find((x) => x.id === b.id);
      Object.assign(target, {
        name: f.name.trim(),
        kind: f.kind,
        targetMode: f.targetMode,
        targetValue: round2(f.targetValue || 0),
        color: f.color || b.color,
        icon: f.icon || b.icon,
        discretionary: !!f.discretionary,
        essential: !!f.essential,
        emergency: !!f.emergency,
        rollover: !!f.rollover,
        note: (f.note || '').trim(),
      });
      if (f.emergency) st.buckets.forEach((x) => { if (x.id !== target.id) x.emergency = false; });
      if (isNew) {
        const sys = st.buckets.find((x) => x.id === UNALLOCATED_ID);
        target.order = st.buckets.filter((x) => x.id !== UNALLOCATED_ID).length;
        st.buckets.push(target);
        if (sys) sys.order = 9999;
      }
    }, isNew ? 'new bucket' : 'bucket');
    m.close();
    toast(isNew ? `${f.name.trim()} created.` : 'Bucket updated.');
  });

  m.el.querySelector('[data-del]')?.addEventListener('click', () => { m.close(); deleteBucket(b); });
}

function nextFreeSlot(s) {
  const used = new Set(s.buckets.map((b) => b.color));
  return (SLOTS.find((x) => x.id !== 's0' && !used.has(x.id)) || SLOTS[0]).id;
}

export async function deleteBucket(b) {
  const s = getState();
  const used = s.transactions.filter((t) => t.bucketId === b.id || t.toBucketId === b.id).length;
  const bal = allBalances(s)[b.id] || 0;
  const ok = await confirmDialog({
    title: `Delete ${b.name}?`,
    text: used
      ? `${used} transaction${used === 1 ? '' : 's'} point at this bucket. They will be moved to Unallocated Money${
        Math.abs(bal) > 0.004 ? `, along with its ${money(bal)} balance` : ''}, so no money is lost.`
      : 'This bucket has no transactions, so nothing else changes.',
    confirmLabel: 'Delete bucket', danger: true,
  });
  if (!ok) return;
  commit((st) => {
    st.transactions.forEach((t) => {
      if (t.bucketId === b.id) t.bucketId = UNALLOCATED_ID;
      if (t.toBucketId === b.id) t.toBucketId = UNALLOCATED_ID;
    });
    st.transactions = st.transactions.filter((t) => !(t.type === 'transfer' && t.bucketId === t.toBucketId));
    st.goals.forEach((g) => { if (g.bucketId === b.id) g.bucketId = null; });
    st.recurring.forEach((r) => { if (r.bucketId === b.id) r.bucketId = UNALLOCATED_ID; });
    st.buckets = st.buckets.filter((x) => x.id !== b.id);
    st.buckets.filter((x) => x.id !== UNALLOCATED_ID).forEach((x, i) => { x.order = i; });
  }, 'delete bucket');
  toast(`${b.name} deleted.`);
}

/** Reorder buckets — used by both drag and the keyboard arrows. */
export function reorderBucket(id, delta) {
  commit((st) => {
    const list = st.buckets.filter((b) => b.id !== UNALLOCATED_ID && !b.archived).sort((a, b) => a.order - b.order);
    const i = list.findIndex((b) => b.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    list.forEach((b, k) => { b.order = k; });
    const sys = st.buckets.find((b) => b.id === UNALLOCATED_ID);
    if (sys) sys.order = 9999;
  });
}

export function reorderBucketTo(id, targetId) {
  commit((st) => {
    const list = st.buckets.filter((b) => b.id !== UNALLOCATED_ID && !b.archived).sort((a, b) => a.order - b.order);
    const from = list.findIndex((b) => b.id === id);
    const to = list.findIndex((b) => b.id === targetId);
    if (from < 0 || to < 0 || from === to) return;
    const [moved] = list.splice(from, 1);
    list.splice(to, 0, moved);
    list.forEach((b, k) => { b.order = k; });
    const sys = st.buckets.find((b) => b.id === UNALLOCATED_ID);
    if (sys) sys.order = 9999;
  });
}

/* ==========================================================================
   Goals
   ========================================================================== */

export function editGoal(goal) {
  const isNew = !goal;
  const s = getState();
  const buckets = spendableBuckets(s);
  const g = goal || {
    id: uid('g'), name: '', targetAmount: 0, deadline: '', bucketId: null,
    manualAmount: 0, createdAt: todayISO(), note: '', archived: false,
  };

  const body = `
    ${field('What are you saving for?', inputHTML('name', { value: g.name, placeholder: 'Japan trip, new laptop…', autofocus: true }))}
    <div class="row-2">
      ${field('Target amount', inputHTML('targetAmount', { type: 'number', step: '0.01', min: 0, value: g.targetAmount || '', inputmode: 'decimal' }))}
      ${field('Deadline', inputHTML('deadline', { type: 'date', value: g.deadline || '' }))}
    </div>
    ${field('Track it with a bucket', selectHTML('bucketId',
      [{ value: '', label: 'No bucket — I will update it myself' }].concat(buckets.map((b) => ({ value: b.id, label: b.name }))),
      g.bucketId || ''), 'Linking a bucket keeps the goal up to date automatically.')}
    ${field('Extra amount already saved elsewhere', inputHTML('manualAmount', { type: 'number', step: '0.01', min: 0, value: g.manualAmount || '', inputmode: 'decimal' }))}
    ${field('Note', `<textarea class="textarea" name="note" placeholder="Optional">${esc(g.note || '')}</textarea>`)}`;

  const m = openModal({
    title: isNew ? 'New goal' : 'Edit goal',
    body,
    foot: `${isNew ? '' : `<button type="button" class="btn btn-danger" data-del>${icon('trash')} Delete</button>`}
      <span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>${isNew ? 'Create goal' : 'Save'}</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    if (!f.name?.trim()) { toast('Give the goal a name.'); return; }
    if (!(f.targetAmount > 0)) { toast('Set a target amount.'); return; }
    commit((st) => {
      const t = isNew ? { ...g } : st.goals.find((x) => x.id === g.id);
      Object.assign(t, {
        name: f.name.trim(),
        targetAmount: round2(f.targetAmount),
        deadline: f.deadline || '',
        bucketId: f.bucketId || null,
        manualAmount: round2(f.manualAmount || 0),
        note: (f.note || '').trim(),
      });
      if (isNew) st.goals.push(t);
    }, isNew ? 'new goal' : 'goal');
    m.close();
    toast(isNew ? `${f.name.trim()} added.` : 'Goal updated.');
  });

  m.el.querySelector('[data-del]')?.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: `Delete ${g.name}?`, text: 'The goal is removed. Any money in its bucket stays exactly where it is.',
      confirmLabel: 'Delete goal', danger: true,
    });
    if (!ok) return;
    commit((st) => { st.goals = st.goals.filter((x) => x.id !== g.id); }, 'delete goal');
    m.close();
    toast('Goal deleted.');
  });
}

/* ==========================================================================
   Investments
   ========================================================================== */

export function editHolding(holding) {
  const isNew = !holding;
  const hd = holding || { id: uid('h'), name: '', kind: 'fund', note: '' };

  const body = `
    ${field('Name', inputHTML('name', { value: hd.name, placeholder: 'Index fund, retirement account…', autofocus: true }))}
    ${field('Kind', selectHTML('kind', [
      { value: 'fund', label: 'Fund or ETF' },
      { value: 'stock', label: 'Individual stock' },
      { value: 'retirement', label: 'Retirement account' },
      { value: 'crypto', label: 'Crypto' },
      { value: 'other', label: 'Other' },
    ], hd.kind))}
    ${isNew ? field('What is it worth today?', inputHTML('value', { type: 'number', step: '0.01', min: 0, placeholder: '0', inputmode: 'decimal' }),
      'Leave blank if you have not put anything in yet.') : ''}
    ${field('Note', `<textarea class="textarea" name="note" placeholder="Optional">${esc(hd.note || '')}</textarea>`)}`;

  const m = openModal({
    title: isNew ? 'Track an investment' : 'Edit investment',
    body,
    foot: `${isNew ? '' : `<button type="button" class="btn btn-danger" data-del>${icon('trash')} Delete</button>`}
      <span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>${isNew ? 'Add' : 'Save'}</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    if (!f.name?.trim()) { toast('Give it a name.'); return; }
    commit((st) => {
      const t = isNew ? { ...hd } : st.holdings.find((x) => x.id === hd.id);
      Object.assign(t, { name: f.name.trim(), kind: f.kind, note: (f.note || '').trim() });
      if (isNew) {
        st.holdings.push(t);
        if (f.value > 0) st.valuations.push({ id: uid('v'), holdingId: t.id, date: todayISO(), value: round2(f.value) });
      }
    }, isNew ? 'new investment' : 'investment');
    m.close();
    toast(isNew ? `${f.name.trim()} added.` : 'Investment updated.');
  });

  m.el.querySelector('[data-del]')?.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: `Stop tracking ${hd.name}?`,
      text: 'Its value history is removed. Contributions you recorded stay in your transactions.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return;
    commit((st) => {
      st.holdings = st.holdings.filter((x) => x.id !== hd.id);
      st.valuations = st.valuations.filter((v) => v.holdingId !== hd.id);
    }, 'delete investment');
    m.close();
    toast('Investment removed.');
  });
}

export function updateValue(holding, currentValue) {
  const body = `
    ${amountField('value', currentValue || '', 'Current value')}
    ${field('As of', inputHTML('date', { type: 'date', value: todayISO() }))}
    <p style="margin:0;font-size:12.5px;color:var(--ink-3);line-height:1.5">
      Check your brokerage and type in what ${esc(holding.name)} is worth right now. Profit and loss is simply
      this value minus everything you have put in.
    </p>`;

  const m = openModal({
    title: `Update ${holding.name}`,
    body,
    foot: `<span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Save value</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    const v = round2(f.value || 0);
    commit((st) => {
      const date = f.date || todayISO();
      const existing = st.valuations.find((x) => x.holdingId === holding.id && x.date === date);
      if (existing) existing.value = v;
      else st.valuations.push({ id: uid('v'), holdingId: holding.id, date, value: v });
    }, 'valuation');
    m.close();
    toast(`${holding.name} is now ${money(v)}.`);
  });
}

/* ==========================================================================
   Recurring expenses
   ========================================================================== */

export function editRecurring(rec) {
  const isNew = !rec;
  const s = getState();
  const buckets = spendableBuckets(s);
  const r = rec || {
    id: uid('r'), name: '', amount: 0, bucketId: buckets[0]?.id, cadence: 'monthly',
    dayOfMonth: 1, nextDate: todayISO(), active: true,
  };

  const body = `
    ${field('Name', inputHTML('name', { value: r.name, placeholder: 'Rent, phone, streaming…', autofocus: true }))}
    <div class="row-2">
      ${field('Amount', inputHTML('amount', { type: 'number', step: '0.01', min: 0, value: r.amount || '', inputmode: 'decimal' }))}
      ${field('How often', selectHTML('cadence', [
        { value: 'monthly', label: 'Every month' },
        { value: 'weekly', label: 'Every week' },
        { value: 'biweekly', label: 'Every 2 weeks' },
        { value: 'yearly', label: 'Every year' },
      ], r.cadence))}
    </div>
    ${field('Comes out of', selectHTML('bucketId', buckets.map((b) => ({ value: b.id, label: b.name })), r.bucketId))}
    ${field('Next due', inputHTML('nextDate', { type: 'date', value: r.nextDate || todayISO() }))}
    ${checkHTML('active', 'Active', r.active !== false)}`;

  const m = openModal({
    title: isNew ? 'New recurring expense' : 'Edit recurring expense',
    body,
    foot: `${isNew ? '' : `<button type="button" class="btn btn-danger" data-del>${icon('trash')} Delete</button>`}
      <span class="grow"></span>
      <button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>${isNew ? 'Add' : 'Save'}</button>`,
  });

  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    if (!f.name?.trim()) { toast('Give it a name.'); return; }
    commit((st) => {
      const t = isNew ? { ...r } : st.recurring.find((x) => x.id === r.id);
      Object.assign(t, {
        name: f.name.trim(), amount: round2(f.amount || 0), bucketId: f.bucketId,
        cadence: f.cadence, nextDate: f.nextDate || todayISO(), active: !!f.active,
      });
      if (isNew) st.recurring.push(t);
    }, isNew ? 'new recurring' : 'recurring');
    m.close();
    toast(isNew ? 'Recurring expense added.' : 'Updated.');
  });

  m.el.querySelector('[data-del]')?.addEventListener('click', () => {
    commit((st) => { st.recurring = st.recurring.filter((x) => x.id !== r.id); }, 'delete recurring');
    m.close();
    toast('Removed.');
  });
}

/** Turn a due recurring expense into a real transaction. */
export function payRecurring(r) {
  commit((st) => {
    st.transactions.unshift({
      id: uid('t'), date: todayISO(), description: r.name, amount: round2(r.amount),
      type: 'expense', bucketId: r.bucketId, recurringId: r.id, createdAt: new Date().toISOString(),
    });
    st.transactions.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    const rec = st.recurring.find((x) => x.id === r.id);
    if (rec) rec.nextDate = advanceRecurring(rec.nextDate || todayISO(), rec.cadence);
  }, 'pay recurring');
  toast(`${r.name} recorded — ${money(r.amount)}.`);
}

/* ==========================================================================
   The + menu
   ========================================================================== */

export async function quickAdd() {
  const pick = await actionSheet('Add', [
    { id: 'income', label: 'Money came in', hint: 'Enter it and divide it into buckets', icon: 'download' },
    { id: 'expense', label: 'Money went out', hint: 'Record something you spent', icon: 'upload' },
    { id: 'move', label: 'Move money', hint: 'Shift funds between buckets', icon: 'swap' },
    { id: 'invest', label: 'Invest money', hint: 'Put money into an investment', icon: 'invest' },
  ]);
  if (pick === 'income') divideIncome();
  if (pick === 'expense') addExpense();
  if (pick === 'move') moveMoney();
  if (pick === 'invest') addContribution();
}
