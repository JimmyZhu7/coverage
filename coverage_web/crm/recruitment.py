"""Is this PERSON part of the user's recruiting world at all?

THE RULE THIS ANSWERS, in the founder's words (2026-08-25): "ensure all
contacts in coverage need to be related to recruitment, any unrelated should
not show up." The question is prior to everything `crm.relevance` asks — that
module decides who may generate a daily action among people who belong here;
this one decides who belongs here.

WHY THE TWO OBVIOUS RULES ARE BOTH WRONG, measured on the founder's own 131
active contacts before this module existed:

  - "At a tiered firm" keeps the wrong people and hides the right ones. Nine
    genuine Hong Kong IB analysts and associates at CLSA and CMB International
    failed it only because he had not tiered those two firms — hiding a real
    banker is the single most expensive mistake this product can make. And it
    KEPT two Amazon rows ("Account Manager, AWS", "Sales") because Amazon is
    on his tier list for corp-strat: the firm being a target does not make an
    AWS account manager part of an IB recruit.

  - "Shares my school" (the blanket USC-alumni exemption `crm.relevance`'s
    REL_SCHOOL used to be the whole story for) let in the real junk: a WRIT
    150 writing professor, a BUAD 306 professor, the campus advising office,
    alumni in food innovation and consumer tech — none of whom have anything
    to do with recruiting — mixed in with campus recruiters at Deloitte, PwC,
    Bain and KPMG and finance-club peers who absolutely do.

    NOTE THE REVERSAL, so the next reader knows it was a choice and not an
    oversight: the school exemption was itself a deliberate earlier decision
    (see `crm/relevance.py`'s rule 1 — "any contact who shares his school may
    generate a daily action"). The founder has now overridden it. A school
    tie alone neither keeps nor hides anybody here; the person's own
    occupation decides, and REL_SCHOOL is only ever reached by people this
    module already kept.

SO THE TEST IS THE PERSON, not the firm and not the school: do they work in,
recruit for, or study toward the tracks Coverage covers (ib / st / am / pe /
corp-strat / consulting)? A CLSA IB Analyst passes on the role alone. A
Deloitte campus recruiter passes because recruiting IS the job. A finance-club
peer passes. A WRIT 150 professor does not, however many emails they have
exchanged. Everything is read off the row — `role`, `notes`/`angle`, the
firm's own `tracks`, the user's own tier list — deterministic, no model, no
guessing (the same doctrine as the rest of this pipeline; see
`capture/discovery.py`'s judgment chain).

THE ASYMMETRY THAT SHAPES EVERY LINE BELOW: a wrongly hidden banker costs the
user a real relationship, invisibly; a wrongly kept professor costs one line
on a board. So keep-signals are scanned first and widely (role AND the user's
own notes), hide-markers are scanned narrowly (the role text only — free
prose must never hide anybody), and a row with no signal either way is KEPT.
Hiding is reserved for people the row itself places outside every track.

HIDDEN, NOT DELETED, AND REVERSIBLE — same posture as the campaign rule
(`crm/campaigns.py::excluded_contact_ids`): nobody is destroyed, the board
says how many it is hiding and links to the ledger
(`crm.views.contact_unrelated`), and `Contact.recruitment_related` is the
user's own word, wins over this rule permanently in both directions, and is
never written by any automated path.
"""

from __future__ import annotations

import re
from collections import namedtuple

from .relevance import is_recruiting_role

KEEP = "keep"
HIDE = "hide"

# One classified person: the verdict, a stable machine code, and the human
# sentence the ledger shows — always a claim about THIS row's own text, never
# an inference ("Role says “Professor”", not "probably not relevant").
Verdict = namedtuple("Verdict", ["verdict", "code", "reason"])

# ---------------------------------------------------------------------------
# Keep vocabulary — work IN or STUDY TOWARD a covered track.
# ---------------------------------------------------------------------------
# `directory.recommend.role_function` already maps a role title onto the six
# tracks and is reused below (one vocabulary, not two). But it was built for
# JOB POSTINGS, whose titles spell things out ("Investment Banking Summer
# Analyst"); a contact's role is read off how people sign their own mail, and
# bankers sign "IB Analyst", "IB VP", "Global Markets Analyst" — none of which
# a posting-title vocabulary matches. These extras cover the signature
# vocabulary, verified against the founder's live rows. Keep-biased on
# purpose: a stray match here KEEPS somebody, which is the cheap direction.
_PERSON_TRACK_MARKERS: tuple[tuple[str, str], ...] = (
    # Bare "IB" and friends: "IB Analyst", "TMT IB Associate", "C&IB". TMT is
    # a banking coverage group — as a person's group label it names banking.
    (r"\bib\b|\bibd\b|\btmt\b|\bbank(?:er|ing)\b|\brestructuring\b"
     r"|\becm\b|\bdcm\b", "ib"),
    # "Global Markets Analyst", "Equity Research", "S&T" — markets seats the
    # posting vocabulary spells differently.
    #
    # THE DESK NAMES ARE HERE BECAUSE THE HIDE LIST CONTAINS `\bsales\b`, and
    # ordering is this module's whole safety mechanism: a keep-signal has to
    # fire BEFORE `_OFF_TRACK_ROLE_RE` is consulted or the person is hidden.
    # The docstring below claims "'Equities Sales' keeps on 'equities' before
    # 'sales' is ever consulted" — true, but only because `role_function`
    # happens to recognise that one product word, and the protection does not
    # generalise. Measured against the classifier as it stood: "Credit Sales",
    # "Prime Brokerage Sales" and "Structured Products Sales" all reached
    # `_OFF_TRACK_ROLE_RE` and were HIDDEN. Those are sell-side markets seats
    # on `st`, one of the six tracks Coverage covers — the wrongly-hidden
    # banker this module's stated asymmetry calls the expensive error, and it
    # was firing on the track whose people sign their titles with the word the
    # hide list bans.
    #
    # Every entry names a DESK, never a word that merely co-occurs with one:
    # a product name (`fixed income`, `structured products`, `prime
    # brokerage`), or a product qualifying `sales` (`credit sales`, `fx
    # sales`). A bare "Sales" still reaches the hide list and still hides the
    # verified Amazon row, because a bare "Sales" names no desk.
    (r"\bs&t\b|\bsales (?:&|and) trading\b|\bglobal markets\b"
     r"|\bequity research\b|\bhedge fund\b"
     r"|\bprime brokerage\b|\bstructured products?\b|\bfixed income\b"
     r"|\bforeign exchange\b|\bderivatives\b|\bcommodities\b"
     r"|\b(?:credit|rates|fx|equity|equities|securities|institutional|flow"
     r"|cross[- ]asset|municipal|convertible|emerging markets)\s+sales\b",
     "st"),
    (r"\bpe\b", "pe"),
    # The study-toward vocabulary: finance-club peers ("Trojan Investing
    # Society", "finance/restructuring interest club", "GIS student investing
    # club") and alumni whose role says only "finance professional". Not a
    # track by name, but squarely inside the industry the six tracks cover.
    (r"\bfinance\b|\bfinancial\b|\binvest(?:ing|ment|or)s?\b", "finance"),
)
_PERSON_TRACK_RES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx, re.IGNORECASE), track) for rx, track in _PERSON_TRACK_MARKERS
)


def _track_signal(text: str) -> str:
    """The track this text places its person in, or "". Checks the posting
    vocabulary first (`role_function` — but only a REAL track: its "none"
    means "names a non-track function" and must not read as a match), then
    the signature extras above."""
    if not text:
        return ""
    from directory.recommend import role_function

    named = role_function(text)
    if named and named != "none":
        return named
    for rx, track in _PERSON_TRACK_RES:
        if rx.search(text):
            return track
    return ""


# ---------------------------------------------------------------------------
# Hide vocabulary — the row itself places them outside every track.
# ---------------------------------------------------------------------------
# Matched against `role` ONLY, and only after every keep-signal has had its
# chance — ordering is the safety mechanism, not the word list. "Equities
# Sales" keeps on "equities" before "sales" is ever consulted; "Fintech IB
# Associate" keeps on "IB" before "fintech" is. Free-prose fields (`notes`,
# `angle`) are never scanned for these: prose mentions everything ("referencing
# her AWS background", "PwC audit before CLSA") and hiding on it is the
# expensive error direction.
#
# Two families, so the ledger can say which claim it is making:
#
# CAMPUS — teaches or administers at a university. The founder's verified
# rows: two professors (WRIT 150, BUAD 306) and the Dornsife First-Year
# Advising office. `crm/relevance.py` once recorded "a professor is a
# perfectly good coffee chat" — that was about the ASK for people already in
# the network, and the founder's new rule overrides it for membership:
# faculty and campus staff are not part of recruiting.
_CAMPUS_ROLE_RE = re.compile(
    r"\bprofessor\b|\blecturer\b|\bfaculty\b|\bdean\b|\binstructor\b"
    r"|\bteaching assistant\b"
    r"|\b(?:on-campus|campus|university) staff\b"
    r"|\bacademic advis\w*\b|\bstudent affairs\b|\bregistrar\b"
    r"|\badmissions\b|\bresidential\b",
    re.IGNORECASE,
)
# OFF-TRACK FUNCTION — a corporate seat outside all six tracks. Verified
# rows: "Account Manager, AWS" and "Sales" at Amazon (a tiered firm — which
# is exactly why the person's role must beat the firm's tier), "technology
# background", "in fintech", "audit/accounting professional". Deliberately
# NOT `directory.recommend._NON_TRACK_FUNCTION` wholesale: that blocklist is
# calibrated for postings and would hide people doctrine protects (the
# "USC alum, HR/people professional" `crm/relevance.py` documents is a normal
# networking contact, and operations/middle-office bankers can still make
# introductions). Every entry here is a phrase that names the person's whole
# profession, not a word that co-occurs with banking.
_OFF_TRACK_ROLE_RE = re.compile(
    r"\bsales\b|\baccount (?:manager|executive)\b|\bcustomer success\b"
    r"|\bmarketing\b|\bproduct manager\b"
    r"|\bsoftware\b|\bdeveloper\b|\bengineer(?:ing)?\b"
    r"|\btechnology\b|\bfintech\b"
    r"|\baudit(?:or|ing)?\b|\baccounting\b|\btax\b|\blegal\b",
    re.IGNORECASE,
)


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def classify_person(
    *,
    role: str = "",
    notes: str = "",
    angle: str = "",
    override: bool | None = None,
    tiered: bool = False,
    firm_tracks=(),
    firm_label: str = "",
) -> Verdict:
    """The ladder, strongest evidence first. Every rung cites its row.

    1. The user's own word (`Contact.recruitment_related`) — wins both ways,
       permanently, over everything below.
    2. A recruiting-function role — recruiting IS the job, whoever employs
       them (`crm.relevance.is_recruiting_role`; the user's explicit
       `recruiting_contact=True` answer joins this rung in
       `contact_verdict`). A Deloitte campus recruiter and a West Monroe
       talent-acquisition manager both pass here.
    3. A role (or the user's own notes about them) naming a covered track or
       the finance vocabulary — a CLSA "IB Analyst" passes on the role alone,
       whatever the tier list says about CLSA.
    4. A role naming a campus or off-track seat — the only path to HIDE the
       rule itself can take, and it is reached only when 2 and 3 said
       nothing. The person's own role beats the firm's tier (the AWS account
       manager at tiered Amazon hides).
    5. Firm evidence — on the user's tier list, or a directory firm whose
       `tracks` are non-empty. Rescues the silent-role rows: a blank-role
       contact at Barclays is somebody the user met through recruiting.
    6. KEPT. No signal either way is not evidence of unrelatedness, and the
       asymmetry above says the tie goes to keeping.
    """
    if override is True:
        return Verdict(KEEP, "override", "You marked them recruitment-related.")
    if override is False:
        return Verdict(HIDE, "override", "You marked them not recruitment-related.")

    role = (role or "").strip()
    if is_recruiting_role(role):
        return Verdict(
            KEEP, "recruiter",
            f"Recruiting is their job: “{_clip(role)}”.",
        )

    track = _track_signal(role)
    if track:
        return Verdict(
            KEEP, "track_role",
            f"Their role places them in the industry: “{_clip(role)}”.",
        )
    for text in (notes, angle):
        track = _track_signal(text)
        if track:
            return Verdict(
                KEEP, "track_notes",
                "Your own notes place them in the industry.",
            )

    campus = _CAMPUS_ROLE_RE.search(role)
    if campus:
        return Verdict(
            HIDE, "campus",
            f"Campus role, not recruiting: “{_clip(role)}”.",
        )
    off_track = _OFF_TRACK_ROLE_RE.search(role)
    if off_track:
        return Verdict(
            HIDE, "off_track",
            f"Their role “{_clip(role)}” is outside the tracks you recruit in.",
        )

    if tiered:
        return Verdict(
            KEEP, "tiered_firm",
            f"At {firm_label or 'a firm'} — on your target list.",
        )
    if firm_tracks:
        return Verdict(
            KEEP, "track_firm",
            f"At {firm_label or 'a firm'}, which works in the tracks you recruit in.",
        )

    return Verdict(KEEP, "no_signal", "Nothing on the row places them outside your recruiting.")


def role_hint_disqualified(role_hint: str) -> bool:
    """The capture-time half of the same rule, for `capture.discovery`.

    A proposal carries only a role hint parsed off the sender's display name
    — no notes, no tier, no override — so this is `classify_person` run on
    exactly that evidence: HIDE if and only if the hint names a campus or
    off-track seat and neither the recruiting nor the track vocabulary
    rescued it first. Pure text, no query, and by construction it can never
    disagree with what the board would decide about the same words.
    """
    return classify_person(role=role_hint).verdict == HIDE


def contact_verdict(contact, *, tiers, firm_tracks, firm_label="") -> Verdict:
    """`classify_person` over one ORM `Contact` row.

    `tiers` is `crm.relevance.tiered_firm_tiers`' dict (membership is the
    test — a None tier is the real "Unranked" answer); `firm_tracks` maps
    firm_id -> the directory firm's `tracks` list, built by the caller from
    rows it already loaded so this stays query-free. `firm_label` likewise:
    a display name for the keep-reason sentence, passed in rather than read
    off `contact.firm` so a caller that did not `select_related` pays no
    per-row query for a decoration.

    `recruiting_contact=True` — the user's own "yes, this person is part of
    the recruiting process" — is folded into the recruiter rung: an explicit
    yes about somebody's recruiting function is also an answer about their
    relevance. An explicit False is NOT folded into anything: "a normal
    networking contact, not a recruiter" says nothing about whether they are
    recruitment-related, so the ladder just runs. The `recruitment_related`
    override still outranks both, in both directions.
    """
    if contact.recruitment_related is None and contact.recruiting_contact is True:
        return Verdict(
            KEEP, "recruiter", "You marked them a recruiting contact."
        )
    return classify_person(
        role=contact.role,
        notes=contact.notes,
        angle=contact.angle,
        override=contact.recruitment_related,
        tiered=contact.firm_id in tiers,
        firm_tracks=tuple(firm_tracks.get(contact.firm_id) or ()),
        firm_label=(firm_label or contact.firm_text or ""),
    )


def hidden_contact_ids(user) -> set[int]:
    """Ids of active contacts the rule (or the user's own override) hides —
    the recruitment-relevance twin of `campaigns.excluded_contact_ids`, for
    callers that have not already loaded the rows (`crm.today`'s cockpit).
    Three `.for_user`/id-scoped queries, no per-row queries.
    """
    from directory.models import Firm

    from .models import Contact
    from .relevance import tiered_firm_tiers

    contacts = list(
        Contact.objects.for_user(user).filter(archived=False)
    )
    if not contacts:
        return set()
    tiers = tiered_firm_tiers(user)
    firm_ids = {c.firm_id for c in contacts if c.firm_id}
    firm_tracks = {}
    firm_names = {}
    for fid, name, tracks in Firm.objects.filter(id__in=firm_ids).values_list(
        "id", "name", "tracks"
    ):
        firm_tracks[fid] = tracks or []
        firm_names[fid] = name
    out = set()
    for c in contacts:
        v = contact_verdict(
            c, tiers=tiers, firm_tracks=firm_tracks,
            firm_label=firm_names.get(c.firm_id, ""),
        )
        if v.verdict == HIDE:
            out.add(c.id)
    return out
