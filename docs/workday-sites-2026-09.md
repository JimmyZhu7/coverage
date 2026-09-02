# Which Workday sites does a tenant publish, and which may we read?

**WS-OPS-13**, unblocked by **D-20**, 2026-09-01 product plan. Probe run
2026-09-02 07:20 to 08:05 UTC from the scraper host (`zhujimmy` macOS, Python
3.13, `coverage_connectors` HTTP layer).

> **Path note.** Same reason as `docs/talnet-2026-09.md`: `docs/research/` is
> gitignored because this repo is public and that directory holds real names.
> Nothing here is private — it is `robots.txt` bodies and job counts for
> public career sites — so it lives at `docs/` where a reader can check it.

## Why this document exists

`audit-opportunities.md` §B5 measured eight of the founder's tiered firms with
a connector pointed at the experienced-hire site and not one campus row ever:
Ares 273 rows, Fidelity International 167, Oaktree 97, Blue Owl 61, Standard
Chartered 27, Moelis 37, Bain & Company 25, Perella Weinberg 2. 689 rows
scraped, 0 campus.

`research-ats-lifecycle.md` Q6 (Grade A) gave the mechanism — on Workday the
unit is `(tenant, siteId)` and a recruiter chooses which requisitions reach
which site — and the method: enumerate the sites from the tenant's own
`robots.txt`, then audit each candidate **by membership**, never by row count.
Two questions had to be settled by fetching rather than by reading, because
the plan's own rule is that nothing enters the catalog on a guess:

1. Does each of those tenants actually publish a campus site alongside its
   main one?
2. Do the nine regional banks named in `SYNTHESIS-PLAN.md` Part D
   recommendation 3 run Workday at all, and where?

## What was done

One `GET /robots.txt` per candidate tenant host, then one `POST` to the CXS
`jobs` endpoint per candidate site. Requests spaced 2 to 2.5 seconds apart,
sending `coverage_connectors.http.USER_AGENT` — the honest string the scraper
sends — with TLS verified against certifi's roots, the fetch layer's default.
No browser user agent anywhere. No `Disallow:`-listed site was fetched, which
is the decision this document exists to record. Nothing was written to the
database by the probe.

Membership audit, per new site: take one title off the secondary site, search
the firm's registered PRIMARY site for it, then repeat with a short
distinctive phrase from the same title so a miss cannot be an artifact of
searching a 60-character string.

## Result 1 — the tenants that already had a board

| Tenant | `Allow:` | `Disallow:` |
|---|---|---|
| `pjtpartners.wd1` | Careers, GeneralInterest2015, **Studentevents**, Students | NonPublicJobs082891, PrivateEvents |
| `hl.wd1` | Campus, Corporate, **Events**, Lateral | **External** |
| `raymondjames.wd1` | RaymondJamesCareers, **RaymondJamesEarlyCareers** | — |
| `guggenheim.wd1` | Guggenheim_Careers, Guggenheim_Careers_Campus, Guggenheim_Partners, **Guggenheim_Undergraduate_Programs** | Guggenheim-Confidential |
| `moelis.wd1` | Experienced-Hires, **University-Hires** | — |
| `blackrock.wd1` | BlackRock_Professional | **BlackRock_Early_Careers_Program**, BlackRock_AIG |
| `aresmgmt.wd1` | External, External-Ada | — |
| `oaktree.wd1` | Oaktree | DirectApply |
| `blueowl.wd1` | blueowl | blue_owl_private |
| `fil.wd3` | 001, fidelitycanada | — |
| `peopleplus.wd3` (Std Chartered) | SCB_Careers | — |
| `pwp.wd1` | PWP_Experienced_Opportunities | — |
| `blackstone.wd1` | 300+, almost all `X-GhostSite-<agency>` | — |

Bold is a site the catalog did not have. `refreshFacet` is disallowed by every
tenant and is omitted: it is an endpoint, not a career site.

**Five of the eight firms on the audit's list publish no campus site at all.**
Ares, Oaktree, Blue Owl, Fidelity International and Standard Chartered each
`Allow:` only their main board. That is a better answer than the row count
that raised the question, and their `NO_CAMPUS_BOARD` markers now rest on it.

## Result 2 — the new sites, and the membership audit

| Site | Rows | Membership probe against the primary |
|---|---|---|
| `pjtpartners.wd1/Studentevents` | 4 | "University College Dublin & Trinity College Dublin …" -> `Students` 0. "Company Presentation" -> `Students` 3, all Camberview full-time analyst reqs, no event. |
| `raymondjames.wd1/RaymondJamesEarlyCareers` | 7 | "2027 Full-Time Analyst, Risk Management" -> `raymondjamescareers` 0. **"2027" -> 0 across the whole primary site.** |
| `moelis.wd1/University-Hires` | 1 | "Join Our Campus Talent Community" -> `Experienced-Hires` 0. "Campus" -> 0. |
| `hl.wd1/Events` | 0 | live and empty; nothing to audit |
| `guggenheim.wd1/Guggenheim_Undergraduate_Programs` | 0 | live and empty; nothing to audit |
| `mtb.wd5/Campus` | 11 | "2027 Management Development Program - Internal Audit" -> `MTB` 0. "2027 Summer" -> `MTB` 0, while "Management Development Program" -> **192** mainframe and programme-manager rows. |

Every pair is disjoint, which is what Q6 predicted and what a row count could
not have told anybody.

**Raymond James is the finding.** Its registered board is large, healthy and
answers zero for "2027": the firm's entire campus cycle — Investment Banking
Summer Analyst in Private Credit and M&A, Equity Research Associate, the Clark
Capital Mentoring Program — was invisible to Coverage, and nothing in the
health report could have said so, because the board it watches is fine.

**Moelis is the counterexample the research warned about.** `University-Hires`
holds exactly one row and the row is a standing talent community, not a
programme. A count of 1 would call the site healthy; the real 2027 London
Summer Analyst sits on tal.net. It is registered with that written next to it.

**M&T's firm-wide site is NOT registered.** Its campus answer is zero and its
noise is 192. That is the whole lesson of `(tenant, siteId)` in one tenant.

## Result 3 — the nine regional banks

| Bank | Tenant found | Site registered | Rows |
|---|---|---|---|
| PNC | `pnc.wd5` | `External` ×2 + `HarrisWilliams` | 24 + 23 + 22 |
| KeyBank | `keybank.wd5` | `External_Career_Site` | 38 |
| Fifth Third | `fifththird.wd5` | `53careers` | 54 |
| Huntington | `huntington.wd12` | `HNBcareers` | 16 |
| M&T | `mtb.wd5` | `Campus` | 11 |
| U.S. Bank | `usbank.wd1` | `US_Bank_Careers` | **0** |
| Regions | `regions.wd5` | **none — every site disallowed** | — |
| Citizens | none found | — | — |
| Comerica | none found | — | — |

**`intern` is the wrong search text on every one of these boards**, for the
reason the Accenture entry in `boards.py` already records: it matches
"Internal" and "International". Measured — PNC 1,449 rows led by
"International Trade Services Analyst Sr"; U.S. Bank 1,297; KeyBank 427.
`internship` is the word the campus requisitions use, and it cuts those to 24,
0 and 38. PNC's unfiltered board reports `total: 2000`, which is Workday's
ceiling and not a count.

**U.S. Bank answers zero for campus on every keyword** — `internship` 0,
`campus` 0, `summer 2027` 0 — against 1,380 experienced reqs. That is a board
with no campus inventory today, not a broken one, so it is registered scoped
and left to report its own zero.

**Citizens and Comerica have no discoverable Workday tenant.** Eight candidate
hosts for Citizens and seven for Comerica; every one answers HTTP 422, which
is what `myworkdayjobs.com` returns for a tenant that does not exist. The
exception is `comerica.wd1`, which answers **HTTP 401 on `robots.txt`** — the
host declining to state its rules at all, which `core/robots.py` already
treats as a refusal rather than as an absence.

## What D-20 decided, and what it costs

**Coverage does not fetch a Workday site its tenant disallows.** Two are known
by name and both are recorded in `boards.UNREACHABLE_BY_POLICY` with a link
out, so a student gets the address even though we do not read it:

- BlackRock, `blackrock.wd1/BlackRock_Early_Careers_Program`
- Regions Financial, `regions.wd5/Regions_Careers` (and its three siblings,
  including `BlackArch_Careers`, its M&A arm)

Regions was not on anybody's list of policy cases before this probe. It is the
sharper one: nine regional banks were to be built and one of them declines the
whole tenant, so "take the `Allow:`-listed sites" costs a named firm rather
than an abstraction.

The cost is bounded and the reason is not. `robots.txt` compliance became the
product's own rule the same week these sites were enumerated (X7), and a
product whose pitch to institutions is that it is careful with data does not
override its own new rule for a handful of rows. The `Allow:`-listed sites
alone added 198 open campus rows across eleven firms.

## One thing this probe measured but did not act on

Blackstone's `Allow:` list runs to 42KB and 300-odd entries, nearly all of
them one site per external recruiting agency (`X-GhostSite-<name>`). Buried in
it are five school-scoped sites — `UK_SEO`, `UK_Notre_Dame`,
`UK_London_Business_School`, `Schwarzman_Scholars`, `Boston_Career_Fairs` —
which are plausibly real campus pipelines and are not registered. Each needs
its own membership audit against `Blackstone_Campus_Careers`, which is already
registered and producing. They are named here so the next pass does not have
to re-read 42KB to find them.
