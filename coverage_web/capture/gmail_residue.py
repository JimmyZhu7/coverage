"""Gmail Live's capped Haiku residue classifier — the AI stage that runs
AFTER a user-triggered "Scan Now" rescan's deterministic pass, and ONLY
then. Modeled exactly on `directory/ai_extract.py`'s pattern (own
`is_configured()` gate on `ANTHROPIC_API_KEY`, plain `urllib` client with
bounded retries, the same grounded-quote verification discipline) — see
that module's docstring for why the grounding rule is non-negotiable.

WHAT "RESIDUE" MEANS HERE
--------------------------
`gmail_live._classify_message` is deliberately binary and regex-only (see
its module docstring): every inbound, non-bounce message from a matched
sender becomes a confident `replied: True` finding. But `_classify_message`
does return `None` — no finding at all — for a message it genuinely cannot
place: an address it can't resolve, a bounce-shaped message with no
recognizable failed recipient, and so on. Those `None`s are silently
dropped by both `backfill_connection` and the live sync today, with zero
information recorded. That is this module's "residue": real inbound
messages Gmail's own per-contact search surfaced, that the free
deterministic pass could not confidently classify at all.

`backfill_connection`'s optional `residue_sink` kwarg is how a caller opts
into collecting these (see that function's docstring) — the default
(`None`) leaves every existing caller, including Phase 1's free
import-triggered scan and the original first-connect backfill, completely
unaffected. Only the "Scan Now" rescan command passes a sink.

THE CLOSED CLASSIFICATION, non-negotiable
------------------------------------------
The model answers exactly one of three fixed outcomes for one residue
message — never open-ended free text, so the grounded-quote check can
actually verify it:

  - "genuine_reply"  -- a real, personal reply from the counterparty.
        Funneled through the SAME `capture.gmail.apply_findings` apply
        layer every other finding in this codebase goes through, so a
        confirmed residue reply ratchets `thread_state`/`warmth` exactly
        like a deterministic one would.
  - "auto_reply"     -- an out-of-office / vacation responder / other
        automated bounce-adjacent reply. Never logged as a touch — the
        whole point of catching it is to NOT warm a contact off a robot.
  - "ambiguous"       -- genuinely unclear even to the model. Left dropped,
        same as it was before this module ever ran.

An answer whose `quote` is not a verbatim substring of the message text
given to the model is never trusted, regardless of which outcome it
claims — treated as "ambiguous" the same as a malformed or missing answer.

THE CAP, non-negotiable
------------------------
At most `MAX_RESIDUE_THREADS` (100) residue THREADS are processed in one
rescan run, no matter how many residue messages the deterministic pass
collected. This is the one call site in the whole Gmail Live feature that
spends real, metered API money per run -- which is exactly why
`run_residue_stage`'s `max_threads` parameter exists: `capture.gmail_live.
run_rescan` computes what the caller's credit balance can actually afford
(`billing.credits.affordable_residue_threads`, docs/credit-system-plan.md
enforcement point 2) and clamps to it BEFORE a single classification call
is made, on top of (never instead of) this hard 100-thread ceiling.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings

from capture.gmail import apply_findings

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0
MAX_INPUT_CHARS = 2000  # a snippet + subject is always far under this

# THE cap. Exactly 100, per the build plan -- not 50, not unbounded. See
# module docstring.
MAX_RESIDUE_THREADS = 100

_OUTCOMES = {"genuine_reply", "auto_reply", "ambiguous"}


def is_configured() -> bool:
    return bool(getattr(settings, "ANTHROPIC_API_KEY", "") or "")


class ResidueClassifyError(Exception):
    """Raised after exhausting retries on a transient API failure."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(f"Anthropic API call failed: {cause}")


def _post_json(payload: dict, *, timeout: float, retries: int) -> dict:
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — deliberate plain HTTPS client
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise ResidueClassifyError(e) from e
            last_error = e
        except Exception as e:  # noqa: BLE001 — network/timeout/etc, all retryable
            last_error = e
        if attempt < retries:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise ResidueClassifyError(last_error)


_RESIDUE_PROMPT = """You are reading one email message that arrived in a student's inbox, on a thread with someone they are tracking in a recruiting CRM. Answer ONLY the question below, from ONLY the text given.

QUESTION: Is this message a genuine, personal reply from that person -- or an automated message (out-of-office / vacation responder / other auto-reply), or is it genuinely unclear which?

Respond with EXACTLY this JSON shape and nothing else:
{"outcome": "genuine_reply" | "auto_reply" | "ambiguous", "quote": "<the exact line or sentence from the text below that your answer is based on, copied verbatim>"}

The "quote" field MUST be an exact, verbatim substring of the text below -- do not paraphrase, summarize, or fix typos in it. If you cannot quote it exactly, or the message gives no clear signal either way, answer {"outcome": "ambiguous", "quote": null}.

TEXT:
\"\"\"
{text}
\"\"\"
"""


def _extract_response_text(api_response: dict) -> str:
    blocks = api_response.get("content") or []
    for block in blocks:
        if block.get("type") == "text":
            return block.get("text") or ""
    return ""


def _grounded(quote: str | None, source: str) -> bool:
    """The model's quote must appear verbatim (whitespace- and case-
    normalized) in the source text -- the anti-hallucination guard. See
    `directory.ai_extract`'s identical rule for why this is non-negotiable.

    Case-INSENSITIVE on purpose: a model asked to copy a quote "verbatim"
    routinely still re-capitalizes the first letter of a sentence it's
    quoting (a genuine, common model habit, not a hypothetical) -- "hey,
    thanks" in the source becomes "Hey, thanks" in the quote. That is the
    same text, and a case-sensitive check would reject it, silently
    downgrading a real, correctly-grounded answer to "ambiguous" -- the
    exact loss this module exists to prevent for text the deterministic
    pass already couldn't classify. This does not weaken the guard against
    a genuinely fabricated quote: it still must match every character of
    the source (mod whitespace and case), so a quote with no real basis in
    the text is rejected exactly as before.
    """
    if not quote:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip().casefold()
    return norm(quote) in norm(source)


def _message_text(message: dict) -> str:
    """Subject + snippet -- the same bounded preview text Gmail's own UI
    shows, not the full message body. Plenty of signal for genuine-reply
    vs. auto-reply/OOO (those almost always announce themselves in the
    first line), and keeps what leaves this app for a third-party API to
    the minimum needed to answer the one question asked."""
    subject = ""
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "subject":
            subject = header.get("value", "")
            break
    snippet = message.get("snippet", "")
    return f"Subject: {subject}\n\n{snippet}"


def _classify_one(message: dict, *, model: str, timeout: float, retries: int) -> tuple[str, str | None]:
    """One residue message -> (outcome, quote). Never raises for a "no
    answer"/rejected case -- only for a genuine API failure, which the
    caller treats the same as "ambiguous" (see `run_residue_stage`)."""
    text = _message_text(message)[:MAX_INPUT_CHARS]
    prompt = _RESIDUE_PROMPT.replace("{text}", text)
    response = _post_json(
        {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
        retries=retries,
    )
    raw = _extract_response_text(response).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return "ambiguous", None

    outcome = parsed.get("outcome")
    quote = parsed.get("quote")
    if outcome not in _OUTCOMES:
        return "ambiguous", None
    if outcome == "ambiguous":
        return "ambiguous", quote
    if not _grounded(quote, text):
        # The core defense: a claimed genuine_reply/auto_reply whose quote
        # doesn't actually appear in the source is never trusted, no matter
        # how confident-sounding the outcome. Downgrade to ambiguous.
        return "ambiguous", None
    return outcome, quote


def _finding_from_genuine_reply(own_email: str, message: dict, thread_id: str) -> dict | None:
    """Build the same finding-dict shape `_classify_message` would have,
    for a residue message the model confidently grounded as a genuine
    reply. Mirrors `gmail_live._classify_message`'s inbound-reply branch."""
    headers = message.get("payload", {}).get("headers", [])
    from_header = ""
    for header in headers:
        if header.get("name", "").lower() == "from":
            from_header = header.get("value", "")
            break
    from_name, from_addr = parseaddr(from_header)
    from_addr = (from_addr or "").strip()
    if not from_addr or from_addr.lower() == own_email.lower():
        return None
    return {
        "name": from_name or from_addr.split("@")[0],
        "email": from_addr.lower(),
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": True,
        "chat_status": "none",
        "chat_scheduled_at": None,
        "evidence": f"AI-classified reply (residue): {message.get('snippet', '')[:300]}",
        "thread_id": thread_id,
    }


def run_residue_stage(
    connection,
    residue: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_threads: int | None = None,
) -> dict:
    """The Phase-3 follow-up stage to a "Scan Now" rescan. `residue` is the
    list `backfill_connection(..., residue_sink=residue)` collected — dicts
    shaped `{"message": <gmail message dict>, "thread_id": str}`.

    Deliberately a separate, well-named function from the deterministic
    stage above it (not folded into `backfill_connection`) so the
    credit-metering pass can wrap exactly this call site — which is exactly
    what `max_threads` is for: `capture.gmail_live.run_rescan` computes what
    the caller's credit balance can actually afford
    (`billing.credits.affordable_residue_threads`) and passes it in here.
    `None` (every other caller, and every existing test) means "no extra
    clamp" — `MAX_RESIDUE_THREADS` alone still applies. When both are given,
    the SMALLER wins; this function never spends more API calls than either
    limit allows.

    Never raises: an API failure on one message downgrades that message to
    "ambiguous" and the run continues; a missing key means the stage is a
    no-op reporting zero everywhere. Always returns a stats dict, even when
    unconfigured or given no residue at all.
    """
    stats = {
        "residue_threads_seen": 0,
        "residue_threads_processed": 0,
        "genuine_reply": 0,
        "auto_reply": 0,
        "ambiguous": 0,
        "touches_logged": 0,
    }
    if not residue:
        return stats

    # Dedup to one representative message per THREAD -- the cap is on
    # threads, not raw messages (a thread can surface more than one
    # unclassified message). First occurrence wins; order follows however
    # the deterministic pass encountered them.
    by_thread: dict[str, dict] = {}
    for entry in residue:
        thread_id = entry.get("thread_id") or ""
        if not thread_id or thread_id in by_thread:
            continue
        by_thread[thread_id] = entry["message"]

    stats["residue_threads_seen"] = len(by_thread)
    if not is_configured():
        return stats

    # THE cap, enforced by slicing before a single API call is made for the
    # 101st+ candidate — and, when the caller passed one, the SMALLER of
    # that hard ceiling and whatever the credit system says is affordable
    # right now (see this function's docstring on `max_threads`).
    effective_cap = MAX_RESIDUE_THREADS if max_threads is None else min(MAX_RESIDUE_THREADS, max_threads)
    thread_items = list(by_thread.items())[:effective_cap]
    stats["residue_threads_processed"] = len(thread_items)

    own_email = connection.gmail_address.lower()
    findings = []
    for thread_id, message in thread_items:
        try:
            outcome, _quote = _classify_one(message, model=model, timeout=timeout, retries=retries)
        except ResidueClassifyError:
            outcome = "ambiguous"
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome == "genuine_reply":
            finding = _finding_from_genuine_reply(own_email, message, thread_id)
            if finding is not None:
                findings.append(finding)

    if findings:
        result = apply_findings(connection.user, findings)
        stats["touches_logged"] = result.touches_logged

    return stats
