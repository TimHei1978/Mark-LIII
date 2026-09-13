"""
Unit tests for plugins/commercial_engine.py and plugins/commercial_engine_status.py.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v

Everything here runs against a MOCKED `requests` module - no real network call,
no real Commercial Engine server needed. test_plugin_discovery.py (separate
file) covers loading the plugin through Mark-LII's REAL, unmodified
core/plugin_loader.py - that one needs no mocking either, since discovery
itself never calls run().

UPDATED (AI Content Factory task "Jarvis - gemeinsame Production Options"):
create_video_production_request no longer fires immediately for category 1/2
unless voice_preference AND subtitle_style are both already known (see
plugins/_production_draft.py) - every existing test that expects an immediate
API call now supplies both explicitly (voice_preference="auto",
subtitle_style="auto" is the least invasive choice, since "auto" behaves like
"field not mentioned" on the Commercial Engine side but is still an EXPLICIT
value here, exercising the real code path rather than sidestepping it).
_production_draft's module-level pending state is cleared in setUp() for
every test class that calls plugin.run(), so no test leaks state into another.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins import commercial_engine as plugin  # noqa: E402
from plugins import commercial_engine_status as status_plugin  # noqa: E402
from plugins import _production_draft  # noqa: E402

# Least-invasive "both fields known" pair for tests that are NOT specifically
# about the missing-parameter conversational flow - "auto" behaves like the
# pre-existing "field not mentioned" default on the Commercial Engine side.
_VOICE_AND_SUBTITLE_KNOWN = {"voice_preference": "auto", "subtitle_style": "auto"}


def _fake_response(status_code=201, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body or {}
    resp.text = text or json.dumps(json_body or {})
    return resp


class CreateVideoProductionRequestTests(unittest.TestCase):
    def setUp(self):
        _production_draft.clear_pending()

    def tearDown(self):
        _production_draft.clear_pending()

    # --- B) Plugin kann einen validen Production Request erzeugen / C) verwendet die bestehende Schnittstelle ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_minimal_request_posts_to_existing_commercial_projects_endpoint(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-1", "status": "DRAFT"})
        result = plugin.run({"product_name": "Kaffeebecher", **_VOICE_AND_SUBTITLE_KNOWN})

        self.assertIn("proj-1", result)
        self.assertEqual(mock_post.call_count, 1)
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "http://localhost:3000/api/commercial-projects")
        self.assertEqual(kwargs["json"]["productName"], "Kaffeebecher")
        self.assertEqual(kwargs["json"]["avatarMode"], "NONE")
        # No invented fields - only what was actually asked for. category is the
        # one exception: a missing category defaults to the local Wan path
        # (see plugin._DEFAULT_PRODUCTION_CATEGORY) rather than being omitted,
        # so it must never silently select the Commercial Engine's mock provider.
        self.assertEqual(kwargs["json"]["category"], plugin._DEFAULT_PRODUCTION_CATEGORY)
        self.assertNotIn("videoDurationSeconds", kwargs["json"])
        self.assertNotIn("productImageRef", kwargs["json"])
        self.assertEqual(kwargs["json"]["voicePreference"], "auto")
        self.assertEqual(kwargs["json"]["subtitleStyle"], "auto")

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_uses_existing_field_names_duration_and_aspect_ratio(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-2"})
        plugin.run({"product_name": "Teekanne", "duration_seconds": 15, "aspect_ratio": "9:16", "platform": "tiktok", **_VOICE_AND_SUBTITLE_KNOWN})
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["videoDurationSeconds"], 15.0)
        self.assertEqual(body["aspectRatio"], "9:16")
        self.assertEqual(body["platform"], "TIKTOK")

    # --- D/E/F) Category 1/2/3 koennen ausgewaehlt werden ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_1_is_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat1"})
        result = plugin.run({"product_name": "Stuhl", "category": 1, **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 1)
        self.assertIn("category 1", result)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_2_is_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat2"})
        plugin.run({"product_name": "Stuhl", "category": 2, **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 2)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_3_is_forwarded(self, mock_post, mock_thread):
        # Category 3 needs neither voice_preference nor subtitle_style (see
        # _production_draft.missing_required_fields) - deliberately NOT supplied
        # here, proving category 3 fires immediately without them.
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat3"})
        plugin.run({"product_name": "Stuhl", "category": 3})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 3)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_invalid_category_falls_back_to_default_not_forwarded_as_garbage(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-x"})
        plugin.run({"product_name": "Stuhl", "category": 7, **_VOICE_AND_SUBTITLE_KNOWN})
        # 7 is not a real category (1/2/3) - it must never be forwarded as-is;
        # it falls back to the same safe default as a missing category.
        self.assertEqual(mock_post.call_args[1]["json"]["category"], plugin._DEFAULT_PRODUCTION_CATEGORY)

    # --- G) Fehlende Category-2-Hardware (o.ae. Backend-Fehler) fuehrt zu einer klaren Fehlermeldung, kein falscher Erfolg ---

    @patch("plugins.commercial_engine.requests.post")
    def test_backend_rejection_surfaces_as_clear_error_not_fake_success(self, mock_post):
        mock_post.return_value = _fake_response(400, text='{"error":{"code":"INVALID_REQUEST_BODY"}}')
        result = plugin.run({"product_name": "Stuhl", "category": 2, **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertIn("rejected", result.lower())
        self.assertNotIn("Started a video production job", result)

    @patch("plugins.commercial_engine.requests.post")
    def test_unreachable_backend_returns_clear_message_never_raises(self, mock_post):
        import requests as _requests_module

        mock_post.side_effect = _requests_module.exceptions.ConnectionError("refused")
        result = plugin.run({"product_name": "Stuhl", "category": 2, **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertIn("unreachable", result.lower())

    # --- I) Ungueltige Dauer wird sauber abgelehnt (hier: nicht einmal gesendet) ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_non_positive_duration_is_dropped_not_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-y"})
        plugin.run({"product_name": "Stuhl", "duration_seconds": -5, **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertNotIn("videoDurationSeconds", mock_post.call_args[1]["json"])

    def test_missing_product_name_never_calls_the_api(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({})
            mock_post.assert_not_called()
            self.assertIn("product name", result.lower())

    def test_missing_product_name_does_not_create_a_pending_draft(self):
        # Nothing to remember yet - matches the pre-existing "fail loudly, no
        # invented state" principle used throughout this codebase.
        plugin.run({})
        self.assertIsNone(_production_draft.get_pending())

    # --- J) Fehlendes/ungueltiges Referenzbild wird sauber behandelt ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_no_reference_image_given_omits_the_field_entirely(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-z"})
        plugin.run({"product_name": "Stuhl", **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertNotIn("productImageRef", mock_post.call_args[1]["json"])

    def test_nonexistent_reference_image_path_is_rejected_locally_before_any_network_call(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({"product_name": "Stuhl", "reference_image": r"C:\definitely\not\a\real\file.jpg", **_VOICE_AND_SUBTITLE_KNOWN})
            mock_post.assert_not_called()
            self.assertIn("can't find", result.lower())

    def test_reference_image_with_unsupported_extension_is_rejected(self):
        real_file = Path(__file__).resolve()  # this very .py file certainly exists
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({"product_name": "Stuhl", "reference_image": str(real_file), **_VOICE_AND_SUBTITLE_KNOWN})
            mock_post.assert_not_called()
            self.assertIn("supported image", result.lower())

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_reference_image_url_is_passed_through_without_local_file_check(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-url"})
        plugin.run({"product_name": "Stuhl", "reference_image": "https://cdn.example.com/product.jpg", **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertEqual(mock_post.call_args[1]["json"]["productImageRef"], "https://cdn.example.com/product.jpg")

    # --- L) Ein interner Fehlschlag darf niemals aus run() herausgeworfen werden ---

    @patch("plugins.commercial_engine.requests.post")
    def test_run_never_raises_even_on_unexpected_internal_error(self, mock_post):
        mock_post.side_effect = RuntimeError("something truly unexpected")
        try:
            result = plugin.run({"product_name": "Stuhl", **_VOICE_AND_SUBTITLE_KNOWN})
        except Exception as e:  # pragma: no cover - the whole point of this test is that this branch is never hit
            self.fail(f"run() raised instead of returning a spoken error string: {e}")
        self.assertIn("failed", result.lower())

    # --- K) Keine Secrets im Tool-Output (hier: Backend-Fehlertext wird gekappt, nicht als Ganzes durchgereicht) ---

    @patch("plugins.commercial_engine.requests.post")
    def test_long_backend_error_body_is_truncated(self, mock_post):
        mock_post.return_value = _fake_response(400, text="x" * 5000)
        result = plugin.run({"product_name": "Stuhl", **_VOICE_AND_SUBTITLE_KNOWN})
        self.assertLess(len(result), 400)

    # --- background thread is actually started for the real production run, and targets the existing /run endpoint ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_starts_background_thread_targeting_existing_run_endpoint(self, mock_post, mock_thread_cls):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-thread"})
        plugin.run({"product_name": "Stuhl", **_VOICE_AND_SUBTITLE_KNOWN})
        mock_thread_cls.assert_called_once()
        _, kwargs = mock_thread_cls.call_args
        self.assertEqual(kwargs["args"], ("proj-thread",))
        self.assertTrue(kwargs["daemon"])

    @patch("plugins.commercial_engine.requests.post")
    def test_background_runner_posts_to_existing_run_endpoint_with_empty_body(self, mock_post):
        mock_post.return_value = _fake_response(202, {"status": "READY"})
        plugin._run_production_in_background("proj-run-1")
        # A successful run also triggers TikTok-draft delivery (_deliver_browser_draft,
        # not covered by a dedicated test here), so the run call is not
        # necessarily the only one - just confirm it happened correctly.
        mock_post.assert_any_call("http://localhost:3000/api/commercial-projects/proj-run-1/run", json={}, timeout=plugin._RUN_TIMEOUT_SECONDS)

    @patch("plugins.commercial_engine.requests.post")
    def test_background_runner_swallows_errors_without_raising(self, mock_post):
        import requests as _requests_module

        mock_post.side_effect = _requests_module.exceptions.Timeout("too slow")
        plugin._run_production_in_background("proj-run-2")  # must not raise


class MultipleReferenceImagesTests(unittest.TestCase):
    """Auftrag 'lokale PC-Jarvis-UI: Mehrfachauswahl' - multiple selected images
    must become ONE draft -> ONE production job (never one job per image), in
    deterministic (upload) order, with single-image behaviour unchanged. Uses
    this file's own path (a real file, so local-existence validation passes)
    for every fake image path, exactly like the pre-existing single-image
    tests above (test_reference_image_with_unsupported_extension_is_rejected)
    already do."""

    def setUp(self):
        _production_draft.clear_pending()
        self._real_file = str(Path(__file__).resolve())

    def tearDown(self):
        _production_draft.clear_pending()

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_multiple_images_are_forwarded_as_one_ordered_list_plus_the_first_as_singular(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-multi"})
        plugin.run({
            "product_name": "Kaffeebecher-Set",
            "reference_images": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg", "https://cdn.example.com/c.jpg"],
            **_VOICE_AND_SUBTITLE_KNOWN,
        })
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["productImageRef"], "https://cdn.example.com/a.jpg")
        self.assertEqual(
            body["productImageRefs"],
            ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg", "https://cdn.example.com/c.jpg"],
        )

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_multiple_images_still_start_exactly_one_job_not_one_per_image(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-multi-one-job"})
        result = plugin.run({
            "product_name": "Kaffeebecher-Set",
            "reference_images": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
            **_VOICE_AND_SUBTITLE_KNOWN,
        })
        mock_post.assert_called_once()
        mock_thread.assert_called_once()
        self.assertIn("proj-multi-one-job", result)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_a_single_image_via_the_singular_field_still_works_unchanged(self, mock_post, mock_thread):
        # Backward compatibility: an existing caller that only ever sets
        # reference_image (never reference_images) must keep working exactly
        # as before - it now also gets a length-1 productImageRefs, matching
        # apps/telegram-bot/src/production.ts's own always-send-both convention.
        mock_post.return_value = _fake_response(201, {"projectId": "proj-single"})
        plugin.run({"product_name": "Kaffeebecher", "reference_image": "https://cdn.example.com/only.jpg", **_VOICE_AND_SUBTITLE_KNOWN})
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["productImageRef"], "https://cdn.example.com/only.jpg")
        self.assertEqual(body["productImageRefs"], ["https://cdn.example.com/only.jpg"])

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_reference_images_takes_precedence_over_a_simultaneous_singular_reference_image(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-precedence"})
        plugin.run({
            "product_name": "Kaffeebecher",
            "reference_image": "https://cdn.example.com/ignored-singular.jpg",
            "reference_images": ["https://cdn.example.com/first.jpg", "https://cdn.example.com/second.jpg"],
            **_VOICE_AND_SUBTITLE_KNOWN,
        })
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["productImageRef"], "https://cdn.example.com/first.jpg")
        self.assertNotIn("ignored-singular.jpg", json.dumps(body))

    def test_an_invalid_path_anywhere_in_the_list_is_rejected_before_any_network_call(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({
                "product_name": "Kaffeebecher",
                "reference_images": ["https://cdn.example.com/a.jpg", r"C:\definitely\not\a\real\file.jpg"],
                **_VOICE_AND_SUBTITLE_KNOWN,
            })
            mock_post.assert_not_called()
            self.assertIn("can't find", result.lower())

    def test_empty_reference_images_list_is_treated_as_no_images_given(self):
        with patch("plugins.commercial_engine.threading.Thread"), patch("plugins.commercial_engine.requests.post") as mock_post:
            mock_post.return_value = _fake_response(201, {"projectId": "proj-empty-list"})
            plugin.run({"product_name": "Kaffeebecher", "reference_images": [], **_VOICE_AND_SUBTITLE_KNOWN})
            body = mock_post.call_args[1]["json"]
            self.assertNotIn("productImageRef", body)
            self.assertNotIn("productImageRefs", body)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_reference_images_supplied_across_two_turns_is_remembered_like_any_other_field(self, mock_post, mock_thread):
        # Mirrors test_answering_both_in_sequence_finally_starts_the_job_with_everything_remembered
        # below, but for images: given in the FIRST (incomplete) call, the draft
        # must still carry them once voice/subtitle complete it in a later call.
        mock_post.return_value = _fake_response(201, {"projectId": "proj-remembered-images"})
        plugin.run({
            "product_name": "Kaffeebecher",
            "category": 1,
            "reference_images": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
        })
        plugin.run({"voice_preference": "auto"})
        plugin.run({"subtitle_style": "auto"})
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["productImageRefs"], ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])

    def test_a_followup_turn_with_no_images_does_not_erase_previously_remembered_images(self):
        # Regression guard for a bug caught during review: PendingProductionDraft.merged_with()
        # must treat an empty images update as "nothing new to merge", never as
        # "erase the field" - an earlier draft of merged_with() would have let an
        # empty tuple silently overwrite an already-known reference_images.
        plugin.run({
            "product_name": "Kaffeebecher",
            "category": 1,
            "reference_images": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
        })
        plugin.run({"voice_preference": "female"})  # no reference_images mentioned this turn
        pending = _production_draft.get_pending()
        self.assertEqual(pending.reference_images, ("https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"))


class MissingVoiceAndSubtitlePreferenceTests(unittest.TestCase):
    """Auftrag 'Jarvis - fehlende Parameter abfragen': category 1/2 requires
    voice_preference AND subtitle_style before create_video_production_request
    is allowed to actually call the Commercial Engine API."""

    def setUp(self):
        _production_draft.clear_pending()

    def tearDown(self):
        _production_draft.clear_pending()

    @patch("plugins.commercial_engine.requests.post")
    def test_category_1_without_voice_preference_asks_for_it_and_does_not_call_the_api(self, mock_post):
        result = plugin.run({"product_name": "Massagebrille", "category": 1})
        mock_post.assert_not_called()
        self.assertIn("stimme", result.lower())

    @patch("plugins.commercial_engine.requests.post")
    def test_category_2_without_voice_preference_asks_for_it_too_same_as_category_1(self, mock_post):
        result = plugin.run({"product_name": "Massagebrille", "category": 2})
        mock_post.assert_not_called()
        self.assertIn("stimme", result.lower())

    @patch("plugins.commercial_engine.requests.post")
    def test_a_missing_question_remembers_product_name_and_category_as_a_pending_draft(self, mock_post):
        plugin.run({"product_name": "Massagebrille", "category": 1})
        pending = _production_draft.get_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.product_name, "Massagebrille")
        self.assertEqual(pending.category, 1)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_answering_voice_preference_asks_for_subtitle_style_next_not_starting_yet(self, mock_post, mock_thread):
        plugin.run({"product_name": "Massagebrille", "category": 1})
        result = plugin.run({"voice_preference": "female"})
        mock_post.assert_not_called()
        self.assertIn("clean", result.lower())

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_answering_both_in_sequence_finally_starts_the_job_with_everything_remembered(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-dialog"})
        plugin.run({"product_name": "Massagebrille", "category": 1})
        plugin.run({"voice_preference": "female"})
        result = plugin.run({"subtitle_style": "premium"})

        mock_post.assert_called_once()
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["productName"], "Massagebrille")
        self.assertEqual(body["category"], 1)
        self.assertEqual(body["voicePreference"], "female")
        self.assertEqual(body["subtitleStyle"], "premium")
        self.assertIn("proj-dialog", result)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_a_fully_complete_single_command_never_asks_anything(self, mock_post, mock_thread):
        # Auftrag Beispiel: "Kategorie 1 mit weiblicher Stimme und Premium-Untertiteln" - no follow-up question at all.
        mock_post.return_value = _fake_response(201, {"projectId": "proj-direct"})
        result = plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "female", "subtitle_style": "premium"})
        mock_post.assert_called_once()
        self.assertIn("proj-direct", result)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_partial_command_with_only_voice_asks_only_for_subtitle_style_not_repeating_voice(self, mock_post, mock_thread):
        # Auftrag Beispiel: "Kategorie 1 mit maennlicher Stimme" -> nur Subtitle-Frage.
        result = plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "male"})
        mock_post.assert_not_called()
        self.assertNotIn("stimme", result.lower())
        self.assertIn("clean", result.lower())

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_partial_command_with_only_subtitle_style_asks_only_for_voice(self, mock_post, mock_thread):
        # Auftrag Beispiel: "Kategorie 2 mit TikTok Dynamic" -> nur Voice-Frage.
        result = plugin.run({"product_name": "Massagebrille", "category": 2, "subtitle_style": "tiktok_dynamic"})
        mock_post.assert_not_called()
        self.assertIn("stimme", result.lower())

    @patch("plugins.commercial_engine.requests.post")
    def test_category_3_never_asks_for_voice_or_subtitle(self, mock_post):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat3-direct"})
        result = plugin.run({"product_name": "Massagebrille", "category": 3})
        self.assertIn("proj-cat3-direct", result)

    @patch("plugins.commercial_engine.requests.post")
    def test_a_correction_before_completion_overwrites_the_earlier_answer(self, mock_post):
        # Auftrag Beispiel: "Doch lieber maennlich" - overwrites a previously given voice_preference.
        plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "female"})
        plugin.run({"voice_preference": "male"})
        pending = _production_draft.get_pending()
        self.assertEqual(pending.voice_preference, "male")
        mock_post.assert_not_called()  # subtitle_style still missing, so nothing fired yet

    def test_unrecognized_voice_preference_value_is_ignored_not_forwarded_as_garbage(self):
        plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "divers"})
        pending = _production_draft.get_pending()
        self.assertIsNone(pending.voice_preference)

    def test_unrecognized_subtitle_style_value_is_ignored_not_forwarded_as_garbage(self):
        plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "auto", "subtitle_style": "cinematic"})
        pending = _production_draft.get_pending()
        self.assertIsNone(pending.subtitle_style)


class CancelProductionDraftTests(unittest.TestCase):
    def setUp(self):
        _production_draft.clear_pending()

    def tearDown(self):
        _production_draft.clear_pending()

    def test_cancel_clears_a_pending_draft(self):
        plugin.run({"product_name": "Massagebrille", "category": 1})
        self.assertIsNotNone(_production_draft.get_pending())

        from plugins import cancel_production_draft

        result = cancel_production_draft.run({})
        self.assertIsNone(_production_draft.get_pending())
        self.assertIn("cancel", result.lower())

    def test_cancel_with_nothing_pending_is_a_harmless_no_op(self):
        from plugins import cancel_production_draft

        result = cancel_production_draft.run({})
        self.assertIsNone(_production_draft.get_pending())
        self.assertIn("wasn't", result.lower())

    @patch("plugins.commercial_engine.requests.post")
    def test_after_cancelling_a_bare_answer_does_not_resurrect_the_cancelled_draft(self, mock_post):
        plugin.run({"product_name": "Massagebrille", "category": 1, "voice_preference": "female"})
        from plugins import cancel_production_draft

        cancel_production_draft.run({})
        # A bare "male" now (as if answering the old question) must NOT resurrect
        # the cancelled product/category - with no product_name at all, run()
        # rejects it immediately (same as any other product-name-less call) and
        # leaves no pending draft behind either.
        result = plugin.run({"voice_preference": "male"})
        self.assertIn("product name", result.lower())
        self.assertIsNone(_production_draft.get_pending())
        mock_post.assert_not_called()


class BaseUrlConfigTests(unittest.TestCase):
    def test_default_base_url(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(plugin._base_url(), "http://localhost:3000")

    def test_base_url_overridable_via_single_env_var(self):
        with patch.dict("os.environ", {"ACF_API_BASE_URL": "http://localhost:4000/"}):
            self.assertEqual(plugin._base_url(), "http://localhost:4000")


class CheckVideoProductionStatusTests(unittest.TestCase):
    @patch("plugins.commercial_engine_status.requests.get")
    def test_ready_status_reports_final_output_path(self, mock_get):
        mock_get.return_value = _fake_response(
            200, {"status": "READY", "currentStage": "DONE", "retryable": False, "finalOutput": {"path": "assets/video/final/x.mp4"}}
        )
        result = status_plugin.run({"project_id": "proj-1"})
        self.assertIn("done", result.lower())
        self.assertIn("assets/video/final/x.mp4", result)

    @patch("plugins.commercial_engine_status.requests.get")
    def test_in_progress_status_names_the_stage_not_a_fake_percentage(self, mock_get):
        mock_get.return_value = _fake_response(200, {"status": "GENERATING", "currentStage": "GENERATION", "retryable": False, "finalOutput": None})
        result = status_plugin.run({"project_id": "proj-1"})
        self.assertIn("generating the video", result.lower())
        self.assertNotIn("%", result)

    @patch("plugins.commercial_engine_status.requests.get")
    def test_retryable_failure_is_named_as_such(self, mock_get):
        mock_get.return_value = _fake_response(200, {"status": "GENERATION_FAILED", "currentStage": "GENERATION", "retryable": True, "finalOutput": None})
        result = status_plugin.run({"project_id": "proj-1"})
        self.assertIn("retry", result.lower())

    @patch("plugins.commercial_engine_status.requests.get")
    def test_unknown_project_id_returns_404_as_clear_message(self, mock_get):
        mock_get.return_value = _fake_response(404, text='{"error":{"code":"COMMERCIAL_PROJECT_NOT_FOUND"}}')
        result = status_plugin.run({"project_id": "does-not-exist"})
        self.assertIn("don't know", result.lower())

    def test_missing_project_id_never_calls_the_api(self):
        with patch("plugins.commercial_engine_status.requests.get") as mock_get:
            result = status_plugin.run({})
            mock_get.assert_not_called()
            self.assertIn("project id", result.lower())

    @patch("plugins.commercial_engine_status.requests.get")
    def test_unreachable_backend_returns_clear_message_never_raises(self, mock_get):
        import requests as _requests_module

        mock_get.side_effect = _requests_module.exceptions.ConnectionError("refused")
        result = status_plugin.run({"project_id": "proj-1"})
        self.assertIn("unreachable", result.lower())


class N8nWebhookPathTests(unittest.TestCase):
    """OPTIONAL GEMINI CREATIVE LAYER / n8n PATH (see module docstring) - ACF_N8N_WEBHOOK_URL
    unset in every OTHER test class above, so those already cover 'default behaviour unchanged'."""

    def setUp(self):
        _production_draft.clear_pending()

    def tearDown(self):
        _production_draft.clear_pending()

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "http://localhost:5678/webhook/production-request"})
    def test_n8n_url_set_posts_to_webhook_not_the_direct_endpoint(self, mock_post, mock_thread):
        result = plugin.run({"product_name": "Kaffeebecher", **_VOICE_AND_SUBTITLE_KNOWN})
        mock_post.assert_not_called()  # direct requests.post is never called on this path directly...
        self.assertEqual(mock_thread.call_count, 1)  # ...it happens inside the backgrounded _run_via_n8n instead
        _, kwargs = mock_thread.call_args
        self.assertEqual(kwargs["target"], plugin._run_via_n8n)
        self.assertIn("Kaffeebecher", result)
        self.assertNotIn("Project ID", result)  # honest: no ID available yet on this path (see KNOWN LIMITATION)

    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "http://localhost:5678/webhook/production-request"})
    def test_run_via_n8n_posts_userRequest_and_productName_to_the_webhook_url(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            mock_post.return_value = _fake_response(200, {"status": "READY"})
            plugin._run_via_n8n("http://localhost:5678/webhook/production-request", "Kaffeebecher", {"product_name": "Kaffeebecher"}, None)
            url = mock_post.call_args[0][0]
            body = mock_post.call_args[1]["json"]
            self.assertEqual(url, "http://localhost:5678/webhook/production-request")
            self.assertEqual(body["productName"], "Kaffeebecher")
            self.assertIn("Kaffeebecher", body["userRequest"])
            self.assertNotIn("productImageRef", body)

    def test_build_user_request_text_includes_creative_notes_verbatim(self):
        text = plugin._build_user_request_text(
            "Kaffeebecher",
            {
                "product_description": "Doppelwandiger Edelstahlbecher",
                "creative_notes": "warme Weihnachtsstimmung, goldene Lichter, keine Menschen",
                "duration_seconds": 15,
                "platform": "tiktok",
                "category": 2,
            },
        )
        self.assertIn("Kaffeebecher", text)
        self.assertIn("Doppelwandiger Edelstahlbecher", text)
        self.assertIn("warme Weihnachtsstimmung, goldene Lichter, keine Menschen", text)
        self.assertIn("15", text)
        self.assertIn("tiktok", text)
        self.assertIn("category 2", text.lower())

    def test_build_user_request_text_never_invents_creative_notes(self):
        text = plugin._build_user_request_text("Kaffeebecher", {"product_name": "Kaffeebecher"})
        self.assertEqual(text, "Kaffeebecher")

    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "http://localhost:5678/webhook/production-request"})
    def test_run_via_n8n_includes_validated_productImageRef_when_given(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            mock_post.return_value = _fake_response(200, {"status": "READY"})
            plugin._run_via_n8n("http://localhost:5678/webhook/production-request", "Kaffeebecher", {}, "C:/tmp/product.jpg")
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["productImageRef"], "C:/tmp/product.jpg")

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "http://localhost:5678/webhook/production-request"})
    def test_invalid_reference_image_is_still_rejected_locally_before_the_n8n_path_even_starts(self, mock_post, mock_thread):
        result = plugin.run({"product_name": "Kaffeebecher", "reference_image": "C:/does/not/exist.jpg", **_VOICE_AND_SUBTITLE_KNOWN})
        mock_thread.assert_not_called()
        self.assertIn("can't find a file", result)

    def test_n8n_webhook_url_helper_returns_none_when_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ACF_N8N_WEBHOOK_URL", None)
            self.assertIsNone(plugin._n8n_webhook_url())

    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "  "})
    def test_n8n_webhook_url_helper_treats_whitespace_as_unset(self):
        self.assertIsNone(plugin._n8n_webhook_url())


if __name__ == "__main__":
    unittest.main()
