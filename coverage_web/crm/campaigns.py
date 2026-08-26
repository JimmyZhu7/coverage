"""Telling the founder's job search apart from the other hat he wears.

THE BUG THIS FIXES, verified against his live mailbox on 2026-08-22. He is a
USC student recruiting for investment banking. He is also "Associate of
External Outreach" for USC's International Consulting Club, and in that role he
mail-merged 201 threads with the subject "Fall 2026 ICC Alumni Digital Panel
Outreach", asking alumni across every industry to speak on a club panel.
Recipients included Delta, Humana, Tacori, WME, Novo Nordisk, Latham & Watkins,
Hanes, BNP Paribas and KPMG. Nobody job-hunting in investment banking emails an
airline, an insurer and a jeweller in one batch.

Coverage ingested all of it as his personal recruiting network. Three cards
that were on his Today queue when this was written:

  - Nick Tehle (J.P. Morgan) replied "happy to help out on either a panel or
    for the mentorship program, as long as the events are earlier than 10pm
    ET." He is helping the CLUB. The queue said "Propose a 15-min chat."
  - Ellen Chung (Persona) filled out the panel form. "Propose a 15-min chat."
  - Ayda Yang (AccraCare, HR) ignored a panel invitation. "Follow up."

WHAT THIS MODULE IS. A deterministic detector for bulk sends, plus the lookup
the queue uses to act on the one question the user answers about each one. No
model in the loop: a mail merge has hard, countable signals, and spending a
token on "is this a mail merge" would be spending money to be less sure.

WHAT IT IS NOT. It is not a hiding mechanism. An undetected or unanswered
campaign behaves exactly as today — everyone stays in the queue. The fix is
putting the question in front of the user, not guessing the answer for them.
See `Campaign.KIND_UNCLASSIFIED`.

WHY NOT IN `coverage_domain.cadence`. Same line that module's docstring draws
and `crm/relevance.py` restates: the engine answers "what does the cadence
consider urgent", and "which hat was I wearing when I sent this" is not that.
The engine takes plain dicts, imports no Django, and keeps its golden fixtures.
Everything here is Django rows and a view decision.

TENANCY. Every query is `.for_user(user)`-scoped. `Campaign` and
`CampaignContact` are private-zone models like everything else in this app.


WHAT THE STORED DATA ACTUALLY SUPPORTS
--------------------------------------
The signals a mail merge really has are: one subject line, a send burst, a
shared CC, and a recipient count. Coverage stores two of those four, and being
straight about which is the difference between a detector and a wish.

  - RECIPIENT COUNT: stored. One `Touch` per recipient, `.for_user`-scoped.
  - SUBJECT: stored going forward only. `Touch.subject` was added with this
    module; `capture.gmail_live` had been reading the Subject header and
    discarding it. Every touch already in the database has a blank one.
  - SEND TIME: stored, at wildly varying precision. On the founder's own
    account, of 117 outbound touches, 96 carry a date-only midnight stamp (the
    bulk-import path), 16 carry the timestamp of the sync run rather than the
    send, and 5 carry a real send clock time. A "20 messages inside 30 seconds"
    test would find nothing here — not because the merge did not happen that
    way, but because that resolution was never written down. So the window is
    24 hours (see `BULK_WINDOW`), which is the precision the column actually
    has, and the recipient count carries the weight.
  - SHARED CC: NOT STORED. The ICC merge CC'd the same colleague
    (`ysaiyed@usc.edu`) on all 201 threads, which is the single cleanest signal
    in the whole problem — and Coverage persists no recipient list at all. Not
    on `Touch`, not on `capture.GmailConnection`, nowhere. There is no message
    metadata table. Adding one is a real change to the capture contract and a
    real privacy decision, so this module does not pretend to a signal it
    cannot populate. It is noted here so the next person does not go looking.

THE FALLBACK. Because no historical touch has a subject, grouping on subject
alone would detect nothing that already exists. `Touch.note` is the only
content this app kept for those rows: for `capture` touches it is a short
evidence line, sometimes literally the subject ("Sent: <subject>") and
sometimes an agent's one-sentence summary. It is a weaker key than a header —
prose varies where a header does not — but it is a real, stored, deterministic
string, and normalizing it recovers exactly the groups a subject would have.
Subject first, note second, one normalizer over both.

WHAT THE FALLBACK MAY NOT DO, and the incident that drew the line. On
2026-08-23 detection recorded campaign 3 on the founder's account: 41
recipients, all 41 originating, signature `outreach sent no reply yet`. That
string is not a subject anybody typed. It is COVERAGE'S OWN WRITING — the
placeholder the pre-`subject` import wrote onto every outreach row it could
not describe ("Outreach sent 2026-07-24, no reply yet"; see `crm/health.py`
for that phrase's history). Forty of those 41 touches carried `subject=''`
because the column post-dates them, so every one fell through to the note,
and the note was identical across them BY CONSTRUCTION rather than because
one message went to forty people. Their real subjects, stamped later, are
forty different personalised lines: "HK Jul 29-31 | Nomura | IBD - USC
Student Coffee Chat Request", "HK Jul 29-31 | CLSA | CICC - ...", one per
banker. Absence of evidence had become evidence of a blast.

That is the expensive direction of the asymmetry `BULK_MIN_RECIPIENTS`
argues below, at its maximum: one tap on "not my recruiting" would have
silenced 41 genuine target-firm relationships at once. So the fallback now
carries a precondition — the note has to contain something the SENDER wrote.
See `_is_app_authored`. The ICC detection the fallback exists for is
unaffected, because a note that echoes a real subject says something Coverage
could not have said by itself.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Campaign, CampaignContact, Contact, Touch

# ---------------------------------------------------------------------------
# 1. The grouping key.
# ---------------------------------------------------------------------------

# `capture.gmail`'s per-thread dedup marker, prepended to the evidence note.
# Unique per thread by construction, so it is the one substring guaranteed to
# make every note in a merge look different. Stripped first, always.
_GMAIL_MARKER_RE = re.compile(r"\[gmail:[0-9a-z]+\]", re.IGNORECASE)

# Reply/forward prefixes in the languages this product sees. A merge's replies
# are not part of the merge, but a subject that arrives with a prefix attached
# should still land in its own campaign rather than a second one.
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:re|fw|fwd|aw|回复|转发)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE
)

# Everything that is not a letter or a space. Digits go with it, which is the
# point: a merge's evidence lines differ almost entirely by date ("sent
# 2026-08-03", "outreach 8/3") and by nothing else. Dropping digits collapses
# those onto one key without any fuzzy matching.
_NOISE_RE = re.compile(r"[^a-z ]+")

# Trimmed to fit `Campaign.signature`. Two different sends whose first 240
# characters are identical are the same send.
_SIGNATURE_MAX = 240


def normalize_subject(text: str | None) -> str:
    """The grouping key for one outbound message.

    Deterministic, total, and deliberately blunt: lowercase, drop the Gmail
    thread marker, drop one reply/forward prefix, drop every digit and symbol,
    collapse whitespace. Two messages from one merge land on the same string;
    two letters a person actually wrote do not, because people do not write the
    same sentence twice.

    Returns "" for anything with no letters left, and "" never groups — see
    `_signature_for`.
    """
    if not text:
        return ""
    s = _GMAIL_MARKER_RE.sub(" ", text).strip().lower()
    s = _REPLY_PREFIX_RE.sub("", s)
    s = _NOISE_RE.sub(" ", s)
    return " ".join(s.split())[:_SIGNATURE_MAX]


# ---------------------------------------------------------------------------
# 1b. Whose words is this note, anyway.
# ---------------------------------------------------------------------------

# Every note Coverage composes ITSELF, copied from the call sites that write
# them. Interpolated values (dates, counts, names, thread markers) are left
# out on purpose: `normalize_subject` already drops digits, and anything else
# that varies per message is exactly the content this list must not swallow.
#
#   `capture_discover`            -> "Discovered by mailbox scan…"
#   `crm.today` / `crm.views`     -> the park audit rows
#   `capture.reclassify`          -> the bulk-demotion correction row
#   `crm.debrief`                 -> the advocate promotion row
#   the pre-`subject` agent import -> "Outreach sent …, no reply yet"
#
# Keep this list in step with those call sites. `test_campaigns.py` asserts
# every entry here is rejected, so a template that drifts out of the list
# still gets caught the moment someone writes a test for it — and the cost of
# missing one is a false NEGATIVE, which is the cheap direction.
_APP_AUTHORED_NOTES = (
    "Discovered by mailbox scan",
    "Discovered by mailbox scan — bulk/automated email, not a reply",
    "Parked from the Today queue",
    "Parked from the Today queue (bulk)",
    "Parked from the Network board (bulk)",
    "Correction: inbound messages recorded as a reply turned out to be bulk "
    "or automated mail. Status recalculated from the remaining evidence.",
    "Promoted to advocate from the chat debrief on "
    "(they said they'd advocate for you).",
    "Outreach sent, no reply yet",
    "Follow-up outreach sent, no reply yet",
)

# Grammar, not content. Present so that a REWORDING of one of the templates
# above — same status words, different glue — is still recognised as this
# app's prose rather than a person's. Deliberately contains no nouns: every
# word that could name a firm, an event or a subject line has to come from a
# real message.
_GLUE_WORDS = frozenset(
    "a an and any are as at be been but by for from had has have her his in "
    "into is it its no not of on or our so still that the their them then "
    "there they this to was were when with you your".split()
)

# The closed vocabulary Coverage writes status notes out of.
_APP_VOCABULARY = frozenset(
    word
    for template in _APP_AUTHORED_NOTES
    for word in normalize_subject(template).split()
) | _GLUE_WORDS


def _is_app_authored(normalized_note: str) -> bool:
    """True when every word in a normalized note comes from Coverage's own
    vocabulary — i.e. the note says nothing the sender said.

    A WORD SET RATHER THAN A PHRASE DENYLIST, because the failure mode is a
    string that is identical across unrelated contacts BY CONSTRUCTION, and
    what makes it identical is that the app assembled it out of a fixed
    vocabulary. Matching phrases would catch exactly the templates listed
    above and miss the next one somebody writes; matching the vocabulary
    catches anything built from the same words in any order, which is what a
    reworded template is. It also composes correctly the one way that matters
    and a denylist could not: "Follow-up
    outreach sent for ICC alumni panel, no reply yet" is boilerplate WRAPPED
    AROUND a real send, `icc`/`alumni`/`panel` are outside the vocabulary, and
    it groups — which is exactly how the ICC merge was detected before
    subjects existed.

    The error this makes is the cheap one. A note whose only content words
    happen to all be Coverage's ("Sent: follow up") is refused a signature and
    its contacts stay in the queue, which is the status quo. The error it
    refuses to make is the expensive one: manufacturing a 41-person campaign
    out of a column that had not been invented yet.
    """
    words = normalized_note.split()
    return bool(words) and all(w in _APP_VOCABULARY for w in words)


def _signature_for(touch) -> str:
    """Subject if the capture path stored one, evidence note otherwise.

    Two sources, one normalizer, and the order is the confidence order: a
    Subject header is what the sender actually typed once and the mail merge
    repeated 201 times, while a note is what this app wrote about the message
    afterwards. See the module docstring for why the second exists at all.

    THE SUBJECT IS NEVER GATED. A header is the sender's own text by
    definition; if forty of them match, forty messages really did carry one
    line. Only the note — the half this app may have written — has to prove it
    is quoting somebody.
    """
    subject = normalize_subject(touch.subject)
    if subject:
        return subject
    note = normalize_subject(touch.note)
    return "" if _is_app_authored(note) else note


# ---------------------------------------------------------------------------
# 2. Detection.
# ---------------------------------------------------------------------------

# Touch kinds that mean "I sent this". `follow_up` belongs with `outreach`
# because a mail merge's second wave is still a mail merge — the founder's ICC
# campaign sent its follow-up on 2026-08-03, a month after the first pass, and
# that wave is as much the campaign as the first one.
OUTBOUND_KINDS = ("outreach", "follow_up")

# How far apart two sends can be and still be one burst. See the module
# docstring: 96 of the founder's 117 outbound touches carry a date-only
# midnight timestamp, so anything tighter than a day is measuring a precision
# the column does not have. Strictly less than 24h, so two date-only sends on
# consecutive days (exactly 24h apart) are two campaigns and cannot chain into
# one unbounded group, while a real merge that straddles midnight stays whole.
BULK_WINDOW = timedelta(hours=24)

# THE FLOOR. How many DISTINCT recipients one signature needs inside one window
# before this is called a campaign rather than a person writing letters.
#
# WHY 8, and why the number matters more than the algorithm:
#
#   - The two errors are wildly asymmetric. A false negative costs the status
#     quo — the contact stays in the queue, exactly as today, and the founder
#     is no worse off than before this module existed. A false positive puts a
#     question in front of him about a genuine recruiting batch, and if he
#     answers it the natural way ("that was for the club") it silently removes
#     real target-firm bankers from his daily plan. Removing the right people
#     is this product's entire job. So the threshold is set where a false
#     positive is close to impossible, not where recall is maximal.
#
#   - Measured on his own 156-contact account: across all 117 outbound touches,
#     the largest signature-plus-window group that is NOT a merge holds 6 (his
#     own one-at-a-time Hong Kong coffee-chat requests, which happened to get
#     the same one-line evidence summary from the sync). 8 clears the observed
#     personal-note ceiling with two to spare.
#
#   - The brief's own worked counter-example — three personal notes that happen
#     to share a subject — is not close to 8, and neither is any plausible
#     variant of it. A student writing eight separate people the same day with
#     a byte-identical subject line is running a merge, not writing eight
#     letters.
#
# Raising this is safe (fewer detections, status quo behaviour). Lowering it is
# not, and should not be done without re-measuring the ceiling above.
BULK_MIN_RECIPIENTS = 8


def _burst_groups(touches: list) -> list[list]:
    """Split one signature's touches into maximal 24-hour bursts.

    Greedy over the sorted list, anchored on the FIRST member of each group
    rather than the previous one. Anchoring on the previous member would let a
    signature the user reuses every week chain into a single group spanning
    months, which is the opposite of a burst.
    """
    groups: list[list] = []
    current: list = []
    anchor = None
    for t in sorted(touches, key=lambda x: x.ts):
        if anchor is None or t.ts - anchor < BULK_WINDOW:
            if anchor is None:
                anchor = t.ts
            current.append(t)
        else:
            groups.append(current)
            current = [t]
            anchor = t.ts
    if current:
        groups.append(current)
    return groups


def _label_for(touches: list) -> str:
    """What the Settings card shows. The most common raw subject in the group
    if the capture path stored subjects, else the most common raw note with the
    thread marker stripped. Display only — never matched on, so it is safe for
    it to be prettier than the signature."""
    for attr in ("subject", "note"):
        raw = [(getattr(t, attr) or "").strip() for t in touches]
        raw = [_GMAIL_MARKER_RE.sub("", r).strip() for r in raw if r]
        if raw:
            return max(set(raw), key=raw.count)[:255]
    return ""


def detect(user) -> list[Campaign]:
    """Find this user's bulk sends and record them. Idempotent.

    Returns every campaign that exists for the user afterwards, newest send
    first — not just the ones this run created, because the caller (a Settings
    page, a sync's tail) wants the current picture rather than a delta.

    WHAT IT NEVER DOES, and these are the manual-override contract:

      - never changes a `kind` the user has answered (`classified_at` is the
        lock). A detector that re-ran nightly and reset an answer to
        "unclassified" would be worse than no detector.
      - never deletes a `CampaignContact` and never flips `originates` after
        the first write. A membership is a fact about what happened in the
        mailbox on one day; a touch logged next week must not be able to
        rewrite it.
      - never writes `Contact.campaign_exempt`. That column is the user's word.

    It DOES refresh `label`, `first_sent`, `last_sent` and `recipient_count` on
    every run, because those are readings off the touches rather than answers,
    and a campaign that has since gained a second wave should say so.
    """
    touches = list(
        Touch.objects.for_user(user)
        .filter(kind__in=OUTBOUND_KINDS)
        .only("id", "contact_id", "ts", "kind", "note", "subject")
    )
    if not touches:
        return _all_campaigns(user)

    # Every touch of every kind, with what it takes to sign it. `originates` is
    # measured against these — see `_first_outside` for the rule. One query.
    all_touches: dict[int, list] = {}
    for t in Touch.objects.for_user(user).only(
        "id", "contact_id", "ts", "note", "subject"
    ):
        all_touches.setdefault(t.contact_id, []).append(t)

    by_signature: dict[str, list] = {}
    for t in touches:
        sig = _signature_for(t)
        if not sig:
            # No stored subject and no stored note. Nothing to group on, and
            # grouping every contentless touch together would make one giant
            # fake campaign out of every hand-logged email the user ever sent.
            continue
        by_signature.setdefault(sig, []).append(t)

    now = timezone.now()
    for signature, sig_touches in by_signature.items():
        # What the relationship would have to predate to NOT originate here.
        # Computed once per signature, across every burst of it, because a mail
        # merge sends in waves: the founder's ICC campaign went out on 6 July
        # and followed up on 3 August. Measuring a 3 August recipient against
        # "their earliest touch of any kind" makes their own 6 July invitation
        # from the SAME campaign count as prior history, and the person the
        # campaign has emailed twice comes out as one it did not start. Which
        # is exactly backwards.
        first_outside = _first_outside(all_touches, signature)

        # ONE CAMPAIGN PER SIGNATURE, not per burst, even though the floor is
        # tested per burst. The two are different questions and conflating them
        # produced a real bug: the founder's ICC merge sent on 6 July and
        # followed up on 3 August, and recording a campaign per burst gave him
        # the same subject line twice in Settings with the same question
        # attached to each. One subject is one send is one decision.
        #
        # The burst split still does the work it exists for — it is what stops
        # a subject the user writes to three people every Monday from ever
        # reaching the floor — it just decides membership rather than identity.
        qualifying: list = []
        earliest: dict[int, object] = {}
        for group in _burst_groups(sig_touches):
            # One touch per contact within the burst — the EARLIEST, because
            # that is the one that would have started the relationship. A merge
            # that hit the same person twice in a day is still one recipient.
            per_contact: dict[int, object] = {}
            for t in group:
                prev = per_contact.get(t.contact_id)
                if prev is None or t.ts < prev.ts:
                    per_contact[t.contact_id] = t
            if len(per_contact) < BULK_MIN_RECIPIENTS:
                continue
            qualifying += group
            for contact_id, t in per_contact.items():
                prev = earliest.get(contact_id)
                if prev is None or t.ts < prev.ts:
                    earliest[contact_id] = t
        if earliest:
            _record(user, signature, qualifying, earliest, first_outside, now)

    return _all_campaigns(user)


def _first_outside(all_touches: dict[int, list], signature: str) -> dict[int, object]:
    """`{contact_id: earliest touch NOT belonging to this campaign}`.

    Two things are deliberately excluded from "prior history", both because
    they are the campaign rather than something that predates it:

      - the campaign's OTHER waves. See the caller.
      - the replies the campaign drew. `capture.gmail` writes an inbound
        touch's evidence from the same thread summary as the outbound one, so
        a reply to the merge signs identically to the merge. Counting it would
        be counting the campaign's own consequence as its precondition, and it
        arrives after the send anyway.

    A contact with nothing outside gets no entry, which `_record` reads as "no
    prior history" — the correct answer for somebody this send is the entire
    reason Coverage has ever heard of.
    """
    out: dict[int, object] = {}
    for contact_id, rows in all_touches.items():
        outside = [t.ts for t in rows if _signature_for(t) != signature]
        if outside:
            out[contact_id] = min(outside)
    return out


@transaction.atomic
def _record(user, signature, group, earliest, first_outside, now) -> None:
    # The window spans every touch in every qualifying burst, not just the
    # first one per contact: a campaign that sent in July and followed up in
    # August ran across both, and reporting only the earlier wave would date it
    # a month before the mail the user actually remembers sending.
    sent = [t.ts for t in group]
    campaign = (
        Campaign.objects.for_user(user).filter(signature=signature).first()
    )
    if campaign is None:
        campaign = Campaign(user=user, signature=signature)
    campaign.label = _label_for(group) or campaign.label
    campaign.first_sent = min(sent)
    campaign.last_sent = max(sent)
    campaign.recipient_count = len(earliest)
    # `kind` and `classified_at` are deliberately absent from this list. See
    # `detect`'s docstring — a user answer is never overwritten by a re-run,
    # and naming the columns explicitly is what guarantees it rather than
    # relying on nobody adding an assignment above.
    if campaign.pk:
        campaign.save(
            update_fields=["label", "first_sent", "last_sent",
                           "recipient_count", "updated"]
        )
    else:
        campaign.save()

    existing = set(
        CampaignContact.objects.for_user(user)
        .filter(campaign=campaign)
        .values_list("contact_id", flat=True)
    )
    # `all_objects` because this is a bulk INSERT, not a read. The tenant
    # manager exists to make an unscoped QUERY impossible, and `bulk_create`
    # reaches `get_queryset()` on its way in — the same reason every other
    # writer in this app (`crm.views.set_firm_tier`, the test helpers) creates
    # through `all_objects`. Every row below carries an explicit `user=user`,
    # and `user` is the argument this whole function is scoped by.
    CampaignContact.all_objects.bulk_create(
        [
            CampaignContact(
                user=user,
                campaign=campaign,
                contact_id=contact_id,
                sent_at=touch.ts,
                # Nothing outside this campaign reached this person first, so
                # this is where the relationship began. `<=` rather than `<`
                # because two touches can share a timestamp to the microsecond
                # when a sync writes a batch, and a tie should count as the
                # start rather than silently not. A contact with no entry in
                # `first_outside` has no history outside the campaign at all,
                # which is the strongest possible yes.
                originates=touch.ts <= first_outside.get(contact_id, touch.ts),
            )
            for contact_id, touch in earliest.items()
            if contact_id not in existing
        ],
        # Belt and braces against a concurrent detect() for the same user; the
        # UniqueConstraint is what actually guarantees it.
        ignore_conflicts=True,
    )


def _all_campaigns(user) -> list[Campaign]:
    return list(Campaign.objects.for_user(user))


# ---------------------------------------------------------------------------
# 3. What the queue asks.
# ---------------------------------------------------------------------------


def excluded_contact_ids(user) -> set[int]:
    """Contacts whose relationship with the user began in a campaign they have
    said is NOT their own recruiting.

    Three conditions, all required:

      - the campaign is classified `other`. `unclassified` and `recruiting`
        both mean "behave as today"; only an explicit human answer removes
        anybody.
      - the membership `originates` — see `CampaignContact`. Someone the user
        was already recruiting before the blast reached them is not club admin.
      - the contact has not been exempted by hand (`Contact.campaign_exempt`).

    Returns ids only. The caller decides what to do with them, and two callers
    now do different things:

      - the daily queue drops their action (`crm.today`, `crm.relevance`),
        with the inbound override on top of it, so a panelist who writes in
        with a real question still surfaces.
      - the Network board takes them off the board entirely
        (`crm.views.contact_list`), and says so, and lists them at
        `crm:contact_campaign_hidden` with a one-click way back.

    WHY THE SECOND ONE EXISTS. This function shipped with a docstring
    promising these people kept the Network board, and the founder read his
    own board and asked why there were still ICC people in his network. Twelve
    members on his live data, nine of them originating. "Not my recruiting" is
    an answer about the relationship, not about one queue, and a board that
    still counts nine club alumni into Firm Coverage and the contact total is
    disagreeing out loud with the answer he just gave.

    WHAT IS STILL TRUE, and it is the line this rule must not cross: nobody is
    deleted, edited or unreachable. Every one of these contacts keeps their
    detail page, their whole touch history, search, the contact book and every
    export, and any direct link to them still works.
    """
    ids = set(
        CampaignContact.objects.for_user(user)
        .filter(campaign__kind=Campaign.KIND_OTHER, originates=True)
        .values_list("contact_id", flat=True)
    )
    if not ids:
        return set()
    exempt = set(
        Contact.objects.for_user(user)
        .filter(id__in=ids, campaign_exempt=True)
        .values_list("id", flat=True)
    )
    return ids - exempt


def classify(user, campaign_id: int, kind: str) -> Campaign | None:
    """Record the user's answer. Returns the campaign, or None if it isn't
    theirs or the kind isn't one of the three.

    Re-answering is a first-class action, not an edge case: the question is
    asked once about ~200 people and the user is allowed to change their mind
    without anything being lost. Setting it back to `unclassified` clears
    `classified_at` too, which puts the campaign back in front of them as an
    open question rather than leaving a stale timestamp claiming it was
    answered.
    """
    if kind not in dict(Campaign.KIND_CHOICES):
        return None
    campaign = Campaign.objects.for_user(user).filter(id=campaign_id).first()
    if campaign is None:
        return None
    campaign.kind = kind
    campaign.classified_at = (
        None if kind == Campaign.KIND_UNCLASSIFIED else timezone.now()
    )
    campaign.save(update_fields=["kind", "classified_at", "updated"])
    return campaign


def campaign_cards(user) -> list[dict]:
    """What the Settings card renders. One dict per campaign, with the counts
    a person needs to recognise a send they made weeks ago: how many people it
    reached, and how many of those the answer would actually move.

    `originating_count` is the second number and it is the important one. A
    campaign covering 201 recipients where 190 of the relationships started
    there is a different decision from one where 190 were already in the queue
    for other reasons, and a single "201 contacts" would hide that.
    """
    campaigns = list(Campaign.objects.for_user(user))
    if not campaigns:
        return []
    originating: dict[int, int] = {}
    for campaign_id in (
        CampaignContact.objects.for_user(user)
        .filter(originates=True)
        .values_list("campaign_id", flat=True)
    ):
        originating[campaign_id] = originating.get(campaign_id, 0) + 1
    return [
        {
            "id": c.id,
            "label": c.label or "Untitled send",
            "kind": c.kind,
            "is_classified": c.is_classified,
            "recipient_count": c.recipient_count,
            "originating_count": originating.get(c.id, 0),
            "first_sent": c.first_sent,
            "last_sent": c.last_sent,
        }
        for c in campaigns
    ]
