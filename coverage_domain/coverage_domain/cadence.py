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
      6. cold / no_reply: 0 outbound -> first_outreach; else
         follow_up at >= followup window; else park once
         max_cold reached and idle >= park window
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

  - DIVERGENCE from the original (deliberate, 2026-07-25): a contact's region
    is now read from an EXPLICIT `region` key first, and only falls back to
    `infer_region(source)` when that key is blank. The original had no region
    column and inferred it by substring-matching "hk" inside the free-text
    `source` — which is a provenance string, so every contact whose source
    didn't happen to say "hk" (including every hand-added one, source
    "manual") silently read as US: re-pinged against US deadlines, skipped
    for HK ones. The fallback is kept rather than removed so rows written
    before the explicit field existed keep the exact meaning they had.

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
    "followup_after_business_days": 5,   # cold, no reply -> follow up in the 5-7 bd window
    "park_after_business_days": 10,      # after max_cold_touches, no reply this long -> park
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

    LEGACY FALLBACK ONLY. `source` is a provenance string, not a region, so
    this is a guess and a bad one for any source that simply doesn't mention
    HK. New callers set `contact["region"]` explicitly; this stays only so
    rows written before that field existed keep their previous meaning. See
    `contact_region`."""
    if not source:
        return None
    return "hk" if "hk" in source.lower() else "us"


def contact_region(contact: Mapping[str, Any]) -> str | None:
    """The region a contact belongs to, or None when it genuinely isn't known.

    The explicit `region` key wins. `infer_region(source)` is consulted ONLY
    when `region` is absent or blank, so setting a region can never be
    overridden by provenance text, and a legacy row with no region behaves
    exactly as it did before the field existed."""
    explicit = (contact.get("region") or "").strip().lower()
    if explicit:
        return explicit
    return infer_region(contact.get("source"))


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
    returned None on a bad parse rather than raising)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
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
    `config.firms()`-backed lookups defaulted."""
    out: dict[Any, dict] = {}
    if firms is None:
        return out
    if isinstance(firms, Mapping):
        items = firms.items()
    else:
        items = ((f.get("id", f.get("firm_id")), f) for f in firms)
    for fid, f in items:
        out[fid] = {"name": f.get("name", fid), "tier": f.get("tier", 3)}
    return out


def _closing_soon(
    firm_dates: Iterable[Mapping[str, Any]], today: date, reping_days: int
) -> dict[Any, dict[str | None, date]]:
    """firm_id -> {region: soonest confirmed app_close within the window}.

    Ported from the original's closing_soon build. Only `confirmed_official`
    app_close dates within `reping_days` qualify (the original relied on
    `kb.confirmed_deadlines()` already filtering to confirmed; here the
    confidence check is explicit on each firm_date row). Keyed by region so
    branch 3 can scope the re-ping to the contact's own region."""
    out: dict[Any, dict[str | None, date]] = {}
    for e in firm_dates:
        if e.get("event_kind") != "app_close":
            continue
        if e.get("confidence") != CONFIRMED:
            continue
        d = _as_date(e.get("date"))
        if d is None or (d - today).days > reping_days:
            continue
        fid = e.get("firm_id")
        region = e.get("region")
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
            unknown), `source` (legacy region fallback only, when `region` is
            blank — see `contact_region`), `archived` (optional; truthy rows
            are skipped).
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
    adv_min_days = int(p["advocate_touch_min_weeks"]) * 7
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
        tier = meta.get(firm_id, {}).get("tier", 3)

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
            expired = hrs is not None and hrs > ty_expiry_days * 24
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
            bd = business_days_since(lt_date, today) if lt_date else 999
            if bd > 4:
                add(
                    "confirm_chat",
                    f"chat was scheduled {bd} business days ago — did it happen? "
                    "log the chat or reschedule",
                    1, business_days=bd,
                )
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
            days = (today - lt_date).days if lt_date else 9999
            if days >= adv_min_days:
                add(
                    "maintain",
                    f"advocate — last touch {days}d ago (target every 4–6 weeks)",
                    2, days_since=days, target_min_weeks=adv_min_days // 7,
                )
            continue

        # 6. cold / no_reply cadence.
        if warmth == "cold" and thread_state == "no_reply":
            outbound = sum(1 for t in ctouches if t.get("kind") in _OUTBOUND_KINDS)
            if outbound == 0:
                add("first_outreach", "added but never contacted — send the first note", 1)
                continue
            bd = business_days_since(lt_date, today) if lt_date else 999
            if outbound >= max_cold and bd >= park_bd:
                add(
                    "park",
                    f"{outbound} touches, no reply, {bd} business days silent — park it",
                    3, outbound=outbound, business_days=bd,
                )
            elif bd >= followup_bd and outbound < max_cold:
                add(
                    "follow_up",
                    f"no reply {bd} business days after touch {outbound} — follow up",
                    1, outbound=outbound, business_days=bd,
                )
            continue

        # 7. replied and idle >= 3 business days -> advance (propose a chat).
        if thread_state == "replied" and warmth in ("replied", "cold"):
            bd = business_days_since(lt_date, today) if lt_date else 999
            if bd >= 3:
                add(
                    "advance",
                    f"they replied — propose a 15-min chat (idle {bd} business days)",
                    1, business_days=bd,
                )

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
