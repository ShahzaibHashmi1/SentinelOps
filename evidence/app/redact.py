"""Redaction of secrets in free text, truncation, and the filter for fault-injection markers.

Every log line that enters a bundle passes clean_line(): secrets are masked first, then the line is cut to
the sample length. Lines that carry a fault-injection marker are withheld (M5 must not learn the injected
fault from labels or text, D68).
"""
from __future__ import annotations

import re

MASK = "[REDACTED]"
MARKER = "chaos"
SAMPLE_CHARS = 300

_KEY = r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
# order matters: quoted JSON values first, then URLs, then headers, then key=value pairs
_PATTERNS = (
    (re.compile(rf'(?i)("{_KEY}"\s*:\s*")([^"]*)(")'), rf"\g<1>{MASK}\g<3>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]*:[^\s/]*@"), rf"\g<1>{MASK}@"),
    (re.compile(r"(?i)\b(authorization)([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic|digest|token)\s+)?[^\s\"',;]+"), rf"\g<1>\g<2>{MASK}"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}"), rf"\g<1> {MASK}"),
    (re.compile(rf"(?i)({_KEY})([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])([^\s\"',;&}}]+)"), rf"\g<1>\g<2>{MASK}"),
)


def redact(text: str) -> str:
    """Mask passwords, tokens, authorization values and credentials inside URLs."""
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def has_marker(text: str) -> bool:
    return MARKER in text.lower()


def clean_line(text: str, limit: int = SAMPLE_CHARS) -> str | None:
    """Redact then truncate one log line; None when the line carries a fault-injection marker."""
    if has_marker(text):
        return None
    return redact(text)[:limit]


def clean_text(text: str, limit: int = 300) -> str:
    """Redact and truncate free text such as an error message; marker text is replaced, never kept."""
    if has_marker(text):
        return "[withheld]"
    return redact(text)[:limit]


def scrub(value):
    """Return a copy of a JSON-like structure in which every string that carries the marker is replaced
    and every remaining string is redacted. Returns (copy, number of replaced strings)."""
    withheld = 0

    def walk(item):
        nonlocal withheld
        if isinstance(item, str):
            if has_marker(item):
                withheld += 1
                return "[withheld]"
            return redact(item)
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, tuple):
            return [walk(x) for x in item]
        if isinstance(item, dict):
            return {walk_key(k): walk(v) for k, v in item.items()}
        return item

    def walk_key(key):
        nonlocal withheld
        if isinstance(key, str) and has_marker(key):
            withheld += 1
            return "[withheld]"
        return key

    return walk(value), withheld
