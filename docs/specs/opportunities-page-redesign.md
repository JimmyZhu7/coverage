# Opportunities page — visual overhaul spec

Date: 2026-07-30. Architect: Fable. Status: implementing.

## The problem, measured against the field

Put next to the boards students actually rate (LinkedIn Jobs, Handshake,
Simplify, OpenQuant), this page reads as decorated rather than dense. Four
defects, in priority order:

1. **Three decorated bands before content.** Gradient hero card (~230px) +
   "Roles for you" block + a two-row filter zone + two stat lines + a
   full-width "Sorted for you" banner. On an 800px viewport the first role
   card is a full screen below the fold. The best boards reach results in
   ~120-160px: small title row, one filter band, content.

2. **No left rail.** Hero text is left-aligned; the ROLE TYPE legend and its
   pills are centered; row 2's five uppercase captions sit at five scattered
   x positions; the scope line, stat strip, and banner are centered again.
   The eye never gets a vertical line to return to. Clean boards are
   ruthlessly left-aligned: every band starts at the same x.

3. **Label noise.** Five uppercase micro-captions (PROGRAMME YEAR, REGION,
   TRACK, COMPANIES, SEARCH) each restate what their control already says
   ("Any Year (879)", "All Companies", placeholder "Firm, role, city").
   Modern boards let controls self-label; captions are for screen readers.

4. **A banner for four words.** "★ Sorted for you." occupies a full-width
   accent box. It is a status chip, not a section.

## The moves

**A. Hero → compact title row** (this page only; other pages keep the
marquee). Modifier class `kin-hero--compact` on the existing element:
no gradient card, no border/shadow/sheen, no icon, no eyebrow. Title drops
to ~26px, sub sits beside/below it, margin tightens. The h1 and sub text
are unchanged. Scoped in `_styles.html`, so Today/Network/Settings are
untouched.

**B. One left-aligned filter band.**
- Segmented pills left-aligned, compact (smaller padding/font). The
  `<legend>` becomes visually hidden — fieldset semantics stay, the pills
  self-describe.
- Row-2 captions wrapped in `<span class="f-cap">` and visually hidden.
  The caption text stays in the DOM (a11y + the pinned "Programme Year"
  string); the controls self-label.
- The Programme Year hint moves out of the label to a single footnote line
  under the whole bar, tied to the select with `aria-describedby`. Verbatim
  string preserved ("Intake year in the posting. Not a graduation year.") —
  it is a tested honesty contract. Inside the `<details>` so it still
  collapses on mobile.
- **Search leads.** CSS `order` puts Search first at ≥640px (DOM order is
  unchanged, so serialization, the mobile disclosure, and the no-JS path
  are untouched). Highest-value control gets the leftmost slot, wider.

**C. Stat lines → one quiet left rail.** Scope line and stat strip go
left-aligned, numbers drop from fs-l to fs-m, margins halve. DOM order is
pinned by tests (sentence < strip < cards) and unchanged. "Sorted for you"
restyles from banner to a small inline chip.

**D. Rhythm.** Hero margin s6→s4; filter band bottom margin tightens;
recommend bar margin s5→s4. Target: first firm card visible at ~500px
scroll on a 1440×800 viewport, from ~900px today.

## Hard constraints (all pinned by tests)

- htmx contract on the form: same URL/target/swap/triggers. DOM order of
  form controls unchanged — visual reorder is CSS `order` only.
- Radio group always has a checked member (mode-reset bug).
- Subset sentence above stat strip above firmcols; said once; no paywall
  vocabulary.
- `role="status"` exactly once, on the Open Roles figure.
- Programme Year hint ships verbatim at every breakpoint.
- "Sorted for you" renders iff personalized.
- `{# #}` comments never multi-line inside `<style>`.
