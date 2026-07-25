# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** Coverage
**Generated:** 2026-07-25 14:46:29
**Category:** Banking/Traditional Finance
**Design Dials:** Density 8/10 (Dense / Dashboard)

---

## ⚠️ PALETTE AND FONTS ARE DELIBERATELY OVERRIDDEN — READ THIS FIRST

The generator recommended a **dark** theme (`#0F172A` background, `#1E40AF`
primary, `#059669` accent) and a **Lexend / Source Sans 3** pairing. Both were
rejected on purpose. Do not reinstate them, and do not "fix" this file back
towards the generated values.

**Why.** Coverage already has a shipped visual identity — paper/ink/navy, light,
serif display — defined as CSS custom properties in
`coverage_web/static/css/coverage.css`. Adopting the generated palette would
have been an unrequested rebrand of a live product, not a UI pass: it would
have flipped a light product to dark, replaced a Fraunces/Instrument Sans
pairing that carries the brand's "considered, premium, not-a-startup" read, and
invalidated every contrast measurement already taken against the existing
tokens. The owner's stated preference is "minimal punchy copy, light premium
design", which the generated dark dashboard palette contradicts directly.

**What this file IS for.** Everything structural in the skill's output applies
and should be followed: the Data-Dense Dashboard pattern, the density
guidance, the component specs, the anti-patterns, and the pre-delivery
checklist at the bottom. Only the two visual-identity sections below were
swapped for Coverage's real tokens.

---

## Global Rules

### Color Palette — Coverage's real tokens

Source of truth: `coverage_web/static/css/coverage.css`. Use the variable, never
the hex, in component CSS.

| Role | Hex | CSS Variable |
|------|-----|--------------|
| Page background | `#f7f6f3` | `--paper` |
| Panels, cards, inputs | `#fffefc` | `--surface` |
| Primary text | `#1a1915` | `--ink` |
| Secondary text | `#52504a` | `--ink-2` |
| Faint text (small labels) | `#6e6a62` | `--ink-3` |
| Hairline border | `#dedad1` | `--line` |
| Emphasized border | `#b8b3a8` | `--line-strong` |
| Accent (navy) | `#1f4e79` | `--accent` |
| Accent hover/active | `#163a5b` | `--accent-ink` |
| Accent tinted surface | `#e9eef4` | `--accent-soft` |
| Accent border | `#cfdbe7` | `--accent-line` |
| Success | `#2f6a45` | `--ok` / `--ok-soft` / `--ok-line` |
| Danger | `#9d2b23` | `--danger` / `--danger-soft` / `--danger-line` |
| Warning / stale | `#7d5410` | `--stale-t` / `--stale-s` / `--stale-l` |

**Color notes:** Paper and ink, with a single navy accent. Light mode only —
there is no dark variant, so do not write `prefers-color-scheme` blocks.
Semantic colours come in text/soft/line triples; use the `-soft` fill behind the
matching text colour and the AA ratio comes out right (measured: `--ok` on
`--ok-soft` 5.6:1, `--danger` on `--danger-soft` 6.7:1, `--stale-t` on
`--stale-s` 6.0:1, `--accent-ink` on `--accent-soft` 9.7:1).

**Never signal by colour alone.** Every status in this product pairs its colour
with a word and an SVG glyph. This is a product about deadlines and confidence
levels; a student who cannot separate the red row from the amber one must still
be able to read which is which.

### Typography — Coverage's real stack

| Role | Family | CSS Variable |
|------|--------|--------------|
| UI / body | Instrument Sans | `--font-ui` |
| Display / headings | Fraunces (serif) | `--font-display` |
| Numerals, dates, countdowns | Spline Sans Mono | `--font-mono` |

Type scale — use the token, never a raw px value:

| Token | Size | Usage |
|-------|------|-------|
| `--fs-micro` | 11px | Uppercase badge labels only |
| `--fs-xs` | 12px | Metadata, dates |
| `--fs-s` | 13px | Small text, pill-adjacent |
| `--fs-m` | 15px | Body |
| `--fs-l` | 17px | Section headings (h2) |
| `--fs-xl` | 22px | Subpage titles |
| `--fs-xxl` | 29px | Top-level page titles |
| `--fs-hero` | 34px | Landing headline only |

Weights: `--w-reg` 400, `--w-med` 600, `--w-bold` 700, `--w-black` 720.
Line height: `--lh-body` 1.6, `--lh-tight` 1.25.

**Tabular figures.** Any number that changes in place — counts, countdowns,
fractions, percentages — takes `font-variant-numeric: tabular-nums`, so it does
not jiggle its own baseline when it updates.

### Spacing Variables — Coverage's real scale

*Density 8/10 (Dense / Dashboard) is the right dial for this product; the scale
below is the one already in the codebase and is what implements it.*

| Token | Value | Usage |
|-------|-------|-------|
| `--s1` | `4px` | Tight gaps |
| `--s2` | `8px` | Icon gaps, inline spacing |
| `--s3` | `12px` | Standard padding, grid gaps |
| `--s4` | `16px` | Card padding |
| `--s5` | `24px` | Section margins |
| `--s6` | `32px` | Between major sections |
| `--s7` | `48px` | Page-level rhythm |
| `--s8` | `64px` | Rare, hero only |

Radii: `--r-badge` 999px, `--r-ctl` 10px, `--r-panel` 16px.
Page widths: `--page-w` 960px, `--page-w-narrow` 680px, `--page-w-wide` 1120px,
`--page-w-full` 1440px.

### Shadow Depths — Coverage's real scale

Two levels only. Elevation in this product is a hover affordance, not decoration.

| Level | Value | Usage |
|-------|-------|-------|
| `--shadow-1` | `0 1px 2px rgba(26,25,21,0.05), 0 4px 14px rgba(26,25,21,0.06)` | Resting cards and panels |
| `--shadow-2` | `0 2px 4px rgba(26,25,21,0.06), 0 10px 28px rgba(26,25,21,0.09)` | Hover / raised |

### Motion

| Token | Value | Usage |
|-------|-------|-------|
| `--t-fast` | `120ms ease` | Colour and border changes |
| `--t-med` | `180ms cubic-bezier(0.2,0.6,0.2,1)` | Shadow, transform, disclosure |

Both sit inside the skill's 150–300ms micro-interaction window (`--t-fast` is
deliberately below it for pure colour swaps, which read as instant either way).
`coverage.css` already carries a global `prefers-reduced-motion` block that
neutralises durations and `:active` transforms — new components inherit it and
must not re-declare their own animations outside it.

---

## Component Specs

> **The hex values in the snippets below are the generator's, not Coverage's.**
> Read them for *structure* — padding rhythm, radius, which properties
> transition, where `cursor: pointer` belongs — and substitute the tokens from
> the palette above for every literal colour. Concretely: `#0F172A` → `var(--surface)`,
> `#1E40AF` → `var(--accent)`, `#E2E8F0` → `var(--line)`, `#DC2626` → `var(--danger)`,
> `var(--shadow-md)` → `var(--shadow-1)`, `var(--shadow-lg)` → `var(--shadow-2)`,
> `200ms ease` → `var(--t-med)`. A snippet pasted verbatim will put a dark card
> on a paper page.

### Buttons

```css
/* Primary Button */
.btn-primary {
  background: #059669;
  color: white;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}

.btn-primary:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

/* Secondary Button */
.btn-secondary {
  background: transparent;
  color: #1E40AF;
  border: 2px solid #1E40AF;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}
```

### Cards

```css
.card {
  background: #0F172A;
  border-radius: 12px;
  padding: 24px;
  box-shadow: var(--shadow-md);
  transition: all 200ms ease;
  cursor: pointer;
}

.card:hover {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
}
```

### Inputs

```css
.input {
  padding: 12px 16px;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  font-size: 16px;
  transition: border-color 200ms ease;
}

.input:focus {
  border-color: #1E40AF;
  outline: none;
  box-shadow: 0 0 0 3px #1E40AF20;
}
```

### Modals

```css
.modal-overlay {
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
}

.modal {
  background: white;
  border-radius: 16px;
  padding: 32px;
  box-shadow: var(--shadow-xl);
  max-width: 500px;
  width: 90%;
}
```

---

## Style Guidelines

**Style:** Data-Dense Dashboard

**Keywords:** Multiple charts/widgets, data tables, KPI cards, minimal padding, grid layout, space-efficient, maximum data visibility

**Best For:** Business intelligence dashboards, financial analytics, enterprise reporting, operational dashboards, data warehousing

**Key Effects:** Hover tooltips, chart zoom on click, row highlighting on hover, smooth filter animations, data loading spinners

### Page Pattern

**Pattern Name:** Real-Time / Operations Landing

- **Conversion Strategy:** For ops/security/iot products. Demo or sandbox link. Trust signals.
- **CTA Placement:** Primary CTA in nav + After metrics
- **Section Order:** 1. Hero (product + live preview or status), 2. Key metrics/indicators, 3. How it works, 4. CTA (Start trial / Contact)

---

## Anti-Patterns (Do NOT Use)

- ❌ Playful design
- ❌ Poor security UX
- ❌ AI purple/pink gradients

### Additional Forbidden Patterns

- ❌ **Emojis as icons** — Use SVG icons (Heroicons, Lucide, Simple Icons)
- ❌ **Missing cursor:pointer** — All clickable elements must have cursor:pointer
- ❌ **Layout-shifting hovers** — Avoid scale transforms that shift layout
- ❌ **Low contrast text** — Maintain 4.5:1 minimum contrast ratio
- ❌ **Instant state changes** — Always use transitions (150-300ms)
- ❌ **Invisible focus states** — Focus states must be visible for a11y

---

## Pre-Delivery Checklist

Before delivering any UI code, verify:

- [ ] No emojis used as icons (use SVG instead)
- [ ] All icons from consistent icon set (Heroicons/Lucide)
- [ ] `cursor-pointer` on all clickable elements
- [ ] Hover states with smooth transitions (150-300ms)
- [ ] Light mode: text contrast 4.5:1 minimum
- [ ] Focus states visible for keyboard navigation
- [ ] `prefers-reduced-motion` respected
- [ ] Responsive: 375px, 768px, 1024px, 1440px
- [ ] No content hidden behind fixed navbars
- [ ] No horizontal scroll on mobile
