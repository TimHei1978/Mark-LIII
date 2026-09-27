"""
Unit tests for plugins/clipping_command.py and plugins/clipping_status.py.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v

Everything here runs against a MOCKED `requests` module - no real network call,
no real Commercial Engine server needed. Same mocking style as
test_commercial_engine_plugin.py.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins import clipping_command  # noqa: E402
from plugins import clipping_status  # noqa: E402


def _fake_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body or {}
    resp.text = text or json.dumps(json_body or {})
    return resp


_SOURCE = {"id": "src-1", "friendlyName": "podcast-folge-12.mp4", "status": "WAITING_FOR_MODE", "durationMs": 600000}


class StartClippingJobTests(unittest.TestCase):
    @patch("plugins.clipping_command.requests.get")
    @patch("plugins.clipping_command.requests.post")
    def test_auto_mode_resolves_source_and_starts_job(self, mock_post, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        mock_post.return_value = _fake_response(200, {"source": {**_SOURCE, "status": "ANALYZING"}, "mode": "AUTO"})

        result = clipping_command.run({"mode": "AUTO", "max_clips": 5})

        self.assertIn("podcast-folge-12.mp4", result)
        sent_body = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_body["sourceId"], "src-1")
        self.assertEqual(sent_body["mode"], "AUTO")
        self.assertEqual(sent_body["maxClips"], 5)
        self.assertEqual(sent_body["requestedBy"], "JARVIS")

    @patch("plugins.clipping_command.requests.get")
    def test_ambiguous_source_returns_clarifying_question_without_calling_post(self, mock_get):
        mock_get.return_value = _fake_response(409, {"outcome": "AMBIGUOUS", "candidates": [{"friendlyName": "Folge 12 final.mp4"}, {"friendlyName": "Folge 12 kurz.mp4"}]})

        with patch("plugins.clipping_command.requests.post") as mock_post:
            result = clipping_command.run({"mode": "AUTO", "source_query": "Folge 12"})
            mock_post.assert_not_called()

        self.assertIn("more than one", result)
        self.assertIn("Folge 12 final.mp4", result)
        self.assertIn("Folge 12 kurz.mp4", result)

    @patch("plugins.clipping_command.requests.get")
    def test_source_not_found_returns_clear_message(self, mock_get):
        mock_get.return_value = _fake_response(404)
        result = clipping_command.run({"mode": "AUTO"})
        self.assertIn("couldn't find", result)

    @patch("plugins.clipping_command.requests.get")
    def test_keyword_mode_without_keywords_asks_instead_of_calling_post(self, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        with patch("plugins.clipping_command.requests.post") as mock_post:
            result = clipping_command.run({"mode": "KEYWORD"})
            mock_post.assert_not_called()
        self.assertIn("keywords", result.lower())

    @patch("plugins.clipping_command.requests.get")
    @patch("plugins.clipping_command.requests.post")
    def test_keyword_mode_forwards_keywords_list(self, mock_post, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        mock_post.return_value = _fake_response(200, {"source": _SOURCE, "mode": "KEYWORD"})

        clipping_command.run({"mode": "KEYWORD", "keywords": ["Zinsen", "Eigenkapital"]})

        sent_body = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_body["keywords"], ["Zinsen", "Eigenkapital"])

    @patch("plugins.clipping_command.requests.get")
    @patch("plugins.clipping_command.requests.post")
    def test_instruction_mode_forwards_text_close_to_verbatim(self, mock_post, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        mock_post.return_value = _fake_response(200, {"source": _SOURCE, "mode": "INSTRUCTION"})

        clipping_command.run({"mode": "INSTRUCTION", "instruction_text": "Why did the first company fail?"})

        sent_body = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_body["instruction"], "Why did the first company fail?")

    @patch("plugins.clipping_command.requests.get")
    @patch("plugins.clipping_command.requests.post")
    def test_manual_mode_converts_seconds_to_milliseconds(self, mock_post, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        mock_post.return_value = _fake_response(200, {"source": _SOURCE, "mode": "MANUAL"})

        clipping_command.run({"mode": "MANUAL", "manual_range_start_seconds": 750, "manual_range_end_seconds": 795})

        sent_body = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_body["manualRange"], {"startMs": 750000, "endMs": 795000})

    def test_invalid_mode_rejected_without_any_network_call(self):
        with patch("plugins.clipping_command.requests.get") as mock_get, patch("plugins.clipping_command.requests.post") as mock_post:
            result = clipping_command.run({"mode": "NOT_A_MODE"})
            mock_get.assert_not_called()
            mock_post.assert_not_called()
        self.assertIn("valid mode", result)

    @patch("plugins.clipping_command.requests.get")
    def test_command_rejected_by_api_surfaces_the_error_message(self, mock_get):
        mock_get.return_value = _fake_response(200, {"outcome": "FOUND", "source": _SOURCE})
        with patch("plugins.clipping_command.requests.post") as mock_post:
            mock_post.return_value = _fake_response(422, text="source has no audio")
            result = clipping_command.run({"mode": "AUTO"})
        self.assertIn("could not be started", result)


class CheckClippingStatusTests(unittest.TestCase):
    @patch("plugins.clipping_status.requests.get")
    def test_reports_status_and_message_for_a_resolved_source(self, mock_get):
        mock_get.side_effect = [
            _fake_response(200, {"outcome": "FOUND", "source": _SOURCE}),
            _fake_response(200, {"status": "ANALYZING", "message": "Job accepted, engine not built yet.", "clips": []}),
        ]
        result = clipping_status.run({"source_query": "Folge 12"})
        self.assertIn("ANALYZING", result)
        self.assertIn("podcast-folge-12.mp4", result)

    @patch("plugins.clipping_status.requests.get")
    def test_ambiguous_source_returns_clarifying_question(self, mock_get):
        mock_get.return_value = _fake_response(409, {"outcome": "AMBIGUOUS", "candidates": [{"friendlyName": "a.mp4"}, {"friendlyName": "b.mp4"}]})
        result = clipping_status.run({"source_query": "Folge 12"})
        self.assertIn("more than one", result)

    @patch("plugins.clipping_status.requests.get")
    def test_not_found_source_returns_clear_message(self, mock_get):
        mock_get.return_value = _fake_response(404)
        result = clipping_status.run({})
        self.assertIn("couldn't find", result)


if __name__ == "__main__":
    unittest.main()
