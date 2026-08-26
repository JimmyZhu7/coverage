# Firm slug -> the domains the firm's PEOPLE send mail from.
#
# A MAIL DOMAIN IS NOT A CAREER-SITE DOMAIN, AND THAT CONFUSION WAS A BUG
# ----------------------------------------------------------------------
# `Firm.domains` is one list read by two very different consumers:
#
#   - board/logo code, which only ever needed the host a POSTING lives on —
#     `blackstone.wd1.myworkdayjobs.com`, `careers.bcg.com`, `jobs.rbc.com`,
#     `moelis-careers.tal.net`. Those arrived by the hundred, because most
#     firms reached this directory through a connector and a connector only
#     knows its ATS host.
#   - `capture.discovery.FirmDomains`, which asks the opposite question:
#     "does the address this human just emailed from belong to a firm the
#     student tracks?" A Goldman banker writes from `@gs.com`. Nobody has
#     ever sent mail from `careers.bcg.com`.
#
# Because only the first consumer had ever populated the column, the second
# one silently refused real people: `gs.com`, `bofa.com`, `tdsecurities.com`
# and friends were absent, so nineteen of forty-eight real bankers in the
# founder's own mailbox failed the firm-match gate and were never proposed.
#
# Every entry BELOW is a mail domain: a domain a human at that firm plausibly
# sends from. Career-site hosts stay where the connectors put them — nothing
# here removes one — but they are never added here, and a reader adding a row
# should ask "would a banker's From: address end in this?" before writing it.
#
# THE BAR FOR ADDING A ROW. A wrong mapping is worse than a missing one: it
# silently attributes a stranger to a firm the student is recruiting at, and
# the proposal card will say so with a straight face. So an entry goes in only
# when the domain is the firm's OWN, unambiguous primary domain. Anything that
# needed a guess was deliberately left out — see `docs/deploy.md` and the
# report accompanying this file for the names that were considered and
# skipped.

from __future__ import annotations

# ---------------------------------------------------------------------------
# Mail domains to ADD to each firm's existing list. Never a replacement: the
# loader appends what is missing and leaves everything already there alone.
# ---------------------------------------------------------------------------
MAIL_DOMAINS: dict[str, list[str]] = {
    # -- The 2026-08-25 fix: the firms whose real bankers the capture pipeline
    #    was refusing. These were applied by hand to the founder's database
    #    (and to the gitignored `data/seeds/firms.yaml`, which is why they are
    #    restated here — see this module's counterpart command).
    "gs": ["gs.com"],
    "bofa": ["bofa.com", "baml.com"],
    "wf": ["wellsfargo.com"],
    "bnpparibas": ["bnpparibas.com"],
    "td": ["tdsecurities.com"],
    "truist": ["truist.com"],
    # Named "Citadel Securities" in the directory but carrying Citadel LLC's
    # own domain, as the founder's database has it. The two are sibling firms
    # (Citadel Securities' people generally write from citadelsecurities.com),
    # so this is reproduced rather than "corrected" — changing it is a
    # directory decision, not a loader decision.
    "citadel-securities": ["citadel.com"],
    # The three boutiques created by the same fix. Their rows are defined in
    # CREATABLE_FIRMS below so a fresh environment gets the firm too, not just
    # a domain with nowhere to attach.
    "qatalyst": ["qatalyst.com"],
    "allen-company": ["allenco.com"],
    "liontree": ["liontree.com"],

    # -- The wider sweep (2026-08-25). Firms already in the directory whose
    #    stored domains were a career site or nothing at all, and whose own
    #    primary domain admits no ambiguity. Firms whose mail domain needed a
    #    guess (MUFG, Santander, Crédit Agricole CIB, GIC, Haitong, HPS,
    #    Marshall Wace, Qube, Squarepoint, Tower Research, Verition, GSA
    #    Capital, Belvedere) are deliberately absent.
    "bcg": ["bcg.com"],                    # had only careers.bcg.com
    "blackrock": ["blackrock.com"],        # had only careers.blackrock.com
    "rbc": ["rbc.com"],                    # had only jobs.rbc.com
    "accenture": ["accenture.com"],
    "deloitte": ["deloitte.com"],
    "ey": ["ey.com"],
    "bmo": ["bmo.com"],
    "cibc": ["cibc.com"],
    "dbs": ["dbs.com"],
    "stifel": ["stifel.com"],
    "socgen": ["socgen.com", "societegenerale.com"],
    "aqr": ["aqr.com"],
    "bridgewater": ["bridgewater.com"],
    "sig": ["sig.com"],
    "xtx": ["xtxmarkets.com"],
    "schonfeld": ["schonfeld.com"],
    "permira": ["permira.com"],
    "janushenderson": ["janushenderson.com"],
    "exoduspoint": ["exoduspoint.com"],
}

# ---------------------------------------------------------------------------
# Firms the loader is allowed to CREATE when they are missing, so a fresh
# environment ends up with somewhere to hang the domains above. Everything
# else in MAIL_DOMAINS is add-only: an unknown slug is reported and skipped,
# never invented, because the canonical definition of a connector firm lives
# in `boards.py`'s catalog and inventing a second one would fork the row.
#
# `name` is also the fallback identity: a firm already present under this name
# is adopted rather than duplicated, whatever its slug (the founder's database
# carries a "Citadel Securities" row with an EMPTY slug — see the command).
# ---------------------------------------------------------------------------
CREATABLE_FIRMS: dict[str, dict] = {
    "qatalyst": {
        "name": "Qatalyst Partners",
        "tracks": ["ib"],
        "regions": ["us"],
        "status": "active",
    },
    "allen-company": {
        "name": "Allen & Company",
        "tracks": ["ib"],
        "regions": ["us"],
        "status": "active",
    },
    "liontree": {
        "name": "LionTree",
        "tracks": ["ib"],
        "regions": ["us"],
        "status": "active",
    },
    "citadel-securities": {
        "name": "Citadel Securities",
        "tracks": ["st"],
        "regions": ["us"],
        "status": "active",
    },
}
