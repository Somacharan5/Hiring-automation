#!/usr/bin/env node
/* ============================================================
   build.mjs — renders resume.json into resume.html
   ------------------------------------------------------------
   Usage:
     node build.mjs                 -> writes resume.html
     node build.mjs --pdf           -> also writes a PDF (needs puppeteer)
     node build.mjs --data other.json --out other.html

   Zero dependencies for the HTML path. Print resume.html from
   Chrome (Ctrl/Cmd+P -> Save as PDF, margins: None, background
   graphics: ON) to get a pixel-identical A4 page.
   ============================================================ */

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/* ---------- args ---------- */
const argv = process.argv.slice(2);
const arg = (flag, fallback) => {
  const i = argv.indexOf(flag);
  return i !== -1 && argv[i + 1] ? argv[i + 1] : fallback;
};
const DATA_PATH = resolve(arg('--data', join(HERE, 'resume.json')));
const OUT_PATH  = resolve(arg('--out',  join(HERE, 'resume.html')));
const CSS_PATH  = resolve(arg('--css',  join(HERE, 'template.css')));
const WANT_PDF  = argv.includes('--pdf');

/* ---------- load ---------- */
for (const p of [DATA_PATH, CSS_PATH]) {
  if (!existsSync(p)) { console.error(`Missing file: ${p}`); process.exit(1); }
}
const data = JSON.parse(readFileSync(DATA_PATH, 'utf8'));
const css  = readFileSync(CSS_PATH, 'utf8');

/* ============================================================
   Inline markup
   ------------------------------------------------------------
     **bold**          -> <strong>
     _italic_          -> <em>
     **_bold italic_** -> nested
     [text](url)       -> <a> (blue + underlined)
   Links are pulled out first so underscores inside URLs are safe.
   ============================================================ */

const esc = (s) => String(s)
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

function rich(src) {
  if (src === null || src === undefined || src === '') return '';
  const links = [];
  let s = String(src).replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    links.push({ label, url });
    return `\u0000${links.length - 1}\u0000`;
  });
  s = esc(s);
  s = s.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/_([\s\S]+?)_/g, '<em>$1</em>');
  s = s.replace(/\u0000(\d+)\u0000/g, (_m, i) => {
    const { label, url } = links[Number(i)];
    return `<a href="${esc(url)}">${rich(label)}</a>`;
  });
  return s;
}

/* ============================================================
   Renderers
   ============================================================ */

function renderHeader(h = {}) {
  if (!h.name && !h.contacts && !h.tagline) return '';
  const sep = esc(h.contactSeparator ?? ' | ');
  const contacts = (h.contacts || [])
    .map(c => (c.url ? `<a href="${esc(c.url)}">${esc(c.text)}</a>` : esc(c.text)))
    .join(sep);
  return [
    '<div class="header">',
    h.name    ? `  <div class="name">${rich(h.name)}</div>` : '',
    contacts  ? `  <div class="contact">${contacts}</div>`  : '',
    h.tagline ? `  <div class="tagline">${rich(h.tagline)}</div>` : '',
    '</div>'
  ].filter(Boolean).join('\n');
}

function renderRow(row) {
  const left = Array.isArray(row.dates)
    ? row.dates
    : (row.label ? [row.label] : (row.dates ? [row.dates] : []));

  const leftCell = `<td class="dates">${
    left.map(l => `<div class="line">${rich(l)}</div>`).join('')
  }</td>`;

  const body = [];
  if (row.heading)    body.push(`<div class="line">${rich(row.heading)}</div>`);
  if (row.subheading) body.push(`<div class="line">${rich(row.subheading)}</div>`);
  if (row.bullets && row.bullets.length) {
    body.push(`<ul>${row.bullets.map(b => `<li>${rich(b)}</li>`).join('')}</ul>`);
  }
  if (row.text) body.push(`<div class="line">${rich(row.text)}</div>`);

  const wide = row.wide === true;
  const bodyCell = `<td class="body${wide ? ' wide' : ''}"${wide ? ' colspan="2"' : ''}>${body.join('')}</td>`;
  const locCell  = wide ? '' : `<td class="location">${rich(row.location ?? '')}</td>`;

  return `<tr>${leftCell}${bodyCell}${locCell}</tr>`;
}

function renderSection(sec) {
  const out = [];
  const space = sec.spaceBefore ? ` style="margin-top:${esc(sec.spaceBefore)}"` : '';

  if (sec.title) {
    const size = sec.titleSize ? `font-size:${esc(sec.titleSize)};` : '';
    const style = (size || sec.spaceBefore)
      ? ` style="${size}${sec.spaceBefore ? `margin-top:${esc(sec.spaceBefore)}` : ''}"`
      : '';
    out.push(`<div class="section-title"${style}>${rich(sec.title)}</div>`);
  } else if (sec.spaceBefore) {
    out.push(`<div class="spacer"${space}></div>`);
  }

  out.push(
    '<table class="grid">',
    '  <colgroup><col class="c1"><col class="c2"><col class="c3"></colgroup>',
    '  <tbody>',
    ...(sec.rows || []).map(r => '    ' + renderRow(r)),
    '  </tbody>',
    '</table>'
  );
  return out.join('\n');
}

/* ============================================================
   Document
   ============================================================ */

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${esc(data.meta?.documentTitle ?? 'Resume')}</title>
<style>
${css}
.spacer { height: 0; }
</style>
</head>
<body>
<div class="page">
${renderHeader(data.header)}
${(data.sections || []).map(renderSection).join('\n')}
</div>
</body>
</html>
`;

writeFileSync(OUT_PATH, html, 'utf8');
console.log(`✓ ${OUT_PATH}`);

/* ---------- optional PDF ---------- */
if (WANT_PDF) {
  try {
    const { default: puppeteer } = await import('puppeteer');
    const pdfPath = OUT_PATH.replace(/\.html$/, '.pdf');
    const browser = await puppeteer.launch({ args: ['--no-sandbox'] });
    const page = await browser.newPage();
    await page.goto('file://' + OUT_PATH, { waitUntil: 'networkidle0' });
    await page.pdf({
      path: pdfPath,
      format: 'A4',
      printBackground: true,
      margin: { top: '0', right: '0', bottom: '0', left: '0' }
    });
    await browser.close();
    console.log(`✓ ${pdfPath}`);
  } catch (e) {
    console.error('PDF step skipped — install puppeteer (`npm i puppeteer`) or just print resume.html from Chrome.');
    console.error('  ' + e.message);
  }
}
