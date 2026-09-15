"""TITLE_ID validation.

A Nintendo Switch TITLE_ID is exactly 16 hexadecimal characters. This module
is the single place that decides whether a candidate string counts as one --
nothing else in the codebase should roll its own regex.

Two very different sources of a TITLE_ID exist, with very different trust
levels, and callers must not conflate them:

  - "filename": pulled out of a bracketed segment in a release filename
    (e.g. "Game [0100000000010000][v0].nsp"). This is metadata a human (or a
    scene group) typed, not something the file format enforces -- it can be
    wrong, stale, or absent. Never treated as confirmed.
  - "atmosphere_path": the TITLE_ID directory name inside an
    `atmosphere/contents/<TITLE_ID>/` structure. This one IS the value DBI
    will actually act on when the mod is merged onto the SD card, so once it
    matches the 16-hex-char shape it is structurally authoritative -- there is
    nothing further to "trust" beyond validating the shape itself.

If a TITLE_ID can't be pinned down with confidence, callers must surface
NEEDS_REVIEW rather than guess.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

_PATTERN = re.compile(r"^[0-9A-Fa-f]{16}$")
_BRACKET_PATTERN = re.compile(r"\[([0-9A-Fa-f]{16})\]")
_ATMOSPHERE_PATTERN = re.compile(r"(?:^|/)atmosphere/(?:contents|titles)/([0-9a-f]{16})(?=/|$)", re.IGNORECASE)
_RELEASE_TAG_PATTERN = re.compile(r"\s*\[(?:[0-9A-Fa-f]{16}|v\d+)\]")
_NAME_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")
_APOSTROPHE_PATTERN = re.compile(r"['‘’]")


def strip_release_tags(name: str) -> str:
    """Removes bracketed TITLE_ID/version tags a release filename embeds
    (e.g. "Game [0100000000010000][v0]" -> "Game") -- the Library UI's own
    display cleanup (web/templates/library.html's `game_name` filter),
    reused here so a TitleDB name search (covers.py's cover-lookup
    fallback for a wrong/stale filename TITLE_ID, see this module's
    docstring) matches on the human game name only."""
    return _RELEASE_TAG_PATTERN.sub("", name).strip()


def normalize_name(name: str) -> str:
    """Collapses a game name to lowercase alphanumeric words only, so e.g.
    "Mortal Kombat™ 1" (DBI's own "Installed games" MTP listing, real
    hardware, 2026-09-15) and "Mortal Kombat 1" (a release filename, no
    trademark glyph) compare equal -- the single shared name-matching rule
    for every "is this the same game, going only by its human name, not a
    TITLE_ID" comparison in this codebase (covers.py's TitleDB name-search
    fallback, mtp/windows.py's installed-games cross-reference). Every
    caller must use this exact function, never roll its own -- see this
    module's docstring on filename metadata never being authoritative
    enough to skip a shared, single normalization rule.

    An apostrophe (straight or curly) is removed outright, never collapsed
    to a space like other punctuation: a release filename commonly drops a
    contraction's apostrophe without adding a space ("Tony Hawk's" ->
    "Tony Hawks", real DBI-listing-vs-filename case, 2026-09-15) -- turning
    it into a space instead would split one word into two ("hawk s") and
    never match "hawks" again."""
    return _NAME_NORMALIZE_PATTERN.sub(" ", _APOSTROPHE_PATTERN.sub("", name.lower())).strip()


def atmosphere_mod_root(entry_path: str) -> Optional[str]:
    normalized = entry_path.replace("\\", "/")
    match = _ATMOSPHERE_PATTERN.search(normalized)
    return normalized[:match.end()] + "/" if match else None


def is_valid_title_id(candidate: str) -> bool:
    return bool(_PATTERN.fullmatch(candidate))


def normalize_title_id(candidate: str) -> Optional[str]:
    """Uppercases a candidate if (and only if) it's a valid TITLE_ID shape."""
    return candidate.upper() if is_valid_title_id(candidate) else None


class TitleIdGuess(NamedTuple):
    title_id: Optional[str]
    source: Optional[str]  # "filename" | "atmosphere_path" | None
    confident: bool        # True only for structurally-derived (atmosphere_path)


def from_filename(name: str) -> TitleIdGuess:
    """Low-confidence guess from a bracketed 16-hex segment in a filename.
    Never confident -- filenames are just labels, not proof."""
    matches = _BRACKET_PATTERN.findall(name)
    if len(matches) != 1:
        # Zero matches: no candidate. More than one: ambiguous, refuse to
        # pick one arbitrarily -- that would be guessing.
        return TitleIdGuess(None, None, False)
    return TitleIdGuess(matches[0].upper(), "filename", False)


def from_atmosphere_path(entry_path: str) -> TitleIdGuess:
    """High-confidence TITLE_ID from a literal
    `atmosphere/contents/<TITLE_ID>/...` path structure. Confident because
    the value comes from where DBI will actually look, not from a label."""
    normalized = entry_path.replace("\\", "/")
    match = _ATMOSPHERE_PATTERN.search(normalized)
    if not match:
        return TitleIdGuess(None, None, False)
    return TitleIdGuess(match.group(1).upper(), "atmosphere_path", True)


# ---------------------------------------------------------------------------
# Base game / update / DLC classification -- pure arithmetic on the public,
# well-documented Nintendo Switch TITLE_ID convention (the same convention
# scene-group filenames' own "[vN]" version tags already rely on):
#   base application:  low 12 bits == 0x000
#   update/patch:       low 12 bits == 0x800, same upper bits as its base
#   DLC/AddOnContent:   low 12 bits in the "DLC family" range, i.e.
#                       (id & ~0xFFF) - 0x1000 recovers the base id
# Verified against real files (Gods With Guns: base ...BC000, DLC
# ...BD001..D003 -- the formula recovers ...BC000 exactly; Rune Factory
# Guardians of Azuma: base ...0000, update ...0800, DLC ...1001..100F).
#
# Shared between switchagent/web/services.py (Library UX grouping) and
# switchagent/queue_worker.py (install-order dependency enforcement --
# see docs/STATE.md's "Base -> Update/DLC/Mod install ordering" section)
# so both consult the exact same classification, never two independently
# maintained copies of this arithmetic.
# ---------------------------------------------------------------------------

class TitleVariant(NamedTuple):
    variant: str          # "BASE" | "UPDATE" | "DLC"
    base_title_id: str    # the recovered base application's TITLE_ID


def classify_title_variant(title_id_value: str) -> TitleVariant:
    """Best-effort, not proof: a hand-crafted or non-conforming TITLE_ID
    could defeat this arithmetic. Worst case something is grouped oddly
    (Library UX, cosmetic) or a dependency check is skipped (queue_worker.py
    treats "cannot classify" as "no dependency", never as "block
    forever" -- see _dependency_status there). Never consulted by transfer
    code itself (RealMtpBackend has no notion of this)."""
    value = int(title_id_value, 16)
    low12 = value & 0xFFF
    if low12 == 0x000:
        return TitleVariant("BASE", f"{value:016X}")
    if low12 == 0x800:
        return TitleVariant("UPDATE", f"{(value & ~0xFFF):016X}")
    dlc_family_base = value & ~0xFFF
    return TitleVariant("DLC", f"{(dlc_family_base - 0x1000):016X}")
