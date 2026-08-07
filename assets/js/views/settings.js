/* ==========================================================================
   views/settings.js — preferences, recurring expenses, accounts, your data.
   ========================================================================== */

import { esc, icon, money, pct, dateLabel, download, todayISO, uid, round2, sum } from '../util.js';
import {
  getState, commit, exportJSON, importJSON, resetAll, loadDemo, spendableBuckets, bucketById,
} from '../store.js';
import { recurringMonthlyTotal, emergencyFund } from '../model.js';
import { toast, confirmDialog, openModal, field, inputHTML, selectHTML, readForm } from '../ui.js';
import { editRecurring, payRecurring } from '../flows.js';

const cvar = (b) => `var(--${b?.color || 's0'})`;

const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'MXN', 'BRL', 'COP', 'ARS', 'CLP', 'JPY', 'CHF', 'SEK', 'NOK', 'INR', 'ZAR'];

export function render() {
  const s = getState();
  const st = s.settings;
  const rec = (s.recurring || []).slice().sort((a, b) => (a.nextDate < b.nextDate ? -1 : 1));
  const ef = emergencyFund(s);

  const prefs = `
    <div class="card c6 card-fill">
      <div class="card-head"><span class="lbl">Preferences</span></div>
      <div style="display:flex;flex-direction:column;gap:13px">
        ${field('Your name', inputHTML('name', { value: st.name || '', placeholder: 'Shown in the greeting' }))}
        <div class="row-2">
          ${field('Currency', selectHTML('currency', CURRENCIES.map((c) => ({ value: c, label: c })), st.currency))}
          ${field('Number format', selectHTML('locale', [
            { value: 'en-US', label: 'English (US)' },
            { value: 'en-GB', label: 'English (UK)' },
            { value: 'es-ES', label: 'Español' },
            { value: 'es-MX', label: 'Español (MX)' },
            { value: 'pt-BR', label: 'Português (BR)' },
            { value: 'fr-FR', label: 'Français' },
            { value: 'de-DE', label: 'Deutsch' },
          ], st.locale))}
        </div>
        ${field('Appearance', selectHTML('theme', [
          { value: 'system', label: 'Match my device' },
          { value: 'dark', label: 'Always dark' },
          { value: 'light', label: 'Always light' },
        ], st.theme))}
      </div>
      <p style="margin:auto 0 0;padding-top:16px;font-size:12px;color:var(--ink-3);line-height:1.55">
        Currency and number format change how every figure is written — they do not convert anything,
        so pick the one you actually think in. Preferences save with the button on the right.
      </p>
    </div>`;

  const targets = `
    <div class="card c6">
      <div class="card-head"><span class="lbl">Your targets</span></div>
      <div style="display:flex;flex-direction:column;gap:13px">
        <div class="row-2">
          ${field('Savings rate target', inputHTML('savingsRateTarget', {
            type: 'number', step: '1', min: 0, max: 100, value: Math.round((st.savingsRateTarget || 0) * 100), inputmode: 'numeric' }), 'Percent of income')}
          ${field('Investment rate target', inputHTML('investmentRateTarget', {
            type: 'number', step: '1', min: 0, max: 100, value: Math.round((st.investmentRateTarget || 0) * 100), inputmode: 'numeric' }), 'Percent of income')}
        </div>
        <div class="row-2">
          ${field('Emergency fund size', inputHTML('emergencyMonths', {
            type: 'number', step: '1', min: 1, max: 24, value: st.emergencyMonths || 6, inputmode: 'numeric' }), 'Months of essentials')}
          ${field('Assumed investment return', inputHTML('annualReturn', {
            type: 'number', step: '0.5', min: -20, max: 30, value: round2((st.annualReturn || 0) * 100), inputmode: 'decimal' }), 'Percent a year, used for forecasts')}
        </div>
      </div>
      <div class="ledger" style="margin-top:14px">
        <div class="ledger-row"><span class="k">Emergency fund needs</span><span class="leader"></span>
          <span class="v">${money(ef.target)}</span></div>
        <div class="ledger-row"><span class="k">Recurring expenses a month</span><span class="leader"></span>
          <span class="v">${money(recurringMonthlyTotal(s))}</span></div>
      </div>
      <button type="button" class="btn btn-primary btn-block" style="margin-top:14px" data-save-settings>Save preferences</button>
    </div>`;

  const recurring = `
    <div class="card c12">
      <div class="card-head">
        <span class="lbl">Recurring expenses</span>
        <div class="card-head-actions">
          <button type="button" class="btn btn-sm" data-newrec>${icon('plus')} Add</button>
        </div>
      </div>
      ${rec.length ? `
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Name</th><th>Bucket</th><th>How often</th><th class="r">Amount</th><th class="r">Next due</th><th></th></tr></thead>
            <tbody>
              ${rec.map((r) => {
                const b = bucketById(r.bucketId);
                return `<tr style="${r.active ? '' : 'opacity:.5'}">
                  <td>${esc(r.name)}</td>
                  <td><span class="txn-desc"><span class="dot" style="--c:${cvar(b)}"></span><span>${esc(b?.name || '—')}</span></span></td>
                  <td class="dim nowrap">${esc({ monthly: 'Every month', weekly: 'Every week', biweekly: 'Every 2 weeks', yearly: 'Every year' }[r.cadence] || r.cadence)}</td>
                  <td class="r">${money(r.amount)}</td>
                  <td class="r nowrap">${esc(dateLabel(r.nextDate, 'short'))}</td>
                  <td class="r">
                    <span class="row-actions">
                      <button type="button" class="btn btn-sm" data-pay="${esc(r.id)}">Record</button>
                      <button type="button" class="icon-btn sm bare" data-editrec="${esc(r.id)}" aria-label="Edit ${esc(r.name)}">${icon('edit')}</button>
                    </span>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
        <p style="margin:12px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
          These are not charged automatically — “Record” turns one into a real transaction and moves the due date on.
          Bills still to come are subtracted from your safe-to-spend figure.
        </p>` : `
        <p style="margin:0;font-size:13px;color:var(--ink-2)">
          Add the bills that come round every month and the dashboard can warn you before they land.
        </p>`}
    </div>`;

  const accounts = `
    <div class="card c6">
      <div class="card-head">
        <span class="lbl">Accounts</span>
        <div class="card-head-actions"><button type="button" class="btn btn-sm" data-newacct>${icon('plus')} Add</button></div>
      </div>
      <div class="ledger">
        ${s.accounts.filter((a) => !a.archived).map((a) => `
          <div class="ledger-row">
            <span class="k" style="color:var(--ink)">${esc(a.name)}</span>
            <span class="leader"></span>
            <span class="v dim">${esc(a.kind)}</span>
            <button type="button" class="icon-btn sm bare" data-delacct="${esc(a.id)}" aria-label="Remove ${esc(a.name)}">${icon('trash')}</button>
          </div>`).join('') || '<p class="dim" style="margin:0;font-size:13px">No accounts yet.</p>'}
      </div>
      <p style="margin:12px 0 0;font-size:12px;color:var(--ink-3);line-height:1.5">
        Accounts are just labels for where money physically sits. Buckets decide what it is <em>for</em>.
      </p>
    </div>`;

  const data = `
    <div class="card c6">
      <div class="card-head"><span class="lbl">Your data</span></div>
      <p style="margin:0 0 14px;font-size:13px;color:var(--ink-2);line-height:1.5">
        Everything lives in this browser on this device. Nothing is uploaded anywhere. Export a backup
        now and then — especially before clearing your browser data.
      </p>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button type="button" class="btn" data-export>${icon('download')} Export backup</button>
        <button type="button" class="btn" data-import>${icon('upload')} Import backup</button>
        <button type="button" class="btn" data-demo>${icon('spark')} Load sample data</button>
        <button type="button" class="btn btn-danger" data-reset>${icon('trash')} Start over</button>
      </div>
      <input type="file" accept="application/json,.json" id="import-file" class="sr-only">
      <div class="ledger" style="margin-top:16px">
        <div class="ledger-row"><span class="k">Transactions stored</span><span class="leader"></span><span class="v">${s.transactions.length}</span></div>
        <div class="ledger-row"><span class="k">Buckets</span><span class="leader"></span><span class="v">${s.buckets.length}</span></div>
        <div class="ledger-row"><span class="k">Goals</span><span class="leader"></span><span class="v">${s.goals.length}</span></div>
        <div class="ledger-row"><span class="k">First recorded</span><span class="leader"></span><span class="v">${
          s.transactions.length ? esc(dateLabel(s.transactions[s.transactions.length - 1].date)) : '—'}</span></div>
      </div>
    </div>`;

  const about = `
    <div class="card c12">
      <div class="card-head"><span class="lbl">How the numbers work</span></div>
      <div class="grid">
        ${[
          ['Money remaining', 'What is sitting in Unallocated Money — income that has arrived but has not been given a job.'],
          ['Safe to spend', 'Everyday-bucket balances plus undivided money, minus bills due before the end of the month.'],
          ['Saved / invested', 'Money that moved into saving or investing buckets this month, minus anything taken back out.'],
          ['Savings rate', 'Saved divided by income, for the current month.'],
          ['Net worth', 'Cash across all buckets, plus what your investments are worth, minus debts.'],
          ['Health score', 'Six habits weighted together: savings rate, investment rate, emergency fund, staying inside buckets, dividing income, and goals on track.'],
        ].map(([t, d]) => `
          <div class="c4">
            <div style="font-size:13.5px;font-weight:600;letter-spacing:-.012em">${esc(t)}</div>
            <p style="margin:5px 0 0;font-size:12.5px;color:var(--ink-2);line-height:1.5">${esc(d)}</p>
          </div>`).join('')}
      </div>
    </div>`;

  return `
    <div class="grid stagger">${prefs}${targets}</div>
    <div class="section-head"><h2>Recurring</h2></div>
    <div class="grid">${recurring}</div>
    <div class="section-head"><h2>Setup</h2></div>
    <div class="grid">${accounts}${data}</div>
    <div class="section-head"><h2>Reference</h2></div>
    <div class="grid">${about}</div>`;
}

export function mount(root, ctx) {
  root.querySelector('[data-save-settings]')?.addEventListener('click', () => {
    const f = readForm(root);
    commit((s) => {
      Object.assign(s.settings, {
        name: (f.name || '').trim(),
        currency: f.currency,
        locale: f.locale,
        theme: f.theme,
        savingsRateTarget: Math.max(0, (f.savingsRateTarget || 0) / 100),
        investmentRateTarget: Math.max(0, (f.investmentRateTarget || 0) / 100),
        emergencyMonths: Math.max(1, f.emergencyMonths || 6),
        annualReturn: (f.annualReturn || 0) / 100,
      });
    });
    ctx.applySettings();
    toast('Preferences saved.');
  });

  root.addEventListener('click', async (e) => {
    if (e.target.closest('[data-newrec]')) { editRecurring(null); return; }

    const er = e.target.closest('[data-editrec]');
    if (er) { editRecurring(getState().recurring.find((r) => r.id === er.dataset.editrec)); return; }

    const pay = e.target.closest('[data-pay]');
    if (pay) { payRecurring(getState().recurring.find((r) => r.id === pay.dataset.pay)); return; }

    if (e.target.closest('[data-newacct]')) { newAccount(); return; }

    const da = e.target.closest('[data-delacct]');
    if (da) {
      const acc = getState().accounts.find((a) => a.id === da.dataset.delacct);
      const ok = await confirmDialog({
        title: `Remove ${acc.name}?`,
        text: 'Transactions that pointed at it simply lose the label. No amounts change.',
        confirmLabel: 'Remove', danger: true,
      });
      if (!ok) return;
      commit((s) => {
        s.accounts = s.accounts.filter((a) => a.id !== acc.id);
        s.transactions.forEach((t) => { if (t.accountId === acc.id) t.accountId = null; });
      }, 'account');
      toast('Account removed.');
      return;
    }

    if (e.target.closest('[data-export]')) {
      download(`ledger-backup-${todayISO()}.json`, exportJSON());
      toast('Backup downloaded.');
      return;
    }

    if (e.target.closest('[data-import]')) { root.querySelector('#import-file').click(); return; }

    if (e.target.closest('[data-demo]')) {
      const ok = await confirmDialog({
        title: 'Load sample data?',
        text: 'This replaces everything currently stored with 14 months of made-up history so you can see how the app behaves. Export a backup first if you need one.',
        confirmLabel: 'Load sample data', danger: true,
      });
      if (!ok) return;
      loadDemo();
      ctx.applySettings();
      toast('Sample data loaded.');
      return;
    }

    if (e.target.closest('[data-reset]')) {
      const ok = await confirmDialog({
        title: 'Delete everything and start over?',
        text: 'Every transaction, bucket, goal and investment is erased from this device. This cannot be undone.',
        confirmLabel: 'Delete everything', danger: true,
      });
      if (!ok) return;
      resetAll();
      ctx.applySettings();
      toast('Cleared. Fresh start.');
    }
  });

  root.querySelector('#import-file')?.addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      importJSON(text);
      ctx.applySettings();
      toast('Backup restored.');
    } catch (err) {
      toast(err.message || 'That file could not be read.');
    }
    e.target.value = '';
  });
}

function newAccount() {
  const m = openModal({
    title: 'New account',
    body: `${field('Name', inputHTML('name', { placeholder: 'Checking, Savings, Cash…', autofocus: true }))}
      ${field('Kind', selectHTML('kind', [
        { value: 'checking', label: 'Checking' },
        { value: 'savings', label: 'Savings' },
        { value: 'investment', label: 'Investment' },
        { value: 'cash', label: 'Cash' },
        { value: 'credit', label: 'Credit card' },
      ], 'checking'))}`,
    foot: `<span class="grow"></span><button type="button" class="btn" data-close>Cancel</button>
      <button type="button" class="btn btn-primary" data-save>Add</button>`,
  });
  m.el.querySelector('[data-save]').addEventListener('click', () => {
    const f = readForm(m.el);
    if (!f.name?.trim()) { toast('Give it a name.'); return; }
    commit((s) => { s.accounts.push({ id: uid('a'), name: f.name.trim(), kind: f.kind, archived: false }); }, 'account');
    m.close();
    toast('Account added.');
  });
}

export const actions = () => '';
export function mountActions() {}
