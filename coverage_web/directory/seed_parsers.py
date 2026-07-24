"""Minimal, purpose-built parsers for the founder's two YAML shapes.

WHY NOT PyYAML: the `coverage-web` environment has no YAML library and this
workstream must not add dependencies. Rather than hand-roll a general YAML
engine, these two functions parse exactly the two concrete file shapes the seed
importer reads — nothing more. They are deliberately narrow and are covered by
unit tests against captured fixtures.

Shapes handled:

1. `firms.yaml` — a `firms:` key over a block sequence of single-line *flow*
   mappings: `- {id: gs, name: Goldman Sachs, tier: 1, tracks: [ib, st], ...}`.
   Values are scalars or one-level flow sequences (`[a, b]`). Full-line `#`
   comments and blank lines are skipped.

2. `kb/timeline_{us,hk}.yaml` — top-level `region:`/`cycle:` scalars, an
   ignored `phases:` block, then a `firm_dates:` block sequence of block
   mappings whose keys can be plain scalars, single/double-quoted scalars,
   multi-line double-quoted scalars (the `note:` field), or an empty inline
   value continued on the next indented line (the wrapped `source:` URL).
"""

from __future__ import annotations

import re


# --------------------------------------------------------------------------- firms.yaml

def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split `text` on `sep`, but not inside `[...]` (so `tracks: [ib, st]`
    stays one part)."""
    parts, depth, buf = [], 0, []
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _scalar(v: str):
    """Coerce a flow-mapping scalar: strip quotes; true/false -> bool; ints ->
    int; everything else stays a (stripped) string."""
    v = v.strip()
    if (len(v) >= 2) and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _flow_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_top_level(inner) if x.strip()]
    return _scalar(v)


def parse_firms_yaml(text: str) -> list[dict]:
    """Return the list of firm dicts from a `firms.yaml`. Each dict carries
    whatever keys the flow mapping had (id, name, tier, tracks, regions,
    status, domains, sponsors)."""
    firms: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^-\s*\{(.*)\}\s*$", line)
        if not m:
            continue
        inner = m.group(1)
        row: dict = {}
        for part in _split_top_level(inner):
            if ":" not in part:
                continue
            key, _, val = part.partition(":")
            row[key.strip()] = _flow_value(val)
        if row:
            firms.append(row)
    return firms


# --------------------------------------------------------------------------- timeline_*.yaml

_KEY_RE = re.compile(r"^  ([A-Za-z_]+):(.*)$")


def _parse_timeline_entry(lines: list[str]) -> dict:
    """Parse one `- key: ...` firm_dates entry (its full block of lines) into a
    flat dict. Handles single-line scalars, single/double-quoted scalars,
    multi-line double-quoted scalars, and an empty inline value whose real
    value sits on the following indented line(s)."""
    # Normalize the leading "- " of the first line to two spaces so every key
    # sits at the same indent.
    norm = ["  " + lines[0][2:]] + lines[1:]
    out: dict = {}
    i, n = 0, len(norm)
    while i < n:
        m = _KEY_RE.match(norm[i])
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()

        if rest == "":
            # Value folded onto following more-indented, non-key lines.
            vals, j = [], i + 1
            while j < n and re.match(r"^\s{3,}\S", norm[j]) and not _KEY_RE.match(norm[j]):
                vals.append(norm[j].strip())
                j += 1
            out[key] = " ".join(vals).strip()
            i = j
        elif rest[0] == '"':
            buf, j = rest, i
            while buf.count('"') < 2 and j + 1 < n:
                j += 1
                buf += " " + norm[j].strip()
            val = buf[buf.index('"') + 1: buf.rindex('"')] if buf.count('"') >= 2 else buf.strip('"')
            out[key] = re.sub(r"\s+", " ", val).strip()
            i = j + 1
        elif rest[0] == "'":
            buf, j = rest, i
            while buf.count("'") < 2 and j + 1 < n:
                j += 1
                buf += " " + norm[j].strip()
            val = buf[buf.index("'") + 1: buf.rindex("'")] if buf.count("'") >= 2 else buf.strip("'")
            out[key] = val.strip()
            i = j + 1
        else:
            out[key] = rest.strip().strip("'\"")
            i += 1
    return out


def parse_timeline_yaml(text: str) -> tuple[str, str, list[dict]]:
    """Return `(region, cycle_label, firm_dates)` from a `timeline_*.yaml`.

    `region`/`cycle_label` are the file-level scalars. `firm_dates` is the list
    of entry dicts (each with key, date, precision, confidence, source, found,
    note). The `phases:` block is intentionally ignored — phases are cycle-level
    and have no firm to attach to. `firm_dates:` is the last top-level block in
    both files, so everything after it belongs to it.
    """
    lines = text.splitlines()
    region = cycle_label = ""
    for ln in lines:
        rm = re.match(r"^region:\s*(.+?)\s*$", ln)
        if rm and not region:
            region = rm.group(1).strip().strip("'\"")
        cm = re.match(r"^cycle:\s*(.+?)\s*$", ln)
        if cm and not cycle_label:
            cycle_label = cm.group(1).strip().strip("'\"")

    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r"^firm_dates:\s*$", ln))
    except StopIteration:
        return region, cycle_label, []

    entries: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines[start + 1:]:
        if re.match(r"^-\s", ln):
            if cur is not None:
                entries.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        entries.append(cur)

    return region, cycle_label, [_parse_timeline_entry(e) for e in entries]
