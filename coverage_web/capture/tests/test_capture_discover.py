"""The discovery door: people a mailbox scan found who aren't on the board yet.

`capture_gmail` never creates a contact, and that rule is right for matching —
an unmatched finding there means the systems drifted, and inventing a row
would hide it. Discovery is the opposite intent and gets its own command
rather than a flag that softens the other one's contract.

The tests below are mostly about what it REFUSES to do, because that is where
an automated nightly job can quietly do damage.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from crm.models import Contact, Touch
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="jimmy@example.com", password="x")


@pytest.fixture
def run(tmp_path, user):
    n = {"i": 0}

    def _run(people, **opts):
        n["i"] += 1
        path = tmp_path / f"people-{n['i']}.json"
        path.write_text(json.dumps(people), encoding="utf-8")
        call_command("capture_discover", email=user.email, findings=str(path), **opts)

    return _run


def test_a_new_person_becomes_a_contact(run, user):
    run([{"name": "Ada Lovelace", "email": "ada@gs.com", "role_guess": "Analyst"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.email == "ada@gs.com"
    assert c.role == "Analyst"
    assert c.source == "capture"
    assert "Discovered by mailbox scan" in c.notes


def test_someone_already_tracked_is_not_duplicated(run, user):
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    run([{"name": "Ada Lovelace", "email": "ada@gs.com"}])
    assert Contact.objects.for_user(user).filter(name="Ada Lovelace").count() == 1


def test_a_name_match_alone_is_enough_to_dedupe(run, user):
    """The scan often has no address — a person found on a CC line, say."""
    Contact.all_objects.create(user=user, name="Ada Lovelace")
    run([{"name": "ada lovelace"}])
    assert Contact.objects.for_user(user).count() == 1


def test_an_archived_person_is_reported_never_resurrected(run, user):
    """Archiving is a deliberate user action. A scan finding them again is
    not consent to undo it — and silently creating a second row would fork
    the relationship's history."""
    Contact.all_objects.create(
        user=user, name="Ada Lovelace", email="ada@gs.com", archived=True
    )
    run([{"name": "Ada Lovelace", "email": "ada@gs.com"}])

    rows = Contact.objects.for_user(user).filter(name="Ada Lovelace")
    assert rows.count() == 1, "no second row was forked"
    assert rows.first().archived is True, "still archived"


def test_a_chat_logs_a_real_touch_through_the_ratchet(run, user):
    run([{"name": "Ada Lovelace", "email": "ada@gs.com", "chat_status": "completed"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.warmth == "chatted"
    assert c.thread_state == "chat_done"
    assert Touch.objects.for_user(user).filter(contact=c, kind="chat").exists()


def test_a_booked_chat_is_scheduled_not_had(run, user):
    """"Let's do Tuesday at noon" is a chat that has not happened yet.

    It gets its own rung: `chat_scheduled` leaves warmth at `replied` and
    parks the thread at `chat_scheduled`. Calling it a chat would fire the
    thank-you cadence for a conversation still in the future."""
    run([{"name": "Ada Lovelace", "email": "ada@gs.com", "chat_status": "scheduled"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.warmth == "replied"
    assert c.thread_state == "chat_scheduled"
    kinds = list(Touch.objects.for_user(user).filter(contact=c).values_list("kind", flat=True))
    assert kinds == ["chat_scheduled"]


def test_a_reply_logs_a_reply_not_a_chat(run, user):
    run([{"name": "Ada Lovelace", "email": "ada@gs.com", "replied": True}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.warmth == "replied"


def test_a_warm_email_reply_alone_never_counts_as_a_chat(run, user):
    """The Ellen Chung case, fixed 2026-08-15.

    The findings contract used to carry a boolean `chatted`, defined only as
    "a real conversation happened" — loose enough that a friendly two-way
    email satisfied it. On 2026-08-12 a discovery run marked Ellen Chung
    `chatted` on evidence that was, in full, "Thanks for the email, filled
    out the form!" on an ICC alumni-panel thread. No call, no meeting. That
    set warmth='chatted'/thread_state='chat_done', so Today nagged for a
    debrief of a conversation that never happened — and no later automated
    run could undo it, because `capture_worklist` excludes `chatted` and
    `advocate` from every re-check. A human had to fix it by hand.

    A reply with no `chat_status` is a reply, however warm it reads.
    """
    run([{"name": "Ellen Chung", "email": "ellen@icc.example",
          "replied": True,
          "evidence": "Thanks for the email, filled out the form! Would love "
                      "to help with the ICC alumni panel — so glad you reached out"}])
    c = Contact.objects.for_user(user).get(name="Ellen Chung")
    assert c.warmth == "replied", "a warm email reply is still just a reply"
    assert c.thread_state != "chat_done"
    kinds = list(Touch.objects.for_user(user).filter(contact=c).values_list("kind", flat=True))
    assert kinds == ["reply_received"], "never kind='chat'"


def test_no_evidence_means_no_touch_and_no_warmth(run, user):
    """Someone found on a distribution list never wrote to you. Inventing
    warmth would put them in the cadence queue as if they had."""
    run([{"name": "Ada Lovelace", "email": "ada@gs.com"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.warmth == "cold"
    assert Touch.objects.for_user(user).filter(contact=c).count() == 0


def test_a_known_firm_slug_links_the_contact_to_the_directory(run, user):
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    run([{"name": "Ada Lovelace", "firm": "gs"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.firm_id == firm.id


def test_an_unknown_firm_is_kept_as_text_not_dropped(run, user):
    """`firm_text` exists so capture never blocks on directory coverage."""
    run([{"name": "Ada Lovelace", "firm": "some-boutique"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert c.firm_id is None
    assert c.firm_text == "some-boutique"


def test_dry_run_creates_nothing(run, user):
    run([{"name": "Ada Lovelace", "email": "ada@gs.com"}], dry_run=True)
    assert Contact.objects.for_user(user).count() == 0


def test_a_nameless_finding_is_skipped(run, user):
    run([{"email": "someone@gs.com"}, {"name": "  "}])
    assert Contact.objects.for_user(user).count() == 0


def test_discovery_is_scoped_to_the_named_user(run, user, django_user_model):
    """Another user's contact with the same name must not suppress this
    user's discovery — the dedup has to be tenant-scoped."""
    other = django_user_model.objects.create_user(email="other@x.com", password="x")
    Contact.all_objects.create(user=other, name="Ada Lovelace", email="ada@gs.com")

    run([{"name": "Ada Lovelace", "email": "ada@gs.com"}])
    assert Contact.objects.for_user(user).filter(name="Ada Lovelace").count() == 1
    assert Contact.objects.for_user(other).filter(name="Ada Lovelace").count() == 1


def test_an_over_long_name_is_truncated_not_fatal(run, user):
    """An AGENT writes these findings. One that hands back a whole email
    signature as a "name" used to raise `DataError: value too long for type
    character varying(255)` mid-batch — killing the run and taking every
    valid row after it down with it. Verified 2026-08-02 against Postgres.
    """
    long_name = "A" * 300
    run([{"name": long_name, "email": "bad@gs.com"},
         {"name": "Real Person", "email": "good@gs.com"}])

    names = set(Contact.objects.for_user(user).values_list("name", flat=True))
    assert "Real Person" in names, "the valid row behind the bad one must survive"
    assert len("A" * 255) == max(len(n) for n in names)


def test_an_over_long_firm_text_and_email_are_bounded_too(run, user):
    """Same exposure on the other two free-text columns the agent fills."""
    run([{"name": "Ada Lovelace", "firm": "B" * 400,
          "email": "c" * 300 + "@gs.com"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    assert len(c.firm_text) <= 255
    assert len(c.email) <= 254


def test_outreach_you_already_sent_is_logged_as_a_touch(run, user):
    """The page used to contradict itself.

    Discovery would find a thread where Jimmy had emailed someone who never
    wrote back, create the contact, and write "Follow-up outreach sent … no
    reply yet" into its notes — while logging no touch, because only a
    reply or a chat counted. Zero touches means the cadence engine's
    never-contacted branch fires, so Today said "Added but never contacted.
    Send the first note." about a person whose own notes said otherwise.
    Observed on live data 2026-08-05 and reported by the owner.
    """
    run([{"name": "Jason Law", "email": "jason_law@intuit.com",
          "outreach_sent": True,
          "evidence": "Follow-up outreach sent for ICC alumni panel, no reply yet"}])
    c = Contact.objects.for_user(user).get(name="Jason Law")
    kinds = list(Touch.objects.for_user(user).filter(contact=c).values_list("kind", flat=True))
    assert kinds == ["outreach"], "the note he already sent is on the record"


def test_the_evidence_ladder_takes_the_strongest_signal(run, user):
    """A thread can show all three. A chat outranks a reply outranks outreach
    — the same order the touch ratchet uses everywhere else."""
    run([{"name": "Ada Lovelace", "email": "ada@gs.com",
          "outreach_sent": True, "replied": True, "chat_status": "completed"}])
    c = Contact.objects.for_user(user).get(name="Ada Lovelace")
    kinds = list(Touch.objects.for_user(user).filter(contact=c).values_list("kind", flat=True))
    assert kinds == ["chat"]

