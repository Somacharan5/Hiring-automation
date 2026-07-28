# Resume kit — locked format, swappable content

Reproduces one exact A4 CV layout every time. Content lives in
`resume.json`; the geometry lives in `template.css` and never changes.

## Quickstart

```bash
node build.mjs        # -> resume.html
```

Open `resume.html` in Chrome → **Ctrl/Cmd+P** → Destination **Save as PDF**,
Margins **None**, Background graphics **on**. One A4 page, identical every time.

Optional, if you'd rather not touch the print dialog:

```bash
npm i puppeteer
node build.mjs --pdf  # -> resume.pdf
```

## Using it with Claude Code

`CLAUDE.md` sits in this folder and tells Claude the rules. Prompts that work:

- *"Add my new role at X to resume.json and rebuild."*
- *"Rewrite the Hike bullets to lead with metrics. JSON only."*
- *"Make a version tailored to an AI PM role — copy resume.json to resume-aipm.json, edit that, and build with `--data resume-aipm.json --out aipm.html`."*

Multiple tailored versions cost you nothing:

```bash
node build.mjs --data resume-aipm.json --out aipm.html
```

## The format, as measured

Everything below was measured off the original PDF, not guessed.

**Page** — A4, 595.28 × 841.89pt. Margins: top 25.5pt, left 28.5pt,
right 2.78pt, bottom 18pt. No borders or rules anywhere: the structure is a
borderless table, which is why it looks aligned without looking ruled.

**Header block** — 537.68pt wide so it centres on the *page*, while the grid
below runs 564pt wide and extends further right. Name 12pt bold, contact line
10pt with ` | ` separators, tagline 10pt italic. All three centred.

**The grid** — three fixed columns:

```
|<-- 55pt -->|<------------- 452pt --------------->|<- 57pt ->|
   dates          company / title / bullets           location
   (left)         (left)                              (centred, bold)
```

Cell padding 5pt on every side, which is what produces the 10pt gap between
entries. Full-width rows (Extra Curricular, Personal) merge columns 2 and 3.

**Type** — Calibri throughout, line-height 1.221 (Google Docs "single").
Body 9pt. Section headings bold: *Work Experience* 12pt, *Education* 10pt
(kept as-is from the original). Links `#1155cc`, underlined.

**Bullets** — Arial `●` at 8pt, smaller than the 9pt text. Marker at the
column's text edge with a 6.8pt hanging indent, so wrapped lines align under
the first character rather than under the dot.

## Files

```
resume.json     content — the only file you edit
template.css    locked layout
build.mjs       renderer (no dependencies)
CLAUDE.md       rules for Claude Code
resume.html     generated
```

## Note on fidelity

Two things were deliberately cleaned up from the original export: a handful of
stray empty paragraphs Google Docs left behind (the page now ends ~14pt higher,
giving you room for another bullet), and the missing space in `Email| +91-…`,
which is now a consistent ` | ` separator. Everything else — wrap points
included — matches line for line.

Calibri must be installed for the wraps to match. On Linux, Carlito is
metrically identical: `apt install fonts-crosextra-carlito`.
