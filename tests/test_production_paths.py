"""
Unit tests for plugins/_production_paths.py (Auftrag "HARDENING-RUNDE",
Prioritaet A/B/D). Pure functions, no mocking, no network - the whole point
of this module is that path resolution is deterministic and testable without
a live Gemini call (see module docstring).

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins._production_paths import resolve_production_path  # noqa: E402


class NumericResolutionTests(unittest.TestCase):
    def test_display_number_1_resolves_to_local_composition_legacy_1(self):
        r = resolve_production_path(display_number=1)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "LOCAL_COMPOSITION")
        self.assertEqual(r.match.legacy_category, 1)

    def test_display_number_2_resolves_to_open_generative_ai_legacy_4(self):
        # THE critical case: displayNumber 2 != legacyCategory 2 (that's Local
        # Story/WAN). Getting this backwards would silently misroute every
        # "Produktionsweg 2" request to the wrong engine.
        r = resolve_production_path(display_number=2)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "OPEN_GENERATIVE_AI")
        self.assertEqual(r.match.legacy_category, 4)

    def test_display_number_3_resolves_to_local_story_wan_legacy_2(self):
        r = resolve_production_path(display_number=3)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "LOCAL_STORY_WAN")
        self.assertEqual(r.match.legacy_category, 2)

    def test_display_number_4_resolves_to_heygen_legacy_5(self):
        r = resolve_production_path(display_number=4)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HEYGEN")
        self.assertEqual(r.match.legacy_category, 5)

    def test_display_number_5_resolves_to_higgsfield_legacy_3(self):
        r = resolve_production_path(display_number=5)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HIGGSFIELD")
        self.assertEqual(r.match.legacy_category, 3)

    def test_display_number_6_resolves_to_clipping_legacy_6(self):
        r = resolve_production_path(display_number=6)
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "CLIPPING")
        self.assertEqual(r.match.legacy_category, 6)

    def test_unknown_numeric_path_never_silently_resolves(self):
        r = resolve_production_path(display_number=9)
        self.assertFalse(r.resolved)
        self.assertIsNone(r.match)
        self.assertIsInstance(r.question, str)
        self.assertIn("9", r.question)

    def test_zero_never_silently_resolves(self):
        r = resolve_production_path(display_number=0)
        self.assertFalse(r.resolved)


class NameResolutionTests(unittest.TestCase):
    def test_path_1_by_name(self):
        r = resolve_production_path(name="local composition")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "LOCAL_COMPOSITION")

    def test_path_2_open_generative_ai(self):
        r = resolve_production_path(name="Open Generative AI")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "OPEN_GENERATIVE_AI")
        self.assertEqual(r.match.legacy_category, 4)

    def test_path_2_h3_alias(self):
        r = resolve_production_path(name="H3")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "OPEN_GENERATIVE_AI")

    def test_path_2_ref2va_alias(self):
        r = resolve_production_path(name="ref2va")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "OPEN_GENERATIVE_AI")

    def test_path_3_wan_alias(self):
        r = resolve_production_path(name="WAN")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "LOCAL_STORY_WAN")
        self.assertEqual(r.match.legacy_category, 2)

    def test_path_3_local_story_alias(self):
        r = resolve_production_path(name="local story")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "LOCAL_STORY_WAN")

    def test_path_4_heygen(self):
        r = resolve_production_path(name="HeyGen")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HEYGEN")
        self.assertEqual(r.match.legacy_category, 5)

    def test_path_4_avatar_presenter_alias(self):
        r = resolve_production_path(name="avatar presenter")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HEYGEN")

    def test_path_5_higgsfield(self):
        r = resolve_production_path(name="Higgsfield")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HIGGSFIELD")
        self.assertEqual(r.match.legacy_category, 3)

    def test_path_6_clipping(self):
        r = resolve_production_path(name="Clipping")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "CLIPPING")

    def test_path_6_video_clipping_phrase(self):
        r = resolve_production_path(name="nutze video clipping")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "CLIPPING")

    def test_path_6_shorts_alias(self):
        r = resolve_production_path(name="shorts aus video")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "CLIPPING")

    def test_matching_is_case_and_whitespace_insensitive(self):
        r = resolve_production_path(name="   HeYgEn   presenter  ")
        self.assertTrue(r.resolved)
        self.assertEqual(r.match.id, "HEYGEN")


class NoSilentFallbackTests(unittest.TestCase):
    """Auftrag Phase 4: an unknown or ambiguous phrase must ALWAYS come back
    as a question, never a guessed/defaulted match."""

    def test_unknown_name_returns_question_not_a_guess(self):
        r = resolve_production_path(name="OpenAI-Avatar-Dings")
        self.assertFalse(r.resolved)
        self.assertIsNone(r.match)
        self.assertIsInstance(r.question, str)
        self.assertGreater(len(r.question), 0)

    def test_completely_unrelated_text_returns_question(self):
        r = resolve_production_path(name="Wetterbericht fuer morgen")
        self.assertFalse(r.resolved)

    def test_phrase_naming_two_real_paths_is_ambiguous_not_guessed(self):
        r = resolve_production_path(name="local composition or wan")
        self.assertFalse(r.resolved)
        self.assertIsNone(r.match)
        self.assertIn("Local Composition", r.question)
        self.assertIn("Local Story / WAN", r.question)

    def test_nothing_given_returns_none_not_a_question(self):
        # Genuinely nothing specified is NOT the same as an unresolvable
        # value - callers keep their own existing sensible default for this
        # case (see commercial_engine.py's _DEFAULT_PRODUCTION_CATEGORY).
        self.assertIsNone(resolve_production_path())
        self.assertIsNone(resolve_production_path(name=""))
        self.assertIsNone(resolve_production_path(name="   "))


if __name__ == "__main__":
    unittest.main()
