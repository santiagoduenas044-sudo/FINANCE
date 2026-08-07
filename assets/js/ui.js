/* ==========================================================================
   ui.js — modals, action sheets, toasts, small form helpers
   ========================================================================== */

import { h, esc, icon, $$, currencySymbol } from './util.js';

/* --- toasts --------------------------------------------------------------- */

let toastHost = null;

export function toast(message, { action, onAction, ms = 4200 } = {}) {
  if (!toastHost) {
    toastHost = h('<div class="toasts" aria-live="polite"></div>');
    document.body.appendChild(toastHost);
  }
  const el = h(`<div class="toast">
    <span>${esc(message)}</span>
    ${action ? `<button type="button" class="toast-action" style="font-weight:600;text-decoration:underline;text-underline-offset:3px;pointer-events:auto">${esc(action)}</button>` : ''}
  </div>`);
  toastHost.appendChild(el);
  let done = false;
  const dismiss = () => {
    if (done) return;
    done = true;
    el.classList.add('out');
    setTimeout(() => el.remove(), 240);
  };
  el.querySelector('.toast-action')?.addEventListener('click', () => { onAction?.(); dismiss(); });
  setTimeout(dismiss, ms);
  return dismiss;
}

/* --- modals --------------------------------------------------------------- */

const stack = [];

/**
 * Open a modal.
 * @returns {{el:HTMLElement, body:HTMLElement, close:Function}}
 */
export function openModal({ title, body = '', foot = '', wide = false, onMount, onClose, dismissable = true }) {
  const prevFocus = document.activeElement;
  const scrim = h('<div class="scrim"></div>');
  const el = h(`
    <div class="modal${wide ? ' wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head">
        <h2>${esc(title)}</h2>
        <button type="button" class="icon-btn bare" data-close aria-label="Close">${icon('close')}</button>
      </div>
      <div class="modal-body">${body}</div>
      ${foot ? `<div class="modal-foot">${foot}</div>` : ''}
    </div>`);

  document.body.appendChild(scrim);
  document.body.appendChild(el);
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => { scrim.classList.add('on'); el.classList.add('on'); });

  const entry = { el, scrim, close };
  stack.push(entry);

  function close(result) {
    const i = stack.indexOf(entry);
    if (i >= 0) stack.splice(i, 1);
    el.classList.remove('on');
    scrim.classList.remove('on');
    setTimeout(() => { el.remove(); scrim.remove(); }, 220);
    if (!stack.length) document.body.style.overflow = '';
    document.removeEventListener('keydown', onKey, true);
    prevFocus?.focus?.();
    onClose?.(result);
  }

  function onKey(e) {
    if (stack[stack.length - 1] !== entry) return;
    if (e.key === 'Escape' && dismissable) { e.stopPropagation(); close(); }
    if (e.key === 'Tab') trapFocus(e, el);
  }
  document.addEventListener('keydown', onKey, true);

  el.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => close()));
  if (dismissable) scrim.addEventListener('click', () => close());

  const bodyEl = el.querySelector('.modal-body');
  wireAmountFields(el);
  onMount?.({ el, body: bodyEl, close });

  // focus the first sensible control
  requestAnimationFrame(() => {
    const first = el.querySelector('[data-autofocus], input:not([type=hidden]), select, textarea, .btn-primary');
    first?.focus?.();
    if (first?.select) try { first.select(); } catch {}
  });

  return { el, body: bodyEl, close };
}

function trapFocus(e, root) {
  const items = $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root)
    .filter((n) => n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

export function closeAllModals() { [...stack].reverse().forEach((s) => s.close()); }

/* --- confirm -------------------------------------------------------------- */

export function confirmDialog({ title, text, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    let decided = false;
    const m = openModal({
      title,
      body: `<p style="margin:0;color:var(--ink-2);font-size:14px;line-height:1.5">${esc(text)}</p>`,
      foot: `<span class="grow"></span>
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-yes>${esc(confirmLabel)}</button>`,
      onClose: () => { if (!decided) resolve(false); },
    });
    m.el.querySelector('[data-yes]').addEventListener('click', () => { decided = true; m.close(); resolve(true); });
  });
}

/* --- action sheet --------------------------------------------------------- */

/** A list of choices. Returns the chosen id, or null. */
export function actionSheet(title, options) {
  return new Promise((resolve) => {
    let picked = null;
    const rows = options.map((o) => `
      <button type="button" class="sheet-row" data-id="${esc(o.id)}"
        style="display:flex;align-items:center;gap:12px;width:100%;padding:13px 12px;border-radius:var(--r);text-align:left;transition:background .14s">
        <span style="width:32px;height:32px;flex:none;border-radius:10px;display:grid;place-items:center;background:var(--wash);color:var(--ink-2)">${icon(o.icon || 'chevronRight')}</span>
        <span style="min-width:0">
          <span style="display:block;font-size:14px;font-weight:550;letter-spacing:-.012em">${esc(o.label)}</span>
          ${o.hint ? `<span style="display:block;font-size:12.5px;color:var(--ink-3);margin-top:1px">${esc(o.hint)}</span>` : ''}
        </span>
        <span style="margin-left:auto;color:var(--ink-3)">${icon('chevronRight')}</span>
      </button>`).join('');
    const m = openModal({
      title,
      body: `<div style="display:flex;flex-direction:column;gap:2px;margin:-6px -6px">${rows}</div>`,
      onClose: () => resolve(picked),
    });
    m.el.querySelectorAll('.sheet-row').forEach((b) => {
      b.addEventListener('mouseenter', () => { b.style.background = 'var(--wash)'; });
      b.addEventListener('mouseleave', () => { b.style.background = ''; });
      b.addEventListener('click', () => { picked = b.dataset.id; m.close(); });
    });
  });
}

/* --- the big amount field ------------------------------------------------- */

/**
 * The oversized "how much?" input. A hidden mirror element sizes the grid cell
 * to the typed value, so the currency symbol stays glued to the number instead
 * of drifting to the edge of a full-width field.
 */
export function amountField(name, value = '', label = 'Amount', { autofocus = true, id = '' } = {}) {
  return `<div class="divide-amount">
    <span class="cur">${esc(currencySymbol())}</span>
    <span class="amt-field">
      <span class="amt-mirror" aria-hidden="true">${esc(String(value || '0'))}</span>
      <input type="number" step="0.01" min="0" inputmode="decimal" name="${esc(name)}"
        ${id ? `id="${esc(id)}"` : ''} ${autofocus ? 'data-autofocus' : ''}
        placeholder="0" aria-label="${esc(label)}" value="${esc(value)}">
    </span>
  </div>`;
}

export function wireAmountFields(root) {
  $$('.amt-field', root).forEach((f) => {
    const input = f.querySelector('input');
    const mirror = f.querySelector('.amt-mirror');
    if (!input || !mirror) return;
    const sync = () => { mirror.textContent = input.value || '0'; };
    input.addEventListener('input', sync);
    input.addEventListener('change', sync);
    sync();
  });
}

/* --- form helpers --------------------------------------------------------- */

export function field(label, control, hint = '') {
  return `<label class="field">
    <span class="lbl">${esc(label)}</span>
    ${control}
    ${hint ? `<span style="font-size:11.5px;color:var(--ink-3)">${esc(hint)}</span>` : ''}
  </label>`;
}

export const inputHTML = (name, opts = {}) => `<input class="input ${opts.cls || ''}" name="${esc(name)}"
  type="${opts.type || 'text'}" ${opts.step ? `step="${opts.step}"` : ''} ${opts.min != null ? `min="${opts.min}"` : ''}
  ${opts.max != null ? `max="${opts.max}"` : ''}
  value="${esc(opts.value ?? '')}" placeholder="${esc(opts.placeholder || '')}"
  ${opts.required ? 'required' : ''} ${opts.autofocus ? 'data-autofocus' : ''} ${opts.inputmode ? `inputmode="${opts.inputmode}"` : ''}>`;

export const selectHTML = (name, options, value) => `<select class="select" name="${esc(name)}">
  ${options.map((o) => `<option value="${esc(o.value)}" ${String(o.value) === String(value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
</select>`;

export const checkHTML = (name, label, checked) => `<label class="check">
  <input type="checkbox" name="${esc(name)}" ${checked ? 'checked' : ''}><span>${esc(label)}</span></label>`;

/** Read a form into a plain object. */
export function readForm(root) {
  const out = {};
  $$('input, select, textarea', root).forEach((el) => {
    if (!el.name) return;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
    else out[el.name] = el.value;
  });
  return out;
}

/* --- colour + icon pickers ------------------------------------------------ */

export function colorPicker(name, slots, value) {
  return `<div class="field">
    <span class="lbl">Colour</span>
    <div style="display:flex;flex-wrap:wrap;gap:7px">
      ${slots.map((s) => `
        <label style="cursor:pointer;position:relative;display:grid;place-items:center">
          <input type="radio" name="${esc(name)}" value="${esc(s.id)}" ${s.id === value ? 'checked' : ''} class="sr-only">
          <span data-swatch="${esc(s.id)}" title="${esc(s.name)}" style="width:26px;height:26px;border-radius:9px;background:var(--${s.id});
            box-shadow:0 0 0 ${s.id === value ? '2px var(--ink)' : '1px var(--hairline)'}, 0 0 0 ${s.id === value ? '4px var(--surface)' : '0'} inset;
            transition:box-shadow .15s;display:block"></span>
        </label>`).join('')}
    </div>
  </div>`;
}

export function iconPicker(name, names, value) {
  return `<div class="field">
    <span class="lbl">Icon</span>
    <div style="display:flex;flex-wrap:wrap;gap:6px">
      ${names.map((n) => `
        <label style="cursor:pointer">
          <input type="radio" name="${esc(name)}" value="${esc(n)}" ${n === value ? 'checked' : ''} class="sr-only">
          <span data-iconopt="${esc(n)}" style="width:32px;height:32px;border-radius:10px;display:grid;place-items:center;
            background:${n === value ? 'var(--ink)' : 'var(--wash)'};color:${n === value ? 'var(--ink-on-accent)' : 'var(--ink-2)'};
            transition:background .15s,color .15s">${icon(n)}</span>
        </label>`).join('')}
    </div>
  </div>`;
}

/** Wire the radio-based pickers so the visual state follows the selection. */
export function wirePickers(root) {
  root.addEventListener('change', (e) => {
    const el = e.target;
    if (el.type !== 'radio') return;
    root.querySelectorAll(`input[name="${el.name}"]`).forEach((r) => {
      const sw = r.nextElementSibling;
      if (!sw) return;
      const on = r.checked;
      if (sw.dataset.swatch) {
        sw.style.boxShadow = on ? '0 0 0 2px var(--ink)' : '0 0 0 1px var(--hairline)';
      } else if (sw.dataset.iconopt) {
        sw.style.background = on ? 'var(--ink)' : 'var(--wash)';
        sw.style.color = on ? 'var(--ink-on-accent)' : 'var(--ink-2)';
      }
    });
  });
}
