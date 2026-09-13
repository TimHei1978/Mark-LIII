"""
Verifies our three plugins load through Mark-LII's REAL, unmodified
core/plugin_loader.py - not a re-implementation of the loader's own rules.
(_production_draft.py is a fourth file in plugins/ but is underscore-prefixed
on purpose - discover_plugins() must SKIP it, same convention as _template.py,
covered by test_underscore_prefixed_helper_is_not_discovered_as_a_plugin below.)

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.plugin_loader import discover_plugins  # noqa: E402
from plugins import _production_draft  # noqa: E402

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


class PluginDiscoveryTests(unittest.TestCase):
    def setUp(self):
        _production_draft.clear_pending()

    def tearDown(self):
        _production_draft.clear_pending()

    # --- A) Plugin laedt erfolgreich (durch den echten Mark-LII-Loader, nicht nur importierbar) ---

    def test_all_three_plugins_are_discovered_as_valid(self):
        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=set(), logger=lambda _msg: None)
        self.assertTrue(registry.has("create_video_production_request"))
        self.assertTrue(registry.has("check_video_production_status"))
        self.assertTrue(registry.has("cancel_production_draft"))

    def test_underscore_prefixed_helper_is_not_discovered_as_a_plugin(self):
        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=set(), logger=lambda _msg: None)
        self.assertFalse(registry.has("_production_draft"))
        self.assertFalse(registry.has("production_draft"))

    def test_no_plugin_is_rejected(self):
        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=set(), logger=lambda _msg: None)
        rejected = [rec for rec in registry._all_records if not rec.valid]
        self.assertEqual(rejected, [], f"Unexpected rejected plugin records: {rejected}")

    def test_tool_declarations_are_well_formed_for_gemini_function_calling(self):
        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=set(), logger=lambda _msg: None)
        declarations = {decl["name"]: decl for decl in registry.get_tool_declarations()}
        self.assertIn("create_video_production_request", declarations)
        self.assertIn("check_video_production_status", declarations)
        self.assertIn("cancel_production_draft", declarations)
        for decl in declarations.values():
            self.assertTrue(decl["description"].strip())
            self.assertEqual(decl["parameters"]["type"], "OBJECT")

    def test_names_do_not_collide_with_core_tools(self):
        # A representative sample of main.py's own built-in tool names - our plugin
        # names must never collide with any of these (the real loader enforces this
        # itself; this test only proves our chosen names are clearly distinct).
        core_tool_names = {"open_app", "send_message", "youtube_video", "computer_settings", "file_controller"}
        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=core_tool_names, logger=lambda _msg: None)
        self.assertTrue(registry.has("create_video_production_request"))
        self.assertTrue(registry.has("check_video_production_status"))
        self.assertTrue(registry.has("cancel_production_draft"))

    def test_dispatch_through_the_real_registry_reaches_our_run_function(self):
        """End-to-end through PluginRegistry.run() (the exact call main.py itself makes),
        not just calling our own run() directly - proves the loader's dispatch/exception
        wrapping/parameter-passing conventions are actually satisfied. category 1/2/3 all
        require category3_confirmed (category 3 only)/voice_preference/subtitle_style
        before firing (see _production_draft.py) - supplied here explicitly since this
        test is about dispatch plumbing, not the conversational flow (covered exhaustively
        in test_commercial_engine_plugin.py)."""
        from unittest.mock import MagicMock, patch

        registry = discover_plugins(plugins_dir=PLUGINS_DIR, core_tool_names=set(), logger=lambda _msg: None)
        with patch("plugins.commercial_engine.requests.post") as mock_post, patch("plugins.commercial_engine.threading.Thread"):
            resp = MagicMock(ok=True, status_code=201)
            resp.json.return_value = {"projectId": "proj-dispatch"}
            mock_post.return_value = resp
            result = registry.run(
                "create_video_production_request",
                {"product_name": "Kaffeebecher", "category": 3, "category3_confirmed": True, "voice_preference": "auto", "subtitle_style": "auto"},
                player=None,
                session_memory=None,
            )
        self.assertIn("proj-dispatch", result)


if __name__ == "__main__":
    unittest.main()
