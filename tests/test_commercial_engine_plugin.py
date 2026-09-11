"""
Unit tests for plugins/commercial_engine.py and plugins/commercial_engine_status.py.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v

Everything here runs against a MOCKED `requests` module - no real network call,
no real Commercial Engine server needed. test_plugin_discovery.py (separate
file) covers loading the plugin through Mark-LII's REAL, unmodified
core/plugin_loader.py - that one needs no mocking either, since discovery
itself never calls run().
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


def _fake_response(status_code=201, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body or {}
    resp.text = text or json.dumps(json_body or {})
    return resp


class CreateVideoProductionRequestTests(unittest.TestCase):
    # --- B) Plugin kann einen validen Production Request erzeugen / C) verwendet die bestehende Schnittstelle ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_minimal_request_posts_to_existing_commercial_projects_endpoint(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-1", "status": "DRAFT"})
        result = plugin.run({"product_name": "Kaffeebecher"})

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

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_uses_existing_field_names_duration_and_aspect_ratio(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-2"})
        plugin.run({"product_name": "Teekanne", "duration_seconds": 15, "aspect_ratio": "9:16", "platform": "tiktok"})
        body = mock_post.call_args[1]["json"]
        self.assertEqual(body["videoDurationSeconds"], 15.0)
        self.assertEqual(body["aspectRatio"], "9:16")
        self.assertEqual(body["platform"], "TIKTOK")

    # --- D/E/F) Category 1/2/3 koennen ausgewaehlt werden ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_1_is_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat1"})
        result = plugin.run({"product_name": "Stuhl", "category": 1})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 1)
        self.assertIn("category 1", result)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_2_is_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat2"})
        plugin.run({"product_name": "Stuhl", "category": 2})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 2)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_category_3_is_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-cat3"})
        plugin.run({"product_name": "Stuhl", "category": 3})
        self.assertEqual(mock_post.call_args[1]["json"]["category"], 3)

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_invalid_category_falls_back_to_default_not_forwarded_as_garbage(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-x"})
        plugin.run({"product_name": "Stuhl", "category": 7})
        # 7 is not a real category (1/2/3) - it must never be forwarded as-is;
        # it falls back to the same safe default as a missing category.
        self.assertEqual(mock_post.call_args[1]["json"]["category"], plugin._DEFAULT_PRODUCTION_CATEGORY)

    # --- G) Fehlende Category-2-Hardware (o.ae. Backend-Fehler) fuehrt zu einer klaren Fehlermeldung, kein falscher Erfolg ---

    @patch("plugins.commercial_engine.requests.post")
    def test_backend_rejection_surfaces_as_clear_error_not_fake_success(self, mock_post):
        mock_post.return_value = _fake_response(400, text='{"error":{"code":"INVALID_REQUEST_BODY"}}')
        result = plugin.run({"product_name": "Stuhl", "category": 2})
        self.assertIn("rejected", result.lower())
        self.assertNotIn("Started a video production job", result)

    @patch("plugins.commercial_engine.requests.post")
    def test_unreachable_backend_returns_clear_message_never_raises(self, mock_post):
        import requests as _requests_module

        mock_post.side_effect = _requests_module.exceptions.ConnectionError("refused")
        result = plugin.run({"product_name": "Stuhl", "category": 2})
        self.assertIn("unreachable", result.lower())

    # --- I) Ungueltige Dauer wird sauber abgelehnt (hier: nicht einmal gesendet) ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_non_positive_duration_is_dropped_not_forwarded(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-y"})
        plugin.run({"product_name": "Stuhl", "duration_seconds": -5})
        self.assertNotIn("videoDurationSeconds", mock_post.call_args[1]["json"])

    def test_missing_product_name_never_calls_the_api(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({})
            mock_post.assert_not_called()
            self.assertIn("product name", result.lower())

    # --- J) Fehlendes/ungueltiges Referenzbild wird sauber behandelt ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_no_reference_image_given_omits_the_field_entirely(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-z"})
        plugin.run({"product_name": "Stuhl"})
        self.assertNotIn("productImageRef", mock_post.call_args[1]["json"])

    def test_nonexistent_reference_image_path_is_rejected_locally_before_any_network_call(self):
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({"product_name": "Stuhl", "reference_image": r"C:\definitely\not\a\real\file.jpg"})
            mock_post.assert_not_called()
            self.assertIn("can't find", result.lower())

    def test_reference_image_with_unsupported_extension_is_rejected(self):
        real_file = Path(__file__).resolve()  # this very .py file certainly exists
        with patch("plugins.commercial_engine.requests.post") as mock_post:
            result = plugin.run({"product_name": "Stuhl", "reference_image": str(real_file)})
            mock_post.assert_not_called()
            self.assertIn("supported image", result.lower())

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_reference_image_url_is_passed_through_without_local_file_check(self, mock_post, mock_thread):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-url"})
        plugin.run({"product_name": "Stuhl", "reference_image": "https://cdn.example.com/product.jpg"})
        self.assertEqual(mock_post.call_args[1]["json"]["productImageRef"], "https://cdn.example.com/product.jpg")

    # --- L) Ein interner Fehlschlag darf niemals aus run() herausgeworfen werden ---

    @patch("plugins.commercial_engine.requests.post")
    def test_run_never_raises_even_on_unexpected_internal_error(self, mock_post):
        mock_post.side_effect = RuntimeError("something truly unexpected")
        try:
            result = plugin.run({"product_name": "Stuhl"})
        except Exception as e:  # pragma: no cover - the whole point of this test is that this branch is never hit
            self.fail(f"run() raised instead of returning a spoken error string: {e}")
        self.assertIn("failed", result.lower())

    # --- K) Keine Secrets im Tool-Output (hier: Backend-Fehlertext wird gekappt, nicht als Ganzes durchgereicht) ---

    @patch("plugins.commercial_engine.requests.post")
    def test_long_backend_error_body_is_truncated(self, mock_post):
        mock_post.return_value = _fake_response(400, text="x" * 5000)
        result = plugin.run({"product_name": "Stuhl"})
        self.assertLess(len(result), 400)

    # --- background thread is actually started for the real production run, and targets the existing /run endpoint ---

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    def test_starts_background_thread_targeting_existing_run_endpoint(self, mock_post, mock_thread_cls):
        mock_post.return_value = _fake_response(201, {"projectId": "proj-thread"})
        plugin.run({"product_name": "Stuhl"})
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

    @patch("plugins.commercial_engine.threading.Thread")
    @patch("plugins.commercial_engine.requests.post")
    @patch.dict("os.environ", {"ACF_N8N_WEBHOOK_URL": "http://localhost:5678/webhook/production-request"})
    def test_n8n_url_set_posts_to_webhook_not_the_direct_endpoint(self, mock_post, mock_thread):
        result = plugin.run({"product_name": "Kaffeebecher"})
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
        result = plugin.run({"product_name": "Kaffeebecher", "reference_image": "C:/does/not/exist.jpg"})
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
