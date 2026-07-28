# CLAUDE.md — rules for this repo

This folder produces **one exact CV layout**. The layout is already
solved and measured. Your job is content, never geometry.

## The one rule

> **Edit `resume.json`. Never edit `template.css`. Never hand-write HTML.**

If asked to "change the format", ask first — that means editing
`template.css`, which breaks the reproducibility this kit exists for.

## Workflow

```bash
node build.mjs            # resume.json + template.css -> resume.html
node build.mjs --pdf      # also emits resume.pdf (needs: npm i puppeteer)
```

Then open `resume.html` and print: **Ctrl/Cmd+P → Destination: Save as PDF →
Margins: None → Background graphics: ON**. That produces a byte-for-byte
A4 page matching the reference.

## Files

| File | Role | Editable |
|---|---|---|
| `resume.json` | all content | **yes — this is the only file you touch** |
| `template.css` | locked geometry, type, colours | no |
| `build.mjs` | renderer | only to add a feature, never to restyle |
| `resume.html` | generated output | no — regenerate it |

## resume.json shape

```jsonc
{
  "meta":   { "documentTitle": "...", "outputFileName": "..." },
  "header": {
    "name": "Soma Charan",
    "contacts": [ { "text": "Email", "url": "mailto:..." }, { "text": "+91-..." } ],
    "contactSeparator": " | ",
    "tagline": "one italic line"
  },
  "sections": [
    {
      "title": "Work Experience",   // null = no heading, just rows
      "titleSize": "12pt",          // Education uses 10pt in the reference
      "spaceBefore": "0pt",         // extra gap above this section
      "rows": [ /* see below */ ]
    }
  ]
}
```

### Row types

All three column widths are fixed: **55pt dates / 452pt body / 57pt location**.

**Dated entry** (jobs, degrees) — col 1 takes one line per array item, so a
two-line date range lines up with the company line and the title line:

```jsonc
{
  "dates": ["Apr’26 -", "Jul’26"],
  "heading": "[**Company**](https://...) _(one-line descriptor)_",
  "subheading": "**Job Title**",
  "location": "Gurugram",
  "bullets": ["...", "..."]
}
```

**Labelled row** (Skills) — a bold label in col 1 instead of dates:

```jsonc
{ "label": "**Skills**", "bullets": ["...", "..."] }
```

**Full-width row** (Extra Curricular, Personal) — `wide: true` merges the
body and location columns; use `text` instead of `bullets` for a single
unbulleted line:

```jsonc
{ "label": "**Personal**", "wide": true, "text": "AI Enthusiast | ..." }
```

## Inline markup (works in every text field)

| Syntax | Renders as |
|---|---|
| `**bold**` | `<strong>` |
| `_italic_` | `<em>` |
| `**_bold italic_**` | both |
| `[label](https://…)` | link — Google-Docs blue `#1155cc`, underlined |
| `[**label**](url)` | bold link (this is how company names are done) |
| nested: `**text [x](url) more**` | link inside a bold run |

Links inside URLs are safe — underscores in a URL will not be eaten by the
italic parser.

## House style of this CV (keep it consistent)

- Company / institution names: **bold**, hyperlinked where a site exists.
- Company descriptor in parentheses: _italic_, on the same line.
- Job title: **bold**, on its own line under the company.
- Location: bold, centred in the right column, on the company line only.
- Bullets: lead with a verb, **bold the achievement or the metric**, leave
  the connective prose regular.
- Dates use a curly apostrophe: `Apr’26`, not `Apr'26`.
- Currency: type `₹` directly.

## Fitting one page

The reference fills roughly 790pt of the 842pt page. If content overflows:

1. cut or shorten bullets — always the first move;
2. drop the oldest role;
3. only then, as a last resort, ask the user before touching
   `--fs-body` or `--cell-pad` in `template.css`.

Never fix overflow by silently changing the CSS.

## Fonts

Calibri, with Carlito as the metric-identical open fallback:

```
Calibri, Carlito, "Segoe UI", Arial, sans-serif
```

Windows and Office machines have Calibri. On Linux: `apt install fonts-crosextra-carlito`.
On macOS without Calibri, install Carlito — otherwise line wraps will shift.
