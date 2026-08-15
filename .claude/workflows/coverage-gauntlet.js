export const meta = {
  name: 'coverage-gauntlet',
  description: 'Reusable Gauntlet Loop for Coverage: recall durable state, blind-critique, adversarially verify, fix in parallel worktrees, merge, live-recheck, integration pass, then record state back',
  whenToUse: 'Run a full quality round against Coverage on demand. args: {focus: [lens keys], execModel, designModel, fetchBudget} — all optional.',
  phases: [
    { title: 'Recall', detail: 'read .claude/gauntlet/STATE.md — the loop\'s memory' },
    { title: 'Critique', detail: '4 data lenses (exec model) + content and design lenses (design model), fresh context, blind to commit messages' },
    { title: 'Verify', detail: 'one fresh-context skeptic per finding, tries to refute' },
    { title: 'Fix', detail: 'one worktree builder per area, in parallel' },
    { title: 'Merge', detail: 'sequential merges into main, full suite gate' },
    { title: 'Recheck', detail: 'live re-verification per area — browser for UI, DB/source for data' },
    { title: 'Integrate', detail: 'one fresh critic walks the WHOLE product for seams the per-part fixes cannot see' },
    { title: 'Record', detail: 'rewrite STATE.md with what this round learned, commit it' },
  ],
}

const REPO = '/Users/zhujimmy/Claude/Projects/Coverage'
const STATE_PATH = '.claude/gauntlet/STATE.md'

// Model policy (founder's standing choice): design-judgment lenses on the
// stronger model, mechanical/data work on the cheaper one. Overridable per
// run via args without editing this file.
const EXEC = (args && args.execModel) || 'sonnet'
const DESIGN = (args && args.designModel) || 'opus'
const FETCH_BUDGET = (args && args.fetchBudget) || 15
const VERIFY_CAP = 12

const LOGIN_RECIPE = `
LOGGING IN (any agent that needs the live app): mint a session, never type a password.
cd ${REPO} && DJANGO_SETTINGS_MODULE=coverage_web.settings.local uv run --project coverage_web python coverage_web/manage.py shell -c "
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
U = get_user_model()
u = U.objects.get(email__iexact='zhujimmy123@gmail.com')
s = SessionStore()
s['_auth_user_id'] = str(u.pk)
s['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
s['_auth_user_hash'] = u.get_session_auth_hash()
s.save()
print(s.session_key)
"
Then open the Browser pane (preview_start {url:'http://localhost:8000/app/'}), set document.cookie = "sessionid=<key>; path=/" via javascript_tool, navigate. If :8000 is not running, start it backgrounded: DJANGO_SETTINGS_MODULE=coverage_web.settings.local uv run --project coverage_web python coverage_web/manage.py runserver
`

phase('Recall')

const recall = await agent(`
Repo: ${REPO}. Read the file ${STATE_PATH} in full — it is the Coverage
Gauntlet loop's durable memory. Also run 'git log --oneline -15' and check
whether any file listed under "Live carve-outs" has had its described fix
land (if clearly landed, note it in carveOutNotes — do NOT edit the file
yourself; the Record step owns writes).

Return: round (the next_round integer from the file), stateText (the file's
full contents, verbatim), carveOutNotes (anything the carve-out list gets
wrong per current git state, or 'current').
`, {
  label: 'recall', phase: 'Recall', model: EXEC, effort: 'low',
  schema: {
    type: 'object', required: ['round', 'stateText', 'carveOutNotes'],
    properties: {
      round: { type: 'integer' },
      stateText: { type: 'string' },
      carveOutNotes: { type: 'string' },
    },
  },
})

const ROUND = recall.round
const STATE = recall.stateText + '\nCARVE-OUT CORRECTIONS THIS RUN: ' + recall.carveOutNotes
log('round ' + ROUND + ' — state recalled (' + STATE.length + ' chars)')

const COMMON = `
Repo: ${REPO} (Django, uv workspace: coverage_web, coverage_domain, coverage_connectors).
This is Coverage, Jimmy's recruiting CRM for finance students. You are one
agent inside ROUND ${ROUND} of its standing Gauntlet Loop (blind critique ->
adversarial verify -> fix -> live recheck -> integrate -> record).

THE LOOP'S DURABLE STATE — objective, metric, boundaries, everything already
fixed, every dead end, live carve-outs, open leads. Obey ALL of it; the
"Fixed mechanisms" list is a do-not-re-report list, "Refuted / dead ends"
is a do-not-re-litigate list, "Live carve-outs" are files you must not
touch or audit for change:
--- STATE BEGIN ---
${STATE}
--- STATE END ---

BLINDNESS RULE: judge what you see TODAY against reality (the live board,
the live rendered page, the firm's own site) — never against what a commit
message claims was fixed.
${LOGIN_RECIPE}
Your final output is a StructuredOutput call. An EMPTY findings list is a
good result — do not invent findings to look useful, and terse copy is this
product's deliberate voice, not a defect. Cap at 8 findings, most severe
first. Every finding needs concrete evidence and a repro a stranger could
run. Severity: high = wrong/misleading info shown or broken control;
medium = quietly missing/confusing/inconsistent; low = cosmetic.
`

const FINDINGS_SCHEMA = {
  type: 'object', required: ['findings', 'notes'],
  properties: {
    findings: {
      type: 'array', maxItems: 10,
      items: {
        type: 'object',
        required: ['key', 'area', 'severity', 'claim', 'evidence', 'repro'],
        properties: {
          key: { type: 'string' },
          area: { type: 'string', enum: ['deadline', 'dedup', 'classification', 'status', 'coverage', 'content', 'design'] },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          claim: { type: 'string' },
          evidence: { type: 'string' },
          repro: { type: 'string' },
        },
      },
    },
    notes: { type: 'string' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object', required: ['confirmed', 'reasoning'],
  properties: {
    confirmed: { type: 'boolean' },
    reasoning: { type: 'string' },
    corrected_claim: { type: 'string' },
  },
}

phase('Critique')

const seedBase = ROUND * 1000
const ALL_LENSES = [
  { key: 'deadlines', model: EXEC, prompt: COMMON + '\nYOUR LENS: DEADLINE TRUTH, fresh sample, random.seed(' + (seedBase + 1) + '). Sample ~15 open postings with a stored deadline, weighted toward nearest, favoring sources not recently re-sampled clean (the state file tells you which were). Fetch budget: ' + FETCH_BUDGET + '. Re-fetch each source, compare stored deadline + precision, quote the page\'s own sentence for any mismatch.' },
  { key: 'dedup', model: EXEC, prompt: COMMON + '\nYOUR LENS: ONE POSTING, ONE ROW, fresh sweep, random.seed(' + (seedBase + 2) + '). Fetch budget: 6. Look for genuinely NEW duplicate patterns only — the state file lists which mechanisms are fixed and which fold ideas are dead ends. The /firms/hsbc/ double-render lead in "Open leads" is yours if still live.' },
  { key: 'classification', model: EXEC, prompt: COMMON + '\nYOUR LENS: LABELS TELL THE TRUTH, fresh sample, random.seed(' + (seedBase + 3) + '), zero network fetches (DB/text only). Sample ~25 open rows with detail_text; check every fact kind for phrase-supports-value, skipping the kinds the state file marks deeply audited.' },
  { key: 'status', model: EXEC, prompt: COMMON + '\nYOUR LENS: OPEN MEANS OPEN, fresh sample, random.seed(' + (seedBase + 4) + '). Fetch budget: ' + FETCH_BUDGET + '. ~10 open rows with old last_checked from under-sampled sources, ~5 recently-closed rows re-verified live. A dead page behind open, or a live page behind closed, is HIGH.' },
  { key: 'content', model: DESIGN, prompt: COMMON + '\nYOUR LENS: CONTENT CLEANLINESS — a product-design judgment call. Log in and browse AS A REAL USER (rendered pages, not templates): Today, Network, 2-3 firm pages, a contact page, Opportunities, My Applications, Calendar, Settings. Flag stale copy contradicting behavior, the same fact stated twice on one screen, raw enum/debug values, filler, cross-page terminology drift, truncation, broken plurals. Do NOT flag terseness or your own stylistic preference. Exact URL + exact text for every finding. Page budget: ' + FETCH_BUDGET + '. The "Open leads" section of state lists unfinished content leads — check those first.' },
  { key: 'design', model: DESIGN, prompt: COMMON + '\nYOUR LENS: BENCHMARK-INFORMED DESIGN CRAFT. The state file\'s "Design benchmark standards" section holds the distilled Linear/Ramp/Mercury craft bar. If your page budget allows, refresh it against one of those live sites first (browser pane; they are public marketing/product pages) — extract STANDARDS (spacing rigor, type-scale discipline, motion restraint, contrast, target sizes), never their look: Coverage\'s paper-ledger identity is settled. Then audit Coverage LIVE against those standards with real measurements (getComputedStyle, getBoundingClientRect, both themes, 375px and 1280px) — never eyeball-only. Propose extensions only within the existing motion vocabulary (kin-reveal, live-dot, cap-pipe, sheen, card-pop). Page budget: ' + FETCH_BUDGET + '. Check "Open leads" first.' },
]

const focus = (args && args.focus && args.focus.length) ? args.focus : null
const LENSES = focus ? ALL_LENSES.filter(l => focus.includes(l.key)) : ALL_LENSES
log('critiquing with ' + LENSES.length + ' lenses: ' + LENSES.map(l => l.key).join(', '))

const found = await parallel(LENSES.map(l => () =>
  agent(l.prompt, { label: 'critic:' + l.key, phase: 'Critique', schema: FINDINGS_SCHEMA, model: l.model })))

const criticNotes = found.map((r, i) => LENSES[i].key + ': ' + (r ? r.notes : 'AGENT FAILED')).filter(Boolean)
const allFindings = found.filter(Boolean).flatMap(r => r.findings)

const seen = new Map()
for (const f of allFindings) { const k = f.key.toLowerCase(); if (!seen.has(k)) seen.set(k, f) }
const rank = { high: 0, medium: 1, low: 2 }
const deduped = Array.from(seen.values()).sort((a, b) => rank[a.severity] - rank[b.severity])
const toVerify = deduped.slice(0, VERIFY_CAP)
const unverified = deduped.slice(VERIFY_CAP)
log(deduped.length + ' deduped findings; verifying ' + toVerify.length + ', ' + unverified.length + ' unverified (recorded as leads)')

phase('Verify')

const modelFor = a => (a === 'content' || a === 'design') ? DESIGN : EXEC

const verified = await parallel(toVerify.map(f => () =>
  agent(COMMON + `
YOU ARE THE SKEPTIC. Try to REFUTE this claim. Prior rounds produced
confident, plausible, WRONG findings; findings right about a symptom but
wrong about the cause; and fixes that passed tests while broken live.
Re-derive everything from scratch; a UI claim requires actually loading and
measuring the live page. Fetch/page budget: 5.

THE CLAIM (area: ${f.area}, severity: ${f.severity}): ${f.claim}
EVIDENCE OFFERED: ${f.evidence}
REPRO: ${f.repro}`,
    { label: 'skeptic:' + f.key, phase: 'Verify', schema: VERDICT_SCHEMA, model: modelFor(f.area) })
    .then(v => ({ ...f, verdict: v }))))

const confirmed = verified.filter(Boolean).filter(x => x.verdict && x.verdict.confirmed)
  .map(x => ({ ...x, claim: x.verdict.corrected_claim || x.claim }))
const refuted = verified.filter(Boolean).filter(x => x.verdict && !x.verdict.confirmed)
log(confirmed.length + ' confirmed, ' + refuted.length + ' refuted')

let fixResults = [], merge = null, rechecks = [], integration = null

if (confirmed.length) {
  phase('Fix')

  const byArea = new Map()
  for (const f of confirmed) {
    if (!byArea.has(f.area)) byArea.set(f.area, [])
    byArea.get(f.area).push(f)
  }
  const areas = Array.from(byArea.keys())
  log('fixing across ' + areas.length + ' parallel worktree builders: ' + areas.join(', '))

  const FIX_RULES = `
Standing rules (non-negotiable):
- You are in an ISOLATED GIT WORKTREE so builders can run in parallel.
  'git branch --show-current' first; commit there; NEVER merge/push/touch
  main yourself — a merge step follows. If 'uv run' fails on a stale venv,
  'uv sync --project coverage_web' first.
- Your worktree may have branched from a STALE base. Before skipping any
  finding as "does not reproduce", check the file on CURRENT MAIN
  (git show origin? no — 'git -C ${REPO} show main:<path>' or read the file
  under ${REPO} directly, read-only). A real, skeptic-confirmed defect that
  is absent in your worktree but present on main gets fixed against main's
  version of the code, flagged clearly in your report.
- Honor every boundary and carve-out in the STATE block above. Live DB is
  read-only; data corrections become dry-run-by-default commands listed in
  dry_run_reports, or a documented run of an existing scoped command.
- A CONTENT fix edits the actual template copy; a DESIGN fix edits CSS or
  markup and is not done until you re-measure the live computed style/pixels.
- If a fix changes a cached raw field, scope-backfill the affected live
  rows or disclose loudly in skipped why the defect stays visible.
- Isolated test DB: coverage_web/coverage_web/settings/_audit_tmp.py with
  DATABASES["default"]["TEST"]={"NAME":"coverage_audit_tmp_test_<area>"},
  run uv run pytest coverage_web coverage_connectors
  --ds=coverage_web.settings._audit_tmp --create-db -q, then rm + dropdb.
- Tests must exercise the REAL failure mode. Commit style: short
  present-tense user-visible title, body explains root cause with evidence.
- Real judgment calls get skipped with a clear why, never pushed through.
- Every fixed[]/skipped[] entry's "key" MUST be the ORIGINAL finding key
  given to you below, copied verbatim — never paraphrased, renumbered, or
  replaced with an index like "1". Round 12 lost a real fix's recheck
  result this exact way: the builder reported key:"1", the Record step's
  key-matching against confirmed findings silently failed to link it, and
  a genuinely-broken-live defect almost got recorded as fixed. If you are
  fixing something not in the numbered list below (found while working),
  give it its own new, descriptive key and say so explicitly — never
  reuse "1"/"2"/etc as a key for anything.
- StructuredOutput must include branch (git branch --show-current) and
  worktree_had_changes.`

  const FIX_SCHEMA = {
    type: 'object', required: ['fixed', 'skipped', 'commits', 'dry_run_reports', 'branch', 'worktree_had_changes'],
    properties: {
      fixed: { type: 'array', items: { type: 'object', required: ['key', 'what'], properties: { key: { type: 'string' }, what: { type: 'string' } } } },
      skipped: { type: 'array', items: { type: 'object', required: ['key', 'why'], properties: { key: { type: 'string' }, why: { type: 'string' } } } },
      commits: { type: 'array', items: { type: 'string' } },
      dry_run_reports: { type: 'array', items: { type: 'string' } },
      branch: { type: 'string' },
      worktree_had_changes: { type: 'boolean' },
    },
  }

  fixResults = (await parallel(areas.map(area => () =>
    agent(`
Repo: ${REPO}. You are the BUILDER for the "${area}" area in round ${ROUND}
of Coverage's Gauntlet Loop, running in PARALLEL with other area builders.
Fix ROOT CAUSES, one logical fix per commit.
${LOGIN_RECIPE}
${FIX_RULES}

STATE (boundaries + carve-outs bind you):
--- STATE BEGIN ---
${STATE}
--- STATE END ---

CONFIRMED DEFECTS IN YOUR AREA ONLY:
${byArea.get(area).map((f, i) => (i + 1) + '. [' + f.severity + ' / ' + f.key + '] ' + f.claim + '\n   Evidence: ' + f.evidence + '\n   Repro: ' + f.repro + '\n   Skeptic: ' + f.verdict.reasoning).join('\n')}
`, { label: 'builder:' + area, phase: 'Fix', schema: FIX_SCHEMA, model: modelFor(area), effort: 'high', isolation: 'worktree' })
      .then(r => ({ area, ...r }))))).filter(Boolean)

  phase('Merge')

  const MERGE_SCHEMA = {
    type: 'object', required: ['merged', 'conflicts', 'tests_green_after_merge', 'summary'],
    properties: {
      merged: { type: 'array', items: { type: 'object', required: ['area', 'branch', 'commits'], properties: { area: { type: 'string' }, branch: { type: 'string' }, commits: { type: 'array', items: { type: 'string' } } } } },
      conflicts: { type: 'array', items: { type: 'object', required: ['area', 'what_happened'], properties: { area: { type: 'string' }, what_happened: { type: 'string' } } } },
      tests_green_after_merge: { type: 'boolean' },
      summary: { type: 'string' },
    },
  }

  merge = await agent(`
Repo: ${REPO}. You are the MERGE agent for round ${ROUND}. In the SHARED
main checkout: confirm branch main + clean-except-known-carve-out-files
status, then 'git merge --no-ff <branch>' each builder branch below with
worktree_had_changes=true, sequentially. A genuine (non-mechanical)
conflict: read both sides; resolve only if obviously mechanical, else
abort THAT merge and record it under conflicts. After all merges run the
FULL suite once (isolated _audit_tmp settings + --create-db; rm + dropdb
after). Then remove the builders' worktrees, prune, delete merged
branches. NEVER 'git add -A' or commit files you did not merge — the
carve-out files in STATE have live uncommitted edits from another session.

BRANCHES: ${JSON.stringify(fixResults.map(r => ({ area: r.area, branch: r.branch, worktree_had_changes: r.worktree_had_changes, commits: r.commits })))}

STATE (carve-outs):
--- STATE BEGIN ---
${STATE}
--- STATE END ---
`, { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA, model: EXEC, effort: 'high' })

  phase('Recheck')

  const RECHECK_SCHEMA = {
    type: 'object', required: ['area', 'per_fix', 'summary'],
    properties: {
      area: { type: 'string' },
      per_fix: { type: 'array', items: { type: 'object', required: ['key', 'still_reproduces'], properties: { key: { type: 'string' }, still_reproduces: { type: 'boolean' }, note: { type: 'string' } } } },
      summary: { type: 'string' },
    },
  }

  rechecks = (await parallel(areas.map(area => () => {
    const areaFix = fixResults.find(r => r.area === area)
    return agent(`
Repo: ${REPO} (shared main checkout, post-merge). You are the RECHECKER for
"${area}", round ${ROUND}. Trust nothing: prior rounds found fixes that
passed tests but were broken live, fixes over-applied to unchecked paths,
and fixes whose cached data was never backfilled. Data fixes: re-run the
original repro read-only against live DB/source (max 8 fetches, 1.5s
apart). CONTENT/DESIGN fixes: load the actual page, read the actual text or
measure the actual computed style. still_reproduces=true only if a real
user still sees the defect.
${LOGIN_RECIPE}
FIXES CLAIMED: ${JSON.stringify(areaFix ? areaFix.fixed : [])}
COMMITS: ${JSON.stringify(areaFix ? areaFix.commits : [])}
ORIGINAL DEFECTS: ${JSON.stringify(byArea.get(area).map(f => ({ key: f.key, claim: f.claim, repro: f.repro })))}
'area' in your output must be exactly "${area}".
`, { label: 'recheck:' + area, phase: 'Recheck', schema: RECHECK_SCHEMA, model: modelFor(area) })
  }))).filter(Boolean)

  phase('Integrate')

  integration = await agent(COMMON + `
YOU ARE THE INTEGRATION CRITIC — the article-pattern step this loop was
missing. ${confirmed.length} per-part fixes just merged. Per-part loops
raise local quality while the WHOLE quietly drifts: seams between pages,
a term renamed on one surface but not another, a motion pattern now used
three slightly different ways, a count corrected in one place still stale
in its sibling. Walk the product END TO END as one continuous session (log
in; Today -> Opportunities -> a role drawer -> a firm page -> Network -> a
contact page -> Calendar -> My Applications -> Settings) and judge the
WHOLE: coherence, consistency, rhythm. Findings here are LEADS for the
next round (recorded in state), not fixed now — so precision matters more
than volume. Page budget: ${FETCH_BUDGET}.

THIS ROUND'S MERGED FIXES (the seams most likely to be fresh):
${JSON.stringify((merge && merge.merged) || [])}
`, { label: 'integration', phase: 'Integrate', schema: FINDINGS_SCHEMA, model: DESIGN })
}

phase('Record')

const record = await agent(`
Repo: ${REPO}. You close round ${ROUND} of Coverage's Gauntlet Loop by
REWRITING ${STATE_PATH} — the loop's only memory. Read the current file,
then rewrite it in place, same section structure, keeping it DISTILLED
(this is a working card, not a changelog; compress old detail as new detail
arrives, git history keeps the full story):

1. next_round: ${ROUND + 1}
2. Fold this round's confirmed-and-fixed mechanisms into "Fixed mechanisms"
   (compressed, mechanism-level).
3. Append genuinely-new refutations to "Refuted / dead ends".
4. Rebuild "Open leads": drop leads resolved this round, add the
   integration critic's findings and the unverified findings below.
   BEFORE trusting any "fixed" claim below, cross-check it against its
   own area's recheck results by key. A recheck key that does NOT match
   any confirmed-findings key (a builder mis-keyed its report — this has
   happened before) is not evidence of nothing; go find which confirmed
   finding it actually refers to from its note/description and file it
   correctly. Never silently drop a recheck result just because its key
   doesn't line up.
5. Append any NEW failed approach this round exposed.
6. Update "Live carve-outs" against current git reality (check git log /
   git status for the listed files; remove entries whose fixes landed).
7. Commit ONLY ${STATE_PATH} ('git add ${STATE_PATH}' — never -A; other
   files in the tree belong to another session), message
   'Gauntlet round ${ROUND}: record state'.

ROUND ${ROUND} RESULTS
Confirmed+fixed: ${JSON.stringify(confirmed.map(f => ({ key: f.key, area: f.area, claim: f.claim.slice(0, 300) })))}
Refuted: ${JSON.stringify(refuted.map(f => ({ key: f.key, reason: (f.verdict.reasoning || '').slice(0, 300) })))}
Unverified (capped): ${JSON.stringify(unverified.map(f => ({ key: f.key, severity: f.severity, claim: f.claim.slice(0, 200) })))}
Integration findings: ${JSON.stringify(((integration && integration.findings) || []).map(f => ({ key: f.key, severity: f.severity, claim: f.claim.slice(0, 300) })))}
Fix skips/disclosures: ${JSON.stringify(fixResults.flatMap(r => r.skipped || []))}
Recheck still-reproduces: ${JSON.stringify(rechecks.flatMap(r => (r.per_fix || []).filter(p => p.still_reproduces).map(p => ({ key: p.key, note: (p.note || '').slice(0, 300) }))))}
Merge conflicts: ${JSON.stringify((merge && merge.conflicts) || [])}
`, {
  label: 'record', phase: 'Record', model: EXEC, effort: 'high',
  schema: {
    type: 'object', required: ['committed', 'summary'],
    properties: { committed: { type: 'boolean' }, summary: { type: 'string' } },
  },
})

return {
  round: ROUND,
  clean: confirmed.length === 0,
  confirmed, refuted, unverified, criticNotes,
  fixResults, merge, rechecks,
  integration: integration ? integration.findings : [],
  stateRecorded: record ? record.committed : false,
  recordSummary: record ? record.summary : 'record agent failed',
}
