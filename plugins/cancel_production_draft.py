"""
cancel_production_draft.py - JARVIS/Mark-LII plugin: cancels a pending video
production draft that create_video_production_request is still waiting on an
answer for (e.g. it just asked "male, female, or no preference?").

SCOPE (see AI Content Factory task "Jarvis - Abbruch"): this ONLY clears the
in-process pending-draft state in _production_draft.py. It never talks to the
Commercial Engine API - there is nothing to cancel there, because
create_video_production_request never calls POST /api/commercial-projects
until every required field (including voice_preference/subtitle_style for
category 1/2) is known. If no draft is pending, this is a harmless no-op.
"""
from __future__ import annotations

from ._production_draft import clear_pending, get_pending

PLUGIN = {
    "name": "cancel_production_draft",
    "description": (
        "Cancels a pending video production request that create_video_production_request is "
        "still waiting on an answer for (for example, it just asked which voice or subtitle "
        "style to use). Use this when the user says something like 'cancel', 'never mind', "
        "'forget it', 'stop', or 'doch nicht' WHILE such a question is open. Does nothing "
        "harmful if no production draft is currently pending."
    ),
    "parameters": {"type": "OBJECT", "properties": {}},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    had_pending = get_pending() is not None
    clear_pending()
    result_text = "Okay, cancelled." if had_pending else "There wasn't a pending video request to cancel."
    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
