"""Checkpoint promotion: lift Decisions and Dead ends out of a finished
session's checkpoint into long-term memory.

Pure parsing — no database, no LLM. Lines are taken verbatim (a small local
LLM was seen distorting technical notes), filtered for lines that only make
sense inside their own session, and prefixed with their origin so a recalled
line still says where it came from.
"""

from __future__ import annotations

import re

# "## Decisions", "### Dead ends", "**Decisions:**", "Dead-ends:" ...
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*|__)?\s*([A-Za-z][A-Za-z +/&()-]*?)\s*:?\s*(?:\*\*|__)?\s*:?\s*$"
)
_MD_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")

KINDS = {
    "decision": re.compile(
        r"^(key\s+)?decisions?(\s*\(?\s*\+?\s*(and\s+)?why\)?)?(\s+made)?$",
        re.IGNORECASE,
    ),
    "dead-end": re.compile(
        r"^(dead[\s-]?ends?|failed attempts?|what didn.?t work)$", re.IGNORECASE
    ),
}
LABEL = {"decision": "Decision", "dead-end": "Dead end"}

MIN_CHARS = 40
MIN_WORDS = 6
MAX_CHARS = 600

# Lines that only matter inside their own session.
_SESSION_ONLY = re.compile(
    r"\b(this session|for now|for the moment|temporar(y|ily)|for this (task|pr|run))\b"
    r"|^(none|n/?a|nothing( yet)?|tbd|-)\.?$",
    re.IGNORECASE,
)


def _heading_kind(line: str):
    """Kind of a section heading line ('decision' / 'dead-end'), '' for any
    other heading, None if the line is not a heading at all."""
    if _BULLET.match(line):
        return None
    m = _HEADING.match(line)
    if not m:
        return None
    is_marked = (
        _MD_HEADING.match(line)
        or line.strip().startswith(("**", "__"))
        or line.rstrip().endswith(":")
    )
    if not is_marked:
        return None
    title = m.group(1).strip()
    for kind, rx in KINDS.items():
        if rx.match(title):
            return kind
    return ""


def _clean(text: str) -> str:
    text = re.sub(r"\*\*|__", "", text)
    return " ".join(text.split())


def extract(state: str) -> list[tuple[str, str]]:
    """[(kind, line)] for every bullet under a Decisions / Dead ends heading.
    Indented continuation lines join their bullet; nested bullets are their own
    entries."""
    out: list[tuple[str, str]] = []
    kind = None  # current section kind; None/'' = not collecting
    current: list[str] = []

    def flush():
        if kind and current:
            out.append((kind, _clean(" ".join(current))))
        current.clear()

    for line in (state or "").splitlines():
        hk = _heading_kind(line)
        if hk is not None:
            flush()
            kind = hk or None
            continue
        if not kind:
            continue
        m = _BULLET.match(line)
        if m:
            flush()
            current.append(m.group(2))
        elif line.strip() and current and line[:1].isspace():
            current.append(line.strip())
        elif not line.strip():
            continue
        else:
            # plain paragraph text inside the section: its own entry
            flush()
            current.append(line.strip())
    flush()
    return out


def keep(text: str) -> bool:
    """Worth keeping beyond its session?"""
    if len(text) < MIN_CHARS or len(text.split()) < MIN_WORDS:
        return False
    if _SESSION_ONLY.search(text):
        return False
    return True


def content_for(kind: str, text: str, project: str) -> str:
    text = text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"
    where = f" ({project})" if project else ""
    return f"{LABEL[kind]}{where}: {text}"


def tags_for(kind: str, project: str, harness: str) -> list[str]:
    tags = {"auto-promoted", kind}
    for t in (project, harness):
        t = re.sub(r"[^a-z0-9._-]+", "-", (t or "").lower()).strip("-")
        if t:
            tags.add(t)
    return sorted(tags)


def candidates(
    state: str, project: str, harness: str
) -> tuple[list[dict], list[tuple[str, str]]]:
    """(kept, dropped): kept = [{kind, text, content, tags}], dropped = [(kind, text)]."""
    kept, dropped, seen = [], [], set()
    for kind, text in extract(state):
        key = (kind, text.lower())
        if key in seen:
            continue
        seen.add(key)
        if keep(text):
            kept.append(
                {
                    "kind": kind,
                    "text": text,
                    "content": content_for(kind, text, project),
                    "tags": tags_for(kind, project, harness),
                }
            )
        else:
            dropped.append((kind, text))
    return kept, dropped
