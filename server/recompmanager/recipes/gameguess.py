"""Guess which game a recomp is for, from its repo name and description.

Only a starting point: the user confirms it with the draft. Multi-game projects,
engine reimplementations and vague descriptions can't be guessed reliably.
"""

from __future__ import annotations

import re

# "Static recompilation of Majora's Mask (and soon …) for PC" → "Majora's Mask"
_OF = re.compile(
    r"\b(?:recomp(?:ilation)?|decomp(?:ilation)?|port|reimplementation|remake|re-?creation)"
    r"s?\s+of\s+(?:the\s+)?(?:game\s+)?(?P<title>.+?)"
    r"(?=\s+(?:for|to|on|that|which|using|with|built|running|\(|\[)\b|[.,;:(\[]|$)",
    re.I,
)
_NOISE = re.compile(
    r"(?i)(?:[-_ ]?(?:recomp(?:iled|ilation)?|decomp(?:ilation)?|port|pc|native|re|engine))+$"
)


def _clean(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip(" \"'`*_-")
    return title[:1].upper() + title[1:] if title else title


def guess_game_title(repo_name: str, description: str | None) -> str | None:
    if description and (m := _OF.search(description)):
        title = _clean(m.group("title"))
        if 2 <= len(title) <= 120:
            return title
    base = _NOISE.sub("", repo_name)
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)  # CamelCase → words
    base = _clean(re.sub(r"[-_]+", " ", base))
    return base or None


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("'", "")).strip("-")
    return s[:60] or "game"
