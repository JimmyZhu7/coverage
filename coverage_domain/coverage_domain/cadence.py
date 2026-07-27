"""Cadence engine — the weekly-priority decision tree, ported from the
`campaign` project's `cadence.py` (`due_actions`) and `tasks.py` (the
backward planner), plus the `cadence.yaml` rule parameters as module
defaults.

Ported behavior (semantics unchanged from the SQLite/YAML original — only
the storage adapter changed, per docs/build-plan.md §4's port table):

  - `due_actions()`'s fixed 7-branch decision tree, in the exact same
    order, returning at most one action per contact:
      1. chat_done + no thank-you since the LATEST chat, and
         that chat is within `thank_you_expires_after_days`    -> thank_you
      2. chat_scheduled stale > 4 business days                -> confirm_chat
      3. warm contact at a firm whose CONFIRMED app_close is
         within `pre_deadline_reping_days`, REGION-SCOPED       -> reping
      4. parked / quiet                                         -> (skip)
      5. advocate idle >= advocate_touch_min_weeks             -> maintain
      6. cold / no_reply: 0 outbound -> first_outreach; 1 outbound and
         idle >= followup window -> follow_up (the ONLY one — see
         `max_cold_touches`'s comment); else park once max_cold
         reached and idle >= park window
      7. replied + idle >= 3 business days                      -> advance
    Sorted by (priority, firm tier, firm name).

  - The second-chat thank-you fix and the "chat_done contacts re-enter the
    cadence once thanked" fix (both come free with the ported branch 1).

  - DIVERGENCE from the original (deliberate, 2026-07-25): branch 1 now
    EXPIRES. The original prompted for a thank-you indefinitely; here the
    prompt stops after `thank_you_expires_after_days` and the contact falls
    through to the rest of the cadence. See that parameter's comment for why.

  - Region scoping (branch 3): an HK app_close never re-pings a US contact
    at the same firm, and vice versa. A contact whose region is unknown keeps
    the both-regions fallback: it matches on the soonest close date across any
    region for the firm.

  - DIVERGENCE from the original (deliberate, 2026-07-25, tightened
    2026-07-27): a contact's region is read from an EXPLICIT `region` key and
    from nothing else. The original had no region column and inferred it by
    substring-matching "hk" inside the free-text `source` — which is a
    provenance string, so every contact whose source didn't happen to say
    "hk" (including every hand-added one, source "manual") silently read as
    US: re-pinged against US deadlines, skipped for HK ones.

    The 07-25 change kept that inference as a fallback for blank rows, on the
    theory that legacy rows should keep the meaning they had. That was wrong,
    and the live data is what showed it. `infer_region` returns a region for
    ANY non-empty string, so the fallback answered confidently for all 51
    blank-region contacts — 19 of them "us" purely because their provenance
    text read "Gmail USC discovery". The documented "unknown" path, which
    three other artifacts are built around (`crm.models.Contact.region`'s
    comment, migration `crm/0005_backfill_contact_region`, and
    `crm.views._in_scope`'s firm fallback), was therefore unreachable: a
    blank region never once produced None. Worse, the wrong-but-confident
    answer SUPPRESSES branch 3 — a contact read as "us" is skipped for an HK
    close — costing the pre-deadline re-ping, the highest-value nudge in the
    engine, on exactly the rows that carry the least information.

    So the fallback is now retired from the read path. Blank means unknown,
    unknown takes the both-regions fallback, and a contact whose region
    matters gets it set explicitly. `infer_region` itself stays in the module
    (see its docstring) as the record of the old rule and as the tool for a
    one-time backfill, should the founder want those guesses written into the
    column where they can be seen and corrected.

  - DIVERGENCE from the original (deliberate, 2026-07-27, REVERTED
    2026-07-28): branch 6 briefly staged the follow-up gap — a longer
    `second_followup_after_business_days` window before follow-up #2 and any
    later one, on the reasoning that a second unanswered note is evidence and
    the right response is to back off rather than keep the same beat. The
    owner's actual call is simpler and stricter: don't send a second
    follow-up at all. One unanswered follow-up is enough evidence on its own
    — park it rather than try again on a longer leash. So there is no
    "later one" for a second window to govern; `max_cold_touches` is now
    capped at 2 in `crm.views.TUNABLE_CADENCE_PARAMS` (one outreach note, one
    follow-up, then park) precisely so that branch can't be reached, and the
    staging key was removed rather than left dormant: a configuration knob
    that LOOKS reachable is worse than no knob at all, because a future
    reader has to rediscover that it can't fire before trusting that it
    doesn't.

  - `tasks_from_change()`: the backward planner. Fires ONLY on
    `confirmed_official` changes (rumor / reported never spawn a task).
    `app_open` -> advocates-in-place task 14d before; `app_close` ->
    re-ping 14d before + submit 5d before; `insight_deadline` -> apply 7d
    before. The <= 3-day in-place-update rule lives in
    `plan_task_write()`, which decides whether a freshly planned task is a
    duplicate of an existing one, an in-place date update, or genuinely
    new — the exact `db.upsert_task` semantics, lifted off the DB.

This module is PURE: unlike `pipeline.py` (which writes to a DB and takes a
connection), the cadence engine only READS and COMPUTES. Every function
takes plain Python data structures (lists of contact / touch / firm_date
dicts, a firm-metadata mapping) and an explicit as-of `datetime`, and
returns plain dicts. It imports nothing from Django, psycopg, or the
network. The web layer fetches the rows (scoped to one `user_id`) and hands
them in; this module never issues a query and never learns a tenant id —
tenancy was enforced at fetch time.

Determinism: no wall-clock read anywhere. The original `due_actions` called
`datetime.now()` inside its thank-you "hours since chat" math; here that
single clock read becomes the caller-supplied `as_of`, so the same inputs
always produce byte-identical output (a golden-fixture-testable property the
original lacked).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Rule parameters — ported verbatim from campaign/cadence.yaml (9 lines, no
# code there). Module-level defaults; every function accepts a `params`
# mapping that overrides individual keys, so the web layer can carry global
# defaults here and still let a value be tuned without a code change.
# ---------------------------------------------------------------------------
CADENCE_DEFAULTS: dict[str, int] = {
    # Gap before the ONE follow-up a cold, never-replied contact gets. Business
    # days since the last touch, set to clear a full calendar week — which
    # takes some arithmetic: `business_days_since` counts Mon-Fri only, so 5
    # business days is exactly 7 calendar days. A window of 5 therefore sits ON
    # one week, not beyond it — the boundary case, and the wrong side of the
    # owner's "more than a week". 6 is the smallest window that always clears
    # it (8 calendar days from a Monday touch, 10 from a Friday one, since the
    # gap then swallows two weekends).
    "followup_after_business_days": 6,   # gap before the (only) follow-up
    "park_after_business_days": 10,      # after max_cold_touches, no reply this long -> park
    # Deliberately capped at 2 in crm.views.TUNABLE_CADENCE_PARAMS: one
    # outreach note, one follow-up, then park — never a second follow-up. See
    # that constant's comment for why this is enforced structurally rather
    # than left as a preference.
    "max_cold_touches": 2,               # initial note + 1 follow-up, then park
    "thank_you_within_hours": 24,        # after any chat
    # ...but the prompt STOPS after this many days. A thank-you note is a
    # courtesy with a shelf life: sent the next morning it lands well, sent
    # three weeks later it reads as an afterthought and draws attention to the
    # silence. Past this window the right move is a fresh reason to talk, not a
    # belated thanks, so the contact falls through to the rest of the cadence
    # instead of being nagged forever. Surfaced by the founder cutover, which
    # replayed months of historical chats and produced a wall of stale
    # thank-you prompts on day one.
    "thank_you_expires_after_days": 7,
    "advocate_touch_min_weeks": 4,       # keep advocates warm every 4-6 weeks
    "advocate_touch_max_weeks": 6,
    "pre_deadline_reping_days": 14,      # re-ping warm contacts when a CONFIRMED app_close is this near
    "stale_thread_days": 21,             # (kept for parity; used by status-style reports, not due_actions)
    "advocate_target": 2,                # advocates-in-place yardstick (from profile.advocate_target)
}

# Confirmed-buckets vocabulary — the only confidence value that may spawn a
# task or drive a re-ping, matching confidence.py's domain-cap ceiling.
CONFIRMED = "confirmed_official"

# Warmth values that count as "warm enough to re-ping" (branch 3).
_WARM = ("replied", "chatted", "advocate")

# Outbound touch kinds — what counts as "you reached out" for the cold-cadence
# touch count (branch 6).
_OUTBOUND_KINDS = ("outreach", "follow_up")


def _merged_params(params: Mapping[str, Any] | None) -> dict[str, int]:
    merged = dict(CADENCE_DEFAULTS)
    if params:
        merged.update(params)
    return merged


def infer_region(source: str | None) -> str | None:
    """'hk' if the contact's free-text `source` mentions HK, else 'us' — the
    same two-way call the original `netdash.data.infer_region` made. A
    missing/empty source returns None rather than guessing 'us', so branch 3
    can stay conservative (match either region) when region truly can't be
    determined. Ported from campaign/src/campaign/region.py.

    RETIRED FROM THE READ PATH (2026-07-27) — `contact_region` no longer calls
    this, and nothing else in the engine does either. It is kept because it is
    the only written record of how region used to be decided, and because a
    ONE-TIME data migration is the right place to use it if the founder ever
    wants the old inference materialised into the `region` column, where a
    human can see it and correct it. Running it on every read is what made it
    harmful: `source` is a provenance string, not a region, so this returns a
    confident answer for any non-empty input and the "unknown" case it
    documents was unreachable in practice. See `contact_region`."""
    if not source:
        return None
    return "hk" if "hk" in source.lower() else "us"


def contact_region(contact: Mapping[str, Any]) -> str | None:
    """The region a contact belongs to, or None when it genuinely isn't known.

    Only the explicit `region` key answers. Blank means UNKNOWN, and unknown
    is a real answer here — branch 3 handles it by matching the soonest close
    across any region for the firm, which is the conservative behaviour the
    whole design documents.

    `infer_region(source)` is deliberately NOT consulted (see that function).
    While it was, this returned a confident region 100% of the time and the
    unknown path below could never be taken."""
    return (contact.get("region") or "").strip().lower() or None


def business_days_since(then: date, now: date) -> int:
    """Whole business days (Mon-Fri) strictly after `then` up to and
    including `now`. Ported verbatim from the original cadence.py."""
    if then >= now:
        return 0
    n, cur = 0, then
    while cur < now:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _as_dt(value: Any) -> datetime | None:
    """Coerce a touch/firm_date timestamp to a tz-aware UTC datetime.

    Accepts a `datetime` (naive assumed UTC), a `date`, or an ISO-8601
    string (the original stored touch timestamps as 'YYYY-MM-DD HH:MM'
    strings; Postgres hands back real datetimes — both are tolerated here so
    fixtures and real rows behave identically). Returns None if unparseable,
    mirroring the original's defensive `_touch_date`/`_hours_since` (which
    returned None on a bad parse rather than raising).

    Tries `fromisoformat` FIRST, before the `strptime` fallback formats.
    `fromisoformat` understands a trailing UTC offset ("+08:00"); the
    `strptime` formats below do not, and — because `text[: len(fmt) + 2]`
    truncates the string to roughly the format's own length before parsing
    — silently chop the offset off rather than raising, so a
    "2026-07-27 09:00:00+08:00" timestamp used to parse as 09:00 UTC: an
    unflagged 8-hour shift. Trying `fromisoformat` first parses the offset
    correctly whenever it's present; the `strptime` loop remains only as a
    fallback for stricter/older textual variants `fromisoformat` rejects."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        text = text.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    return None


def _as_date(value: Any) -> date | None:
    dt = _as_dt(value)
    return dt.date() if dt is not None else None


def _touch_dt(t: Mapping[str, Any]) -> datetime | None:
    return _as_dt(t.get("ts"))


def _firm_meta(firms: Mapping | Iterable[Mapping] | None) -> dict[Any, dict]:
    """Normalize the `firms` argument to {firm_id: {"name", "tier"}}.

    Accepts either a mapping keyed by firm id, or an iterable of firm dicts
    (each carrying `id` or `firm_id`). Tier lives on the per-user
    `user_firms` join in the multi-tenant schema, so the caller is expected
    to fold it in before handing firms here; a missing tier defaults to 3
    and a missing name to the id, exactly as the original
    `config.firms()`-backed lookups defaulted.

    `crm.UserFirm.tier` is a nullable column, and `crm.views.set_firm_tier`
    deliberately writes `tier=None` when a firm is dragged to the
    "Unranked" lane — a real, on-the-record value, not an absent key. So the
    None-coercion below is required in addition to `dict.get`'s default:
    `f.get("tier", 3)` only substitutes 3 when the key is MISSING, and would
    otherwise hand back a bare `None` that later breaks a `sort()` comparing
    tiers to ints (see `due_actions`)."""
    out: dict[Any, dict] = {}
    if firms is None:
        return out
    if isinstance(firms, Mapping):
        items = firms.items()
    else:
        items = ((f.get("id", f.get("firm_id")), f) for f in firms)
    for fid, f in items:
        tier = f.get("tier")
        out[fid] = {"name": f.get("name", fid), "tier": 3 if tier is None else tier}
    return out


def _closing_soon(
    firm_dates: Iterable[Mapping[str, Any]], today: date, reping_days: int
) -> dict[Any, dict[str | None, date]]:
    """firm_id -> {region: soonest confirmed app_close within the window}.

    Ported from the original's closing_soon build. Only `confirmed_official`
    app_close dates within `reping_days` qualify (the original relied on
    `kb.confirmed_deadlines()` already filtering to confirmed; here the
    confidence check is explicit on each firm_date row). Keyed by region so
    branch 3 can scope the re-ping to the contact's own region.

    A date already in the past is dropped, not just one too far in the
    future: without this, a stale `confirmed_official` app_close that has
    already come and gone would still occupy the per-region `min()` bucket
    below and could beat out (and hide) a genuinely upcoming close for the
    same firm/region, on top of firing a priority-0 re-ping for a deadline
    that's already over."""
    out: dict[Any, dict[str | None, date]] = {}
    for e in firm_dates:
        if e.get("event_kind") != "app_close":
            continue
        if e.get("confidence") != CONFIRMED:
            continue
        d = _as_date(e.get("date"))
        if d is None or d < today or (d - today).days > reping_days:
            continue
        fid = e.get("firm_id")
        # Case-normalized only — NOT collapsed with the blank/None fallback,
        # which is a separate, deliberate behavior (see contact_region) that
        # needs a product decision before it changes.
        region = e.get("region")
        region = region.lower() if isinstance(region, str) else region
        bucket = out.setdefault(fid, {})
        if region not in bucket or d < bucket[region]:
            bucket[region] = d
    return out


def due_actions(
    contacts: Iterable[Mapping[str, Any]],
    touches: Iterable[Mapping[str, Any]],
    firm_dates: Iterable[Mapping[str, Any]] | None = None,
    *,
    as_of: datetime,
    firms: Mapping | Iterable[Mapping] | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[dict]:
    """All cadence-due actions for one user's contacts, ranked.

    Pure port of campaign `cadence.due_actions`. The caller fetches the
    user's non-archived `contacts`, all their `touches`, the relevant shared
    `firm_dates`, and a `firms` metadata mapping (name + per-user tier),
    then passes them in. Nothing here reads a database or a wall clock.

    Args:
        contacts: contact dicts. Keys used: `id`, `firm_id` (or `firm`),
            `firm_text` (display fallback when firm_id is None), `warmth`,
            `thread_state`, `region` ("us" / "hk" / blank-or-absent for
            unknown — the ONLY key consulted for region; see
            `contact_region`), `archived` (optional; truthy rows are
            skipped).
        touches: touch dicts across those contacts. Keys used: `contact_id`,
            `ts`, `kind`.
        firm_dates: shared firm_date dicts. Keys used: `firm_id`,
            `event_kind`, `region`, `date`, `confidence`.
        as_of: the as-of instant. `today = as_of.date()` drives all
            business-day math; `as_of` itself drives the thank-you
            "hours since chat" calculation (the original's one
            `datetime.now()` read).
        firms: firm metadata (see `_firm_meta`).
        params: overrides for `CADENCE_DEFAULTS`.

    Returns:
        A list of action dicts, each:
            {"contact", "action", "reason", "priority", "tier",
             "firm_name", "ctx"}
        `contact` is the input dict as-is (the web layer needs it). `ctx`
        carries the raw numbers the reason string renders, so a UI can build
        its own phrasing without re-parsing `reason`. Sorted by
        (priority, tier, firm_name) — priority 0 is most urgent.
    """
    p = _merged_params(params)
    followup_bd = int(p["followup_after_business_days"])
    park_bd = int(p["park_after_business_days"])
    max_cold = int(p["max_cold_touches"])
    ty_hours = int(p["thank_you_within_hours"])
    ty_expiry_days = int(p["thank_you_expires_after_days"])
    adv_min_weeks = int(p["advocate_touch_min_weeks"])
    adv_min_days = adv_min_weeks * 7
    adv_max_weeks = int(p["advocate_touch_max_weeks"])
    reping_days = int(p["pre_deadline_reping_days"])

    today = as_of.date()
    meta = _firm_meta(firms)
    closing_soon = _closing_soon(firm_dates or (), today, reping_days)

    # Group touches by contact once, sorted ascending by timestamp.
    by_contact: dict[Any, list[Mapping[str, Any]]] = {}
    for t in touches:
        by_contact.setdefault(t.get("contact_id"), []).append(t)
    for lst in by_contact.values():
        lst.sort(key=lambda t: (_touch_dt(t) or datetime.min.replace(tzinfo=timezone.utc)))

    actions: list[dict] = []
    for c in contacts:
        if c.get("archived"):
            continue
        cid = c.get("id")
        ctouches = by_contact.get(cid, [])
        firm_id = c.get("firm_id", c.get("firm"))
        firm_name = meta.get(firm_id, {}).get("name") or c.get("firm_text") or firm_id or "?"
        # Same None-coercion as _firm_meta (a `.get(..., 3)` default alone
        # would not catch an explicit `tier=None` from an "Unranked" drag —
        # that used to raise a TypeError comparing None to int in the
        # `actions.sort()` below).
        _tier = meta.get(firm_id, {}).get("tier")
        tier = 3 if _tier is None else _tier

        last = ctouches[-1] if ctouches else None
        lt_date = _as_date(last.get("ts")) if last else None

        def add(action: str, reason: str, prio: int, **ctx: Any) -> None:
            actions.append({
                "contact": c, "action": action, "reason": reason,
                "priority": prio, "tier": tier, "firm_name": firm_name, "ctx": ctx,
            })

        thread_state = c.get("thread_state")
        warmth = c.get("warmth")

        # 1. thank-you within `ty_hours` of a chat, scoped to the LATEST chat
        #    touch. Once thanked, the contact FALLS THROUGH to the reping /
        #    maintain cadence below instead of dropping out forever.
        if thread_state == "chat_done":
            chats = [t for t in ctouches if t.get("kind") == "chat"]
            latest_chat = chats[-1] if chats else None
            chat_dt = _touch_dt(latest_chat) if latest_chat else None
            thanked = False
            for t in ctouches:
                if t.get("kind") != "thank_you":
                    continue
                t_dt = _touch_dt(t)
                if chat_dt is None or (t_dt is not None and t_dt >= chat_dt):
                    thanked = True
                    break
            hrs = None
            if chat_dt is not None:
                hrs = (as_of - chat_dt).total_seconds() / 3600
            # The window has closed: too late for the note to read as a thanks.
            # Deliberately checked BEFORE `thanked`, so an expired prompt and a
            # sent one behave identically from here on — both fall through.
            #
            # An undatable chat (chat_dt is None — reachable via the
            # import/reconciliation path, which can write a `chat` touch
            # without a parseable `ts`) counts as expired too: if you can't
            # date the chat, you can't claim the thank-you is still timely,
            # and treating it as "not yet expired" made `hrs is None` stay
            # False forever, pinning an immortal daily thank-you prompt.
            expired = chat_dt is None or (hrs is not None and hrs > ty_expiry_days * 24)
            if not thanked and not expired:
                overdue = hrs is not None and hrs > ty_hours
                add(
                    "thank_you",
                    "chat done"
                    + (f" {hrs:.0f}h ago" if hrs is not None else "")
                    + " — send thank-you"
                    + (" (OVERDUE)" if overdue else f" (within {ty_hours}h)"),
                    0 if overdue else 1,
                    hours=hrs, overdue=overdue, window_hours=ty_hours,
                )
                continue
            # Thanked for the latest chat, or the window has closed — fall
            # through to the reping / maintain cadence below either way.

        # 2. a scheduled-but-not-logged chat gone stale > 4 business days.
        if thread_state == "chat_scheduled":
            # `bd is None` (no dateable touch on record) is treated the same
            # as "definitely stale" — the branch still needs to surface
            # SOMETHING, but the reason text below says so honestly instead
            # of rendering a 999-day sentinel as if it were a real count.
            bd = business_days_since(lt_date, today) if lt_date else None
            if bd is None or bd > 4:
                reason = (
                    "chat was scheduled but no touches are on record — did it "
                    "happen? log the chat or reschedule"
                    if bd is None else
                    f"chat was scheduled {bd} business days ago — did it happen? "
                    "log the chat or reschedule"
                )
                add("confirm_chat", reason, 1, business_days=bd)
            continue

        # 3. pre-deadline re-ping for warm contacts at closing firms, scoped
        #    to the contact's own region.
        by_region = closing_soon.get(firm_id)
        close: date | None = None
        if by_region:
            region = contact_region(c)
            if region is None:
                close = min(by_region.values())
            elif region in by_region:
                close = by_region[region]
        if close is not None and warmth in _WARM:
            window_start = close - timedelta(days=reping_days)
            already = any(
                t.get("kind") == "reping"
                and (_as_date(t.get("ts")) or date.min) >= window_start
                for t in ctouches
            )
            if not already:
                add(
                    "reping",
                    f"{firm_name} app closes {close.isoformat()} (confirmed) — "
                    "re-ping before you submit",
                    0, close_date=close.isoformat(),
                )
                continue

        # 4. parked / quiet -> skip.
        if thread_state in ("parked", "quiet"):
            continue

        # 5. advocate idle >= advocate_touch_min_weeks -> maintain.
        if warmth == "advocate":
            days = (today - lt_date).days if lt_date else None
            if days is None or days >= adv_min_days:
                # The range in the copy is rendered from the params, not
                # hardcoded — only `advocate_touch_min_weeks` gates this
                # branch and it IS user-tunable from Settings; a hardcoded
                # "4–6" used to keep printing even after the min was tuned
                # away from the default, e.g. to 8.
                reason = (
                    "advocate — no dateable touch on record "
                    f"(target every {adv_min_weeks}–{adv_max_weeks} weeks)"
                    if days is None else
                    f"advocate — last touch {days}d ago (target every "
                    f"{adv_min_weeks}–{adv_max_weeks} weeks)"
                )
                add("maintain", reason, 2, days_since=days, target_min_weeks=adv_min_weeks)
            continue

        # 6. cold / no_reply cadence.
        if warmth == "cold" and thread_state == "no_reply":
            outbound = sum(1 for t in ctouches if t.get("kind") in _OUTBOUND_KINDS)
            if outbound == 0:
                add("first_outreach", "added but never contacted — send the first note", 1)
                continue
            # `bd is None` means outbound touches exist but none carry a
            # dateable ts — treated as "definitely stale enough" for both
            # thresholds below, same reasoning as branch 2, so the branch
            # still fires but never prints the old 999-day sentinel as a
            # real number.
            bd = business_days_since(lt_date, today) if lt_date else None
            # Park is checked first. `max_cold_touches` is capped at 2 in
            # crm.views.TUNABLE_CADENCE_PARAMS (one outreach note, one
            # follow-up), so `outbound >= max_cold` here always means "the
            # one follow-up already went out and still got no reply" — there
            # is no second follow-up to stage a later window for.
            if outbound >= max_cold and (bd is None or bd >= park_bd):
                reason = (
                    f"{outbound} touches, no reply, no dateable touch on record — park it"
                    if bd is None else
                    f"{outbound} touches, no reply, {bd} business days silent — park it"
                )
                add("park", reason, 3, outbound=outbound, business_days=bd)
            elif outbound < max_cold and (bd is None or bd >= followup_bd):
                reason = (
                    f"no reply after touch {outbound}, no dateable touch on record — follow up"
                    if bd is None else
                    f"no reply {bd} business days after touch {outbound} — follow up"
                )
                add(
                    "follow_up", reason, 1, outbound=outbound, business_days=bd,
                    window_business_days=followup_bd,
                )
            continue

        # 7. replied and idle >= 3 business days -> advance (propose a chat).
        if thread_state == "replied" and warmth in ("replied", "cold"):
            bd = business_days_since(lt_date, today) if lt_date else None
            if bd is None or bd >= 3:
                reason = (
                    "they replied — propose a 15-min chat (no dateable touch on record)"
                    if bd is None else
                    f"they replied — propose a 15-min chat (idle {bd} business days)"
                )
                add("advance", reason, 1, business_days=bd)

    actions.sort(key=lambda a: (a["priority"], a["tier"], str(a["firm_name"])))
    return actions


# ---------------------------------------------------------------------------
# Backward planner — ported from campaign/src/campaign/tasks.py.
# ---------------------------------------------------------------------------
# Per-event backward lead times, in days. Ported verbatim from tasks.py.
TASK_PLAN: dict[str, list[dict[str, Any]]] = {
    "app_open": [
        {"kind": "advocate_target", "lead_days": 14},
    ],
    "app_close": [
        {"kind": "reping", "lead_days": 14},
        {"kind": "submit", "lead_days": 5},
    ],
    "insight_deadline": [
        {"kind": "insight_app", "lead_days": 7},
    ],
}

# Kinds of change that can produce a task (mirrors tasks_from_change's guard).
_TASK_CHANGE_KINDS = ("new_date", "updated_date", "corroborated")


def tasks_from_change(
    change: Mapping[str, Any],
    *,
    today: date,
    firms: Mapping | Iterable[Mapping] | None = None,
    params: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Backward-plan concrete tasks from a single firm_date change.

    Pure port of campaign `tasks.tasks_from_change`. Fires ONLY on
    `confirmed_official` changes — a rumor or reported date shows up in the
    brief but never creates or moves a task on its own. A date already in the
    past (relative to `today`) produces nothing.

    Args:
        change: a change dict. Keys used: `kind` (one of new_date /
            updated_date / corroborated — anything else yields nothing),
            `confidence` (must equal `confirmed_official`), `key` (the
            firm_date key `"<firm_id>/<cycle>/<event_kind>"`), `new` (the new
            date string; only its leading date token is read), `firm_id`
            (optional; falls back to the first path segment of `key`),
            `region` (optional; an "hk" region tags the title).
        today: the as-of date (past dates are dropped against this).
        firms: firm metadata for the display name (see `_firm_meta`).
        params: overrides `CADENCE_DEFAULTS`; only `advocate_target` is read,
            for the app_open task's "{target}+ advocates" title.

    Returns:
        A list of planned-task dicts, each:
            {"kind", "firm_id", "source_key", "due", "title", "why",
             "event_kind"}
        (empty when the change doesn't qualify). `due` is an ISO date string
        `lead_days` before the event. These are proposals — persistence and
        de-duplication are `plan_task_write`'s job.
    """
    if change.get("kind") not in _TASK_CHANGE_KINDS:
        return []
    if change.get("confidence") != CONFIRMED:
        return []
    key = change.get("key")
    new = change.get("new")
    if not key or not new:
        return []
    d = _as_date(str(new).split(" ")[0])
    if d is None or d < today:
        return []

    p = _merged_params(params)
    firm_id = change.get("firm_id") or str(key).split("/")[0]
    meta = _firm_meta(firms)
    name = meta.get(firm_id, {}).get("name", firm_id)
    target = int(p["advocate_target"])
    event = str(key).split("/")[-1]
    region_tag = " (HK)" if change.get("region") == "hk" else ""

    out: list[dict] = []

    def add(kind: str, lead_days: int, title: str, why: str) -> None:
        out.append({
            "kind": kind, "firm_id": firm_id, "source_key": key,
            "event_kind": event,
            "due": (d - timedelta(days=lead_days)).isoformat(),
            "title": title, "why": why,
        })

    if event == "app_open":
        add("advocate_target", 14,
            f"Have {target}+ advocates at {name}{region_tag} before apps open {d.isoformat()}",
            f"{name} app opens {d.isoformat()} (confirmed). Advocates in place before "
            "open = referrals ready day one.")
    elif event == "app_close":
        add("reping", 14,
            f"Re-ping warm {name}{region_tag} contacts — app closes {d.isoformat()}",
            "2 weeks out: remind advocates you're applying, ask for a referral/flag.")
        add("submit", 5,
            f"Submit {name}{region_tag} application (closes {d.isoformat()})",
            "5-day buffer before the confirmed close. Rolling review — earlier beats the "
            "deadline.")
    elif event == "insight_deadline":
        add("insight_app", 7,
            f"Submit {name}{region_tag} insight/sophomore program app (deadline {d.isoformat()})",
            "Insight programs are the sophomore fast-track — confirmed deadline.")

    return out


# Lead time (days) within which a planned task's due-date shift is treated as
# an in-place update of the existing task rather than a new one. Ported from
# db.upsert_task's <=3-day rule.
TASK_INPLACE_UPDATE_DAYS = 3


def plan_task_write(
    planned: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Decide how one planned task reconciles against already-stored tasks.

    Pure port of the write half of `db.upsert_task`: a task is identified by
    its `(source_key, kind)` pair. Given the freshly `planned` task and the
    user's `existing` open tasks, this returns the write intent WITHOUT
    performing any write — the caller (web layer) executes it against the DB.

    De-dup / in-place-update rule:
      - No existing task with the same `(source_key, kind)` -> `insert`.
      - An existing one whose `due` date differs by <= 3 days (or not at all)
        -> `update_in_place` (refresh title/why/due on the SAME row; prevents
        duplicate-task spam when a confirmed date drifts by a day or two).
      - An existing one whose `due` moved by MORE than 3 days -> `reschedule`
        (a material shift; the caller may supersede the old row). This is the
        boundary the original's `<= 3` comparison drew.

    Returns:
        {"op": "insert" | "update_in_place" | "reschedule",
         "task": <planned>, "existing_id": <id or None>, "delta_days": <int>}
    """
    key = (planned.get("source_key"), planned.get("kind"))
    new_due = _as_date(planned.get("due"))
    match = None
    for e in existing or ():
        if (e.get("source_key"), e.get("kind")) == key:
            match = e
            break
    if match is None:
        return {"op": "insert", "task": dict(planned), "existing_id": None, "delta_days": None}

    old_due = _as_date(match.get("due"))
    if new_due is None or old_due is None:
        delta = None
        op = "update_in_place"
    else:
        delta = abs((new_due - old_due).days)
        op = "update_in_place" if delta <= TASK_INPLACE_UPDATE_DAYS else "reschedule"
    return {"op": op, "task": dict(planned), "existing_id": match.get("id"), "delta_days": delta}
