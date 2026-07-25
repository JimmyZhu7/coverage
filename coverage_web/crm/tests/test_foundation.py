"""Tests for the four foundation fixes that put user-owned data behind the
domain engines instead of hardcoded placeholders:

  1. `Contact.angle` (the user's PRIVATE note about a person) must never reach
     a mailto: body. The compose draft comes from `Contact.opener`.
  2. Firm fit's sponsorship component reads the user's PER-REGION
     `work_authorization` instead of the hardcoded `needs_sponsorship=None`
     that neutralized it for everyone.
  3. `Contact.region` is an explicit field, defaulted from the firm only when
     the firm is unambiguous — replacing the "does `source` contain 'hk'"
     guess that made every hand-added contact a US contact.
  4. The cadence rule parameters and the weekly touch goal are per-user, with
     the override whitelist enforced server-side.

All read-only views here use plain `@pytest.mark.django_db`; nothing in this
file goes through `crm.services.log_touch` (which would need the transactional
fixture — see test_views.py's module docstring).
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import views as crm_views
from crm.models import Contact, Touch
from directory.models import Firm, FirmDate

User = get_user_model()

# Every mailto: URL in a rendered page. The href is quoted, so stopping at the
# closing quote captures the whole URL including its query string.
_MAILTO_RE = re.compile(r"mailto:[^\"'\s>]*")


def _user(email="student@example.com", **kwargs):
    return User.objects.create_user(email=email, password="x", **kwargs)


def _mailto_urls(html: str) -> list[str]:
    urls = _MAILTO_RE.findall(html)
    assert urls, "expected at least one mailto: link on the page"
    return urls


# ---------------------------------------------------------------------------
# 1. The angle-leak regression.
# ---------------------------------------------------------------------------
# A private note whose words would be plainly visible in a URL if it leaked:
# `urlencode(quote_via=quote)` leaves letters untouched, so a substring check
# catches the leak whatever the encoding does to the punctuation around it.
PRIVATE_ANGLE = "USC alum, seems insecure about his exit opps"
DRAFT_OPENER = "Hi Jane, I am a sophomore at USC looking at markets."


@pytest.mark.django_db
def test_angle_never_leaks_into_mailto_on_contact_detail(client):
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
        angle=PRIVATE_ANGLE, opener=DRAFT_OPENER,
    )
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()

    for url in _mailto_urls(body):
        for word in ("insecure", "exit", "opps"):
            assert word not in url, f"angle leaked into a mailto body: {url}"
    # The angle still renders on the card — it's private, not hidden.
    assert "seems insecure" in body


@pytest.mark.django_db
def test_angle_never_leaks_into_mailto_in_the_cadence_queue(client):
    """The Today queue builds its own compose links. The angle isn't even
    passed into the queue's contact dicts any more, so it must be absent from
    the whole page, not just from the URLs."""
    user = _user()
    Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
        angle=PRIVATE_ANGLE, opener=DRAFT_OPENER,
        warmth="cold", thread_state="no_reply",
    )
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()

    assert "First outreach" in body  # the card is really there
    assert "insecure" not in body
    for url in _mailto_urls(body):
        assert "insecure" not in url


@pytest.mark.django_db
def test_compose_body_comes_from_the_opener(client):
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@acme.com",
        angle=PRIVATE_ANGLE, opener=DRAFT_OPENER,
    )
    client.force_login(user)
    body = client.get(reverse("crm:contact_detail", args=[contact.id])).content.decode()
    assert any("sophomore" in url for url in _mailto_urls(body))


def test_mailto_helper_puts_the_body_it_is_given_in_the_query():
    """Unit-level guard on the helper itself, so the rule survives a caller
    being added somewhere the route tests above don't cover."""
    url = crm_views._mailto("a@b.com", "bcc@c.com", body="hello there")
    assert "body=hello%20there" in url
    assert url.startswith("mailto:a%40b.com?")


# ---------------------------------------------------------------------------
# 2. Sponsorship fit is derived, not frozen.
# ---------------------------------------------------------------------------
def _score_axes(client, user, contact):
    client.force_login(user)
    resp = client.get(reverse("crm:contact_detail", args=[contact.id]))
    assert resp.status_code == 200
    return resp.context["firm_score"]["axes"]["structural"]


@pytest.mark.django_db
def test_firm_fit_reads_the_users_work_authorization(client):
    """Same firm, same contact — only the user's work authorization differs,
    and the structural axis moves. Before this, every user was scored with
    `needs_sponsorship=None` forever."""
    firm = Firm.objects.create(
        slug="nosponsor", name="No Sponsor Capital", regions=["us"],
        tracks=["ib"], sponsors=False,
    )

    citizen = _user("citizen@example.com", regions=["us"], tracks=["ib"],
                    work_authorization={"us": "citizen"})
    visa = _user("visa@example.com", regions=["us"], tracks=["ib"],
                 work_authorization={"us": "sponsorship"})
    unknown = _user("unknown@example.com", regions=["us"], tracks=["ib"])

    axes = {}
    for u in (citizen, visa, unknown):
        c = Contact.all_objects.create(user=u, name="Pat Banker", firm=firm)
        axes[u.email] = _score_axes(client, u, c)

    assert axes["citizen@example.com"]["sponsorship_ok"] is True
    assert axes["visa@example.com"]["sponsorship_ok"] is False
    assert axes["unknown@example.com"]["sponsorship_ok"] is None
    assert (
        axes["visa@example.com"]["score"]
        < axes["unknown@example.com"]["score"]
        < axes["citizen@example.com"]["score"]
    )


@pytest.mark.django_db
def test_firm_fit_sponsorship_is_scoped_to_the_firms_region(client):
    """A visa requirement in Hong Kong doesn't follow the student to a US-only
    firm — the whole point of keying work authorization by region."""
    us_firm = Firm.objects.create(
        slug="usonly", name="US Only Partners", regions=["us"], tracks=["ib"], sponsors=False,
    )
    hk_firm = Firm.objects.create(
        slug="hkonly", name="HK Only Partners", regions=["hk"], tracks=["ib"], sponsors=False,
    )
    user = _user(regions=["us", "hk"], tracks=["ib"],
                 work_authorization={"us": "citizen", "hk": "sponsorship"})

    at_us = Contact.all_objects.create(user=user, name="US Person", firm=us_firm)
    at_hk = Contact.all_objects.create(user=user, name="HK Person", firm=hk_firm)

    assert _score_axes(client, user, at_us)["sponsorship_ok"] is True
    assert _score_axes(client, user, at_hk)["sponsorship_ok"] is False


# ---------------------------------------------------------------------------
# 3. Explicit contact region.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_region_defaults_from_an_unambiguous_firm():
    user = _user()
    us_only = Firm.objects.create(slug="us1", name="US One", regions=["us"])
    c = Contact.all_objects.create(user=user, name="Pat", firm=us_only)
    assert c.region == "us"


@pytest.mark.django_db
def test_region_stays_blank_when_the_firm_is_ambiguous():
    """Both regions, no regions, or only regions this product doesn't model —
    all leave the field blank rather than guessing."""
    user = _user()
    cases = {
        "dual": ["us", "hk"],
        "none": [],
        "unmodeled": ["sg", "eu"],
    }
    for slug, regions in cases.items():
        firm = Firm.objects.create(slug=slug, name=slug.title(), regions=regions)
        c = Contact.all_objects.create(user=user, name=f"Pat {slug}", firm=firm)
        assert c.region == "", f"{slug} firm should not imply a region, got {c.region!r}"

    # No firm at all is the same story.
    assert Contact.all_objects.create(user=user, name="Firmless").region == ""


@pytest.mark.django_db
def test_csv_import_also_gets_the_firm_region_default():
    """The import path uses bulk_create, which never calls save() — the one
    place the model-level default would silently not apply."""
    from accounts import services

    user = _user()
    Firm.objects.create(slug="hkco", name="HK Co", regions=["hk"])
    Firm.objects.create(slug="dualco", name="Dual Co", regions=["us", "hk"])
    services.parse_contacts_csv(
        user,
        "name,email,firm\n"
        "Wing Lee,wing@hkco.example,HK Co\n"
        "Sam Both,sam@dualco.example,Dual Co\n",
    )
    assert Contact.all_objects.get(name="Wing Lee").region == "hk"
    assert Contact.all_objects.get(name="Sam Both").region == ""


@pytest.mark.django_db
def test_explicit_region_is_never_overwritten_by_the_firm():
    user = _user()
    us_only = Firm.objects.create(slug="us1", name="US One", regions=["us"])
    c = Contact.all_objects.create(user=user, name="Pat", firm=us_only, region="hk")
    c.refresh_from_db()
    assert c.region == "hk"


@pytest.mark.django_db
def test_partial_save_persists_a_defaulted_region():
    """`save(update_fields=...)` must still write the region we just filled
    in, or the default would be computed and then silently dropped."""
    user = _user()
    firm = Firm.objects.create(slug="us1", name="US One", regions=["us"])
    c = Contact.all_objects.create(user=user, name="Pat")
    assert c.region == ""
    c.firm = firm
    c.save(update_fields=["firm"])
    c.refresh_from_db()
    assert c.region == "us"


@pytest.mark.django_db
def test_contact_form_accepts_a_region(client):
    user = _user()
    client.force_login(user)
    resp = client.post(
        reverse("crm:contact_new"),
        {"name": "Ada Lovelace", "firm_text": "A Boutique", "role": "Analyst",
         "email": "ada@example.com", "linkedin": "", "school": "", "region": "hk",
         "angle": "", "opener": "", "notes": ""},
    )
    assert resp.status_code == 302
    assert Contact.all_objects.get(name="Ada Lovelace").region == "hk"


@pytest.mark.django_db
def test_region_renders_as_a_chip_on_the_contact_card(client):
    user = _user()
    # warmth="replied" so both land in a rendered warmth section — a cold
    # contact with no touches belongs to none of them.
    Contact.all_objects.create(user=user, name="Hong Konger", region="hk", warmth="replied")
    Contact.all_objects.create(user=user, name="Region Unknown", warmth="replied")
    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert '<span class="pill cc-pill">HK</span>' in body
    # One chip, not one per contact: the unknown-region card shows nothing.
    assert body.count('class="pill cc-pill">HK<') == 1


@pytest.mark.django_db
def test_cadence_reping_uses_the_contacts_region_not_its_source(client):
    """End to end through the view: two hand-added contacts (source="manual",
    which the legacy inference reads as 'us') at a firm with a confirmed HK
    close. Only the one marked HK is re-pinged."""
    user = _user()
    today = timezone.localdate()
    firm = Firm.objects.create(slug="dual", name="Dual Firm", regions=["us", "hk"])
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=today + timedelta(days=5), precision="day", confidence=1.0,
    )

    hk = Contact.all_objects.create(
        user=user, name="Hong Konger", firm=firm, source="manual", region="hk",
        warmth="chatted", thread_state="replied",
    )
    us = Contact.all_objects.create(
        user=user, name="New Yorker", firm=firm, source="manual", region="us",
        warmth="chatted", thread_state="replied",
    )
    for c in (hk, us):
        Touch.all_objects.create(
            user=user, contact=c, ts=timezone.now() - timedelta(days=30),
            kind="chat", channel="coffee_chat",
        )

    actions, _, _ = crm_views._build_actions(user)
    by_name = {a["contact"]["name"]: a["action"] for a in actions}
    assert by_name.get("Hong Konger") == "reping"
    assert by_name.get("New Yorker") != "reping"


# ---------------------------------------------------------------------------
# 3b. The region backfill migration's rule.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_region_backfill_only_fills_unambiguous_firms():
    """Runs the data migration's own function against the current models (the
    fields it touches haven't changed since), so the "exactly one modeled
    region, never overwrite" rule is pinned rather than described."""
    import importlib

    from django.apps import apps as django_apps

    migration = importlib.import_module("crm.migrations.0005_backfill_contact_region")

    user = _user()
    us_firm = Firm.objects.create(slug="us1", name="US One", regions=["us"])
    hk_firm = Firm.objects.create(slug="hk1", name="HK One", regions=["HK"])  # case-insensitive
    dual = Firm.objects.create(slug="dual", name="Dual", regions=["us", "hk"])
    sg = Firm.objects.create(slug="sg1", name="SG One", regions=["sg"])

    # Created with region already blanked below — Contact.save() would default
    # it, and the migration is what we're testing here.
    rows = {
        "us": Contact.all_objects.create(user=user, name="US Person", firm=us_firm),
        "hk": Contact.all_objects.create(user=user, name="HK Person", firm=hk_firm),
        "dual": Contact.all_objects.create(user=user, name="Dual Person", firm=dual),
        "sg": Contact.all_objects.create(user=user, name="SG Person", firm=sg),
        "none": Contact.all_objects.create(user=user, name="Firmless Person"),
        "preset": Contact.all_objects.create(user=user, name="Preset Person", firm=us_firm),
    }
    Contact.all_objects.filter(pk__in=[r.pk for r in rows.values()]).update(region="")
    Contact.all_objects.filter(pk=rows["preset"].pk).update(region="hk")

    migration.backfill_region(django_apps, None)

    def region_of(key):
        return Contact.all_objects.get(pk=rows[key].pk).region

    assert region_of("us") == "us"
    assert region_of("hk") == "hk"
    assert region_of("dual") == ""
    assert region_of("sg") == ""
    assert region_of("none") == ""
    assert region_of("preset") == "hk", "an existing value must not be overwritten"


# ---------------------------------------------------------------------------
# 4. Per-user cadence parameters and weekly goal.
# ---------------------------------------------------------------------------
def test_cadence_params_whitelist_drops_unknown_and_out_of_range():
    user = User(cadence_params={
        "followup_after_business_days": 2,      # valid
        "max_cold_touches": 999,                # out of range -> dropped
        "park_after_business_days": 0,          # below the floor -> dropped
        "advocate_touch_min_weeks": True,       # bool is not an int here
        "pre_deadline_reping_days": "14",       # wrong type -> dropped
        "thank_you_within_hours": 1,            # not tunable -> dropped
        "__class__": "nope",                    # not a key at all
    })
    assert crm_views._cadence_params(user) == {"followup_after_business_days": 2}


def test_cadence_params_tolerates_a_non_dict_column():
    assert crm_views._cadence_params(User(cadence_params=[])) == {}
    assert crm_views._cadence_params(User(cadence_params=None)) == {}
    assert crm_views._cadence_params(User()) == {}


@pytest.mark.django_db
def test_user_cadence_params_change_the_queue():
    """The override reaches the engine: a contact 2 business days past one
    cold outreach isn't due under the default 5-business-day window, but is
    once the user tightens it."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Recently Emailed", warmth="cold", thread_state="no_reply",
    )
    Touch.all_objects.create(
        user=user, contact=contact, ts=timezone.now() - timedelta(days=2),
        kind="outreach", channel="email",
    )

    default_actions, _, _ = crm_views._build_actions(user)
    assert "follow_up" not in {a["action"] for a in default_actions}

    user.cadence_params = {"followup_after_business_days": 1}
    user.save(update_fields=["cadence_params"])
    tuned_actions, _, _ = crm_views._build_actions(user)
    assert "follow_up" in {a["action"] for a in tuned_actions}


@pytest.mark.django_db
def test_ignored_cadence_override_leaves_the_default_in_place():
    """An out-of-range value is dropped, not clamped and not passed through:
    the queue behaves exactly as the default does."""
    user = _user()
    contact = Contact.all_objects.create(
        user=user, name="Recently Emailed", warmth="cold", thread_state="no_reply",
    )
    Touch.all_objects.create(
        user=user, contact=contact, ts=timezone.now() - timedelta(days=2),
        kind="outreach", channel="email",
    )
    user.cadence_params = {"followup_after_business_days": 0}  # below the floor
    user.save(update_fields=["cadence_params"])
    actions, _, _ = crm_views._build_actions(user)
    assert "follow_up" not in {a["action"] for a in actions}


@pytest.mark.django_db
def test_weekly_touch_goal_defaults_and_overrides():
    user = _user()
    assert crm_views._cockpit_context(user)["pace"]["goal"] == crm_views.WEEKLY_TOUCH_GOAL

    user.weekly_touch_goal = 25
    user.save(update_fields=["weekly_touch_goal"])
    assert crm_views._cockpit_context(user)["pace"]["goal"] == 25

    # A stored 0 falls back rather than producing a division by zero.
    user.weekly_touch_goal = 0
    user.save(update_fields=["weekly_touch_goal"])
    pace = crm_views._cockpit_context(user)["pace"]
    assert pace["goal"] == crm_views.WEEKLY_TOUCH_GOAL
