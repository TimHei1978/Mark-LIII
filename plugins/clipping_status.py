"""
clipping_status.py - JARVIS/Mark-LII plugin: checks the status/results of a
Production Path 6 (Clipping) job started earlier with start_clipping_job.

Deliberately self-contained (does not import the sibling plugin module),
same pairing principle as commercial_engine_status.py/commercial_engine.py.
Resolves the source the same way start_clipping_job does (natural-language
source_query -> GET /api/clipping/sources/resolve), so the user can ask
"how far along is the clipping of Podcast Episode 12" without needing to
remember/repeat an id.

Never transfers the actual video/clip files - only status text and counts
(Auftrag "Jarvis"-Ergaenzung Phase 10: "Jarvis muss nicht grosse Videodateien
selbst uebertragen"). The real master files stay entirely local.
"""
from __future__ import annotations

import os

import requests

PLUGIN = {
    "name": "check_clipping_status",
    "description": (
        "Checks the status or results of a Production Path 6 'Clipping' job started earlier "
        "with start_clipping_job - use when the user asks things like 'how far along is the "
        "clipping', 'are my five clips ready', 'which clips were made from Podcast Episode 12'. "
        "Do NOT use this for our own product advertising video pipeline (see "
        "check_video_production_status for that)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source_query": {
                "type": "STRING",
                "description": "How the user referred to the source video - a filename fragment, or omit/leave empty for 'the new/latest video'.",
            },
        },
    },
}

_DEFAULT_BASE_URL = "http://localhost:3000"
_REQUEST_TIMEOUT_SECONDS = 15


def _base_url() -> str:
    return os.environ.get("ACF_API_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        source_query = parameters.get("source_query")
        params = {"query": source_query.strip()} if isinstance(source_query, str) and source_query.strip() else {}

        try:
            resolve_response = requests.get(f"{_base_url()}/api/clipping/sources/resolve", params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return "The Clipping interface is currently unreachable. Please make sure the Commercial Engine API is running."

        if resolve_response.status_code == 404:
            return "I couldn't find a matching clipping source."
        if resolve_response.status_code == 409:
            try:
                candidates = resolve_response.json().get("candidates", [])
            except Exception:
                candidates = []
            names = [c.get("friendlyName", "unknown") for c in candidates if isinstance(c, dict)]
            listing = "; ".join(names) if names else "several videos"
            return f"I found more than one matching video: {listing}. Which one did you mean?"
        if not resolve_response.ok:
            return f"The Clipping interface returned an error: {resolve_response.text[:300]}"

        source = resolve_response.json().get("source") or {}
        source_id = source.get("id")
        friendly_name = source.get("friendlyName", "the video")
        if not source_id:
            return "The Clipping interface returned an unexpected response."

        try:
            results_response = requests.get(f"{_base_url()}/api/clipping/projects/{source_id}/results", timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return "The Clipping interface is currently unreachable checking results."

        if not results_response.ok:
            return f"Could not check the status of '{friendly_name}': {results_response.text[:300]}"

        results = results_response.json()
        status = results.get("status", "unknown")
        message = results.get("message", "")
        clips = results.get("clips", [])
        clip_count_note = f" {len(clips)} clip(s) so far." if clips else ""
        result_text = f"'{friendly_name}' is in status {status}. {message}{clip_count_note}".strip()
    except Exception as e:
        return f"Sir, check_clipping_status failed: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
