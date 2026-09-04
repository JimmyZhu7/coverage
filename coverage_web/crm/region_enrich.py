"""Place a contact by their own public record — profile, firm bio, FINRA.

WHY THIS EXISTS, AND WHAT IT IS NOT.

`Contact.resolve_region` bans probabilistic signals on the write path, and
the ban is measured: signature cities and phone country codes are firm-wide
templates that name an OFFICE, Exchange rewrites every Date offset to +0000,
and send-hour clustering resolved 12% of contacts and only after a reply. A
wrong region silently mis-scopes deadline warnings. "Write only what a stated
fact entails; ask for everything else."

The 94 contacts this was built for have nothing to ask. They sit at Citi,
Goldman, JPM, Morgan Stanley, UBS and Jefferies — firms that recruit in BOTH
of the founder's markets, so `default_region_from_firm` correctly declines —
and every one is outbound-only: he wrote, they never replied. No signature,
no timezone, no LinkedIn URL. The row holds a name, an address and a firm,
and the region inference ladder has read all three and found nothing.

What does exist is the person's own public record. A LinkedIn headline says
"Vice President at UBS · New York". A firm bio page says where a banker
sits. A FINRA BrokerCheck registration exists only for someone registered in
the United States. Those are statements ABOUT THE PERSON, not templates about
the firm — which is exactly the line the ban draws — and this module reads
only those, only with the URL it read them from, and only when the page
names both the person and the firm. It says "unknown" rather than choose
between two people who share a name.

It is one Messages API call per contact: Claude with the web-search server
tool, asked to search and then answer through a strict-schema tool so the
result is structured rather than parsed out of prose. Nothing here writes to
the database — `enrich_contact_regions` (management command) does that, dry
by default, with an undo file, mirroring `backfill_contact_regions`.

COST, so nobody is surprised, and MEASURED rather than estimated: search
results are injected into the context, and on the first seven live contacts
that ran 24-32k input tokens each. At Opus 5 pricing that is about fifteen
cents a contact — roughly fourteen dollars for the 94. The search is the
cost, which is why the command saves a plan on the dry run and can apply
from it without searching again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_SEARCHES = 3
REQUEST_TIMEOUT_SECONDS = 120.0

MARKETS = frozenset({"us", "hk", "other", "unknown"})
CONFIDENCES = ("high", "medium", "low")

# The answer comes back through this tool rather than as prose, so the shape
# is guaranteed and there is nothing to regex. `strict: True` is the house
# style (assistant/tools.py) and makes the schema binding.
RECORD_TOOL = {
    "name": "record_placement",
    "description": (
        "Record where this specific person is based, from what the search "
        "found. Call exactly once, after searching. If the pages do not "
        "clearly name THIS person at THIS firm, or name more than one "
        "plausible person, answer market=unknown."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "person_matched", "market", "city", "confidence",
            "source_url", "evidence",
        ],
        "properties": {
            "person_matched": {
                "type": "boolean",
                "description": (
                    "True only if a page names BOTH this person and this firm "
                    "and there is one plausible match, not several."
                ),
            },
            "market": {
                "type": "string",
                "enum": ["us", "hk", "other", "unknown"],
                "description": (
                    "us = based anywhere in the United States. hk = based in "
                    "Hong Kong. other = based somewhere that is neither "
                    "(London, Singapore, Tokyo...). unknown = the pages do "
                    "not state where THIS person is based."
                ),
            },
            "city": {
                "type": "string",
                "description": "The city named for the person, or empty.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "high = a page about this person states their location "
                    "(a profile headline, a firm bio, a FINRA BrokerCheck "
                    "branch). medium = a strong but indirect statement. "
                    "low = a guess; prefer unknown."
                ),
            },
            "source_url": {
                "type": "string",
                "description": "The URL the location was read from, or empty.",
            },
            "evidence": {
                "type": "string",
                "description": (
                    "One sentence quoting or closely paraphrasing what the "
                    "page said about the person's location."
                ),
            },
        },
    },
}

SYSTEM = """You place one finance professional in a recruiting market for a student's CRM.

You are given a person's name, work email and firm. Use web search (up to three searches) to find where THIS PERSON is based, then call record_placement exactly once.

Rules that decide the answer:
- Only a statement about the PERSON counts: their own profile headline or location, a firm bio page about them, a deal announcement naming them and their office, or a FINRA BrokerCheck record (which exists only for people registered in the United States, so it alone is strong evidence of "us").
- The firm's headquarters, a generic office address, a template email signature, or where the firm "has offices" does NOT count. Those describe the firm, not the desk.
- The page must name both the person and the firm. If the search shows several different people with this name, or the only match is at a different firm, answer person_matched=false and market=unknown.
- Never guess. "unknown" is a correct answer and costs nothing. A wrong market silently mis-scopes the student's deadline warnings.
- Markets: us = anywhere in the United States. hk = Hong Kong. other = anywhere else (state the city). unknown = not stated.
- Always give the source_url you read the location from."""


@dataclass
class Placement:
    """What one call found. `writable` is the only thing the command reads
    to decide whether to place the row; everything else is for the human
    reading the dry run and the undo file."""
    market: str
    city: str
    confidence: str
    source_url: str
    evidence: str
    person_matched: bool
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        """Only a high-confidence, person-matched, sourced us/hk answer is
        written. "other" is deliberately excluded here and opted into by the
        command (`--allow-other`): `Contact.firm_markets`'s comment says
        "other" is only ever written by a human, and a first pass should not
        widen that quietly."""
        return (
            self.person_matched
            and self.confidence == "high"
            and bool(self.source_url.strip())
            and self.market in ("us", "hk")
        )

    @property
    def stated(self) -> bool:
        """A sourced, person-matched answer of any market including "other"."""
        return (
            self.person_matched
            and self.confidence == "high"
            and bool(self.source_url.strip())
            and self.market in ("us", "hk", "other")
        )


def _prompt(name: str, email: str, firm: str, role: str) -> str:
    parts = [f"Name: {name}", f"Work email: {email}", f"Firm: {firm}"]
    if role:
        parts.append(f"Title on file: {role}")
    parts.append(
        "Where is this person based? Search, then call record_placement once."
    )
    return "\n".join(parts)


def _record_from_response(response) -> dict | None:
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "record_placement":
            return dict(block.input)
    return None


def _searches(response) -> int:
    usage = getattr(response, "usage", None)
    stu = getattr(usage, "server_tool_use", None)
    return int(getattr(stu, "web_search_requests", 0) or 0)


def enrich(
    name: str,
    email: str,
    firm: str,
    *,
    role: str = "",
    client=None,
    model: str = MODEL,
) -> Placement | None:
    """One call. Returns a Placement, or None when the model did not answer
    through the tool (a refusal, a pause that did not resume, an API error)
    — None means "this run learned nothing", never "unknown market".

    `client` is injectable so tests never touch the network; the default is
    the advisor's own configured client.
    """
    if client is None:
        from assistant.client import get_client, is_configured
        if not is_configured():
            logger.info("region_enrich: ANTHROPIC_API_KEY not set; skipping")
            return None
        client = get_client().with_options(timeout=REQUEST_TIMEOUT_SECONDS)

    import anthropic

    messages = [{"role": "user", "content": _prompt(name, email, firm, role)}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES},
        RECORD_TOOL,
    ]
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            output_config={"effort": "low"},
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        # A server-tool turn can pause; resume once with the content appended.
        if getattr(response, "stop_reason", "") == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            response = client.messages.create(
                model=model,
                max_tokens=2000,
                output_config={"effort": "low"},
                system=SYSTEM,
                tools=tools,
                messages=messages,
            )
    except anthropic.APIStatusError as exc:
        logger.warning("region_enrich: API %s for %r: %s", exc.status_code, email, exc)
        return None
    except anthropic.APIConnectionError as exc:
        logger.warning("region_enrich: connection error for %r: %s", email, exc)
        return None

    record = _record_from_response(response)
    if record is None:
        logger.info("region_enrich: no record_placement call for %r", email)
        return None

    market = str(record.get("market", "unknown")).strip().lower()
    if market not in MARKETS:
        market = "unknown"
    confidence = str(record.get("confidence", "low")).strip().lower()
    if confidence not in CONFIDENCES:
        confidence = "low"

    return Placement(
        market=market,
        city=str(record.get("city", "") or "").strip()[:120],
        confidence=confidence,
        source_url=str(record.get("source_url", "") or "").strip()[:512],
        evidence=str(record.get("evidence", "") or "").strip()[:500],
        person_matched=bool(record.get("person_matched", False)),
        searches=_searches(response),
        input_tokens=int(getattr(response.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(response.usage, "output_tokens", 0) or 0),
        raw=record,
    )
