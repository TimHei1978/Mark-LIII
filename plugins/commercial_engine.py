"""
commercial_engine.py - JARVIS/Mark-LII plugin: starts a product advertising
video production job in the existing AI Content Factory "Commercial Engine".

SCOPE (see AI Content Factory repo, HANDOVER.md/ARCHITECTURE.md, "Jarvis"):
this plugin is ONLY a thin, validating HTTP client for the ALREADY-EXISTING
POST /api/commercial-projects (+ /:id/run) endpoints of that project's own
apps/api server. It has NO knowledge of, and makes NO decisions about,
ComfyUI, Wan, Higgsfield, FFmpeg, or any concrete video provider - which
provider actually runs is decided entirely by that project's own Category
Router (apps/api/src/commercialEngine.ts), never here. This file only builds
a request in the Commercial Engine's OWN existing request shape and forwards
it - see companion plugin commercial_engine_status.py for checking on a job
started here.

SECURITY: this plugin performs NO filesystem writes/moves/deletes, runs NO
shell/git commands, and never reads or forwards any secret/API key (the
Commercial Engine API takes none today). The only local-filesystem access is
a read-only existence/extension check on a reference-image path the caller
supplies, before forwarding that path as a plain string - the same trust
boundary a human typing the same path into curl on this machine would have.

WHY THE REAL PRODUCTION RUN HAPPENS ON A BACKGROUND THREAD: a real Category 2
(local Wan/ComfyUI) job takes on the order of half an hour per 5-second
segment on this project's reference hardware; Category 3 (Higgsfield) is
usually faster but still not instant. Blocking this plugin's run() for that
long would freeze the entire live voice conversation (Mark-LII awaits each
tool call before it can continue talking). So this plugin only waits for the
FAST create-call (milliseconds), then hands the actual "start production"
call to a daemon background thread and returns immediately - the Commercial
Engine keeps driving the job to completion on its own server process
regardless of what Mark-LII does afterwards (see AI Content Factory
HANDOVER.md, "Jarvis": Category 1/2/3 keep working through their existing
interfaces independent of whether Jarvis is even running). Use
check_video_production_status (companion plugin) to poll the result later.

OPTIONAL GEMINI CREATIVE LAYER / n8n PATH (added 2026-09-08, see AI Content
Factory workflows/n8n/production-request.json and
packages/commercial-engine/src/creative/productionBrief.ts): if the
environment variable ACF_N8N_WEBHOOK_URL is set, this plugin sends the
request to that n8n webhook INSTEAD of directly to the Commercial Engine.
n8n then calls the Gemini creative/prompt layer (turning the request into a
rich, structured ProductionBrief - visual style/mood/camera/lighting/negative
prompt, not just the coarse fields this plugin's own PLUGIN schema captures)
before forwarding to the SAME existing POST /api/commercial-projects (+
.../run) endpoints this plugin already uses directly. Without this variable,
behaviour is completely unchanged (direct call, as before) - this is a purely
additive, opt-in extension, not a rewrite.

KNOWN LIMITATION of the n8n path specifically: unlike the direct path (fast
create + backgrounded run, immediate project ID), the n8n webhook used here
is a single synchronous call that only returns once the ENTIRE production run
(including Category 2/3's real runtime) has finished - there is no separate
fast "create" step to get an early project ID from n8n. This plugin therefore
runs the WHOLE webhook call on its own background thread and returns
immediately with a generic "started" message, WITHOUT a project ID - the
companion check_video_production_status plugin cannot yet poll an n8n-started
job by ID. Splitting the n8n workflow into an async create+run pair (so the
same fast-ack/poll-later UX as the direct path becomes possible) is real
follow-up work, deliberately not built now (see "nicht ueberbauen").
"""
from __future__ import annotations

import os
import threading

import requests

PLUGIN = {
    "name": "create_video_production_request",
    "description": (
        "Starts a product advertising video production job in the AI Content Factory "
        "Commercial Engine (our own product-video pipeline). Use this when the user asks "
        "to create or generate a product advertising video, commercial, or TikTok/"
        "Instagram/YouTube ad video from a product photo or description - for example "
        "'create a 15 second ad video from this product photo', 'make me a TikTok video "
        "for product X', 'use category 2 for this one', 'make this locally with Wan', "
        "'do this one with Higgsfield'. This starts an asynchronous job and returns "
        "immediately with a project ID - it does NOT wait for the video to finish; use "
        "check_video_production_status afterwards to find out when it's done. Do NOT use "
        "this for general video playback, editing, or YouTube search (see youtube_video "
        "for that) - this is exclusively for STARTING a new production job in our own "
        "pipeline."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "product_name": {"type": "STRING", "description": "The product's name. Required."},
            "product_description": {
                "type": "STRING",
                "description": "Short factual description of the product, only if the user actually gave one - never invent product details.",
            },
            "creative_notes": {
                "type": "STRING",
                "description": (
                    "The user's stylistic/creative/atmosphere wishes for the video, verbatim or close to "
                    "it, ONLY if they actually said something like this - e.g. 'warm Christmas mood, golden "
                    "lights, light snow, no people or hands', 'high-end, cinematic', 'summery and bright'. "
                    "Separate from product_description (which is only factual product info). Never invent "
                    "mood/style details the user did not ask for."
                ),
            },
            "reference_image": {
                "type": "STRING",
                "description": "Local file path (on this computer) to a real product photo, only if the user gave or clearly referenced one.",
            },
            "duration_seconds": {
                "type": "NUMBER",
                "description": "Requested video length in seconds (e.g. 5, 10, 15), only if the user actually said or clearly implied one - never invent a number.",
            },
            "category": {
                "type": "INTEGER",
                "description": (
                    "Production category, ONLY set this if the user explicitly asked for one: "
                    "1 = local composition (assembles existing product images/clips, no AI video generation), "
                    "2 = local Wan/ComfyUI (free, runs on this computer's own GPU, generates a new AI video, may be "
                    "unavailable on machines without a suitable GPU), "
                    "3 = Higgsfield (paid cloud AI video generation service). "
                    "Leave this out entirely if the user did not specify - the Commercial Engine picks its own default."
                ),
            },
            "aspect_ratio": {
                "type": "STRING",
                "description": "One of 9:16, 16:9, 1:1, 4:3, 3:4, 9:21, 21:9. Omit if the user did not mention one.",
            },
            "platform": {
                "type": "STRING",
                "description": "Target publishing platform for compliance labeling: tiktok, instagram, youtube, facebook, or other. Omit if not mentioned.",
            },
        },
        "required": ["product_name"],
    },
}

_DEFAULT_BASE_URL = "http://localhost:3000"
# A missing category must never silently select the Commercial Engine's mock
# provider.  Jarvis requests are production requests, so use the local Wan
# path by default; callers can still explicitly select category 1 or 3.
_DEFAULT_PRODUCTION_CATEGORY = 2
# Only for the fast create-call (milliseconds in practice) - NOT for the real
# production run, which happens on its own background thread (see module docstring).
_CREATE_TIMEOUT_SECONDS = 10
# Generous but finite ceiling for the background production-run call - never
# infinite (see task requirement: no unbounded loops/retries).
_RUN_TIMEOUT_SECONDS = 3 * 60 * 60
_ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_ALLOWED_ASPECT_RATIOS = {"9:16", "16:9", "1:1", "4:3", "3:4", "9:21", "21:9"}
_ALLOWED_PLATFORMS = {"tiktok", "instagram", "youtube", "facebook", "other"}


def _base_url() -> str:
    """ACF_API_BASE_URL lets the user point this at a non-default host/port without
    touching Mark-LII's own config system (see module docstring: no Jarvis-specific
    config added to the Commercial Engine, and no Commercial Engine config added to
    Mark-LII - this plugin reads its own, single, optional environment variable)."""
    return os.environ.get("ACF_API_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _run_production_in_background(project_id: str) -> None:
    try:
        requests.post(f"{_base_url()}/api/commercial-projects/{project_id}/run", json={}, timeout=_RUN_TIMEOUT_SECONDS)
    except Exception:
        # Swallowed on purpose: the Commercial Engine keeps the job's real result
        # (GENERATION_FAILED / READY / etc.) visible via GET .../status regardless
        # of whether THIS client-side call itself completed cleanly - see
        # check_video_production_status. No retry here (no unbounded loops).
        pass


def _n8n_webhook_url() -> str | None:
    """See module docstring, 'OPTIONAL GEMINI CREATIVE LAYER / n8n PATH'. Unset by default -
    existing direct-to-Commercial-Engine behaviour is the default, unchanged path."""
    return os.environ.get("ACF_N8N_WEBHOOK_URL", "").strip() or None


def _build_user_request_text(product_name: str, parameters: dict) -> str:
    """Builds a natural-language sentence from the fields Mark-LII's OWN Gemini Live already
    extracted (including the new free-text creative_notes) for the n8n/Gemini-creative-layer
    webhook - this is NOT a second raw voice capture, just the already-recognised fields
    phrased as one request, since Mark-LII's Gemini Live has already consumed the user's
    original sentence by the time this plugin runs."""
    parts = [product_name]
    description = parameters.get("product_description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    creative_notes = parameters.get("creative_notes")
    if isinstance(creative_notes, str) and creative_notes.strip():
        parts.append(creative_notes.strip())
    duration = parameters.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        parts.append(f"{duration} second video")
    platform = parameters.get("platform")
    if isinstance(platform, str) and platform.strip():
        parts.append(f"for {platform.strip()}")
    category = parameters.get("category")
    if isinstance(category, (int, float)) and int(category) in (1, 2, 3):
        parts.append(f"Use category {int(category)}.")
    return ". ".join(parts)


def _run_via_n8n(webhook_url: str, product_name: str, parameters: dict, product_image_ref: str | None) -> None:
    """Fire-and-forget: the n8n webhook is a single synchronous call covering creative-brief +
    create + run (see module docstring, KNOWN LIMITATION) - runs on its own background thread
    so it never blocks the live voice conversation, same reasoning as
    _run_production_in_background above, just for the whole chain instead of only .../run."""
    body: dict = {"userRequest": _build_user_request_text(product_name, parameters), "productName": product_name}
    if product_image_ref:
        body["productImageRef"] = product_image_ref
    try:
        requests.post(webhook_url, json=body, timeout=_RUN_TIMEOUT_SECONDS)
    except Exception:
        # Swallowed on purpose, same reasoning as _run_production_in_background - no project ID
        # exists yet to report a failure against here (see check_video_production_status limitation).
        pass


def _validate_reference_image(raw_path: str) -> tuple[str | None, str | None]:
    """Returns (productImageRef_value, error_message) - exactly one is None."""
    path = raw_path.strip()
    if path.lower().startswith(("http://", "https://")):
        return path, None  # a URL - the Commercial Engine already handles this case itself
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        return None, f"I can't find a file at '{path}' - please check the path and try again."
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTENSIONS:
        return None, f"'{path}' doesn't look like a supported image file ({', '.join(sorted(_ALLOWED_IMAGE_EXTENSIONS))})."
    return abs_path, None


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        product_name = str(parameters.get("product_name") or "").strip()
        if not product_name:
            return "I need at least a product name to start a video production."

        reference_image = parameters.get("reference_image")
        validated_image_ref: str | None = None
        if isinstance(reference_image, str) and reference_image.strip():
            validated_image_ref, image_error = _validate_reference_image(reference_image)
            if image_error:
                return image_error

        webhook_url = _n8n_webhook_url()
        if webhook_url:
            # See module docstring, OPTIONAL GEMINI CREATIVE LAYER / n8n PATH + KNOWN LIMITATION.
            threading.Thread(
                target=_run_via_n8n,
                args=(webhook_url, product_name, parameters, validated_image_ref),
                daemon=True,
                name=f"acf-n8n-{product_name[:20]}",
            ).start()
            result_text = (
                f"Started a video production request for {product_name} through the creative planning layer. "
                f"This runs Gemini creative planning and the full production together, so I don't have a "
                f"project ID for it yet - ask me again in a bit if you want me to check on it by name."
            )
            if player:
                try:
                    player.write_log(f"JARVIS: {result_text}")
                except Exception:
                    pass
            return result_text

        body: dict = {"productName": product_name, "avatarMode": "NONE"}

        description = parameters.get("product_description")
        if isinstance(description, str) and description.strip():
            body["productDescription"] = description.strip()

        duration = parameters.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            body["videoDurationSeconds"] = float(duration)

        category = parameters.get("category")
        if isinstance(category, (int, float)) and int(category) in (1, 2, 3):
            body["category"] = int(category)
        else:
            body["category"] = _DEFAULT_PRODUCTION_CATEGORY

        aspect_ratio = parameters.get("aspect_ratio")
        if isinstance(aspect_ratio, str) and aspect_ratio in _ALLOWED_ASPECT_RATIOS:
            body["aspectRatio"] = aspect_ratio

        platform = parameters.get("platform")
        if isinstance(platform, str) and platform.strip().lower() in _ALLOWED_PLATFORMS:
            body["platform"] = platform.strip().upper()

        if validated_image_ref:
            body["productImageRef"] = validated_image_ref

        try:
            response = requests.post(f"{_base_url()}/api/commercial-projects", json=body, timeout=_CREATE_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException:
            return "The video production interface is currently unreachable. Please make sure the Commercial Engine API is running."

        if not response.ok:
            detail = response.text[:300]
            return f"The video production interface rejected the request: {detail}"

        created = response.json()
        project_id = created.get("projectId")
        if not project_id:
            return "The video production interface returned an unexpected response."

        threading.Thread(target=_run_production_in_background, args=(project_id,), daemon=True, name=f"acf-run-{project_id}").start()

        category_note = f" (category {body['category']})"
        result_text = (
            f"Started a video production job{category_note} for {product_name}. Project ID {project_id}. "
            f"This can take anywhere from about a minute to over an hour depending on the category - "
            f"ask me to check the status of project {project_id} later."
        )
    except Exception as e:
        return f"Sir, create_video_production_request failed: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
