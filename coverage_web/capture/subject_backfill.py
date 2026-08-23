"""Stamping Subject headers onto touches that were written before there was
a column to put them in.

WHY THIS EXISTS
---------------
`Touch.subject` was added on 2026-08-22 alongside `crm.campaigns`, and
`capture.gmail._stamp_subject` fills it in only on the way past — a touch is
stamped at the moment it is logged, and never afterwards. Every row already in
the database therefore has a blank subject, on an account where all 292 of them
were written before the column existed.

That blank is what stops the detector from seeing the send it was built for.
`crm.campaigns` groups outbound touches by normalized subject and falls back to
the evidence note when the subject is blank — a fallback that works only when
the sync happened to write the subject INTO the prose. On the founder's account
it did not: his ICC mail merge (201 threads, one subject line) was logged by an
agent-run scan whose notes are one-sentence summaries, each written about a
different person, so 201 identical messages produced ~78 different grouping
keys and the campaign could not form. The queue keeps asking him to follow up
on club admin.

WHAT THE DATABASE ACTUALLY KEPT
-------------------------------
`Touch` stores no headers and no body (§10). What it does keep is
`capture.gmail`'s per-thread dedup marker, `[gmail:<thread_id>]`, prepended to
the evidence note — the same marker `capture.reclassify` and `crm.campaigns`
both already read. That is a real, exact, stored foreign key back into the
mailbox: given a thread id, the Gmail API can still be asked what the subject
was, and the answer is a header rather than a guess.

So the backfill is a join, not an inference. An operator (or an agent with the
mailbox connector) resolves thread ids to subjects OUT OF BAND and hands the
result in as JSON; this module does the matching and the writing, and refuses
to do anything clever with the rows the join does not cover.

THE MAPPING FILE
----------------
A single JSON object, thread id to subject::

    {
      "19fbcd1fe5310001": "Fall 2026 ICC Alumni Digital Panel Outreach",
      "19f2e6ef0c479dc0": "Re: USC student interested in your desk",
      "19aaaaaaaaaaaaaa": null
    }

`null` (or an empty string) is a first-class value and means "this thread id no
longer resolves in the mailbox". It is counted and reported separately from a
thread id that is simply absent from the file, because those are different
facts: one is a thread that was looked up and is gone, the other is a thread
nobody looked up. Neither one is ever guessed at.

WHAT IT REFUSES TO DO, and every refusal is reported rather than silent
----------------------------------------------------------------------
1. **Never overwrites a non-blank subject.** A stamped row was stamped by the
   live capture path from the header itself; this command is working from a
   file a human assembled, which is strictly weaker evidence. The guard is
   applied twice — once when the report is built, and again in the UPDATE's own
   WHERE clause, so a sync that lands between the report and the commit cannot
   be overwritten either.
2. **Never invents a marker.** 192 of the founder's 292 touches carry no
   `[gmail:...]` marker at all: manual overrides, hand-logged coffee chats,
   imports, and discovery-scan rows written before the marker convention. There
   is no thread id to join on and no honest way to produce one, so they are
   counted, explained, and left alone. That count is the real coverage ceiling
   of this approach and the report prints it plainly.
3. **Never resolves an ambiguity.** A note carrying two different markers whose
   subjects disagree is skipped and listed. This should not happen; if it does,
   somebody should look rather than have a coin flipped for them.

Live network: none — this module never talks to Gmail. Live database:
read-only unless the caller commits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from crm.models import Touch

# The same marker `capture.gmail` writes, `capture.reclassify` strips and
# `crm.campaigns` ignores. Alphanumeric because Gmail thread ids are hex in
# practice but the writer never constrained them.
_GMAIL_MARKER_RE = re.compile(r"\[gmail:([0-9a-zA-Z]+)\]")

# `Touch.subject` is a CharField(max_length=255), and `capture.gmail`'s live
# path truncates to the same width. Matching it here means a backfilled row and
# a live-stamped row of the same message are byte-identical, which matters
# because `crm.campaigns` groups on them together.
SUBJECT_MAX = 255


def thread_ids_in(note: str | None) -> list[str]:
    """Every distinct `[gmail:<id>]` id in a note, in first-seen order."""
    seen: list[str] = []
    for tid in _GMAIL_MARKER_RE.findall(note or ""):
        if tid not in seen:
            seen.append(tid)
    return seen


def load_mapping(path: str) -> dict[str, str]:
    """Read the JSON file into `{thread_id: subject}`.

    Empty/`null` subjects are KEPT, as empty strings, because "looked up and
    gone" is information the report prints. Raises `ValueError` with a usable
    message on anything that is not a flat object of string keys.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(
            "mapping must be a JSON object of {thread_id: subject}, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"mapping key {key!r} is not a thread id string")
        if value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value.strip()[:SUBJECT_MAX]
        else:
            raise ValueError(
                f"mapping value for {key!r} must be a string or null, "
                f"got {type(value).__name__}"
            )
    return out


@dataclass
class Stamp:
    """One touch this run would write."""
    touch: Touch
    thread_id: str
    subject: str


@dataclass
class Skip:
    """One touch this run will not write, and the reason in plain words."""
    touch: Touch
    reason: str
    thread_id: str = ""


@dataclass
class Report:
    stamps: list[Stamp] = field(default_factory=list)
    # Every refusal, bucketed by why. Reported separately because they mean
    # different things about the state of the data (see module docstring).
    already_stamped: list[Skip] = field(default_factory=list)
    unmapped: list[Skip] = field(default_factory=list)
    unresolvable: list[Skip] = field(default_factory=list)
    unmarked: list[Skip] = field(default_factory=list)
    ambiguous: list[Skip] = field(default_factory=list)

    touches_seen: int = 0
    mapping_size: int = 0
    # Thread ids in the mapping that no touch of this user's carries. Not an
    # error — the operator may have resolved a wider set than this account uses
    # — but a large number means the mapping was built against the wrong data.
    unused_thread_ids: list[str] = field(default_factory=list)

    @property
    def marked_touches(self) -> int:
        return (
            len(self.stamps) + len(self.already_stamped) + len(self.unmapped)
            + len(self.unresolvable) + len(self.ambiguous)
        )

    @property
    def subject_counts(self) -> list[tuple[str, int]]:
        """`(subject, touches)` for everything this run would stamp, commonest
        first. This is the sanity check the operator actually reads: a mail
        merge shows up here as one subject on dozens of rows, and a mapping
        built against the wrong mailbox shows up as dozens of subjects on one
        row each."""
        counts: dict[str, int] = {}
        for stamp in self.stamps:
            counts[stamp.subject] = counts.get(stamp.subject, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def build_report(user, mapping: dict[str, str]) -> Report:
    """Judge every touch this user has against the mapping. Reads only.

    Scoped to one user by `for_user`, and to every touch KIND rather than just
    the outbound ones: a subject is a fact about the message, not about which
    direction it went, and `crm.campaigns._first_outside` reads the signature of
    inbound touches too.
    """
    report = Report(mapping_size=len(mapping))
    used: set[str] = set()

    for touch in Touch.objects.for_user(user).order_by("ts", "id"):
        report.touches_seen += 1
        tids = thread_ids_in(touch.note)

        if not tids:
            report.unmarked.append(Skip(
                touch, "no [gmail:...] marker — this row was never linked to a "
                "Gmail thread, so there is nothing to look the subject up by",
            ))
            continue

        hits = {tid: mapping[tid] for tid in tids if tid in mapping}
        used.update(hits)

        if not hits:
            report.unmapped.append(Skip(
                touch, "thread id is not in the mapping file — nobody looked "
                "this thread up", thread_id=tids[0],
            ))
            continue

        resolved = {tid: subj for tid, subj in hits.items() if subj}
        if not resolved:
            report.unresolvable.append(Skip(
                touch, "the mapping records this thread as unresolvable in the "
                "mailbox (deleted, or the id no longer answers)",
                thread_id=next(iter(hits)),
            ))
            continue

        distinct = set(resolved.values())
        if len(distinct) > 1:
            report.ambiguous.append(Skip(
                touch, "this note carries markers for two threads whose "
                f"subjects disagree ({sorted(distinct)!r}) — left alone",
                thread_id=",".join(sorted(resolved)),
            ))
            continue

        thread_id, subject = next(iter(resolved.items()))

        if (touch.subject or "").strip():
            report.already_stamped.append(Skip(
                touch, f"already carries a subject ({touch.subject!r}) — a "
                "stamped row came from the header itself and is never "
                "overwritten", thread_id=thread_id,
            ))
            continue

        report.stamps.append(Stamp(touch, thread_id, subject))

    report.unused_thread_ids = sorted(set(mapping) - used)
    return report


def commit(user, report: Report) -> int:
    """Write the report's stamps. Returns how many rows actually changed.

    `filter(subject="")` in the UPDATE is not belt-and-braces decoration: the
    report was built by a read that has since ended, and a Gmail sync running
    in the same minute stamps subjects from the real header. Making the blank
    check part of the WHERE clause means the database, not this process's
    snapshot, is what decides a row is still unstamped — so the returned count
    can legitimately be lower than `len(report.stamps)`, and that is the honest
    number to print.
    """
    written = 0
    for stamp in report.stamps:
        written += (
            Touch.objects.for_user(user)
            .filter(id=stamp.touch.id, subject="")
            .update(subject=stamp.subject[:SUBJECT_MAX])
        )
    return written
