"""
_production_draft.py - shared, in-process pending-production-draft state for
create_video_production_request (commercial_engine.py) and
cancel_production_draft.py.

Underscore-prefixed on purpose - core/plugin_loader.py's discover_plugins()
skips underscore-prefixed files (same convention as _template.py), so this
is a private helper module, never itself registered as a Gemini tool.

WHY THIS EXISTS (see AI Content Factory task "Jarvis - fehlende Parameter
abfragen"): create_video_production_request previously fired immediately with
whatever single-call parameters Gemini's function-calling extracted - no
mechanism existed to ask "which voice, which subtitle style?" across turns and
remember the answer (confirmed by reading the previous version of run() and by
test_missing_product_name_never_calls_the_api, which only ever exercised the
single-call path). Cross-turn continuity for WHAT WAS ALREADY SAID is Gemini
Live's own conversational memory (it already sees the whole conversation) -
this module is a small, explicit SAFETY NET on Mark-LII's own side, mirroring
core/confirm.py's existing `_pending: Optional[_Pending]` module-level-state
pattern (same file, same technique, not a new architecture): even if Gemini's
retry call omits a field it should remember, this module still has it.

Explicitly NOT a new state engine (Auftrag: "Keine neue grosse State Engine"):
one small dataclass, three module-level functions, no persistence across
process restarts (a restarted Mark-LII simply has no pending draft, same as
a cancelled one - acceptable, matches core/confirm.py's own volatile design).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

# Category 1/2 share the real Commercial Engine voice + subtitle pipeline
# (see AI Content Factory COMPONENTS/telegram-production-bot.md, "OmniVoice-
# Integration & Kategorie-2-Korrektur") - both fields are only REQUIRED when
# category is 1 or 2. Category 3 (Higgsfield) is explicitly out of scope for
# this change (Auftrag: "Category 3 nicht ungeprueft aendern").
_CATEGORIES_REQUIRING_VOICE_AND_SUBTITLE = (1, 2)


@dataclass(frozen=True)
class PendingProductionDraft:
    product_name: Optional[str] = None
    product_description: Optional[str] = None
    creative_notes: Optional[str] = None
    reference_image: Optional[str] = None
    # Deterministically-ordered multi-image selection (see Mark-LII/ui.py's
    # FileDropZone.current_files() and main.py's tool-dispatch auto-fill) -
    # additive to reference_image, mirroring apps/api's own productImageRef/
    # productImageRefs convention (apps/telegram-bot/src/production.ts uses the
    # exact same "singular = first, plural = all, in upload order" shape) so
    # multiple selected images become ONE draft -> ONE production job, not one
    # job per image. A tuple (not list) so the frozen dataclass stays hashable
    # and merged_with()'s "falsy means no update" check below stays correct.
    reference_images: Optional[tuple[str, ...]] = None
    duration_seconds: Optional[float] = None
    category: Optional[int] = None
    aspect_ratio: Optional[str] = None
    platform: Optional[str] = None
    voice_preference: Optional[str] = None  # "male" | "female" | "auto"
    subtitle_style: Optional[str] = None  # "clean" | "tiktok_dynamic" | "premium" | "auto"

    def merged_with(self, **updates: object) -> "PendingProductionDraft":
        """Returns a NEW draft with only the non-empty supplied fields overlaid -
        never erases an already-known field with an absent/None one (a partial
        follow-up call must not forget what was already established)."""
        non_empty = {k: v for k, v in updates.items() if v is not None and v != "" and v != ()}
        return replace(self, **non_empty)

    def missing_required_fields(self) -> list[str]:
        """Auftrag Abschnitt 31-35: nur tatsaechlich fehlende Pflichtfelder werden
        erfragt, nie bereits bekannte erneut. category selbst wird hier NICHT als
        fehlend gefuehrt - der bestehende Default (siehe commercial_engine.py,
        _DEFAULT_PRODUCTION_CATEGORY) bleibt unveraendert, das war schon vor diesem
        Auftrag so und ist nicht Teil dieser Aenderung."""
        if self.category not in _CATEGORIES_REQUIRING_VOICE_AND_SUBTITLE:
            return []
        missing = []
        if not self.voice_preference:
            missing.append("voice_preference")
        if not self.subtitle_style:
            missing.append("subtitle_style")
        return missing


_pending: Optional[PendingProductionDraft] = None


def get_pending() -> Optional[PendingProductionDraft]:
    return _pending


def set_pending(draft: Optional[PendingProductionDraft]) -> None:
    global _pending
    _pending = draft


def clear_pending() -> None:
    global _pending
    _pending = None
