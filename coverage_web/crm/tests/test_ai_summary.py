"""crm.ai_summary: the AI-written relationship note cached on the contact row.

Two things are pinned here, and one of them matters more than everything else
in the file.

THE ONE THAT MATTERS: `Contact.notes` and `Contact.angle` are the student's
own words about a person, and this feature reads them as prompt context. The
sentinel test below seeds both with distinctive strings, runs a generation
that succeeds, and asserts both columns come back BYTE-IDENTICAL. That is the
whole safety contract of the feature: the write path may only ever reach
`ai_summary` / `ai_summary_generated_at`.

THE REST is `assistant.brief`'s posture, tested the same way it is: always the
cheap model whatever plan the student is on, never a raise, and `None` (with
nothing written) rather than manufactured filler whenever there is nothing
specific to say.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm import ai_summary
from crm.models import Contact, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db

REAL_SUMMARY = (
    "You met Jordan at the HKU markets panel in June and have traded three "
    "emails since. They replied within a day each time and flagged the HK "
    "graduate deadline unprompted. The last exchange was three weeks ago."
)


class FakeComplete:
    """Stands in for `directory.ai_extract.complete_text`, recording every
    call so the model tier and the prompt can both be asserted."""

    def __init__(self, result_or_exception=REAL_SUMMARY):
        self.result_or_exception = result_or_exception
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if isinstance(self.result_or_exception, Exception):
            raise self.result_or_exception
        return self.result_or_exception


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(slug="north-bank", name="North Bank")


@pytest.fixture
def contact(user, firm):
    return Contact.all_objects.create(
        user=user, name="Jordan Lee", firm=firm, role="Analyst",
        angle="Met at the HKU markets panel.",
        notes="Wants to hear how the Rotman case went.",
    )


def _history(user, contact, count=3):
    """A varied, realistic history — the thing a summary is written from."""
    rows = [
        ("outreach", "email", "Cold intro after the panel."),
        ("reply_received", "email", "Replied next morning, happy to chat."),
        ("chat", "coffee_chat", "Walked through the HK grad timeline."),
        ("follow_up", "email", "Sent the thank-you and my resume."),
    ]
    made = []
    for i, (kind, channel, note) in enumerate(rows[:count]):
        made.append(
            Touch.all_objects.create(
                user=user, contact=contact, kind=kind, channel=channel, note=note,
                ts=timezone.now() - timedelta(days=30 - i * 7),
            )
        )
    return made


# ---------------------------------------------------------------------------
# THE SAFETY TEST. If only one test in this file survives, it is this one.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_generation_leaves_the_students_own_notes_and_angle_byte_identical(
    user, contact, monkeypatch
):
    """`notes` and `angle` go INTO the prompt as context and must come out of
    a successful generation untouched, to the byte. A feature that can edit
    the student's own words about a person — even to "improve" them — is the
    one failure mode this whole design exists to make impossible."""
    sentinel_angle = "SENTINEL-ANGLE-7f3a: met at the HKU panel, dad knows her."
    sentinel_notes = "SENTINEL-NOTES-91cd: do NOT mention the Barclays offer.\nSecond line kept."
    contact.angle = sentinel_angle
    contact.notes = sentinel_notes
    contact.save()
    _history(user, contact, count=4)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete())

    assert ai_summary.regenerate(contact) == REAL_SUMMARY

    contact.refresh_from_db()
    assert contact.angle == sentinel_angle
    assert contact.notes == sentinel_notes
    # And from a completely fresh read of the row, not just the in-memory one.
    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.angle == sentinel_angle
    assert fresh.notes == sentinel_notes
    assert fresh.ai_summary == REAL_SUMMARY


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_failed_generation_also_leaves_notes_and_angle_untouched(
    user, contact, monkeypatch
):
    """The failure path writes nothing at all — not a blanked summary, and
    certainly not a stray edit to the student's own fields."""
    contact.angle = "SENTINEL-ANGLE"
    contact.notes = "SENTINEL-NOTES"
    contact.save()
    _history(user, contact, count=4)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete(RuntimeError("boom")))

    assert ai_summary.regenerate(contact) is None

    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.angle == "SENTINEL-ANGLE"
    assert fresh.notes == "SENTINEL-NOTES"
    assert fresh.ai_summary == ""
    assert fresh.ai_summary_generated_at is None


# ---------------------------------------------------------------------------
# Generation: the happy path, and every way it declines to say something.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_real_history_produces_a_stored_stamped_summary(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete())

    before = timezone.now()
    text = ai_summary.regenerate(contact)

    assert text == REAL_SUMMARY
    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.ai_summary == REAL_SUMMARY
    assert fresh.ai_summary_generated_at is not None
    assert fresh.ai_summary_generated_at >= before


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_no_history_returns_none_writes_nothing_and_spends_no_call(contact, monkeypatch):
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    assert ai_summary.regenerate(contact) is None

    assert fake.calls == []
    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.ai_summary == ""
    assert fresh.ai_summary_generated_at is None


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_single_touch_is_too_thin_to_narrate(user, contact, monkeypatch):
    """One logged event is not a relationship — the history already shows it
    in one line, and paraphrasing it would be the filler this returns None
    rather than manufacture."""
    _history(user, contact, count=1)
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    assert ai_summary.regenerate(contact) is None
    assert fake.calls == []
    assert Contact.all_objects.get(pk=contact.pk).ai_summary == ""


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_models_nothing_to_say_answer_writes_nothing(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete("NOTHING TO SAY"))

    assert ai_summary.regenerate(contact) is None
    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.ai_summary == ""
    assert fresh.ai_summary_generated_at is None


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_an_empty_response_writes_nothing(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete("   "))

    assert ai_summary.regenerate(contact) is None
    assert Contact.all_objects.get(pk=contact.pk).ai_summary == ""


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_an_api_error_is_swallowed_and_never_raised(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(
        ai_summary, "complete_text", FakeComplete(RuntimeError("network blip"))
    )

    assert ai_summary.regenerate(contact) is None  # no exception escapes


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_failed_rewrite_keeps_the_summary_the_student_already_had(
    user, contact, monkeypatch
):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete())
    ai_summary.regenerate(contact)
    stamp = Contact.all_objects.get(pk=contact.pk).ai_summary_generated_at

    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete(RuntimeError("down")))
    assert ai_summary.regenerate(contact) is None

    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.ai_summary == REAL_SUMMARY
    assert fresh.ai_summary_generated_at == stamp


@override_settings(ANTHROPIC_API_KEY="")
def test_an_unconfigured_key_spends_no_call_and_writes_nothing(
    user, contact, monkeypatch
):
    _history(user, contact, count=3)
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    assert ai_summary.regenerate(contact) is None
    assert fake.calls == []


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_very_long_response_is_capped(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(
        ai_summary, "complete_text",
        FakeComplete("x" * (ai_summary.MAX_SUMMARY_CHARS + 200)),
    )

    text = ai_summary.regenerate(contact)
    assert len(text) == ai_summary.MAX_SUMMARY_CHARS


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_rewrite_replaces_the_previous_summary_and_restamps(
    user, contact, monkeypatch
):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete("First pass."))
    ai_summary.regenerate(contact)
    first_stamp = Contact.all_objects.get(pk=contact.pk).ai_summary_generated_at

    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete("Second pass."))
    ai_summary.regenerate(contact)

    fresh = Contact.all_objects.get(pk=contact.pk)
    assert fresh.ai_summary == "Second pass."
    assert fresh.ai_summary_generated_at >= first_stamp


# ---------------------------------------------------------------------------
# The model tier: cheap, always, whatever the student is paying.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
@pytest.mark.parametrize("plan", ["", "free", "pro"])
def test_the_call_always_uses_the_cheap_tier_whatever_the_plan(
    user, contact, monkeypatch, plan
):
    """A relationship recap is bookkeeping prose, not the judgement call a Pro
    plan buys — `assistant.plans.limits_for` is deliberately never consulted.
    Also guards against silently inheriting `complete_text`'s own default,
    which is the EXPENSIVE model."""
    user.plan = plan
    user.save()
    _history(user, contact, count=3)
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    ai_summary.regenerate(contact)

    assert fake.calls[0]["model"] == ai_summary.SUMMARY_MODEL
    assert fake.calls[0]["model"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# The prompt: built entirely from this contact's own scoped rows.
# ---------------------------------------------------------------------------

def test_the_prompt_carries_the_contacts_own_facts_and_history(user, contact):
    touches = _history(user, contact, count=3)
    prompt = ai_summary.build_prompt(contact, list(reversed(touches)))

    assert "Jordan Lee" in prompt
    assert "North Bank" in prompt
    assert "Analyst" in prompt
    assert "Walked through the HK grad timeline." in prompt


def test_the_prompt_labels_notes_and_angle_as_context_not_material_to_restate(
    user, contact
):
    prompt = ai_summary.build_prompt(contact, [])
    assert "Met at the HKU markets panel." in prompt
    assert "Wants to hear how the Rotman case went." in prompt
    assert prompt.count("do not restate") == 2


def test_the_prompt_falls_back_to_firm_text_when_there_is_no_directory_firm(user):
    c = Contact.all_objects.create(user=user, name="Sam", firm_text="Some Boutique Shop")
    assert "Some Boutique Shop" in ai_summary.build_prompt(c, [])


def test_the_prompt_truncates_a_very_long_note(user, firm):
    c = Contact.all_objects.create(user=user, name="Sam", firm=firm, notes="x" * 5000)
    assert "x" * 5000 not in ai_summary.build_prompt(c, [])


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_history_handed_to_the_model_is_capped(user, contact, monkeypatch):
    """Bounded input, at the same cap `assistant.tools._get_contact` uses for
    the same contact's "recent interactions"."""
    for i in range(ai_summary.MAX_TOUCHES + 6):
        Touch.all_objects.create(
            user=user, contact=contact, kind="outreach", channel="email",
            note=f"NOTE-NUMBER-{i}", ts=timezone.now() - timedelta(days=i),
        )
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    ai_summary.regenerate(contact)

    prompt = fake.calls[0]["prompt"]
    assert prompt.count("NOTE-NUMBER-") == ai_summary.MAX_TOUCHES
    # Newest first: the oldest rows are the ones dropped.
    assert "NOTE-NUMBER-0" in prompt
    assert f"NOTE-NUMBER-{ai_summary.MAX_TOUCHES + 5}" not in prompt


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_machine_bookkeeping_markers_are_stripped_from_the_prompt(
    user, contact, monkeypatch
):
    """The same `_display_note` scrub the contact page and the advisor tool
    already apply — a `[gmail:...]` idempotency marker is our bookkeeping,
    not a fact about the relationship."""
    Touch.all_objects.create(
        user=user, contact=contact, kind="reply_received", channel="email",
        note="[gmail:t_991] She replied about the HK desk.", ts=timezone.now(),
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        note="First note.", ts=timezone.now() - timedelta(days=3),
    )
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)

    ai_summary.regenerate(contact)

    prompt = fake.calls[0]["prompt"]
    assert "gmail:t_991" not in prompt
    assert "She replied about the HK desk." in prompt


# ---------------------------------------------------------------------------
# Staleness: a display fact, never a trigger.
# ---------------------------------------------------------------------------

def test_no_summary_yet_is_not_called_stale(user, contact):
    _history(user, contact, count=3)
    assert ai_summary.touches_since_summary(contact) == 0
    assert ai_summary.is_stale(contact) is False


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_touches_logged_after_the_stamp_are_counted(user, contact, monkeypatch):
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete())
    ai_summary.regenerate(contact)

    assert ai_summary.touches_since_summary(contact) == 0

    for i in range(ai_summary.STALE_AFTER_TOUCHES):
        Touch.all_objects.create(
            user=user, contact=contact, kind="outreach", channel="email",
            note=f"new {i}", ts=timezone.now() + timedelta(minutes=i + 1),
        )

    assert ai_summary.touches_since_summary(contact) == ai_summary.STALE_AFTER_TOUCHES
    assert ai_summary.is_stale(contact) is True


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_prepassed_touch_list_and_the_query_agree(user, contact, monkeypatch):
    """The contact page passes its already-loaded history in to avoid a second
    query; both paths must produce the same number."""
    _history(user, contact, count=3)
    monkeypatch.setattr(ai_summary, "complete_text", FakeComplete())
    ai_summary.regenerate(contact)
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        note="after", ts=timezone.now() + timedelta(minutes=5),
    )

    loaded = list(contact.touches.all())
    assert ai_summary.touches_since_summary(contact, loaded) == 1
    assert ai_summary.touches_since_summary(contact) == 1


# ---------------------------------------------------------------------------
# The view: POST-only, tenant-scoped, and every state renders plainly.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_view_renders_the_summary_labelled_and_dated(
    client, user, contact, monkeypatch
):
    _history(user, contact, count=3)
    monkeypatch.setattr("crm.ai_summary.complete_text", FakeComplete())
    client.force_login(user)

    resp = client.post(reverse("crm:contact_ai_summary", args=[contact.pk]))

    assert resp.status_code == 200
    assert b"HKU markets panel" in resp.content
    assert b"AI-drafted from this contact's history" in resp.content
    assert b"Written " in resp.content


@override_settings(ANTHROPIC_API_KEY="")
def test_the_view_says_nothing_written_when_the_api_is_dark(client, user, contact):
    _history(user, contact, count=3)
    client.force_login(user)

    resp = client.post(reverse("crm:contact_ai_summary", args=[contact.pk]))

    assert resp.status_code == 200
    assert b"Nothing written." in resp.content
    assert Contact.all_objects.get(pk=contact.pk).ai_summary == ""


def test_the_generate_button_carries_a_csrf_token(client, user, contact):
    """Found live, and it was already broken for the coffee-chat brief next
    door: both buttons are bare `hx-post`s with no surrounding <form>, so
    there is no `csrf_token` hidden input for htmx to pick up and Django
    answered every real click with a 403. The default test client does not
    enforce CSRF, which is exactly why no existing test caught it.

    The fix is this app's own spelling of it (crm/week.html,
    directory/_results.html): `hx-headers` on the wrapper, which is also the
    swap target, so it survives every re-render."""
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()

    for target in ("contact-ai-summary", "contact-ai-brief"):
        marker = f'id="{target}"'
        assert marker in body
        assert "X-CSRFToken" in body[body.index(marker):body.index(marker) + 200]


@override_settings(ANTHROPIC_API_KEY="")
def test_a_real_csrf_checked_post_is_accepted(user, contact):
    """The end of that bug, tested the way it actually failed: a client that
    enforces CSRF, posting the way the browser does."""
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    page = csrf_client.get(reverse("crm:contact_detail", args=[contact.pk]))
    token = page.cookies["csrftoken"].value

    resp = csrf_client.post(
        reverse("crm:contact_ai_summary", args=[contact.pk]), HTTP_X_CSRFTOKEN=token
    )

    assert resp.status_code == 200


def test_the_view_requires_post(client, user, contact):
    client.force_login(user)
    resp = client.get(reverse("crm:contact_ai_summary", args=[contact.pk]))
    assert resp.status_code == 405


def test_the_view_404s_for_another_tenants_contact(client, user, contact):
    intruder = User.objects.create_user(email="intruder@example.com", password="x")
    client.force_login(intruder)
    resp = client.post(reverse("crm:contact_ai_summary", args=[contact.pk]))
    assert resp.status_code == 404


def test_the_view_requires_login(client, contact):
    resp = client.post(reverse("crm:contact_ai_summary", args=[contact.pk]))
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# The detail page: shows the stored note, and never generates on a GET.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_opening_the_contact_page_never_spends_a_call(
    client, user, contact, monkeypatch
):
    """Generation is a deliberate POST. A GET — a reload, a prefetch, a
    crawler walking the network list — must cost nothing."""
    _history(user, contact, count=4)
    fake = FakeComplete()
    monkeypatch.setattr(ai_summary, "complete_text", fake)
    client.force_login(user)

    resp = client.get(reverse("crm:contact_detail", args=[contact.pk]))

    assert resp.status_code == 200
    assert fake.calls == []
    assert Contact.all_objects.get(pk=contact.pk).ai_summary == ""
    assert b"Write summary" in resp.content


def test_the_detail_page_renders_a_stored_summary_in_its_own_card(
    client, user, contact
):
    contact.ai_summary = REAL_SUMMARY
    contact.ai_summary_generated_at = timezone.now()
    contact.angle = "MY OWN ANGLE LINE"
    contact.save()
    client.force_login(user)

    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()

    assert "Relationship summary" in body
    assert "HKU markets panel" in body
    assert "MY OWN ANGLE LINE" in body
    # Separate boxes: the angle sits in the reach card, the summary in its own
    # card below it, and nothing merges the two.
    assert body.index("MY OWN ANGLE LINE") < body.index('id="contact-ai-summary"')


def test_the_detail_page_says_how_far_behind_the_summary_has_fallen(
    client, user, contact
):
    contact.ai_summary = REAL_SUMMARY
    contact.ai_summary_generated_at = timezone.now() - timedelta(days=10)
    contact.save()
    _history(user, contact, count=3)  # all logged before the stamp
    Touch.all_objects.create(
        user=user, contact=contact, kind="reply_received", channel="email",
        note="after the summary", ts=timezone.now(),
    )
    client.force_login(user)

    body = client.get(reverse("crm:contact_detail", args=[contact.pk])).content.decode()

    assert "1 interaction logged since" in body
