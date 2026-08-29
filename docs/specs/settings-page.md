# Settings Page — Audit and Spec

*Authored 2026-07-30. Inputs: full read of `accounts/{views,forms,services,models}.py`,
`templates/accounts/settings.html`, `crm/views.py`, `crm/coverage.py`, `capture/*`,
`coverage_domain/cadence.py`, settings modules, live page + allauth routes inspected in the
browser against the founder account (137 contacts / 129 touches / 25 archived), and read-only
DB queries. Product posture per `docs/product-brief.md` and `docs/build-plan.md`.*

**Scope note:** the Profile photo field (upload / preview / drag-drop / server-side square
crop / EXIF strip / 8 MB cap) was just reworked and is done. Nothing in this spec touches it.

---

## Part 1 — Audit findings, ranked

### 1. Account-level basics exist but are orphaned and off-brand (MUST)

The founder account signs in with an email + password (`has_usable_password() == True`, zero
`SocialAccount` rows). django-allauth is mounted at `/accounts/` and therefore these routes
**already work**:

- `/accounts/password/change/` — change password
- `/accounts/email/` — add/verify/change email addresses
- `/accounts/3rdparty/` — connected social accounts (Google/Apple/Microsoft/LinkedIn
  providers are conditionally registered in `settings/base.py`)

Verified in the browser: `/accounts/password/change/` renders **allauth's raw default
template** — no Coverage shell, black text on the dark background, a bare `<ul>` "Menu:"
with Change Email / Account Connections / Sign Out links. Only `login / logout / signup /
password_reset*` have Coverage templates (`templates/account/`). Nothing on the Settings
page links to any of these. A real product's most basic account controls exist here as
unreachable, broken-looking pages. Settings has no Account section at all.

### 2. The export overclaims — "all your data" is two of nine tables (MUST)

Settings copy: *"Bring contacts in from a CSV, or download all your data."* The privacy
policy goes further: *"export everything as CSV at any time."* What `/welcome/export/`
actually serves (`accounts/services.py`):

- `contacts.csv` — 12 columns, but **omits** `linkedin`, `school`, `school_affiliation`,
  and `archived` (25 of the founder's 137 contacts are archived; the export can't
  distinguish them, so a round-trip resurrects archived people as active)
- `touches.csv` — complete

**Not exported at all:** target firms + tiers (`user_firms`), tracked applications +
interview dates (`user_opportunities`), tasks, capture events, fit scores, the profile
itself (angles, work authorization, cadence overrides). For a product whose brand is "your
data is yours, honestly," the export saying "everything" while shipping two files is
exactly the over-claim class the project keeps fixing (the "New" badge, the derived class
year, the ignored setting).

### 3. The Language section saves a value nothing reads (MUST — remove it)

`User.language` is written by the Settings POST and read back **only to re-render the same
dropdown**. There is no `LocaleMiddleware`, no `django.utils.translation.activate()` call,
no `{% trans %}`/`{% load i18n %}` anywhere in `coverage_web/templates/`, and no translation
catalogs. Choosing 中文 changes nothing. This is precisely the defect the codebase's own
comments warn about ("a settings page that happily saves a value the engine then ignores"
— `accounts/forms.py:35`). Cut the section until an i18n pass exists. Keep the column
(harmless, already populated with `"en"`).

### 4. The capture address is a leakable shared secret with no rotation (MUST)

`u-<slug>@in.coverage.app` identifies the user to the inbound webhook
(`capture/services.py` resolves `User.objects.get(capture_slug=slug)` from the recipient
list). Anyone who learns the address — a forwarded email, a screenshot, a BCC visible to a
recipient's "reply all" — can send mail to it and inject capture events, pending contacts,
and (via the deterministic extractors) touches into that student's private CRM. Today the
only remedy is deleting your account. Rotation is structurally safe: nothing durable stores
the address (mailto links are generated per click; `capture_events.provider_ref` is a
Message-ID, not the slug), and an unknown slug simply fails to resolve. A regenerate
control is cheap and closes a real hole.

### 5. Business-day math runs on UTC's day, and no one can change that (MUST)

`TIME_ZONE = "UTC"` and every "today" in the product is `timezone.localdate()`
(`crm/views.py:328,559,708`) — i.e. UTC's date. The stated audience is **HK and US**
students. For an HK student (UTC+8), the cadence queue, the pace-ring week, follow-up
windows, and "app closes in N days" all roll over at **8 a.m. their time**, and their
Sunday-evening logging lands on the wrong week. `coverage_domain/cadence.py` takes an
explicit as-of date, so it honours whatever day the web layer passes — the fix is a
per-user timezone plus a tiny activation middleware, and every `localdate()` call site is
correct automatically. There is currently no field, no control, and no honest label saying
"days are UTC days."

### 6. Email Capture section under-serves its own machinery (SHOULD)

The card shows the address + Copy, nothing else — while `/capture/health/` (last received,
counts by status) and `/capture/review/` (needs_review queue) already exist and are
reachable only from the top nav. The founder's row: **0 capture events ever**. When a real
student's BCC silently fails (risk register #3), Settings — the page they'll check — says
nothing. The health facts should be surfaced here (or at minimum linked), and the section
should honestly show "Nothing received yet" state.

### 7. A user-facing engine parameter has no control: advocate target (SHOULD)

`crm/coverage.py:advocate_target()` reads `User.assets["advocate_target"]` (default 2) —
the advocates-per-firm yardstick that drives gap states and the Network axis of the firm
fit score. The founder's row carries the key (ported by cutover); no UI can set it. The
read side is already defensive (falls back unless `int >= 1`), so a control is honoured
today. Belongs beside the other engine knobs in Cadence.

8. **Sign out everywhere** (SHOULD) — sessions are DB-backed; there is no "sign out other
   devices" control and no session visibility. Cheap, standard, and a trust feature for an
   audience logging in from library computers.
9. **"Your Data" counts are slightly dishonest** (SHOULD) — "Contacts: 137" counts archived
   rows, while the Network page shows 112. Say "137 (25 archived)" and link the archived
   list (`/app/contacts/archived/` exists and is good).
10. **Notification preferences are correctly absent — keep it that way for now.** The
    Monday digest (build-plan §7 M4) is **not built**: `digest_sent/digest_opened` exist
    only as reserved event names; outbound email is configured for password resets only.
    Shipping a digest toggle before the digest would be the inverse over-claim. The day the
    digest ships, a Notifications control + unsubscribe link ship **with it** (see LATER).
11. **Delete page misses the backup caveat** (SHOULD) — build-plan §10: "state the
    backup-expiry window." The delete page says "no undo, no waiting period" but doesn't
    mention that DB backups persist briefly. One honest sentence, once the deploy target's
    backup retention is known.

### What is honest today and must not be wrecked

- **Blank means UNSET, never a guessed default.** Work-auth stores nothing for an
  unanswered region ("stays honestly unknown — never guessed either way"); clearing a
  cadence field removes the override; empty angles removes the key; blank pace = NULL =
  product default (a stored 0 would behave like NULL, so the form floors at 1). Every new
  control must follow this.
- **The section-marker guard.** Every form POSTs a hidden `section` value; an unmarked POST
  is a no-op, not a silent profile wipe (`accounts/views.py:283-293`). Keep for all new forms.
- **Cadence's single source of truth.** Ranges live in `crm.views.TUNABLE_CADENCE_PARAMS`
  (the point of use); the form imports them; out-of-range stored values are dropped on read
  AND on form init. `max_cold_touches` is capped at (1, 2) to make "never a second
  follow-up" structural — do not widen (staged windows were tried and reverted 2026-07-28).
- **The stale cycle survives** (`ProfileForm.__init__`): a stored `target_cycles` value that
  rolled off the choices is appended back, checked and enabled, labelled "no longer
  offered", instead of silently clearing. Enabled, not disabled: a disabled checkbox is
  dropped from the POST, which is the silent clear itself.
- **Each section saves independently** with PRG; a failing section re-renders only itself.
- **Capture copy claims only what's true**: "Your Private BCC Capture Address" — no
  "connects to Gmail" language anywhere (deploy.md §4 gate). Keep it that way.
- **Delete is a real hard delete**: type-your-email confirm, honest itemised list,
  transactional per-table deletes scoped `.for_user()`, allauth rows swept by cascade.

---

## Part 2 — Research

Products behind auth walls (Simplify, Huntr, Teal apps) were assessed via their own help
centers rather than live UI; grouping patterns below are from vendor docs.

**Grouping.** LinkedIn splits settings into six top-level categories — Account preferences,
Sign in & security, Visibility, Data privacy, Advertising data, Notifications
([LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a1337839/managing-your-account-and-privacy-settings-overview?lang=en)).
Notion separates the personal cluster (Account with email/security, Notifications,
Settings/preferences) from workspace administration
([Notion Help](https://www.notion.com/help/account-settings),
[notification settings](https://www.notion.com/help/notification-settings)). Linear nests
personal Account / Preferences / Security & Access apart from workspace admin, with
connected devices and applications under Security & Access
([Linear docs — Preferences](https://linear.app/docs/account-preferences),
[Security](https://linear.app/docs/security-and-access)). The consistent pattern: **"who
you are" and "how the product behaves" are separated from "how you get in and what we hold
on you."** Ten flat cards is fine at this count *if the rail communicates those groups.*

**Destructive actions.** GitHub's Danger Zone is the category norm: destructive controls
quarantined at the bottom, each gated by a type-to-confirm where the button stays disabled
until the typed value matches
([DataCamp on GitHub's flow](https://www.datacamp.com/tutorial/how-to-delete-a-github-repository),
[Zapier](https://zapier.com/blog/github-delete-repository/)). Coverage's delete page
already matches this (server-side match on the typed email). Keep destructive actions on
their own confirm page, never a one-click on the settings page itself.

**Delete flows in the direct comparators.** Simplify: Settings → Account Management →
Delete Account, permanence stated, deactivate offered as the softer alternative
([Simplify Help](https://help.simplify.jobs/articles/8765497-how-to-deactivate-or-delete-your-simplify-account)).
Teal: delete from account settings with confirmation; help docs explicitly tell users to
**export before deleting** because deletion is unrecoverable
([Teal Knowledge Base](https://help.tealhq.com/en/articles/9457676-deleting-your-account)).
Coverage already links export from the delete page — matching the best practice.

**Export.** Huntr's "Download My Data" is the model for the category: **one ZIP of multiple
CSVs, one per data type** (Job Data, Activity Data, Profile Data, Contacts Data), from
Personal Account Settings
([Huntr Help Center](https://help.huntr.co/en/articles/10503230-account-faq-and-account-settings),
[Download/Export Your Board](https://help.huntr.co/en/articles/11757717-download-export-your-board)).
Teal exports per-surface (tracker CSV, resumes as PDF). For a product that stores a
student's private relationship graph, Huntr's everything-in-one-ZIP is the right bar; a
partial export labeled "all your data" is below it.

**Notifications.** LinkedIn and Notion both give notifications their own top-level section
with per-type toggles ([Notion](https://www.notion.com/help/notification-settings)). Nobody
ships a notification section with zero notification types — which supports deferring
Coverage's until the digest exists.

**The finance-student tools set a low bar.** The Trackr's product is tracker tables + paid
alerts ([the-trackr.com](https://the-trackr.com/)); OffCycle is a feed + HR-contact unlock
([WSO thread](https://www.wallstreetoasis.com/forum/investment-banking/offcycle-internship-tracker)).
Neither markets any settings/data surface. A genuinely honest export/delete/rotation story
is a *differentiator* with this audience, not table stakes — consistent with the brief's
"trust with this audience depends on the tracker being real."

---

## Part 3 — Design spec

### A. Section list and order

Keep the one-page, sticky-rail, independent-card architecture (it works, and the section
count stays manageable). Introduce **rail group headers** and reorder. Final structure:

```
YOU
  1. Profile                (existing; + Timezone field, see B1)
  2. Work Authorization     (existing, unchanged)
  3. Outreach Assets        (existing, unchanged)
HOW COVERAGE PACES YOU
  4. Cadence                (existing; + Advocate Target row, see B4)
  5. Weekly Pace            (existing, unchanged)
EMAIL CAPTURE
  6. Email Capture          (existing; + health facts + Regenerate, see B2)
ACCOUNT
  7. Sign-In & Security     (NEW, see B3)
  8. Your Data              (existing; counts fixed, export completed, see B5)
  9. Legal                  (existing, unchanged)
 10. Danger Zone            (existing; copy addition, see C)
```

Moves and cuts, justified:

- **Language: CUT** (audit #3). Dead control. Re-add only with a real i18n pass (LATER).
- **Sign-In & Security: NEW**, placed at the head of the Account group — the audit's #1
  gap. Mirrors LinkedIn's "Sign in & security" and Linear's "Security & Access".
- **Cadence and Weekly Pace stay separate cards** but sit under one group header. Merging
  them into one card was considered and rejected: they save independently today, the
  failure-isolation of per-section POSTs is worth keeping, and a merged card would be the
  longest on the page.
- **Timezone lives inside Profile**, not its own section — it is a fact about the student,
  and Profile is where school/class-year/regions already live.
- **Danger Zone stays last, visually quarantined** (GitHub pattern; already true).

Rail markup: add `.settings-nav-group` header elements (same styling recipe as the existing
`.settings-nav-title`). On mobile (≤820px) the rail already collapses to wrapped chips —
group headers are `display: none` there, as the title is today.

### B. New and changed controls

Every control below follows the house rules: hidden `section` marker, PRG on success,
blank = unset, validation errors re-render only the owning section, and the read side
independently guards against bad stored values.

#### B1. Timezone (Profile card, new field)

- **Stores:** `User.timezone`, `CharField(max_length=64, blank=True, default="")` — a new
  column. Value is an IANA zone name validated against `zoneinfo.available_timezones()`.
  Blank means **unset**.
- **Widget:** one `<select>`. A curated shortlist first (Hong Kong, US Eastern/Central/
  Mountain/Pacific, London, Singapore — the six-market region vocabulary Coverage already
  uses), then an "All timezones" optgroup with the full sorted zoneinfo list. No JS needed.
- **What reads it:** a new ~15-line middleware: if `request.user.is_authenticated` and
  `user.timezone`, call `django.utils.timezone.activate(ZoneInfo(user.timezone))`, else
  `deactivate()` (falls back to UTC). Every existing `timezone.localdate()` call site
  (Today's as-of date, the pace week, snooze checks) becomes correct with **zero changes**;
  `coverage_domain/cadence.py` already takes the resulting date as an explicit parameter.
- **Default / unset behaviour:** UTC, and the field's hint says so honestly:
  *"Unset: Coverage uses UTC days, so 'today' rolls over at midnight UTC."* Do not guess
  from `regions` — a guessed timezone silently moving someone's week boundary is the exact
  bug class this page exists to avoid.
- **Onboarding is untouched** (this is not an onboarding step; the honest UTC default is
  fine until the user cares).

#### B2. Email Capture (rework the existing card)

Three rows replace the bare address line:

1. **Your capture address** — the existing `code` + Copy button, unchanged, plus one hint
   line: *"BCC this on outreach, or forward replies to it. Treat it like a private
   address: anyone who has it can log mail into your CRM."*
2. **Status** — read `capture/services.capture_health(user)` (already exists; costs two
   aggregate queries): "Last received {timesince}" or, honestly, *"Nothing received yet.
   Send yourself a test BCC to check the loop."* If `counts.needs_review > 0`, show it with
   a link to `/capture/review/`. Link "Full capture health" → `/capture/health/`.
3. **Regenerate address** — a plain (non-danger-styled but consequential) button → its own
   confirm page `/welcome/capture/regenerate/` (GET shows consequences, POST executes;
   same page pattern as delete):
   - **Does:** assign a fresh `_generate_capture_slug()` (retry on the unique constraint),
     save, record `product_event("capture_address_regenerated")`, redirect to Settings with
     a success flash showing the **new** address.
   - **Copy on the confirm page:** *"Your current address stops working immediately.
     Anything sent to it is ignored — not forwarded, not queued. Update any Gmail filter or
     saved BCC that uses the old address. Past captured activity is untouched."*
   - **Reversibility:** none (old slug is gone); say so. This is deliberate — a leaked
     address must die.
   - Safe because nothing durable stores the rendered address (verified: mailto links are
     built per request; `capture_events.provider_ref` is a Message-ID; unknown slugs fail
     resolution harmlessly in `capture/services.py:85-90`).

#### B3. Sign-In & Security (new card)

All rows are links/buttons to dedicated pages — no inline password fields on Settings.

1. **Email** — display `user.email` with a "Verified" tick when the primary
   `EmailAddress.verified` is true. Button "Manage email" → `/accounts/email/`.
   **Requires styling the allauth template** (`templates/account/email.html`) in the
   Coverage shell — the route already works. Recommend also setting
   `ACCOUNT_CHANGE_EMAIL = True` so allauth runs a single-address change flow (add new →
   verify → old replaced) instead of exposing a multi-address list a student doesn't need.
   Note for the builder: email is the USERNAME_FIELD; allauth keeps `user.email` in sync
   with the primary verified address.
2. **Password** — if `user.has_usable_password()`: "Change password" →
   `/accounts/password/change/` (style `templates/account/password_change.html`). Else:
   "Set a password" → `/accounts/password/set/` with the hint *"You currently sign in with
   {provider or 'a magic link'}."* Never show a password row that implies a password exists
   when it doesn't.
3. **Connected accounts** — "Manage" → `/accounts/3rdparty/` (style
   `templates/socialaccount/connections.html`). **Render this row only when at least one
   social provider is actually configured** (`settings/base.py` already computes the active
   provider list from env client-ids) — an empty connections page is a broken promise.
4. **Sign out everywhere** — POST button (own `section` marker or dedicated URL
   `/welcome/security/signout-all/`), confirm inline via a standard confirm page. Server:
   iterate `Session.objects.all()`, `.get_decoded().get('_auth_user_id')`, delete matches
   except `request.session.session_key`. DB-backed sessions make this exact. Success flash:
   "Signed out on all other devices." (At current single-digit session counts a full-table
   scan is fine; note in code to revisit if sessions grow.)

#### B4. Advocate Target (new row in the Cadence card)

- **Stores:** `User.assets["advocate_target"]` — the key `crm/coverage.py` already reads.
  Same copy-then-set discipline as `OutreachAssetsForm.apply_to` (own exactly this key).
- **Form:** IntegerField, `min 1, max 5`, blank = remove the key = product default (2).
  Placeholder shows the default, matching the other cadence rows. Range rationale: the read
  side rejects `< 1`; above 5 the gap ladder is unreachable for any real student — clamp
  and say so in the error message.
- **Label/desc:** "Advocate Target — advocates per firm before Coverage calls that firm
  covered. Feeds the gap ladder on Network and the network axis of your firm fit score.
  Default: 2."
- **What reads it:** `crm/coverage.advocate_target()` (guarded), `crm/views.py:785,1170`.
  Honoured today with no engine change.

#### B5. Your Data (fix the existing card)

1. **Counts row:** "Contacts — 137 (25 archived)" with the archived count linking to
   `/app/contacts/archived/`. Query is `filter(archived=True).count()` on the already-scoped
   queryset.
2. **Copy fix (MUST, one line):** replace "download all your data" with words that are true
   the day they ship. If the export still covers contacts + touches, say *"export your
   contacts and touch history."*
3. **Complete the export (SHOULD):** adopt the Huntr model — one "Download everything
   (.zip)" built with stdlib `zipfile` + the existing csv writers, containing:
   `contacts.csv` (add `linkedin`, `school`, `school_affiliation`, `archived` columns),
   `touches.csv` (as is), `firms.csv` (firm, tier, status), `applications.csv`
   (opportunity title/firm/status, interview dates), `tasks.csv`,
   `capture_events.csv` (occurred_at, direction, counterparty, status — no raw MIME),
   `profile.csv` (one row: school, class year, cycle, regions, tracks, work auth, angles,
   cadence overrides, weekly goal). Keep the two individual CSV buttons. Fit scores can be
   omitted from v1 of the ZIP (derived data, recomputable) — but then the export page must
   not claim to include them. Update the privacy policy's "export everything" line in the
   same change so page and policy agree.

### C. Destructive actions

Inventory after this spec: **Delete account** (existing), **Regenerate capture address**
(new), **Sign out everywhere** (new, mildly destructive).

Shared pattern (matches GitHub/Simplify/Teal findings and the existing delete page):

- Never destructive on click within Settings. Each action gets a server-rendered confirm
  page (no JS-only modals; htmx optional but the no-JS path must work).
- The confirm page states, in plain bullets: what is removed, what survives, and whether
  there is an undo. No euphemisms; "cannot be undone" only where literally true.
- Type-to-confirm reserved for the account delete (typed email, server-validated —
  existing behaviour, keep). Regenerate and sign-out-everywhere are one honest
  confirm-button (their blast radius is recoverable-by-action: re-saving a filter,
  re-logging-in).
- **Delete account, two additions:**
  1. After the backup-retention window of the deploy target is known, add one sentence:
     *"Encrypted database backups expire within N days; nothing restores your data after
     that."* Do NOT ship a guessed N — flagged LATER until deploy.md pins it.
  2. Surface the per-table counts `delete_user_and_data` already returns in the final
     flash on the logged-out landing ("Deleted 137 contacts, 138 touches, …") — honest
     receipts, zero new queries. (Nice-to-have.)
- Danger Zone card itself stays exactly one row (Delete). Regenerate belongs in Email
  Capture (it protects data; it doesn't destroy the account), Sign out everywhere in
  Sign-In & Security. Quarantining everything scary in one bucket would bury the capture
  rotation where nobody looks for it.

### D. Honesty rules — what this page must never imply

1. **No control ships before its reader.** A stored value with no consumer is forbidden
   (this kills Language, and it is the reason the Notifications section waits for the
   digest). Conversely, no engine parameter that is user-facing should be Settings-invisible
   (this adds Advocate Target).
2. **Blank is unset, and unset is labeled.** Every optional control states what happens
   when empty ("uses the default of 10", "stays honestly unknown", "Coverage uses UTC
   days"). Never render a default value INTO an input — placeholders only.
3. **Counts mean what they say.** Any number on the page states its population ("25
   archived" split out; capture "Nothing received yet" rather than an empty-looking zero).
4. **Export copy enumerates.** "Everything" is only written when it is everything; the
   export page lists the files and their columns' spirit, not a vibe.
5. **Capture claims stay pre-OAuth.** No "connect", "sync", or "Gmail" language anywhere in
   the capture card or onboarding echo (deploy.md §4 gate). The address is described as
   what it is: an inbound mailbox you must send things to.
6. **Security rows reflect the actual account.** "Change password" only when a usable
   password exists; connections row only when providers are configured; verified badge only
   from `EmailAddress.verified`.
7. **The engine's caps are visible, not negotiable.** Cadence inputs keep min/max mirrored
   from `TUNABLE_CADENCE_PARAMS`; `max_cold_touches` stays (1, 2); an out-of-range stored
   value is dropped, never displayed as if honoured.

### E. Responsive + accessibility

Breakpoints (existing tokens; no rebrand; grid already collapses at 820px):

- **375** — single column. The `.set-row` flex (label left, control right) must stack:
  add `@media (max-width: 560px) { .set-row { flex-direction: column; align-items:
  stretch; } .set-row-control { justify-content: flex-start; } }` so selects and number
  inputs don't get crushed beside long labels. The capture address `code` needs
  `overflow-wrap: anywhere` (24-char localpart + domain). Copy/Regenerate buttons ≥44px
  tap targets. Rail chips wrap (existing behaviour) — group headers hidden.
- **768** — still the single-column ≤820 layout; chips in one or two rows. Verify the new
  Sign-In & Security rows (link buttons) right-align without wrapping mid-label.
- **1024** — two-column grid active (240px rail + fluid main, max 1160px). Sticky rail must
  not overlap the taller rail (12 items + 4 group headers): confirm total rail height fits
  in ~700px viewport minus the 88px offset; if not, rail gets `max-height:
  calc(100vh - 100px); overflow-y: auto`.
- **1440** — capped at 1160px, centered (existing). No changes.

Accessibility (fix as part of this work — some of this is broken today):

- **Programmatic labels are missing.** `.set-row-label` is a `div`; the work-auth selects,
  cadence numbers, and pace input rely on visual adjacency only. Make each a real
  `<label for="{{ field.id_for_label }}">` (keep the class; styling identical). This is
  the single highest-value a11y fix on the page.
- Scroll-spy: set `aria-current="true"` alongside `.is-active`; the rail is a `<nav
  aria-label="Settings sections">`.
- Each `section` gets `aria-labelledby` pointing at its `h2` id.
- Flash messages render in a container with `role="status"` (success) / `role="alert"`
  (errors); the per-section inline errors are associated via `aria-describedby` on the
  input.
- Danger/regenerate buttons: descriptive accessible names ("Delete account permanently",
  "Regenerate capture address"), not bare "Delete"/"Regenerate".
- `prefers-reduced-motion` already disables smooth scroll — keep; ensure the `kin-reveal`
  entrance animation also respects it (check `coverage.css`; if not, gate it).
- Copy button: after click, update an `aria-live="polite"` region ("Address copied"), not
  just the visual label swap.

### F. Implementation checklist (ordered)

Constraints throughout: Django + htmx server-rendered, no React; design tokens
(paper/ink/navy, `--s*`, `--fs-*`) unchanged; **multi-line template comments must be
`{% comment %}` — never brace-hash** (guards: `accounts/tests/test_template_comments.py`,
`directory/tests/test_styles_block.py`); every new private-model query goes through
`.for_user()`; nothing here requests any Gmail scope.

1. **Copy fixes (no schema, ship first):** "Your Data" export line; contacts count split
   ("N archived" + link); capture card hint line. Update privacy.html's "export
   everything" to match reality until step 8 lands.
2. **Cut the Language section** (template + the `language` POST branch in
   `settings_view`; leave the model column). Remove the rail entry.
3. **Rail groups + reorder** sections to the Part 3A structure. Pure template/CSS.
4. **A11y pass:** real `<label for>` on every control, `aria-current` scroll-spy,
   `aria-labelledby` sections, `role="status"` flashes, aria-live copy confirmation.
   Responsive `.set-row` stack at ≤560px; capture-address wrap.
5. **Sign-In & Security card** + styled allauth templates (`account/password_change.html`,
   `account/password_set.html`, `account/email.html`, `socialaccount/connections.html`)
   extending `base.html` in Coverage's visual language. Conditional rows per D6. Set
   `ACCOUNT_CHANGE_EMAIL = True`. Route-level tests: pages render inside the shell,
   anonymous users redirected.
6. **Sign out everywhere:** view + confirm page + session sweep (spare current key) +
   `product_event`. Test: other sessions die, current survives.
7. **Regenerate capture address:** service (unique-retry slug rewrite) + confirm page +
   flash with new address + `product_event`. Tests: old slug no longer resolves in the
   inbound path; regeneration is idempotent-safe under the unique constraint.
8. **Complete the export:** add missing contact columns; new csv builders for firms /
   applications / tasks / capture events / profile; ZIP endpoint (`?kind=all`); export
   page lists contents; privacy.html updated to the now-true "everything". Tests: every
   private model with user data appears in the ZIP; archived contacts flagged.
9. **Timezone:** migration (`User.timezone`), middleware, Profile field + honest unset
   hint, validation against `zoneinfo`. Tests: activated request's `localdate()` shifts;
   blank stays UTC; bogus stored value deactivates cleanly (guarded read).
10. **Advocate Target row** in Cadence: form field (1–5, blank clears key), template row,
    test that `crm/coverage.advocate_target` picks it up and that blank restores default.
11. **Email Capture health rows:** wire `capture_health()` facts + review-queue link +
    empty state into the card.
12. **Danger Zone receipts** (optional): thread `delete_user_and_data` counts into the
    goodbye flash.

Each step is independently shippable; 1–4 are pure template/CSS/copy and carry no
migration risk.

### G. MUST / SHOULD / LATER

**MUST (broken, or missing something the product needs):**
- Export copy honesty (audit #2) — the one-line fix ships even if the full export doesn't.
- Remove Language (audit #3).
- Sign-In & Security section + styled allauth pages (audit #1).
- Regenerate capture address (audit #4).
- Per-user timezone (audit #5) — the engine's day math is wrong for half the stated
  audience; must land before real HK students do.
- Programmatic labels on existing controls (E) — currently failing basic a11y.

**SHOULD (real improvement, no blockers):**
- Complete ZIP export (+ privacy-policy line update to match).
- Capture health surfaced in the card.
- Advocate Target control.
- Sign out everywhere.
- Rail grouping + reorder; responsive `.set-row` stack.
- Archived-count split + link.
- Delete-page receipts.

**LATER (needs a product decision or data we don't have):**
- **Notifications section** — blocked on the digest existing (M4 remnant). When built:
  one toggle ("Monday digest email"), default per founder decision, unsubscribe link in
  the email that writes the same flag, CAN-SPAM basics. Ship toggle and digest in the
  same release, never the toggle first.
- **Backup-expiry sentence** on the delete page — blocked on choosing the deploy target
  and reading its backup retention (deploy is deliberately paused).
- **Language / i18n** — re-add only with an actual translation pass (LocaleMiddleware,
  catalogs for the five LANGUAGES). The column and choices are ready.
- **Two-factor / passkeys** (allauth `mfa` module) — right-sized after real users exist;
  the Sign-In & Security card gives it an obvious home.
- **Deactivate (soft-pause) as an alternative to delete** — Simplify offers it; Coverage's
  `deleted_at` column exists but is unused. Product decision: is a paused-account state
  worth its support surface pre-launch? Not before real users.
- **Plan/It's-free row** — deliberately absent; billing is out of v1 by decision. Add an
  account-status row only when billing ships.

**Genuine uncertainties, flagged rather than papered over:**
- Whether Google login will be configured at deploy (no `SocialAccount` rows and no client
  ids locally). The connections row's conditional rendering covers both outcomes.
- `ACCOUNT_CHANGE_EMAIL = True` interaction with magic-link-only users should be smoke-
  tested; allauth's flow differs subtly when no password exists.
- The 512px avatar / MEDIA_ROOT ephemerality note in `models.py` still stands for deploy —
  out of this spec's scope but adjacent (Settings renders the avatar).
- Timezone shortlist composition (which zones ride above the fold of the select) is a
  30-second founder call; the control doesn't depend on it.
