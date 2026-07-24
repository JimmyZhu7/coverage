"""Idempotently create a demo student with realistic contacts so the logged-in
product (weekly list, fit scores, warmth) is full of content on first look.
Run via: manage.py shell < scripts/demo_seed.py   (safe to run repeatedly)."""
from datetime import timedelta

from django.utils import timezone
from django.contrib.auth import get_user_model

from directory.models import Firm, FirmDate
from crm.models import Contact, UserFirm
from crm import services

User = get_user_model()
EMAIL, PW = "demo@coverage.local", "demo1234"

if User.objects.filter(email=EMAIL).exists():
    print("demo student already set up — nothing to do")
else:
    now = timezone.now()
    u = User.objects.create_user(email=EMAIL, password=PW)
    u.name, u.school, u.class_year = "Demo Student", "Demo University", 2028
    u.target_cycle, u.regions, u.tracks = "sa2028_ib", ["us"], ["ib"]
    u.onboarded_at = now
    u.save()

    firms = list(Firm.objects.order_by("name")[:3])
    while len(firms) < 3:                       # safety if the directory wasn't seeded
        firms.append(Firm.objects.create(name=f"Demo Firm {len(firms)+1}",
                                         slug=f"demo-firm-{len(firms)+1}"))
    f1, f2, f3 = firms[0], firms[1], firms[2]
    for f, tier in ((f1, 1), (f2, 1), (f3, 2)):
        UserFirm.all_objects.get_or_create(user=u, firm=f, defaults={"tier": tier, "status": "target"})

    # a confirmed close ~10 days out at f3 -> the weekly list shows a re-ping
    FirmDate.objects.get_or_create(
        firm=f3, cycle="sa2028_ib", region="us", event_kind="app_close",
        defaults={"date": now.date() + timedelta(days=10), "confidence": 1.0,
                  "source_url": "seed:demo"})

    def contact(name, email, firm, angle="", alum=False):
        return Contact.all_objects.create(user=u, firm=firm, name=name, email=email,
                                          source="met at a US info session", angle=angle,
                                          school_affiliation=alum)

    # A story across the warmth ladder so scores + the weekly list have variety.
    maya = contact("Maya Chen", "maya@example.com", f1, "covered the semiconductor deal", alum=True)
    james = contact("James Okafor", "james@example.com", f1, "leads the FIG group")
    priya = contact("Priya Nair", "priya@example.com", f2, "USC alum, super responsive", alum=True)
    tom = contact("Tom Weiss", "tom@example.com", f2)
    contact("Sofia Reyes", "sofia@example.com", f3, "warm intro from a classmate")  # no touches -> first_outreach
    daniel = contact("Daniel Kim", "daniel@example.com", f3, "covers the account I'm targeting")

    def touch(c, *kinds):
        for k in kinds:
            services.log_touch(u.id, c.id, k, "email")

    touch(maya, "outreach", "reply_received", "chat")     # chatted
    try:
        services.set_contact_state(u.id, maya.id, warmth="advocate", note="offered to refer me")
    except Exception:
        pass                                              # advocate is a nice-to-have
    touch(james, "outreach", "reply_received", "chat")    # chat_done, no thank-you -> thank_you action
    touch(priya, "outreach", "reply_received")            # replied
    touch(tom, "outreach")                                # cold / no_reply
    touch(daniel, "outreach", "reply_received")           # replied, at a firm closing soon -> re-ping

    print(f"demo student ready: {EMAIL} / {PW}  ({Contact.objects.for_user(u).count()} contacts)")
