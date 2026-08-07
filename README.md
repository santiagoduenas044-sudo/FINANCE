# Ledger

A personal finance dashboard built around one habit: **when money comes in, you
decide where every dollar goes.** Everything else on screen — savings rate,
goals, forecasts, the health score — is worked out from that.

Runs entirely in your browser. No account, no server, no data leaving the
device. Installs to the home screen on iPhone and to the Dock on a Mac.

---

## Running it

**On a Mac (or any computer), from this folder:**

```bash
python3 -m http.server 8080
# then open http://localhost:8080
```

Any static server works. There is no build step and no dependencies — the app
is plain ES modules, CSS and SVG.

**As a single file.** `dist/ledger.html` is the whole app in one document
(fonts and icons included). Double-click it, AirDrop it, email it to yourself —
it works offline from `file://` with no server at all.

```bash
node build.js            # regenerate dist/ledger.html
node build.js --preview  # also write dist/preview.html (opens on sample data)
```

**On your iPhone.** Host the folder anywhere static (GitHub Pages, iCloud
Drive + a local server, Netlify…), open it in Safari, then Share → *Add to Home
Screen*. It then runs full-screen, works offline, and keeps its own data.

> Your data lives in this browser's local storage, on this device. It is not
> synced. Export a backup from **Settings → Your data** now and then — and
> before you clear your browser data.

---

## What is in it

**Home** — how much you have, where it is, what is safe to spend, and how much
you are saving and investing. Progress rings, a 12-month spending chart, an
allocation bar, a net worth tracker, a financial health score, insights, and a
3/6/12-month forecast.

**Buckets** — every category of money, with its monthly target, what went in,
what went out, what is left, its share of income and its balance history. Add,
rename, recolour, re-target, reorder (drag or ⌥←/⌥→) and delete freely.

**Goals** — a name, a target and a date. The app works out what is still
needed, how long is left, the monthly amount required, whether you are on track
at your current pace, and the likely finish date.

**Transactions** — date, description, amount, category, account, type.
Searchable and filterable; editing one recalculates everything.

**Investments** — contributions, current value, profit and loss, monthly
contributions and growth over time. Values only change when you update them —
nothing is estimated or fetched.

**History** — month against month: income, spending, savings, investments,
household contribution, savings rate, and the change versus the previous month.
Plus an end-of-month review.

**Settings** — currency and number format, appearance, your savings and
investment rate targets, emergency fund size, assumed investment return,
recurring bills, accounts, and backup/restore.

---

## How the numbers work

Deliberately simple. There are only four kinds of transaction:

| Type | What it means |
|---|---|
| **Income** | Money arrives. It lands in **Unallocated Money** until you divide it. |
| **Transfer** | Bucket → bucket. Dividing your income creates these. |
| **Expense** | Money leaves a bucket and is gone. |
| **Contribution** | Money leaves a bucket and becomes investment value. |

Everything else follows from those:

- **Bucket balance** — what came in, minus what went out.
- **Money remaining** — what is sitting in Unallocated Money.
- **Safe to spend** — everyday-bucket balances + undivided money, minus bills
  due before the end of the month that their bucket cannot cover.
- **Saved / invested this month** — money that moved into saving or investing
  buckets, minus anything taken back out.
- **Savings rate** — saved ÷ income, for the month.
- **Net worth** — cash across all buckets + what your investments are worth −
  debts.
- **Health score** — six habits weighted together: savings rate (25),
  investment rate (20), emergency fund (20), staying inside buckets (15),
  dividing your income (10), goals on track (10).

Nothing about your income or your percentages is hard-coded. The starter
buckets ship with suggested target percentages purely so there is something to
edit on day one — change any of them, and they never have to add up to 100%.

---

## Keyboard shortcuts

`1`–`7` jump between views · `i` record income · `e` record spending ·
`n` the add menu · `Esc` close a dialog.

---

## Layout of the code

```
index.html              app shell
manifest.webmanifest    home-screen install
sw.js                   offline cache for the app shell (never your data)
build.js                bundles everything into dist/ledger.html
assets/
  css/app.css           design tokens, layout, components — light and dark
  fonts/                Schibsted Grotesk + Azeret Mono, self-hosted
  js/
    util.js             formatting, dates, DOM helpers, the icon set
    store.js            state, local persistence, defaults, sample data
    model.js            every financial calculation, as pure functions
    charts.js           the SVG chart set
    ui.js               modals, sheets, toasts, form helpers
    flows.js            dividing income, spending, editing buckets and goals
    app.js              shell, routing, theme, onboarding
    views/              one module per screen
```

`model.js` is the place to look if you want to check the maths. It has no DOM
dependencies — every function takes the state and returns numbers.
