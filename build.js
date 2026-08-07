#!/usr/bin/env node
/* ==========================================================================
   build.js — produce dist/ledger.html: the whole app in one file.
   --------------------------------------------------------------------------
   The app itself is plain ES modules with no dependencies and needs no build
   step to run. This exists so the app can also be opened straight from disk
   (double-click, AirDrop, email attachment) where module loading over file://
   is blocked. Fonts and icons become data URIs; the module graph is flattened
   into a tiny CommonJS-style registry.

   Usage:  node build.js
   ========================================================================== */

const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const ENTRY = 'assets/js/app.js';

/* --- module graph --------------------------------------------------------- */

const modules = new Map();

function resolveSpec(fromFile, spec) {
  const abs = path.resolve(path.dirname(path.join(ROOT, fromFile)), spec);
  return path.relative(ROOT, abs).split(path.sep).join('/');
}

function transform(src, file) {
  const exported = [];
  let out = src;

  // import * as ns from '…'
  out = out.replace(/^[ \t]*import\s+\*\s+as\s+([A-Za-z0-9_$]+)\s+from\s+['"]([^'"]+)['"];?/gm,
    (_m, name, spec) => {
      const id = resolveSpec(file, spec);
      collect(id);
      return `const ${name} = __require(${JSON.stringify(id)});`;
    });

  // import { a, b as c } from '…'   (may span lines)
  out = out.replace(/^[ \t]*import\s*\{([\s\S]*?)\}\s*from\s*['"]([^'"]+)['"];?/gm,
    (_m, names, spec) => {
      const id = resolveSpec(file, spec);
      collect(id);
      const binding = names.split(',').map((s) => s.trim()).filter(Boolean)
        .map((s) => {
          const m2 = s.match(/^([A-Za-z0-9_$]+)\s+as\s+([A-Za-z0-9_$]+)$/);
          return m2 ? `${m2[1]}: ${m2[2]}` : s;
        }).join(', ');
      return `const { ${binding} } = __require(${JSON.stringify(id)});`;
    });

  // bare import '…'
  out = out.replace(/^[ \t]*import\s+['"]([^'"]+)['"];?/gm, (_m, spec) => {
    const id = resolveSpec(file, spec);
    collect(id);
    return `__require(${JSON.stringify(id)});`;
  });

  // export { a, b as c }
  out = out.replace(/^[ \t]*export\s*\{([^}]*)\}\s*;?/gm, (_m, names) => {
    names.split(',').map((s) => s.trim()).filter(Boolean).forEach((s) => {
      const m2 = s.match(/^([A-Za-z0-9_$]+)\s+as\s+([A-Za-z0-9_$]+)$/);
      exported.push(m2 ? [m2[2], m2[1]] : [s, s]);
    });
    return '';
  });

  // export function|const|let|var|class|async function NAME
  out = out.replace(/^[ \t]*export\s+(async\s+function|function\*?|const|let|var|class)\s+([A-Za-z0-9_$]+)/gm,
    (_m, kw, name) => { exported.push([name, name]); return `${kw} ${name}`; });

  if (/^\s*export\s/m.test(out)) {
    throw new Error(`Unhandled export syntax in ${file}:\n  ${out.match(/^\s*export\s.*/m)[0]}`);
  }
  if (/^\s*import\s/m.test(out)) {
    throw new Error(`Unhandled import syntax in ${file}:\n  ${out.match(/^\s*import\s.*/m)[0]}`);
  }

  const tail = exported.map(([name, local]) => `  __exports[${JSON.stringify(name)}] = ${local};`).join('\n');
  return `${out}\n${tail}\n`;
}

function collect(file) {
  if (modules.has(file)) return;
  modules.set(file, null); // reserve, so cycles terminate
  const src = fs.readFileSync(path.join(ROOT, file), 'utf8');
  modules.set(file, transform(src, file));
}

/* --- assets --------------------------------------------------------------- */

const dataURI = (file, mime) =>
  `data:${mime};base64,${fs.readFileSync(path.join(ROOT, file)).toString('base64')}`;

function inlineCSS() {
  let css = fs.readFileSync(path.join(ROOT, 'assets/css/app.css'), 'utf8');
  css = css.replace(/url\(['"]?\.\.\/fonts\/([^'")]+)['"]?\)/g,
    (_m, name) => `url(${dataURI(`assets/fonts/${name}`, 'font/woff2')})`);
  return css;
}

/* --- build ---------------------------------------------------------------- */

collect(ENTRY);

const bundle = `(function () {
'use strict';
var __defs = {};
var __cache = {};
function __require(id) {
  if (__cache[id]) return __cache[id].exports;
  var mod = __cache[id] = { exports: {} };
  if (!__defs[id]) throw new Error('Module not found: ' + id);
  __defs[id](mod.exports);
  return mod.exports;
}
${[...modules.entries()].map(([file, code]) => `
__defs[${JSON.stringify(file)}] = function (__exports) {
${code}
};`).join('\n')}

__require(${JSON.stringify(ENTRY)});
})();`;

const preview = process.argv.includes('--preview');

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Ledger — Personal Finance</title>
<meta name="description" content="Divide every dollar of income into buckets, then see exactly how much you have, where it is, and what is safe to spend.">
<meta name="theme-color" content="#0B0C0E">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Ledger">
<link rel="apple-touch-icon" href="${dataURI('assets/icon-180.png', 'image/png')}">
<link rel="icon" href="${dataURI('assets/icon.svg', 'image/svg+xml')}" type="image/svg+xml">
<style>
${inlineCSS()}
</style>
</head>
<body>
<div id="app"></div>
<script>
${bundle}
</script>
</body>
</html>
`;

/* The preview build drops the document skeleton (its host supplies one) and
   opens on sample data, so the design can be reviewed without typing anything in. */
const previewHTML = `<title>Ledger — Personal Finance</title>
<style>
${inlineCSS()}
</style>
<div id="app"></div>
<script>
window.__LEDGER_PREVIEW__ = true;
${bundle}
</script>
`;

fs.mkdirSync(path.join(ROOT, 'dist'), { recursive: true });
fs.writeFileSync(path.join(ROOT, 'dist/ledger.html'), html);

const kb = (n) => `${(n / 1024).toFixed(0)} KB`;
console.log(`dist/ledger.html — ${kb(Buffer.byteLength(html))} (${modules.size} modules inlined)`);

if (preview) {
  fs.writeFileSync(path.join(ROOT, 'dist/preview.html'), previewHTML);
  console.log(`dist/preview.html — ${kb(Buffer.byteLength(previewHTML))}`);
}
