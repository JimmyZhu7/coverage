"""Campaign separation — telling the founder's job search apart from the club
mail merge he sends wearing a different hat.

Every case here corresponds to something verified against his live mailbox and
his live 156-contact database on 2026-08-22:

  - he mail-merged 201 threads with the subject "Fall 2026 ICC Alumni Digital
    Panel Outreach" as USC's International Consulting Club's Associate of
    External Outreach, to alumni at an airline, a health insurer, a jeweller, a
    talent agency and a law firm;
  - Coverage read all of it as his recruiting network, and his Today queue
    proposed a 15-minute chat to a J.P. Morgan banker whose reply was "happy to
    help out on either a panel or for the mentorship program";
  - and separately, his own one-at-a-time Hong Kong coffee-chat requests
    produced signature groups of up to 6, which is the false-positive ceiling
    `BULK_MIN_RECIPIENTS` has to clear.

`transaction=True` for the same reason `test_relevance.py` uses it: the queue
paths reach `crm.services`, which opens its own connection.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm import campaigns as camp
from crm import relevance as rel
from crm.models import Campaign, CampaignContact, Contact, Touch, UserFirm
from crm.today import _build_actions
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)

ICC_SUBJECT = "Fall 2026 ICC Alumni Digital Panel Outreach"


def _user(email="camp@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


def _target_firm(user, slug="jpm", name="J.P. Morgan", tier=1):
    firm = Firm.objects.create(slug=slug, name=name)
    UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    return firm


def _merge(user, *, n, subject=ICC_SUBJECT, at=None, firm=None,
           prefix="Alum", days_ago=20):
    """One mail merge: `n` contacts, one outbound touch each, all inside the
    same second — which is what the founder's real merge looked like
    (2026-08-03T15:30:04Z through 15:30:35Z)."""
    at = at or (timezone.now() - timedelta(days=days_ago))
    made = []
    for i in range(n):
        c = Contact.all_objects.create(
            user=user, name=f"{prefix} {i}", firm=firm,
            firm_text="" if firm else "Some Employer",
        )
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=at + timedelta(seconds=i), subject=subject,
        )
        made.append(c)
    return made


def _actions_by_name(user):
    actions, _ = _build_actions(user)
    return {a["contact"]["name"]: a for a in actions}


# ---------------------------------------------------------------------------
# 1. Detection fires on a real merge.
# ---------------------------------------------------------------------------
def test_detection_fires_on_a_real_bulk_merge():
    """THE MEASURED BUG. 201 threads, one subject, one burst."""
    user = _user()
    _merge(user, n=12)

    found = camp.detect(user)

    assert len(found) == 1
    assert found[0].recipient_count == 12
    assert ICC_SUBJECT.lower().startswith(found[0].signature.split()[0])
    assert found[0].label == ICC_SUBJECT
    # Detected is not classified. Nobody has been hidden.
    assert found[0].kind == Campaign.KIND_UNCLASSIFIED
    assert found[0].classified_at is None


def test_detection_does_not_fire_on_three_personal_notes_sharing_a_subject():
    """The explicit counter-example: a genuine personal note that happens to
    share a subject with two others is NOT a campaign."""
    user = _user()
    _merge(user, n=3, subject="Coffee chat?")

    assert camp.detect(user) == []


def test_detection_does_not_fire_just_below_the_floor():
    """The founder's own one-at-a-time Hong Kong coffee-chat requests produced
    signature groups of 6. Seven must still be silence."""
    user = _user()
    _merge(user, n=camp.BULK_MIN_RECIPIENTS - 1, subject="USC student coffee chat")

    assert camp.detect(user) == []


def test_a_signature_reused_across_weeks_is_not_one_giant_burst():
    """Anchoring the 24-hour window on the FIRST member of each group, not the
    previous one — otherwise a subject the user writes every Monday chains into
    a single campaign spanning months."""
    user = _user()
    now = timezone.now()
    for week in range(4):
        _merge(user, n=3, subject="Weekly check in", prefix=f"W{week}",
               at=now - timedelta(days=30 - week * 7))

    assert camp.detect(user) == []


def test_a_merge_that_straddles_midnight_stays_one_campaign():
    """The window is measured in hours, not calendar days, so a send that runs
    from 23:58 to 00:02 is one campaign rather than two half-sized ones that
    each miss the floor."""
    user = _user()
    base = timezone.now() - timedelta(days=10)
    midnight = base.replace(hour=23, minute=58, second=0, microsecond=0)
    _merge(user, n=5, at=midnight, prefix="Late")
    _merge(user, n=5, at=midnight + timedelta(minutes=4), prefix="Early")

    found = camp.detect(user)

    assert len(found) == 1
    assert found[0].recipient_count == 10


def test_detection_falls_back_to_the_evidence_note_when_no_subject_is_stored():
    """Every touch already in the founder's database has a blank subject —
    `Touch.subject` was added with this feature and `capture.gmail_live` had
    been discarding the header. The evidence note is the only content those
    rows kept, so it is the fallback key. See `crm.campaigns`'s docstring."""
    user = _user()
    for i in range(10):
        c = Contact.all_objects.create(user=user, name=f"Historic {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20, seconds=i),
            # Exactly the shape `capture.gmail` writes: the per-thread dedup
            # marker plus a short evidence line. The marker is unique per
            # thread and must be stripped, or nothing ever groups.
            note=f"[gmail:19fbcd1fe531{i:04d}] Jimmy sent ICC alumni panel "
                 f"outreach 2026-08-0{i % 9 + 1}; no reply yet.",
        )

    found = camp.detect(user)

    assert len(found) == 1
    assert found[0].recipient_count == 10
    # The date inside the note varies per row and must not split the group.
    assert "gmail" not in found[0].signature


# ---------------------------------------------------------------------------
# 1b. …and the fallback's precondition: the note has to be somebody's words.
#
# CAMPAIGN 3, live account, 2026-08-23: 41 recipients, all 41 originating,
# signature `outreach sent no reply yet`. Not a subject line — Coverage's own
# placeholder, on 40 touches whose `subject` column had not been invented when
# they were written. One answer of "not my recruiting" would have silenced 41
# genuine target-firm bankers whose real subjects, stamped later, were 40
# different personalised lines.
# ---------------------------------------------------------------------------
_HK_SUBJECTS = [
    "HK Jul 29-31 | Nomura | IBD - USC Student Coffee Chat Request",
    "HK Jul 29-31 | CLSA | CICC - USC Student Coffee Chat Request",
    "HK Jul 29-31 | Goldman Sachs | Nomura - USC Student Coffee Chat Request",
    "HK Jul 29-31 | HSBC | BNP Paribas - USC Student Coffee Chat Request",
    "HK Jul 29-31 | Lazard | CICC - USC Student Coffee Chat Request",
    "HK Jul 29-31 | CITIC | ICBC - USC Student Coffee Chat Request",
    "HK Jul 29-31 | Citi | Blackstone - USC Student Coffee Chat Request",
    "HK Jul 29-31 | Greenhill | UBS - USC Student Coffee Chat Request",
]


def test_coverages_own_boilerplate_note_never_forms_a_campaign():
    """THE CAMPAIGN 3 BUG. Forty subject-less touches sharing nothing but the
    placeholder this app wrote onto all of them. Identical by construction is
    not evidence of a shared send."""
    user = _user()
    at = timezone.now() - timedelta(days=30)
    for i in range(40):
        c = Contact.all_objects.create(user=user, name=f"Banker {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            # Date-only midnight, the bulk-import path's stamp: 96 of the
            # founder's 117 outbound touches carry one, so all 40 land in a
            # single burst and clear the floor five times over.
            ts=at.replace(hour=0, minute=0, second=0, microsecond=0),
            note="Outreach sent 2026-07-24, no reply yet",
        )

    assert camp.detect(user) == []
    assert CampaignContact.objects.for_user(user).count() == 0


def test_the_same_people_still_do_not_group_once_their_real_subjects_land():
    """The follow-on half. The subject backfill gives those touches their
    actual headers, which are forty different personalised lines — so the
    right answer stays "no campaign" for a second, independent reason."""
    user = _user()
    at = timezone.now() - timedelta(days=30)
    for i, subject in enumerate(_HK_SUBJECTS * 5):
        c = Contact.all_objects.create(user=user, name=f"Banker {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=at, subject=f"{subject} [{i}]",
            note="Outreach sent 2026-07-24, no reply yet",
        )

    assert camp.detect(user) == []


@pytest.mark.parametrize("note", [
    "Outreach sent 2026-07-24, no reply yet",
    "Follow-up outreach sent 2026-08-03, no reply yet",
    "Discovered by mailbox scan",
    "Discovered by mailbox scan — bulk/automated email, not a reply",
    "Parked from the Today queue",
    "Parked from the Today queue (bulk)",
    "Parked from the Network board (bulk)",
    "Correction: 3 inbound messages recorded as a reply turned out to be "
    "bulk or automated mail. Status recalculated from the remaining evidence.",
    "Promoted to advocate from the chat debrief on 2026-08-01 "
    "(they said they'd advocate for you).",
])
def test_every_note_this_app_composes_is_refused_a_signature(note):
    """One case per call site that writes a `Touch.note`. Keep this list in
    step with `campaigns._APP_AUTHORED_NOTES` and with the writers it names."""
    assert camp._is_app_authored(camp.normalize_subject(note))


@pytest.mark.parametrize("note", [
    "Fall 2026 ICC Alumni Digital Panel Outreach",
    "Sent: HK Jul 29-31 | Nomura | IBD - USC Student Coffee Chat Request",
    "Follow-up outreach sent for the ICC alumni panel, no reply yet",
    "ICC alumni panel outreach backfilled from Gmail",
])
def test_a_note_carrying_the_senders_words_keeps_its_signature(note):
    """Boilerplate WRAPPED AROUND a real send still groups — that composition
    is how the ICC merge was found before `Touch.subject` existed."""
    assert not camp._is_app_authored(camp.normalize_subject(note))


def test_the_icc_merge_still_detects_through_notes_alone():
    """The detection the fallback exists for, in the shape it actually had:
    twelve recipients, no subject stored anywhere, the club's real subject
    surviving only inside an evidence line this app wrote around it."""
    user = _user()
    at = timezone.now() - timedelta(days=20)
    for i in range(12):
        c = Contact.all_objects.create(user=user, name=f"Alum {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=at + timedelta(seconds=i),
            note=f"[gmail:19fbcd1fe531{i:04d}] Outreach sent 2026-07-06 for "
                 f"the Fall 2026 ICC Alumni Digital Panel, no reply yet",
        )

    found = camp.detect(user)

    assert len(found) == 1
    assert found[0].recipient_count == 12
    assert "icc" in found[0].signature


def test_detection_is_idempotent():
    user = _user()
    _merge(user, n=12)

    camp.detect(user)
    camp.detect(user)
    camp.detect(user)

    assert Campaign.objects.for_user(user).count() == 1
    assert CampaignContact.objects.for_user(user).count() == 12


def test_detection_is_scoped_to_one_tenant():
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    _merge(mine, n=10)
    _merge(theirs, n=10)

    camp.detect(mine)

    assert Campaign.objects.for_user(mine).count() == 1
    assert Campaign.objects.for_user(theirs).count() == 0


# ---------------------------------------------------------------------------
# 2. `originates` — whose relationship actually started here.
# ---------------------------------------------------------------------------
def test_someone_already_being_recruited_does_not_originate_in_the_blast():
    """The banker the founder had been working for a month, who also got the
    club merge because he is a USC alum. His relationship did not start there
    and a club answer must not sweep him out."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    prior = people[0]
    Touch.all_objects.create(
        user=user, contact=prior, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=60), subject="Quick question",
    )

    campaign = camp.detect(user)[0]
    membership = CampaignContact.objects.for_user(user).get(
        campaign=campaign, contact=prior
    )

    assert membership.originates is False
    assert CampaignContact.objects.for_user(user).filter(
        campaign=campaign, originates=True
    ).count() == 9


def test_a_second_wave_of_the_same_campaign_does_not_cancel_originating():
    """CAUGHT ON HIS REAL DATA. The ICC merge went out on 6 July and followed
    up on 3 August. Measuring a 3 August recipient against "their earliest
    touch of any kind" makes their own 6 July invitation — from the same
    campaign — count as prior history, so the person the campaign emailed
    TWICE came out as one it did not start. Exactly backwards."""
    user = _user()
    people = _merge(user, n=10, prefix="Wave", days_ago=45)
    for c in people:
        Touch.all_objects.create(
            user=user, contact=c, kind="follow_up", channel="email",
            ts=timezone.now() - timedelta(days=20), subject=ICC_SUBJECT,
        )

    found = camp.detect(user)

    # One subject is one send is one decision — two waves must not become two
    # identical questions in Settings.
    assert len(found) == 1
    assert found[0].recipient_count == 10
    assert CampaignContact.objects.for_user(user).filter(
        campaign=found[0], originates=True
    ).count() == 10
    # And the window spans both waves rather than reporting only the latest.
    assert (found[0].last_sent - found[0].first_sent) > timedelta(days=20)


def test_a_reply_to_the_campaign_does_not_cancel_originating():
    """`capture.gmail` writes an inbound touch's evidence from the same thread
    summary as the outbound one, so a reply to the merge signs identically to
    it. Counting that as prior history would be counting the campaign's own
    consequence as its precondition."""
    user = _user()
    people = _merge(user, n=10, days_ago=20)
    Touch.all_objects.create(
        user=user, contact=people[0], kind="reply_received", channel="email",
        ts=timezone.now() - timedelta(days=19), subject=ICC_SUBJECT,
    )

    campaign = camp.detect(user)[0]

    assert CampaignContact.objects.for_user(user).get(
        campaign=campaign, contact=people[0]
    ).originates is True


def test_a_prior_relationship_stamped_the_same_midnight_does_not_originate():
    """THE SECOND MISSING VALUE. 96 of the founder's 117 outbound touches carry
    a date-only midnight stamp, so "same day" and "same instant" are the same
    number in this column. A banker he had already been working, whose earlier
    touch was imported at that same midnight, must not be read as somebody the
    blast introduced him to."""
    user = _user()
    midnight = (timezone.now() - timedelta(days=20)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    people = _merge(user, n=10, at=midnight)
    Touch.all_objects.create(
        user=user, contact=people[0], kind="outreach", channel="email",
        ts=midnight, subject="Coffee chat after the info session",
    )

    campaign = camp.detect(user)[0]

    assert CampaignContact.objects.for_user(user).get(
        campaign=campaign, contact=people[0]
    ).originates is False
    # Everyone the campaign really did introduce is untouched by this.
    assert CampaignContact.objects.for_user(user).filter(
        campaign=campaign, originates=True
    ).count() == 9


# ---------------------------------------------------------------------------
# 3. Classification and the queue.
# ---------------------------------------------------------------------------
def test_an_unclassified_campaign_changes_nothing():
    """The default must never hide anybody. Surfacing the question is the fix;
    guessing the answer would be a worse bug than the one being fixed."""
    user = _user()
    firm = _target_firm(user)
    _merge(user, n=10, firm=firm, days_ago=20)
    camp.detect(user)

    assert camp.excluded_contact_ids(user) == set()
    assert len(_actions_by_name(user)) == 10


def test_classifying_a_campaign_as_other_empties_it_out_of_the_queue():
    """THE FIX. Ten tier-1 bankers who only ever heard from him about a club
    panel stop producing daily actions."""
    user = _user()
    firm = _target_firm(user)
    _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]

    before = _actions_by_name(user)
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)
    after = _actions_by_name(user)

    assert len(before) == 10
    assert after == {}
    # And they are still there, in full.
    assert Contact.objects.for_user(user).filter(archived=False).count() == 10


def test_classifying_as_recruiting_leaves_the_queue_alone():
    user = _user()
    firm = _target_firm(user)
    _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]

    camp.classify(user, campaign.id, Campaign.KIND_RECRUITING)

    assert len(_actions_by_name(user)) == 10


def test_reclassification_puts_them_back():
    """Changing your mind is a first-class action, not an edge case."""
    user = _user()
    firm = _target_firm(user)
    _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]

    camp.classify(user, campaign.id, Campaign.KIND_OTHER)
    assert _actions_by_name(user) == {}

    camp.classify(user, campaign.id, Campaign.KIND_RECRUITING)
    assert len(_actions_by_name(user)) == 10

    camp.classify(user, campaign.id, Campaign.KIND_UNCLASSIFIED)
    reset = Campaign.objects.for_user(user).get(id=campaign.id)
    assert reset.classified_at is None
    assert len(_actions_by_name(user)) == 10


def test_re_detection_never_overwrites_the_users_answer():
    """The manual-override contract. A detector that re-ran nightly and reset
    an answer to "unclassified" would be worse than no detector."""
    user = _user()
    _merge(user, n=10)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)
    answered_at = Campaign.objects.for_user(user).get(id=campaign.id).classified_at

    camp.detect(user)

    still = Campaign.objects.for_user(user).get(id=campaign.id)
    assert still.kind == Campaign.KIND_OTHER
    assert still.classified_at == answered_at


def test_re_detection_never_flips_originates():
    """A membership is a fact about what happened in the mailbox on one day. A
    touch logged next week must not be able to rewrite it."""
    user = _user()
    people = _merge(user, n=10, days_ago=20)
    campaign = camp.detect(user)[0]
    assert CampaignContact.objects.for_user(user).get(
        campaign=campaign, contact=people[0]
    ).originates is True

    # A backfill later discovers an OLDER email to the same person.
    Touch.all_objects.create(
        user=user, contact=people[0], kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=90), subject="Something else",
    )
    camp.detect(user)

    assert CampaignContact.objects.for_user(user).get(
        campaign=campaign, contact=people[0]
    ).originates is True


def test_a_hand_exempted_contact_keeps_their_place_in_the_queue():
    """`Contact.campaign_exempt` is the user's word about one person inside a
    two-hundred-person answer, and detection never writes it."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    rescued = people[0]
    rescued.campaign_exempt = True
    rescued.save(update_fields=["campaign_exempt"])

    camp.detect(user)

    assert camp.excluded_contact_ids(user) == {
        c.id for c in people if c.id != rescued.id
    }
    assert set(_actions_by_name(user)) == {rescued.name}
    assert Contact.objects.for_user(user).get(id=rescued.id).campaign_exempt is True


# ---------------------------------------------------------------------------
# 4. The inbound override still wins.
# ---------------------------------------------------------------------------
def test_a_campaign_contact_who_wrote_in_still_surfaces():
    """Nick Tehle (J.P. Morgan) replied to the panel invitation. He is helping
    the club, so he gets no coffee-chat ask — but a person who wrote to you and
    is waiting on an answer surfaces whatever else is true about them. Same
    override `crm.relevance` already implements for non-relevant contacts, not
    a second mechanism."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    replier = people[0]
    Touch.all_objects.create(
        user=user, contact=replier, kind="reply_received", channel="email",
        # Six calendar days, not four: the surfacing action is engine branch
        # 7, which needs the reply idle >= 3 BUSINESS days. Four calendar
        # days back is only 2 business days when the test runs on a Sunday,
        # and this test failed every weekend until the audit run of
        # 2026-08-23 (a Sunday) caught it. Six calendar days is at least
        # four business days from any day of the week.
        ts=timezone.now() - timedelta(days=6),
    )
    # `Touch.all_objects.create` writes the row without running the pipeline
    # ratchet, so the state it would have moved is set here — same as
    # `test_relevance.py`'s own inbound case.
    replier.warmth = "replied"
    replier.thread_state = "replied"
    replier.save(update_fields=["warmth", "thread_state"])

    actions = _actions_by_name(user)

    assert set(actions) == {replier.name}
    assert actions[replier.name]["relevance"] == rel.REL_INBOUND
    assert actions[replier.name]["owed_reply"] is True


def test_a_campaign_contact_with_nothing_owed_stays_out():
    """The override is narrow on purpose: it grants an answer to somebody who
    wrote, not permanent readmission to the queue."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    # They replied, and he already answered. Nothing is owed.
    replier = people[0]
    Touch.all_objects.create(
        user=user, contact=replier, kind="reply_received", channel="email",
        ts=timezone.now() - timedelta(days=10),
    )
    Touch.all_objects.create(
        user=user, contact=replier, kind="follow_up", channel="email",
        ts=timezone.now() - timedelta(days=9),
    )
    replier.warmth = "replied"
    replier.thread_state = "replied"
    replier.save(update_fields=["warmth", "thread_state"])

    assert _actions_by_name(user) == {}


# ---------------------------------------------------------------------------
# 5. The pure relevance function, with no database behind it.
# ---------------------------------------------------------------------------
def test_relevance_gate_is_a_pure_function_of_the_dict():
    tiered = {"firm_id": 7, "campaign_excluded": True}
    assert rel.contact_relevance(tiered, {7: 1}, owed_reply=False) is rel.REL_NONE
    assert rel.contact_relevance(tiered, {7: 1}, owed_reply=True) == rel.REL_INBOUND
    # Without the flag, the same person is a tier-1 target.
    assert rel.contact_relevance(
        {"firm_id": 7}, {7: 1}, owed_reply=False
    ) == rel.REL_TIERED


def test_a_campaign_contact_is_never_told_they_are_not_a_target_firm():
    """A campaign contact who wrote in comes back as REL_INBOUND, whose usual
    lead sentence is "Not a target firm". The ICC merge reached bankers at
    J.P. Morgan and BNP Paribas, so that sentence would have been the inverse
    of the founder's own tier list."""
    reason = rel.keep_warm_reason({
        "contact": {"campaign_excluded": True, "warmth": "chatted"},
        "relevance": rel.REL_INBOUND,
        "relevance_tier": 1,
    })

    assert "Not a target firm" not in reason
    assert reason.startswith("From one of your campaigns")


def test_a_campaign_contact_who_wrote_in_is_asked_for_a_reply_not_a_chat():
    """WATCHED LIVE (audit 2026-08-23). The surfaced panelist's card kept the
    engine's own action — branch 7's "they replied — propose a 15-min chat" —
    which is the exact ask the classification existed to stop. The override
    grants an answer and nothing else, so the card is a Reply."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    replier = people[0]
    Touch.all_objects.create(
        user=user, contact=replier, kind="reply_received", channel="email",
        ts=timezone.now() - timedelta(days=6),
    )
    replier.warmth = "replied"
    replier.thread_state = "replied"
    replier.save(update_fields=["warmth", "thread_state"])

    a = _actions_by_name(user)[replier.name]
    assert a["label"] == rel.CAMPAIGN_REPLY_LABEL
    assert a["reason"] == rel.CAMPAIGN_REPLY_REASON
    assert a["action"] == "advance"


def test_a_campaign_contact_never_carries_a_reping():
    """The worse variant, also watched live: the panelist's firm had a
    confirmed close inside the re-ping window, so his card read "app closes
    2026-08-30. Re-ping before you submit" — a priority-0 recruiting ask, in
    "Don't lose these", snooze-exempt, about a relationship the user had just
    said was not their recruiting. The deadline chip goes with it: the close
    date still lives on every surface that IS about their recruiting."""
    from directory.models import FirmDate

    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=7), confidence=1.0,
    )
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    replier = people[0]
    Touch.all_objects.create(
        user=user, contact=replier, kind="reply_received", channel="email",
        ts=timezone.now() - timedelta(days=4),
    )
    replier.warmth = "replied"
    replier.thread_state = "replied"
    replier.region = "us"
    replier.save(update_fields=["warmth", "thread_state", "region"])

    a = _actions_by_name(user)[replier.name]
    assert a["action"] == "advance"
    assert a["label"] == rel.CAMPAIGN_REPLY_LABEL
    assert a["priority"] == 1
    assert a["closes_on"] is None
    assert "Re-ping" not in a["reason"]


def test_waiting_on_reply_does_not_hold_campaign_contacts_forever():
    """On the founder's real account the ICC merge alone would fill the
    "Waiting on reply" strip with 190-odd club recipients, drowning every
    genuine recruiting wait. Once the send is classified `other`, its
    originating contacts leave that strip along with the queue — and come
    back the moment the answer is changed."""
    from crm.today import _cockpit_context

    user = _user()
    # No firm on purpose: at a non-target employer these contacts never had a
    # queue card (the relevance gate drops them), so "Waiting on reply" was
    # the ONE surface still holding them — which is exactly the leak.
    people = _merge(user, n=10, days_ago=20)
    for c in people:
        c.thread_state = "no_reply"
        c.save(update_fields=["thread_state"])
    campaign = camp.detect(user)[0]

    names = {p.name for p in people}
    waiting = {c.name for c in _cockpit_context(user)["waiting"]["people"]}
    assert names & waiting  # unclassified: status quo, they are listed

    camp.classify(user, campaign.id, Campaign.KIND_OTHER)
    waiting = {c.name for c in _cockpit_context(user)["waiting"]["people"]}
    assert not (names & waiting)

    camp.classify(user, campaign.id, Campaign.KIND_RECRUITING)
    waiting = {c.name for c in _cockpit_context(user)["waiting"]["people"]}
    assert names & waiting


def test_classify_message_counts_only_this_campaign(client):
    """WATCHED LIVE (audit 2026-08-23): with a 9-recipient merge already
    classified `other`, answering an 8-recipient send flashed "17 contacts
    affected". The sentence names one send, so the number is that send's."""
    from django.urls import reverse

    user = _user("counts@example.com")
    firm = _target_firm(user, slug="counts-bank", name="Counts Bank")
    _merge(user, n=10, firm=firm, days_ago=20)
    _merge(user, n=8, subject="Speaker series invitation", days_ago=10,
           prefix="Guest")
    detected = camp.detect(user)
    big = next(c for c in detected if c.recipient_count == 10)
    small = next(c for c in detected if c.recipient_count == 8)
    client.force_login(user)

    client.post(reverse("crm:classify_campaign"),
                {"campaign": big.id, "kind": Campaign.KIND_OTHER})
    resp = client.post(
        reverse("crm:classify_campaign"),
        {"campaign": small.id, "kind": Campaign.KIND_OTHER}, follow=True,
    )
    body = resp.content.decode()
    assert "8 contacts hidden" in body
    assert "18 contacts hidden" not in body


# ---------------------------------------------------------------------------
# 6. The normalizer.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [
    (ICC_SUBJECT, f"Re: {ICC_SUBJECT}"),
    (ICC_SUBJECT, f"FWD: {ICC_SUBJECT}"),
    ("Outreach sent 2026-07-24, no reply yet", "Outreach sent 2026-08-03, no reply yet"),
    ("[gmail:19fbcd1f] Sent: Panel", "[gmail:aaaabbbb] Sent: Panel"),
])
def test_normalizer_collapses_what_a_merge_varies(a, b):
    assert camp.normalize_subject(a) == camp.normalize_subject(b)


@pytest.mark.parametrize("a,b", [
    ("Coffee chat?", "Quick question"),
    ("USC student reaching out", "USC alum reaching out"),
])
def test_normalizer_keeps_apart_what_a_person_writes(a, b):
    assert camp.normalize_subject(a) != camp.normalize_subject(b)


def test_a_touch_with_no_subject_and_no_note_never_groups():
    """Otherwise every hand-logged email with no evidence line would collapse
    into one enormous fake campaign."""
    user = _user()
    for i in range(20):
        c = Contact.all_objects.create(user=user, name=f"Blank {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20),
        )

    assert camp.detect(user) == []


# ---------------------------------------------------------------------------
# 7. The Settings card.
# ---------------------------------------------------------------------------
def test_campaign_cards_state_both_counts():
    """"201 recipients" alone would overstate what an answer does — only the
    contacts whose relationship started here actually move."""
    user = _user()
    people = _merge(user, n=10, days_ago=20)
    Touch.all_objects.create(
        user=user, contact=people[0], kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=90), subject="Earlier",
    )
    camp.detect(user)

    card = camp.campaign_cards(user)[0]

    assert card["recipient_count"] == 10
    assert card["originating_count"] == 9
    assert card["is_classified"] is False


def test_the_settings_page_shows_the_question_and_the_post_answers_it(client):
    """End to end through the real URLs: the card renders only when there is
    something to answer, and the POST changes tomorrow's queue."""
    from django.urls import reverse

    user = _user("settings@example.com")
    firm = _target_firm(user, slug="settings-bank", name="Settings Bank")
    _merge(user, n=10, firm=firm, days_ago=20)
    client.force_login(user)

    # No campaigns detected yet — no card, no nav link.
    assert 'id="campaigns"' not in client.get(reverse("accounts:settings")).content.decode()

    campaign = camp.detect(user)[0]
    body = client.get(reverse("accounts:settings")).content.decode()
    assert 'id="campaigns"' in body
    assert ICC_SUBJECT in body
    assert "10 recipients" in body

    resp = client.post(reverse("crm:classify_campaign"),
                       {"campaign": campaign.id, "kind": Campaign.KIND_OTHER})

    assert resp.status_code == 302
    assert _actions_by_name(user) == {}
    after = client.get(reverse("accounts:settings")).content.decode()
    assert "Not your recruiting" in after


def test_another_tenant_cannot_classify_your_campaign_through_the_view(client):
    from django.urls import reverse

    mine = _user("owner@example.com")
    theirs = _user("intruder@example.com")
    _merge(mine, n=10)
    campaign = camp.detect(mine)[0]

    client.force_login(theirs)
    resp = client.post(reverse("crm:classify_campaign"),
                       {"campaign": campaign.id, "kind": Campaign.KIND_OTHER})

    assert resp.status_code == 404
    assert Campaign.objects.for_user(mine).get(
        id=campaign.id
    ).kind == Campaign.KIND_UNCLASSIFIED


def test_classify_rejects_an_unknown_kind_and_another_tenants_campaign():
    mine = _user("mine2@example.com")
    theirs = _user("theirs2@example.com")
    _merge(mine, n=10)
    campaign = camp.detect(mine)[0]

    assert camp.classify(mine, campaign.id, "nonsense") is None
    assert camp.classify(theirs, campaign.id, Campaign.KIND_OTHER) is None
    assert Campaign.objects.for_user(mine).get(
        id=campaign.id
    ).kind == Campaign.KIND_UNCLASSIFIED


# ---------------------------------------------------------------------------
# 8. The Network board.
#
# THE COMPLAINT, from the founder reading his own board after answering the
# question in Settings: "why are there still icc people in my network?" The
# rule stopped at the daily queue and left all twelve of them (nine
# originating) in Firm Coverage, the warmth sections and the contact count.
# "Not my recruiting" is an answer about the relationship, not about one
# queue, so the board honours it too now — visibly, reversibly, and without
# deleting anybody.
# ---------------------------------------------------------------------------

def _board(client):
    from django.urls import reverse

    return client.get(reverse("crm:contact_list"))


def _classified(user, kind):
    """One 10-person merge at a tier-1 target firm, answered `kind`."""
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, kind)
    return firm, people, campaign


def test_an_other_campaigns_contacts_come_off_the_network_board(client):
    """The fix. Ten club alumni that Settings said were not his recruiting are
    not on the board that exists to show his recruiting network."""
    user = _user()
    firm, people, _ = _classified(user, Campaign.KIND_OTHER)
    mine = Contact.all_objects.create(user=user, name="Real Banker", firm=firm)
    client.force_login(user)

    resp = _board(client)
    body = resp.content.decode()

    assert "Real Banker" in body
    for c in people:
        assert c.name not in body
    assert resp.context["contact_total"] == 1
    assert len(camp.excluded_contact_ids(user)) == 10
    # And nobody was deleted: the contact book still holds all eleven.
    assert Contact.objects.for_user(user).count() == 11
    assert mine.id not in camp.excluded_contact_ids(user)


def test_the_product_says_what_it_is_hiding_and_links_to_it(client):
    """A count that drops by ten with no explanation is its own bug. The
    number, and the way to look, still exist — they moved.

    They used to sit in a strip above the board itself. That strip was
    removed on 2026-08-28, twice pointed at and the second time as "take all
    of this away, hide": three stacked bands of meta-text before the first
    contact card, none of which was a contact. What was NOT allowed to go
    with it is this guarantee, so the count and the route live in Settings >
    Your Data now, beside the archived count that was always there.

    Asserted on Settings rather than the board on purpose: this test is about
    the promise, not the place, and moving it again should mean moving this
    assertion again rather than quietly deleting it."""
    from django.urls import reverse

    user = _user()
    _classified(user, Campaign.KIND_OTHER)
    client.force_login(user)

    # Gone from the board, and no longer announced there.
    board = _board(client).content.decode()
    assert "hidden from this board" not in board

    settings_body = client.get(reverse("accounts:settings")).content.decode()
    assert "10 not recruiting" in settings_body
    assert reverse("crm:contact_campaign_hidden") in settings_body


def test_an_unclassified_campaign_changes_nothing_on_the_board(client):
    """A detected send nobody has answered behaves exactly as before this
    module existed. Detection alone hides nobody."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    camp.detect(user)
    client.force_login(user)

    resp = _board(client)
    body = resp.content.decode()

    for c in people:
        assert c.name in body
    assert resp.context["contact_total"] == 10
    assert camp.excluded_contact_ids(user) == set()


def test_a_recruiting_campaign_changes_nothing_on_the_board(client):
    """"That batch WAS my job search" is an answer too, and it is the answer
    that must never remove anybody."""
    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_RECRUITING)
    client.force_login(user)

    resp = _board(client)
    body = resp.content.decode()

    for c in people:
        assert c.name in body
    assert resp.context["contact_total"] == 10
    assert camp.excluded_contact_ids(user) == set()


def test_a_contact_the_campaign_did_not_originate_stays_on_the_board():
    """Somebody he was already recruiting before the blast reached them is not
    club admin. `originates` decides, and this is the case that makes hiding
    safe to do at all."""
    user = _user()
    firm = _target_firm(user)
    people = _merge(user, n=10, firm=firm, days_ago=20)
    prior = people[0]
    Touch.all_objects.create(
        user=user, contact=prior, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=90), subject="Coffee chat?",
    )
    campaign = camp.detect(user)[0]
    camp.classify(user, campaign.id, Campaign.KIND_OTHER)

    hidden = camp.excluded_contact_ids(user)

    assert prior.id not in hidden
    assert hidden == {c.id for c in people if c.id != prior.id}


def test_a_hand_exempted_contact_stays_on_the_board(client):
    """`Contact.campaign_exempt` is the user's word about one person inside a
    two-hundred-person answer, and it wins on the board exactly as it wins in
    the queue."""
    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_OTHER)
    rescued = people[0]
    rescued.campaign_exempt = True
    rescued.save(update_fields=["campaign_exempt"])
    client.force_login(user)

    resp = _board(client)
    body = resp.content.decode()

    assert rescued.name in body
    assert resp.context["contact_total"] == 1
    assert len(camp.excluded_contact_ids(user)) == 9


def test_every_count_on_the_board_agrees_with_what_it_renders(client):
    """The bulk-save count drift, one page over: a person hidden from the grid
    but still counted in a header is the same class of bug. Every number this
    page prints, checked against the cards it actually drew."""
    user = _user()
    firm, people, _ = _classified(user, Campaign.KIND_OTHER)
    for i, warmth in enumerate(("advocate", "chatted", "replied")):
        c = people[i]
        c.warmth = warmth
        c.save(update_fields=["warmth"])
    Contact.all_objects.create(
        user=user, name="Real Banker", firm=firm, warmth="advocate"
    )
    client.force_login(user)

    resp = _board(client)
    ctx = resp.context
    body = resp.content.decode()

    # The contact grid: header count == cards drawn == warmth sections summed.
    assert ctx["contact_total"] == body.count('class="contact-card') == 1
    assert sum(len(s["cards"]) for s in ctx["sections"]) == 1
    assert "Real Banker" in body
    # Bulk selection lives on this same grid now (the "Contacts Needing
    # Action" panel this test used to check is gone — see
    # crm/views.py::contact_list) — so a hidden contact must not get a
    # checkbox here either. `.cc-check` values are exactly the ids the grid
    # is willing to act on; none of the excluded ten may appear among them.
    hidden = camp.excluded_contact_ids(user)
    checkbox_ids = {int(v) for v in re.findall(r'class="cc-check"[^>]*value="(\d+)"', body)}
    assert not checkbox_ids & hidden
    # The firm card: one contact, one advocate. Not eleven and four.
    cards = [c for s in ctx["tier_sections"] for c in s["cards"]]
    assert [c["contact_count"] for c in cards] == [1]
    assert [c["advocates"] for c in cards] == [1]
    # `firm_total` counts FIRMS, not contacts, and must not move.
    assert ctx["firm_total"] == 1
    # The Coverage Gaps strip reads the same warmths as the cards below it.
    assert [g["advocates"] for g in ctx["gaps"]] == [1]


def test_the_hidden_count_does_not_move_with_the_board_tab(client):
    """This used to guard a per-tab caveat: a US tab reading "10 hidden"
    while all ten sat in Hong Kong is the same class of lie as hiding them
    silently, so the caveat was scoped and the route back was not.

    The caveat is gone (2026-08-28) and the count moved to Settings, which
    has no tabs — so the scoping question the old test asked can no longer be
    asked. What replaces it is the invariant that survived the move: the
    number is a fact about the ACCOUNT, so standing on any board tab must not
    change it, and the board must not restate it.

    Ten Hong Kong contacts, read from the US tab: the board mentions none of
    it, and Settings says ten either way."""
    from django.urls import reverse

    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_OTHER)
    for c in people:
        c.region = "hk"
        c.save(update_fields=["region"])
    client.force_login(user)

    us = client.get(reverse("crm:contact_list") + "?scope=us").content.decode()
    hk = client.get(reverse("crm:contact_list") + "?scope=hk").content.decode()
    for body in (us, hk):
        assert "hidden from this board" not in body
        assert "Not recruiting" not in body

    settings_body = client.get(reverse("accounts:settings")).content.decode()
    assert "10 not recruiting" in settings_body


def test_the_hidden_list_names_the_send_that_hid_them(client):
    """"These ten are hidden" is a fact about the software. "These ten arrived
    on Fall 2026 ICC Alumni Digital Panel Outreach" is one he can check against
    his own memory."""
    from django.urls import reverse

    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_OTHER)
    client.force_login(user)

    resp = client.get(reverse("crm:contact_campaign_hidden"))
    body = resp.content.decode()

    assert resp.context["contact_total"] == 10
    for c in people:
        assert c.name in body
    assert ICC_SUBJECT[:44] in body


def test_bringing_one_back_puts_them_on_the_board_and_leaves_the_rest(client):
    """Reversible in practice, not only in principle — the same job Unarchive
    does for the archived list. One person, not the whole send."""
    from django.urls import reverse

    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_OTHER)
    rescued = people[0]
    client.force_login(user)

    resp = client.post(reverse("crm:contact_campaign_keep", args=[rescued.id]))

    assert resp.status_code == 302
    assert Contact.objects.for_user(user).get(
        id=rescued.id
    ).campaign_exempt is True
    board = _board(client)
    assert rescued.name in board.content.decode()
    assert board.context["contact_total"] == 1
    assert len(camp.excluded_contact_ids(user)) == 9


def test_another_tenant_cannot_unhide_your_contact(client):
    from django.urls import reverse

    mine = _user("owner3@example.com")
    theirs = _user("intruder3@example.com")
    _, people, _ = _classified(mine, Campaign.KIND_OTHER)
    client.force_login(theirs)

    resp = client.post(reverse("crm:contact_campaign_keep", args=[people[0].id]))

    assert resp.status_code == 404
    assert Contact.objects.for_user(mine).get(
        id=people[0].id
    ).campaign_exempt is False


def test_a_hidden_contact_keeps_every_direct_surface(client):
    """Hidden, not deleted. The detail page and every direct link still work —
    the board is the only thing that stopped listing them."""
    from django.urls import reverse

    user = _user()
    _, people, _ = _classified(user, Campaign.KIND_OTHER)
    hidden = people[0]
    client.force_login(user)

    detail = client.get(reverse("crm:contact_detail", args=[hidden.id]))

    assert detail.status_code == 200
    assert hidden.name in detail.content.decode()
    assert Contact.objects.for_user(user).filter(id=hidden.id).exists()


def test_the_firm_page_and_its_rosters_hide_them_too(client):
    """The board's bug relocated one click to the right. "Who do I know here"
    is the same claim on /firms/<slug>/ as it is on the Network board."""
    from django.urls import reverse

    user = _user()
    firm, people, _ = _classified(user, Campaign.KIND_OTHER)
    Contact.all_objects.create(user=user, name="Real Banker", firm=firm)
    client.force_login(user)

    resp = client.get(reverse("directory:firm_detail", args=[firm.slug]))
    body = resp.content.decode()

    assert "Real Banker" in body
    for c in people:
        assert c.name not in body

# ---------------------------------------------------------------------------
# 9. Retiring a campaign the evidence no longer supports.
#
# Detection is append-only, so the fix to `_signature_for` stops campaign 3
# being made again and cannot un-make the row that already exists on the
# founder's account. `--retire` is the clean-up, and its whole design is the
# list of things it refuses to do.
# ---------------------------------------------------------------------------
def _boilerplate_campaign(user, *, n=41):
    """A campaign 3: `n` unrelated contacts grouped on Coverage's own note,
    forced into the table the way detection used to put it there."""
    at = timezone.now() - timedelta(days=30)
    campaign = Campaign.all_objects.create(
        user=user, signature="outreach sent no reply yet",
        label="outreach sent 2026-07-24, no reply yet",
        first_sent=at, last_sent=at, recipient_count=n,
    )
    for i in range(n):
        c = Contact.all_objects.create(user=user, name=f"Banker {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email", ts=at,
            subject=f"HK Jul 29-31 | Firm {i} | IBD - USC Student Coffee Chat",
            note="Outreach sent 2026-07-24, no reply yet",
        )
        CampaignContact.all_objects.create(
            user=user, campaign=campaign, contact=c, sent_at=at, originates=True,
        )
    return campaign


def test_a_campaign_whose_signature_no_longer_qualifies_is_retirable():
    user = _user()
    campaign = _boilerplate_campaign(user)

    retirable, held_back = camp.stale_campaigns(user)

    assert [c.id for c in retirable] == [campaign.id]
    assert held_back == []


def test_retire_writes_nothing_on_a_dry_run():
    user = _user()
    campaign = _boilerplate_campaign(user)

    retired, _ = camp.retire_stale(user, dry_run=True)

    assert [c.id for c in retired] == [campaign.id]
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None
    assert len(camp.campaign_cards(user)) == 1


def test_retiring_hides_the_card_and_keeps_every_row():
    user = _user()
    campaign = _boilerplate_campaign(user)

    camp.retire_stale(user, dry_run=False)

    assert camp.campaign_cards(user) == []
    stored = Campaign.objects.for_user(user).get(id=campaign.id)
    assert stored.retired_at is not None
    # Nothing deleted: the row, its label and all 41 memberships survive.
    assert stored.label == "outreach sent 2026-07-24, no reply yet"
    assert CampaignContact.objects.for_user(user).filter(
        campaign=campaign
    ).count() == 41


def test_a_retired_campaign_can_no_longer_be_answered():
    """The dangerous POST: a Settings page loaded before the retirement,
    submitting "not my recruiting" against 41 people who were never one."""
    user = _user()
    campaign = _boilerplate_campaign(user)
    camp.retire_stale(user, dry_run=False)

    assert camp.classify(user, campaign.id, Campaign.KIND_OTHER) is None
    assert Campaign.objects.for_user(user).get(
        id=campaign.id
    ).kind == Campaign.KIND_UNCLASSIFIED
    assert camp.excluded_contact_ids(user) == set()


def test_retirement_never_touches_a_campaign_the_user_answered_by_hand():
    """The lock is `classified_at`, the same one `detect` respects. A stale
    campaign carrying a human answer is reported and left standing."""
    user = _user()
    campaign = _boilerplate_campaign(user)
    camp.classify(user, campaign.id, Campaign.KIND_RECRUITING)

    retirable, held_back = camp.stale_campaigns(user)
    retired, _ = camp.retire_stale(user, dry_run=False)

    assert retirable == [] and retired == []
    assert [c.id for c in held_back] == [campaign.id]
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None


def test_a_real_campaign_is_never_retired():
    """The ICC merge's signature is still produced by its touches, so the
    weakest possible staleness test leaves it exactly alone."""
    user = _user()
    _merge(user, n=12)
    campaign = camp.detect(user)[0]

    retirable, held_back = camp.stale_campaigns(user)

    assert retirable == [] and held_back == []
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None


def test_detection_un_retires_a_signature_that_qualifies_again():
    """The second half of "reversible": a retirement made on today's evidence
    does not outlive the evidence."""
    user = _user()
    _merge(user, n=12)
    campaign = camp.detect(user)[0]
    Campaign.objects.for_user(user).filter(id=campaign.id).update(
        retired_at=timezone.now()
    )
    assert camp.campaign_cards(user) == []

    camp.detect(user)

    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None
    assert len(camp.campaign_cards(user)) == 1


def test_the_command_retires_only_with_the_flag_and_reports_what_it_did():
    from io import StringIO

    from django.core.management import call_command

    user = _user("retire-cmd@example.com")
    campaign = _boilerplate_campaign(user, n=9)

    out = StringIO()
    call_command("detect_campaigns", user=user.email, stdout=out)
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None

    out = StringIO()
    call_command("detect_campaigns", user=user.email, retire=True,
                 dry_run=True, stdout=out)
    assert "RETIRED" in out.getvalue()
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is None

    out = StringIO()
    call_command("detect_campaigns", user=user.email, retire=True, stdout=out)
    assert "RETIRED" in out.getvalue()
    assert Campaign.objects.for_user(user).get(id=campaign.id).retired_at is not None
