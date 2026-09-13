"""
commercial_engine_status.py — JARVIS/Mark-LII plugin: checks the status of a
video production job previously started by commercial_engine.py
(create_video_production_request).

Deliberately self-contained (does not import the sibling plugin module) so
either file keeps working if copied/moved on its own - the two are a matched
pair, not a hard dependency. `project_id` relies on the conversation's own
memory: Gemini already saw the ID in create_video_production_request's own
returned sentence earlier in the same conversation, so it can supply it again
here without this plugin needing any process-level state of its own.

Same scope/security notes as commercial_engine.py: read-only HTTP client
against the existing Commercial Engine API, no filesystem/shell/git access,
no secrets.
"""
from __future__ import annotations

import os

import requests

PLUGIN = {
    "name": "check_video_production_status",
    "description": (
        "Checks the status of a video production job started earlier with "
        "create_video_production_request - use when the user asks things like "
        "'is my video ready', 'what's the status of that video', 'did the production "
        "finish'. Requires the project ID that create_video_production_request returned "
        "earlier in this conversation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "project_id": {"type": "STRING", "description": "The project ID returned by create_video_production_request."},
        },
        "required": ["project_id"],
    },
}

_DEFAULT_BASE_URL = "http://localhost:3000"
_REQUEST_TIMEOUT_SECONDS = 10

_STAGE_DESCRIPTIONS = {
    "PLANNING": "still being planned",
    "GENERATION": "generating the video",
    "AUDIO": "generating voiceover audio",
    "ASSEMBLY": "assembling the final video",
    "QUALITY": "running quality checks",
    "COMPLIANCE": "running compliance checks",
    "REVIEW": "waiting for human review",
    "DONE": "finished",
    "FAILED": "failed",
}


def _base_url() -> str:
    return os.environ.get("ACF_API_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        project_id = str(parameters.get("project_id") or "").strip()
        if not project_id:
            return "I need the project ID to check its status - what was it?"

        try:
            response = requests.get(f"{_base_url()}/api/commercial-projects/{project_id}/status", timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return "The video production interface is currently unreachable. Please make sure the Commercial Engine API is running."

        if response.status_code == 404:
            return f"I don't know a project with ID {project_id}."
        if not response.ok:
            return f"The video production interface returned an error checking that status: {response.text[:300]}"

        status = response.json()
        stage = status.get("currentStage", "")
        stage_text = _STAGE_DESCRIPTIONS.get(stage, stage.lower() if stage else "unknown")

        if status.get("status") == "READY":
            final_output = status.get("finalOutput") or {}
            path = final_output.get("path", "an unknown location")
            result_text = f"Project {project_id} is done - the finished video is at {path}."
        elif status.get("retryable") and status.get("failedStage"):
            # `retryable` alone is NOT proof of a failure since the WAN/ComfyUI recovery fix
            # (2026-09-13): a still-GENERATING project is also retryable (a long-running
            # provider job can be nudged along via the same endpoint), but nothing actually
            # went wrong there - `failedStage` is only set when the project genuinely failed
            # (see apps/api's toCommercialProjectStatusView()), so it is the real disambiguator.
            result_text = f"Project {project_id} hit a problem during {stage_text} and can be retried - want me to retry it?"
        elif status.get("retryable"):
            result_text = f"Project {project_id} is taking longer than usual to generate ({stage_text}) - nothing has gone wrong, want me to check on it again?"
        elif stage_text == "failed":
            result_text = f"Project {project_id} failed and is not retryable."
        else:
            result_text = f"Project {project_id} is still in progress - {stage_text}."
    except Exception as e:
        return f"Sir, check_video_production_status failed: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
