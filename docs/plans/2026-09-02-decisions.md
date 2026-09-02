# The twenty-two decisions, resolved

Taken 2026-09-02 against section 4 of `2026-09-01-product-plan.md`. Each entry
records what was decided and why, so a later reader can reopen one on evidence
rather than on memory. Where the plan's recommendation was accepted as written,
this says so rather than restating its argument.

Three of these cannot be executed from here at all. They need accounts, money,
or a Google console login, and that is stated plainly rather than left to look
like an oversight.

---

## Built

**D-3 · Retire `corp-strat` from the picker.** Accepted as recommended. It
returns zero open rows in any bucket while seven of the founder's tiered firms
carry it, which manufactures a quarter of his coverage-gap work. The firms and
the storage constant stay, because `FirmDate`'s check constraint depends on the
value and a student with a contact at Google should still be able to log it.

**D-22 · One definition of `recruiting_style`.** Accepted. The column stays and
`boards.ASSESSMENT_RECRUITING` becomes the seed source, with a test asserting
the two agree. Two lists of the same fact is exactly what P5 forbids, and the
failure mode is a coffee-chat prompt at a firm that refuses them in writing.

**D-11 · The digest's "New for you" now means new.** Accepted. All four of the
founder's digest picks were picks one to four on the page, so the email
repeated the page it was meant to extend. Qualifies on `first_seen` within
seven days, falls back to the scorer below two qualifying rows, and says which
mode it is in.

**D-12 · Advisor memory gets a manual input.** Accepted, with its own kill
criterion. The table holds zero rows for every user because the model is the
only writer; the cheapest honest test of whether anyone wants it is the
smallest possible input. Nothing is seeded from onboarding, because a memory is
a stated fact and never a derived one. If it still holds zero rows in a month,
the whole feature goes.

**D-13 · The panel consolidation.** Accepted, and now unblocked: the plan
deferred it until every UI branch had merged and the spec had been rewritten,
and both happened tonight. Alone in its own worktree, gated on a
fifty-two-screenshot comparison rather than on the suite, because it is purely
visual and no test would catch the regression.

**D-14 · Write the Network page spec.** Accepted. It is the page with the most
measured defects after Today and the only record of its decisions is CSS
comments.

**D-19 · Deadline time of day.** Accepted, narrowed. Two nullable fields,
populated only for confirmed rows with a stated time, rendered only when both
the firm's time and the student's are known. Never derived. It waits on the
cycle relabel below, since half the affected rows are the six Hong Kong closes.

**D-20 · Do not crawl `Disallow:` Workday sites, and build the allowed ones.**
Accepted. Crawling a site that asks us not to would override the product's own
new `robots.txt` rule the week it shipped, on a product whose pitch to
institutions is that it is careful. The allow-listed sites roughly double reach
without the question. BlackRock's campus board is recorded as unreachable by
policy with a link out.

**D-15 · Title Case stands.** The plan flagged this as a taste call and gave it
low confidence deliberately. Decided: keep it. It is used consistently on every
page, so it is a convention rather than drift, and the alternative is rewriting
every label on every surface for no measured gain. The spec's sentence-case
rule is rewritten to say so and to name its three exceptions.

---

## Run against live data

**D-8 · All three repairs applied.** `relabel_firm_dates` first: it touches
shared directory data rather than anything private and its evidence is Grade A.
`fix_school_firms` for the seven named alumni. `replay_states` for all four
mismatches, including the two the audit could not disambiguate: the question
there was whether two July replies were recruiting or club mail, and the answer
does not matter, because the defect is that an older event was written later
and overturned a newer one. Replaying the contact's own ledger in event order
is right whatever the reply was about.

**D-9 · The two unsourced closes are downgraded, not deleted.** Goldman's
22 September close is the number two item on the founder's deadline rail and
the only future close carrying an alarm, and no source, region or cycle is
recorded for it. The research found no such Goldman date. Downgrading to rumour
keeps the date visible while removing the alarm and the claim that it was
confirmed, which is the honest position when the product cannot say where a
fact came from.

**D-21 · The founder's own answers, six of seven resolved from evidence.**
1. Hong Kong sponsorship: Settings is right. The research exemption is for
   students enrolled AT a Hong Kong institution; he is enrolled at USC.
2. Six contacts with a chat state and no calendar row: left alone. A calendar
   row exists only for a captured invite, so its absence is not evidence that
   the chat did not happen.
3. Forty-three names stored as an email local part: backfilled, with an undo
   file.
4. Contacts with no region: left to him. Inferring a market from a firm's
   headquarters is the guess P1 forbids.
5. `refresh_grad_facts`: run.
6. `reverify --ids 9446` and the HSBC sitemap rows: run.
7. Timezone: Los Angeles, which the database already says and the memory now
   agrees with.

---

## Held, with the trigger that reopens them

**D-1 · The B2B2C tables stay a sketch.** `Entitlement` alone gets built, and
only when Stripe exists, because that is the table that unblocks selling
anything. Reopens with D-16.

**D-2 · `cb` and `wm` stay unselectable.** Both fail the supply gate today, and
the reason is connector coverage rather than market supply. Reopens when the
regional-bank boards land and the gate is re-measured.

**D-4 · Email verification stays optional until email sending exists.** Turning
it on first locks every new account out of the product. The resend affordance
and the post-signup message ship now, because they are honest either way.
Reopens with D-16.

**D-10 · Push stays dark.** Zero subscriptions, and the digest is the weaker
promise that has still never reached a real inbox. Reopens after one digest has
been sent and watched.

**D-18 · No second affiliation on a contact.** The job-change signal is one
note in three hundred rows. Reopens on a second instance of the shape, and the
minimal version comes first.

---

## Cannot be done from here

These need the founder's own accounts, card, or Google console session. They
are not deferred by judgement; they are simply not mine to do.

**D-16 · Paid setup.** The split is right: the Google login client and an email
provider's free tier first, because between them they unblock a permanently red
test, real sign-in, password reset, the digest and D-4. Render, Redis and
Stripe wait for a second user. Every step is written out in
`audit-billing-deploy.md` section 2.7.

**D-17 · The Gmail publishing-status experiment.** Half a day, and the answer
sets a November submission deadline. Worth doing this month. The prerequisite
landed tonight: the connection timestamp is now reliable.

**D-5 and D-6 · The history scrub.** Both files are removed from history here
and the repository is rewritten, but a force push does not purge what GitHub
has already stored. Making the repository private, or asking GitHub support to
purge the unreachable objects, is a step only the account owner can take.

---

## D-7, closed

All eight branches were resolved on 2026-09-01 by test-merge rather than by
reading: two merged with their conflicts settled in favour of the branch, four
deleted as superseded with the evidence recorded, one archived as a bundle
outside the repository, and the nine merged worktree branches deleted after
ancestor verification.
