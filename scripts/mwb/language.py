"""Directive-language check for the narrative.

The briefing informs; it never directs. Masters make the final decision. Words
that read as an instruction, a verdict on safety, or an operating limit are
flagged before the brief is rendered, with a suggested neutral alternative.
"""

from __future__ import annotations

import re

FLAGGED = [
    (r"\blimits?\b", "reference threshold"),
    (r"\b(un)?safe(ly|ty)?\b", "describe the condition, not a safety verdict"),
    (r"\bexceed(s|ed|ing|ance)?\b", "reaches / is at or above"),
    (r"\bbreach(es|ed)?\b", "reaches / is at or above"),
    (r"\b(cancel|suspend|halt)\w*\b", "describe the forecast; service decisions rest with the Master"),
    (r"\bshut\s?down\b", "describe the forecast; service decisions rest with the Master"),
    (r"\btie[ -]?up\b", "describe the forecast; service decisions rest with the Master"),
    (r"\b(should|must|ought)\b", "state what the data show"),
    (r"\b(recommend|advise|advisable|consider)\w*\b", "state what the data show"),
    (r"\bno[- ]go\b", "flagged for attention"),
    (r"\b(do not|don't|avoid) (sail|operate|cross|depart)\w*\b", "state what the data show"),
]


def check(narrative: dict) -> list[str]:
    """Return one line per flagged phrase: field, phrase, suggestion."""
    out = []

    def walk(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(f"{prefix}.{k}" if prefix else k, x)
        elif isinstance(v, str) and prefix != "author":
            for pat, hint in FLAGGED:
                for m in re.finditer(pat, v, re.I):
                    ctx = v[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
                    out.append(f"{prefix}: '{m.group(0)}' in “…{ctx}…” → {hint}")

    walk("", narrative)
    return out
