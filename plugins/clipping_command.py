"""
clipping_command.py - JARVIS/Mark-LII plugin: starts/steers a Production Path 6
(Clipping) job in the AI Content Factory - turns an already-ingested long-form
source video (local Clipping Inbox OR Telegram) into short-form clips.

SCOPE (see AI Content Factory repo, HANDOVER.md, "Master Female Presenter"/
"Production Path 6" rounds): this plugin is ONLY a thin, validating HTTP
client for the ALREADY-EXISTING POST /api/clipping/sources/resolve and
POST /api/clipping/commands endpoints of that project's own apps/api server.
It has NO knowledge of, and makes NO decisions about, transcription, semantic
segmentation, candidate ranking, FFmpeg, captions, or face/subject tracking -
ALL of that intelligence lives exclusively in the AI Content Factory itself.
This plugin never receives, stores, or forwards a real local file path for a
clipping source - only a stable `sourceId` the Content Factory itself
resolved and returned (see AI Content Factory Auftrag "Jarvis als
gleichwertiger Control Channel", 2026-09-27, Phase 2/15).

SOURCE RESOLUTION, NOT GUESSING: "the new video", "the latest video", "Podcast
Episode 12" etc. are resolved via the Content Factory's own
GET /api/clipping/sources/resolve?query=... - if that call reports multiple
matching candidates (AMBIGUOUS), this plugin returns a short spoken
clarifying question listing the candidates and does NOT guess/pick one. There
is deliberately no Python-side "pending draft" state for this (unlike
commercial_engine.py's multi-turn slot-filling) - source resolution is
cheap and stateless to re-run, so the user's next, more specific utterance
naturally re-invokes this same tool with a narrower source_query.

TIMESTAMP PARSING: for MANUAL mode, this plugin asks Gemini Live's own
function-calling to extract manual_range_start_seconds/manual_range_end_seconds
as plain numbers directly from the user's utterance (e.g. "from 12:30 to
13:15" -> 750 / 795) - no timestamp-string parsing happens in this file, so
there is no second implementation of that logic living outside the Content
Factory's own (already tested) parser.

SECURITY: no filesystem access, no shell/git commands, no secrets forwarded
(the Clipping API takes none today).
"""
from __future__ import annotations

import os

import requests

PLUGIN = {
    "name": "start_clipping_job",
    "description": (
        "Starts or steers a Production Path 6 'Clipping' job in the AI Content Factory - "
        "turns an existing long-form video (already placed in the local Clipping Inbox "
        "folder, or previously sent to the Telegram bot) into short-form clips. Use this "
        "when the user asks things like 'take the new video from the clipping folder and "
        "make me the five best clips', 'clip out all the parts about real estate financing "
        "from Podcast Episode 12', 'find the part where it explains why the first company "
        "failed', 'cut a clip from Podcast Episode 12 from 12:30 to 13:15'. Do NOT use this "
        "for our own product advertising video pipeline (see create_video_production_request "
        "for that) - Clipping only processes EXISTING long-form video into shorter clips, it "
        "never generates new video content. This starts an asynchronous job - it does NOT "
        "wait for clips to be produced; use check_clipping_status afterwards.\n\n"
        "SOURCE REFERENCE: source_query is how the user referred to the source video in "
        "natural language - 'the new video'/'the latest video' (leave source_query empty for "
        "this - it means the same thing), a filename fragment like 'Podcast Episode 12', or "
        "similar. Never invent or guess a source - if this function reports multiple matching "
        "videos, it returns a clarifying question; ask the user and call this function again "
        "with a more specific source_query, do not pick one yourself.\n\n"
        "MODE: exactly one of AUTO (automatic best-clip selection - optionally set max_clips, "
        "default 5), KEYWORD (pass keywords as a list of search terms/topics), INSTRUCTION "
        "(pass instruction_text - the user's free-form description of what to find, forward "
        "it close to verbatim, the semantic interpretation happens in the Content Factory "
        "itself, do not pre-summarize or simplify it), or MANUAL (pass "
        "manual_range_start_seconds/manual_range_end_seconds - convert any spoken timestamp "
        "like '12:30' or '00:12:30' into seconds yourself, e.g. 12:30 -> 750)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source_query": {
                "type": "STRING",
                "description": "How the user referred to the source video - a filename fragment, or omit/leave empty for 'the new/latest video'.",
            },
            "mode": {
                "type": "STRING",
                "description": "One of AUTO, KEYWORD, INSTRUCTION, MANUAL.",
            },
            "max_clips": {
                "type": "INTEGER",
                "description": "AUTO only - how many clips to produce, only if the user said a number (e.g. 3, 5, 10). Omit for the system default.",
            },
            "keywords": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "KEYWORD only - the search terms/topics, e.g. ['real estate financing', 'interest rates', 'down payment'].",
            },
            "instruction_text": {
                "type": "STRING",
                "description": "INSTRUCTION only - the user's free-form description of what to find, forwarded close to verbatim.",
            },
            "manual_range_start_seconds": {
                "type": "NUMBER",
                "description": "MANUAL only - clip start time in seconds (convert any spoken timestamp yourself, e.g. 12:30 -> 750).",
            },
            "manual_range_end_seconds": {
                "type": "NUMBER",
                "description": "MANUAL only - clip end time in seconds.",
            },
        },
        "required": ["mode"],
    },
}

_DEFAULT_BASE_URL = "http://localhost:3000"
_REQUEST_TIMEOUT_SECONDS = 15
_ALLOWED_MODES = {"AUTO", "KEYWORD", "INSTRUCTION", "MANUAL"}


def _base_url() -> str:
    return os.environ.get("ACF_API_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _resolve_source(query: str | None) -> tuple[dict | None, str | None]:
    """Returns (source_summary, None) on a single match, or (None, spoken_message) for
    AMBIGUOUS/NOT_FOUND/an unreachable API - NEVER guesses among multiple candidates
    (Auftrag Phase 4: 'Ambiguitaet nicht raten')."""
    params = {"query": query} if query else {}
    try:
        response = requests.get(f"{_base_url()}/api/clipping/sources/resolve", params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        return None, "The Clipping interface is currently unreachable. Please make sure the Commercial Engine API is running."

    if response.status_code == 404:
        return None, "I couldn't find a matching clipping source. Make sure the video has been placed in the local Clipping Inbox folder or sent to the bot, and has finished being detected."
    if response.status_code == 409:
        try:
            candidates = response.json().get("candidates", [])
        except Exception:
            candidates = []
        names = [c.get("friendlyName", "unknown") for c in candidates if isinstance(c, dict)]
        listing = "; ".join(names) if names else "several videos"
        return None, f"I found more than one matching video: {listing}. Which one did you mean?"
    if not response.ok:
        return None, f"The Clipping interface returned an error resolving that video: {response.text[:300]}"

    body = response.json()
    return body.get("source"), None


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        mode = str(parameters.get("mode") or "").strip().upper()
        if mode not in _ALLOWED_MODES:
            return f"I need a valid mode - one of {', '.join(sorted(_ALLOWED_MODES))}."

        source_query = parameters.get("source_query")
        source, resolve_error = _resolve_source(source_query.strip() if isinstance(source_query, str) else None)
        if resolve_error:
            return resolve_error
        source_id = source.get("id") if source else None
        if not source_id:
            return "The Clipping interface returned an unexpected response resolving the source video."

        body: dict = {"sourceId": source_id, "mode": mode, "requestedBy": "JARVIS"}

        if mode == "AUTO":
            max_clips = parameters.get("max_clips")
            if isinstance(max_clips, (int, float)) and max_clips > 0:
                body["maxClips"] = int(max_clips)
        elif mode == "KEYWORD":
            keywords = parameters.get("keywords")
            if not isinstance(keywords, list) or not keywords:
                return "Which keywords or topics should I look for?"
            body["keywords"] = [str(k) for k in keywords if isinstance(k, str) and k.strip()]
        elif mode == "INSTRUCTION":
            instruction_text = parameters.get("instruction_text")
            if not isinstance(instruction_text, str) or not instruction_text.strip():
                return "What should I look for? Please describe it."
            body["instruction"] = instruction_text.strip()
        elif mode == "MANUAL":
            start_seconds = parameters.get("manual_range_start_seconds")
            end_seconds = parameters.get("manual_range_end_seconds")
            if not isinstance(start_seconds, (int, float)) or not isinstance(end_seconds, (int, float)):
                return "What start and end time should the clip cover?"
            body["manualRange"] = {"startMs": int(start_seconds * 1000), "endMs": int(end_seconds * 1000)}

        try:
            response = requests.post(f"{_base_url()}/api/clipping/commands", json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return "The Clipping interface is currently unreachable. Please make sure the Commercial Engine API is running."

        if not response.ok:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except Exception:
                detail = response.text[:300]
            return f"The Clipping job could not be started: {detail}"

        friendly_name = source.get("friendlyName", "the video")
        result_text = (
            f"Clipping job accepted for '{friendly_name}' (mode: {mode.lower()}, source ID {source_id}). "
            f"Note: the actual clip-selection/rendering engine for Production Path 6 is not built yet, "
            f"so no clips exist yet - ask me to check the status later once it is."
        )
    except Exception as e:
        return f"Sir, start_clipping_job failed: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
