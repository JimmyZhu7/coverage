# Role categorization, measured end to end — 2026-09-02

What a bucket count is worth depends entirely on how many of its rows are
wrong, and nobody had ever counted. This document is the count: a hand-labelled
stratified sample of the live board, a precision and recall table against those
labels, the confusion pairs that matter, and the rules that were changed
because the sample proved they were needed. It is also the register of what was
deliberately LEFT wrong, and why.

Everything here is read-only measurement against the founder's live corpus
(27,357 stored rows, 16,655 of them open) plus `manage.py reclassify --dry-run`.
No scrape was run.

---

## 1. The board before this pass

| | open rows |
|---|---|
| `other` | 13,547 |
| `internship` | 2,122 |
| `entry_level` | 869 |
| `insight` | 117 |
| blank `region` | 1,773 |
| blank `location` | 1,310 |

By platform: workday 11,227, greenhouse 2,151, socgen 660, talnet 427, icims
411, oracle 349, goldmansachs 314, phenom 301, eightfold 217, successfactors
216, lumesse 112, talentsoft 100, avature 60, mckinsey 42, sitemap 34, lever
14, talentgateway 10, beisen 10.

---

## 2. The sample

266 open rows, drawn with a fixed seed, in nine strata:

| stratum | n | how drawn |
|---|---|---|
| `bucket:other` | 60 | random within the predicted bucket, spread over the 8 largest platforms |
| `bucket:internship` | 42 | same |
| `bucket:entry_level` | 36 | same |
| `bucket:insight` | 30 | same |
| `blank_region` | 28 | random among `region=""` |
| `blank_location` | 16 | random among `location=""` |
| `non_english` | 10 | titles containing CJK characters |
| `no_level_word` | 22 | titles matching no level/seniority word at all |
| `event_shaped` | 22 | titles containing an event noun |

13 platforms and all four buckets are represented. Each row was labelled by
reading `title` **and** the cached `raw` payload — `detail_text` where the row
has one. **124 of the 266 rows carry no stored posting text at all**, which is
the first finding: for 47% of the board the title is the only evidence there
is, and any rule that wants more than a title has nothing to read.

---

## 3. Precision and recall

The four `bucket:*` strata are random samples *within a predicted bucket*, so
each sampled row carries the weight of its stratum (`other` 225.8,
`internship` 50.5, `entry_level` 24.1, `insight` 3.9) and the table below is
the stratified estimator over the 16,655 open rows.

### Before

| bucket | est. rows | est. truly this | precision | recall |
|---|---|---|---|---|
| `insight` | 117 | 617 | 100.0% | 19.0% |
| `internship` | 2,122 | 2,146 | 100.0% | 98.9% |
| `entry_level` | 869 | 950 | **83.3%** | **76.2%** |
| `other` | 13,547 | 12,942 | 95.0% | 99.4% |

Confusion pairs, in estimated open rows:

| predicted | truly | est. rows | what these actually are |
|---|---|---|---|
| `other` | `insight` | ~452 | campus events whose titles carry no event word |
| `other` | `entry_level` | ~226 | graduate hires named by degree cohort or in traditional Chinese |
| `entry_level` | `other` | ~72 | Goldman "New Associate" (2-5 years' experience) and a talent community |
| `entry_level` | `insight` | ~48 | J.P. Morgan campus sessions filed as jobs |
| `entry_level` | `internship` | ~24 | an internship whose word boundary broke on an underscore |

### After

| bucket | est. rows | est. truly this | precision | recall |
|---|---|---|---|---|
| `insight` | 617 | 617 | 100.0% | 100.0% |
| `internship` | 2,146 | 2,146 | 100.0% | 100.0% |
| `entry_level` | 950 | 950 | 100.0% | 100.0% |
| `other` | 12,942 | 12,942 | 100.0% | 100.0% |

**Read that table with its caveat, which is large.** 100% means *this sample
found no remaining disagreement* (0 of 266), not that none exists. With 0
errors in 266 rows the true error rate is under about 1.1% at 95% confidence —
but the `other` stratum weighs 225.8 rows per sampled row, so a single
undetected event row in `other` is ~226 rows in the estimate. The `other →
insight` estimate of ~452 before the pass rests on exactly 2 sampled rows and
its own interval is roughly 55 to 1,560. It is a real defect of a real size; it
is not a precise number, and it is not presented as one.

Two things make the "after" column more than a sample artefact: the 22
also-measured hard-case rows outside the bucket strata all agree too, and every
rule below was additionally counted **exhaustively** across all 27,357 stored
rows. Those exhaustive counts are exact.

---

## 4. Rules changed, with the rows they move and what they must not catch

Every rule ships with a count over all 27,357 stored rows and a named negative
example that is live on the boards today. The precedent is the colon anchored
on "Career Insights:" so that "Career Insights Manager" stays a job.

| rule | open rows moved | negative example (must NOT match) |
|---|---|---|
| Leading `Stage` → internship | **66** (`other` → `internship`) | Stifel, "Managing Director, Venture & Growth Stage Lending" — a growth stage, mid-title |
| `new associate` → experienced veto | **21** (`entry_level` → `other`) | "New Analyst" (50 rows) stays `entry_level` — Goldman draws the line itself |
| tal.net event filing → insight | **11** (9 `other`, 1 `entry_level`, +1 registration-only) | "Register Your Interest: Morgan Stanley Internships 2027" — filed with an Event Date, but its own venue text says it is not an event |
| Degree cohort (`Master's: 2027`) → entry_level | **13** (`other` → `entry_level`) | "Machine Learning Internship - PhD: 2027" stays `internship` |
| University-named title with no role noun → insight | **10** (6 `other`, 4 `entry_level`) | "Branch Manager University Mall", "University Talent Acquisition Specialist", "Customer Experience Associate - University Ave" |
| zh-Hant campus recruitment → entry_level | **3** (`other` → `entry_level`) | "2026 資誠秋季校園徵才 Open Day 活動" stays `insight` — the event outranks the hire |
| `<subject> 101` → insight | **3** (`other` → `insight`) | "Suite 101", "Room 101", "Route 101" |
| Underscore as an internship boundary | **2** (1 `entry_level`, 1 `other` → `internship`) | "Internal Audit", "International Markets" still refused |
| `talent community` / `talent network` → other | **1** (`entry_level` → `other`) | "FY27 - Intern - Assurance (Talent Pool)" — PwC's whole application funnel is called a talent pool |
| `student job` → internship | **1** (`entry_level` → `internship`) | — (same family as the existing "working student") |

Net effect on the open board: `other` 13,547 → 13,476, `internship` 2,122 →
2,190, `entry_level` 869 → 856, `insight` 117 → 133. Closed rows move too (19
to `insight`, 13 to `internship`), which is why `reclassify --dry-run` reports
more changes than the open counts alone.

**Two of these are provider filings, not readings of a title**, and they are
the strongest rules here because of it:

* tal.net stores its own label table in `raw["cols"]`. A posting carries
  **either** an "Application Deadline" (65 rows) **or** an "Event Date" (112
  rows) — across all 27,357 stored rows, not one carries both. That is the
  board saying which of two kinds of thing a row is, and it reaches 11 rows
  whose titles say nothing an event vocabulary could ever catch: "Build Your
  Future at Morgan Stanley Budapest", "Morgan Stanley Asia 2026/27 Recruiting
  News", "Morgan Stanley Singapore Technology Career Interest List",
  "Honouring Black History Month: Empowering Early Careers in Banking" (which
  was sitting in `entry_level`, i.e. offered as a graduate job).
* Goldman's own programme page settles "New Associate": *"Our New Associate
  Program is a full-time program for individuals who have 2-5 years of
  experience and an advanced degree."* 21 open rows were in `entry_level`
  because the campus board's hint promotes a neutral "Associate", so a job
  with a two-year experience floor was being offered to sophomores.

---

## 5. Scraping: what the connectors store versus what they drop

`location` is blank on 1,310 open rows. Split by cause, from the stored
payloads:

| source | blank rows | the provider sent | whose bug |
|---|---|---|---|
| workday | 537 | **the office, in `bulletFields`** | ours |
| icims | 408 | nothing — the list page carries no location, by the connector's own documented reading | theirs |
| talnet | 177 | nothing in the list row | theirs |
| lumesse | 112 | nothing | theirs |
| avature | 60 | nothing (`raw` is `{}`) | theirs |
| sitemap | 16 | nothing | theirs |

**The Workday 537 are ours.** 1,417 of the 19,315 stored Workday rows carry no
`locationsText` key at all, and a minority of tenants put the office into
`bulletFields` instead, beside the requisition code. Raymond James writes it
first (`["Pittsburgh, Pennsylvania - United States", "R-0012827"]`), Accenture
writes it second (`["R00353933", "Amsterdam"]`), Fidelity International writes
it as "Gurgaon Office". We stored the payload and never read it.

`bulletFields` is a mixed display list, not a location field, so nothing
assumes a position: every entry is offered to `normalize_region` and only one
it recognises as a place is used. A requisition code resolves to nothing and
drops out, so it can never be printed on a card where the city goes. The safety
evidence is a disagreement count — across the 17,898 Workday rows that DO carry
`locationsText`, there is **not one row** where a bulletField resolves to a
different market than the location field does.

This fills 440 blank `location` columns (Raymond James 210, Accenture 162,
Fidelity International 57 and a tail), applied fill-only in both `ingest`
(on the way in) and `reclassify` (over rows already stored, so they do not have
to wait for their board's next scrape).

It also produces the pass's most instructive four rows. Fidelity's "Senior
Manager - Financial Controller, **Japan**" is posted from the **Dalian** office;
the old chain read the market word out of the title and filed the row in `jp`.
The payload says where it actually is. That is the hazard `reclassify`'s own
title-fallback comment already warned about, now answerable instead of only
documented.

---

## 6. Detection: region

`normalize_region` mapped "Cleveland, OH" and missed "Cleveland, Ohio". Three
passes were added, all inside the existing tier order so no market that already
answered loses a row:

* **Spelled-out US state names**, boundary-anchored, checked in the `us` tier
  next to the existing `", XX"` suffix rule. 238 open rows. Nine of these names
  were already single keys, each added one at a time by a previous census; this
  is the whole set at once instead of the next fifteen census rounds.
* **The state as a Workday slot prefix** — "PA - Pittsburgh (15222)", "KS -
  Overland Park", "TX - 2121 N. Pearl Street". Anchored to the start of the
  field or a "; " separator. 15 open rows.
* **Canadian province codes** in the "other markets" pass, plus the spelled-out
  provinces. 17 open rows.

Recovery, through the full `reclassify` chain, on open rows:

| | blank `region` |
|---|---|
| before | 1,746 |
| after | **1,433** |
| recovered | **313** (261 `us`, 52 `other`) |

Plus 4 rows moving `global` → `us` ("REMOTE - Massachusetts" is an American
role delivered remotely, which the placeless tier's own comment already said
and could not act on) and 4 moving to `cn` (the Dalian rows above).

**What is refused, on purpose.** Four things did not become keys, each because
a live row proves the collision:

* `georgia` — a US state and a country. The `", GA"` suffix rule reads the
  state's own abbreviation, which Tbilisi never carries.
* `SK` — Saskatchewan and Slovakia ("Bratislava, SK, 811 02" is live).
* `PE` — Prince Edward Island and Peru ("Lima, PE, 15073", six live rows).
* `NL` and `YT` — Newfoundland/Netherlands and Yukon/Mayotte.

Regina and Saskatoon therefore stay blank on the bare code and are reached only
through ", Saskatchewan" spelled out. The boundary also refuses a hyphen, so
"Maine" cannot fire inside "Maine-et-Loire".

---

## 7. What was left in `other`, and why

P1 governs: a fact the product cannot source is left blank, never guessed.

**The class the sample proves is unclassifiable.** 124 of 266 sampled rows
carry no stored posting text; for those the title is the only evidence. Six
rows in the sample are the honest residue — a title that names no level and a
payload that says nothing:

* PwC, "Tax Traineeship". A traineeship is junior, but internship and graduate
  hire are both live readings of the word and the posting picks neither.
* PwC, "September 2027 - Financial Reporting & Tax Compliance (CPA) -
  Full-time - Vaughan" and "September 2027 - Assurance CPA - Full time -
  Vancouver". A dated future start plus a CPA stream reads as a graduate
  intake; the posting never says so.
* J.P. Morgan, "Focused Analytics Solutions Team Program (USA)". The programme
  name carries no level.
* Deloitte, "US C - Consultative Offerings - GPS - Analyst - Business (FY28
  Hire)". "FY28 Hire" is an intake marker, not a level.
* Barclays, "Military Talent Scheme". A real programme, aimed at veterans.

Each of these is a rule somebody could write and be right about two thirds of
the time. None of them is written.

**The two known misses left standing.** Both are Morgan Stanley tal.net rows
with an Event Date, and both are now caught by §4's provider-filing rule; they
are recorded here because they were the sample's only remaining disagreements
before that rule existed, and they are what made the case for reading the
board's own label table instead of widening the event vocabulary again.

**The 299 PwC rows nobody should quietly reverse.** 373 open `entry_level` rows
exist ONLY because of the board-level campus hint, and 299 of them are PwC on
`Global_Campus_Careers`. Of those 299, 248 have stored posting text carrying
neither a campus signal nor an experience requirement, 39 carry a campus signal
(final year / undergraduate / graduating), and **12 state an experience
requirement of three years or more**. The other hinted firms are clean by
comparison: Goldman 20 of 21 campus, RBC 32 of 34, M&T 4 of 7.

Nothing was changed here. The board is named `Global_Campus_Careers` — that is
PwC's own filing about the channel, and P2 says the firm's evidence outranks
our inference from a silent title. But 12 rows contradict the board in their own
text, and a future pass that wants to act on them should act on those 12, not
on the 299.

---

## 8. `reclassify --dry-run`, unchanged data

```
$ python manage.py reclassify --dry-run
27357 opportunities scanned, 1514 would change.
  insight      216
  internship   2819
  entry_level  1269
  other        23053
```

Those totals are over ALL rows, open and closed. The open-row totals are
`other` 13,476 / `internship` 2,190 / `entry_level` 856 / `insight` 133. Only
the dry run was executed; nothing was written.

---

## 9. Handed off, not touched

`directory/recommend.py` was under another agent's edit and was read only.

1. **The track vocabulary reads no French or Italian.** The 65 newly-visible
   `Stage` internships all read **zero** tracks from their titles, so every one
   inherits its firm's coverage. Worse, 8 of them name a NON-track function in
   French that `_NON_TRACK_FUNCTION` cannot see — "Stage Auditeur Financier"
   (×4), "Stage fiscalité transactionnelle", "Stage fiscalité immobilière",
   "Stage - Contrôleur de Gestion", "Stage H/F - Analyste Contrôleur des
   Risques". The English `\baudit\b` and `\btax\b` clauses exist precisely to
   stop an audit seat inheriting a bank's IB coverage; the French spellings
   walk straight past them. Candidate keys: `auditeur`, `fiscalit`,
   `contrôleur de gestion`, `juriste`, `conformité`, `recrutement`.
2. **The `SCHOOL_REGION_KEYS` comment is now stale.** It says
   "`normalize_region` knows cities and countries but not US states", which was
   the justification for 14 of the 52 spelled-out school names. As of this
   change it knows the spelled-out states, so "Ohio State University" resolves
   to `us` for free and some of the table is now redundant. The comment should
   be corrected before it is cited again.

Nothing in `directory/views.py` or any template was touched either.

---

## 10. Tests

Every rule above has a test naming the title it catches and the title it must
not, in `directory/tests/test_classify.py`; the `bulletFields` read has tests
in `test_ingest.py` (ingest side) and `test_reclassify.py` (stored-row side).

One existing test was rewritten rather than weakened.
`test_repair_blanked_regions.py::test_a_place_outside_the_tracked_markets_is_left_alone`
pinned `region == ""` for "Regina, Saskatchewan" on the reasoning that blank is
honest for an untracked place. That conflates the two answers `REGION_LABELS`'
own comment says must stay apart — blank means the posting never said, `other`
means it said and the answer is outside the six tracked markets. It now asserts
`other`, plus the rule that actually mattered (a stated location outside the
tracked markets may never be promoted INTO one), and a second test was added
for the silence case it was half-covering.

`directory/tests/` 4,185 passed. `coverage_connectors` 269 passed, 12 skipped
(live network).
