"""
_production_paths.py - canonical Production Path (1-6) resolution for
Jarvis/Mark-LII (Auftrag "HARDENING-RUNDE", Prioritaet A: "Jarvis muss
Production Paths 1-6 korrekt und namentlich verstehen").

Mirrors (documented, deliberate duplication - same convention as AI Content
Factory's own apps/telegram-bot/src/menu.ts PRODUCTION_PATHS array, whose
header comment documents the identical "no cross-repo dependency" principle)
the SINGLE canonical registry at
packages/commercial-engine/src/types/productionPath.ts. Mark-LII has no
dependency on that TypeScript package (separate repo, separate runtime), so
this is a small, explicit Python mirror - values below must be kept in sync
by hand if that file ever changes (same already-accepted risk documented for
apps/telegram-bot's own mirror; packages/database/src/types.ts's own
"muss manuell synchron gehalten werden" comment documents the same pattern a
third time in that repo).

CRITICAL DISTINCTION (the reason this module exists as its own file, not
just a dict inline in commercial_engine.py - getting this wrong would
silently misroute a production):

- displayNumber (1-6): the NEW, user-facing number ("Produktionsweg 2"),
  what a human actually says out loud. THIS is what Jarvis's NLU/the user
  means by "1 = Local Composition, 2 = Open Generative AI, 3 = Local
  Story/WAN, 4 = HeyGen, 5 = Higgsfield, 6 = Clipping".
- legacyCategory: the OLD, already-persisted numbering apps/api's
  POST /api/commercial-projects actually reads as its `category` JSON
  field (see apps/api/src/commercialEngine.ts's ProductionCategory).
  The two numberings are DIFFERENT for four of the six paths:

    displayNumber | id                  | legacyCategory
    1             | LOCAL_COMPOSITION   | 1
    2             | OPEN_GENERATIVE_AI  | 4
    3             | LOCAL_STORY_WAN     | 2
    4             | HEYGEN              | 5
    5             | HIGGSFIELD          | 3
    6             | CLIPPING            | 6 (never sent this way - see below)

  Sending a raw displayNumber straight through as `category` would silently
  send "Produktionsweg 2" (Open Generative AI) to the Local Story/WAN
  provider instead (legacyCategory 2) - exactly the kind of silent
  misrouting this hardening round exists to close. This module's whole job
  is translating a user-facing selection to the correct legacyCategory
  before anything is ever sent to the API.

Path 6 (Clipping) is not a generative product-video path - it already has
its own, separate Jarvis plugins (clipping_command.py/clipping_status.py)
and its own separate AI Content Factory API (POST /api/clipping/commands).
A resolved CLIPPING selection here must therefore never be forwarded as
`category` to POST /api/commercial-projects (that call hard-fails with a
typed UNSUPPORTED_CAPABILITY error, see apps/api/src/commercialEngine.ts) -
callers of resolve_production_path() must check for id == "CLIPPING" and
redirect the user to the clipping plugin instead of proceeding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# (id, displayNumber, legacyCategory, label) - order = displayNumber, matches
# productionPath.ts's own PRODUCTION_PATHS array order.
_PATHS: tuple[tuple[str, int, int, str], ...] = (
    ("LOCAL_COMPOSITION", 1, 1, "Local Composition"),
    ("OPEN_GENERATIVE_AI", 2, 4, "Open Generative AI"),
    ("LOCAL_STORY_WAN", 3, 2, "Local Story / WAN"),
    ("HEYGEN", 4, 5, "HeyGen"),
    ("HIGGSFIELD", 5, 3, "Higgsfield"),
    ("CLIPPING", 6, 6, "Clipping"),
)

_BY_DISPLAY_NUMBER = {p[1]: p for p in _PATHS}
_BY_ID = {p[0]: p for p in _PATHS}
_BY_LEGACY_CATEGORY = {p[2]: p for p in _PATHS}

# Alias phrases (lowercase, matched as a normalized substring - see
# _normalize()) - Auftrag Phase 5's own examples, deliberately NOT
# aggressive (no bare single generic word like "video" or "ai" that could
# match more than one path and force a spurious ambiguous-result).
_ALIASES: tuple[tuple[str, str], ...] = (
    ("LOCAL_COMPOSITION", "local composition"),
    ("LOCAL_COMPOSITION", "lokale composition"),
    ("LOCAL_COMPOSITION", "lokales werbevideo"),
    ("OPEN_GENERATIVE_AI", "open generative ai"),
    ("OPEN_GENERATIVE_AI", "open generative"),
    ("OPEN_GENERATIVE_AI", "h3"),
    ("OPEN_GENERATIVE_AI", "ref2va"),
    ("LOCAL_STORY_WAN", "local story"),
    ("LOCAL_STORY_WAN", "local wan"),
    ("LOCAL_STORY_WAN", "wan story"),
    ("LOCAL_STORY_WAN", "wan"),
    ("HEYGEN", "heygen presenter"),
    ("HEYGEN", "avatar presenter"),
    ("HEYGEN", "heygen"),
    ("HIGGSFIELD", "higgsfield"),
    ("CLIPPING", "video clipping"),
    ("CLIPPING", "shorts aus video"),
    ("CLIPPING", "clipping"),
    ("CLIPPING", "clips"),
)
# Longest-alias-first: when a longer phrase ("heygen presenter") and a
# shorter one it contains ("heygen") both map to the SAME path id, the order
# doesn't change the result (duplicate ids are deduped below) - this is only
# to keep the (unlikely) case of a future overlapping alias pair between two
# DIFFERENT ids resolving to the more specific one deterministically.
_ALIASES = tuple(sorted(_ALIASES, key=lambda pair: -len(pair[1])))

_KNOWN_PATHS_HINT = (
    "1 Local Composition, 2 Open Generative AI, 3 Local Story/WAN, 4 HeyGen, "
    "5 Higgsfield, 6 Clipping"
)


@dataclass(frozen=True)
class ProductionPathMatch:
    id: str
    display_number: int
    legacy_category: int
    label: str


@dataclass(frozen=True)
class ProductionPathResolution:
    """Exactly one of `match` / `question` is ever set - never both, never
    neither. Auftrag Phase 4 ("kein stiller Fallback"): an unresolved or
    ambiguous input always comes back as an explicit, spoken-friendly
    question, never a guessed/defaulted match."""

    match: Optional[ProductionPathMatch] = None
    question: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.match is not None


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def resolve_production_path(
    *, display_number: Optional[int] = None, name: Optional[str] = None
) -> Optional[ProductionPathResolution]:
    """Resolves a user-facing display number (1-6) OR a free-text name/alias
    to exactly one canonical production path - or a structured, never-silent
    question if it can't. Returns None only when NEITHER argument was given
    at all (the caller decides what "nothing specified" means - this module
    never invents that decision; only an actually-given, unresolvable value
    must produce a question, per Auftrag Phase 4)."""
    if display_number is not None:
        found = _BY_DISPLAY_NUMBER.get(int(display_number))
        if found:
            return ProductionPathResolution(match=ProductionPathMatch(*found))
        return ProductionPathResolution(
            question=(
                f"Es gibt keinen Produktionsweg {display_number}. Bitte nenne einen Weg von 1 bis 6 "
                f"({_KNOWN_PATHS_HINT}) oder den Namen des Wegs."
            )
        )

    if name is not None and name.strip():
        normalized = _normalize(name)
        matched_ids: list[str] = []
        for path_id, alias in _ALIASES:
            if alias in normalized and path_id not in matched_ids:
                matched_ids.append(path_id)

        if len(matched_ids) == 1:
            return ProductionPathResolution(match=ProductionPathMatch(*_BY_ID[matched_ids[0]]))
        if len(matched_ids) > 1:
            names = ", ".join(_BY_ID[pid][3] for pid in matched_ids)
            return ProductionPathResolution(
                question=f"'{name}' ist mehrdeutig - meinst du {names}? Bitte nenne den Produktionsweg genauer (Name oder Nummer 1-6)."
            )
        return ProductionPathResolution(
            question=(
                f"Ich kenne keinen Produktionsweg namens '{name}'. Bitte nenne einen Weg von 1 bis 6 "
                f"({_KNOWN_PATHS_HINT}) oder einen bekannten Namen (z. B. HeyGen, Higgsfield, WAN, Clipping)."
            )
        )

    return None


def label_for_legacy_category(legacy_category: int) -> Optional[str]:
    """Display helper for result text - e.g. 'Open Generative AI' for legacy
    category 4. Returns None for an unrecognized value (callers should never
    pass one, but this stays a safe lookup, not an assertion)."""
    found = _BY_LEGACY_CATEGORY.get(legacy_category)
    return found[3] if found else None
