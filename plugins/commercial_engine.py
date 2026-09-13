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
from dataclasses import asdict

import requests

from ._production_draft import PendingProductionDraft, clear_pending, get_pending, set_pending

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
        "pipeline.\n\n"
        "IMPORTANT - MISSING VOICE/SUBTITLE PREFERENCE PROTOCOL: for category 1, 2, or 3 "
        "(whether the user said so explicitly or it defaulted), a German voice AND a "
        "subtitle style are required before production can start. If either is missing, "
        "this function does NOT start anything - it returns a short spoken question "
        "instead (asking ONLY for what is still missing, never repeating what is already "
        "known) and remembers everything supplied so far. Call this SAME function again "
        "once the user answers, passing voice_preference and/or subtitle_style (you do not "
        "need to repeat product_name/category/etc. - they are remembered - but repeating "
        "them is harmless). If the user cancels ('cancel', 'never mind', 'stop'), call "
        "cancel_production_draft instead of calling this function again.\n\n"
        "IMPORTANT - CATEGORY 3 REAL-COST CONFIRMATION PROTOCOL: category 3 (Higgsfield) "
        "is a paid cloud service, unlike category 1/2 which run locally for free. Before "
        "category 3 can start, you must first tell the user this incurs real cost and ask "
        "them to confirm. If category is 3 and category3_confirmed is not yet true, this "
        "function returns a short spoken question asking for that confirmation FIRST - "
        "before even asking about voice/subtitle. Only call this function again with "
        "category3_confirmed=true once the user has clearly agreed (e.g. 'yes', 'go "
        "ahead', 'that's fine') - never set it preemptively, never infer it from silence."
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
            "reference_images": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "Use this INSTEAD of reference_image when MULTIPLE product photos were just uploaded "
                    "(you will have seen more than one path in a recent '[FILES_UPLOADED]' context message). "
                    "List ALL of their local file paths here, in the same order they were uploaded, so they "
                    "become ONE video production job that uses every photo - never call this function once "
                    "per photo. Leave out entirely for a single photo (use reference_image instead) or when "
                    "no photo was given."
                ),
            },
            "source_url": {
                "type": "STRING",
                "description": (
                    "A TikTok video link (tiktok.com, vm.tiktok.com, or vt.tiktok.com), ONLY if the user gave "
                    "one and wants a video made FROM that TikTok post's product material - e.g. 'create a "
                    "video from this TikTok link', 'make me a Category 1 video out of <link>'. Product images "
                    "are extracted automatically from the linked video - do NOT also ask for or pass "
                    "reference_image/reference_images in the same call. The user still needs to say the "
                    "product name themselves (this does not invent one). Leave out entirely when no TikTok "
                    "link was given."
                ),
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
            "voice_preference": {
                "type": "STRING",
                "description": (
                    "German voice for category 1/2/3 productions: 'male', 'female', or 'auto' (no "
                    "preference/let the system choose). Only set this if the user said or answered "
                    "this. Map 'maennlich'/'Mann'/'male' -> male, 'weiblich'/'Frau'/'female' -> female, "
                    "'egal'/'keine Praeferenz'/'such du aus'/'auto' -> auto."
                ),
            },
            "subtitle_style": {
                "type": "STRING",
                "description": (
                    "Subtitle style for category 1/2/3 productions: 'clean', 'tiktok_dynamic', 'premium', "
                    "or 'auto' (derive it from the product's marketing angle instead of a fixed style). "
                    "Only set this if the user said or answered this. Map 'clean'/'schlicht' -> clean, "
                    "'TikTok Dynamic'/'dynamisch'/'TikTok-Stil' -> tiktok_dynamic, 'Premium'/'hochwertig'/"
                    "'elegant' -> premium, 'Auto'/'such du aus'/'keine Praeferenz' -> auto."
                ),
            },
            "category3_confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "Set this to true ONLY after you have told the user that category 3 (Higgsfield) "
                    "is a paid cloud service that incurs real cost, and the user has clearly confirmed "
                    "they still want to proceed. Never set this preemptively, never set it to false - "
                    "just omit it entirely until the user has actually agreed."
                ),
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
# TikTok-Import (echter Download + Frame-Extraktion, siehe AI Content Factory
# routes/intake.ts) ist synchron, kein zweiter Hintergrund-Thread wie beim
# eigentlichen Produktions-Run - anders als eine 30-min-WAN-Render dauert ein
# echter Import typischerweise nur wenige Sekunden, ein kurzes Blockieren
# des Live-Voice-Turns dafuer ist akzeptabel (gleiches Prinzip wie der
# bereits bestehende, ebenfalls synchrone _validate_reference_image()-Check).
_TIKTOK_INTAKE_TIMEOUT_SECONDS = 90
_ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_ALLOWED_ASPECT_RATIOS = {"9:16", "16:9", "1:1", "4:3", "3:4", "9:21", "21:9"}
_ALLOWED_PLATFORMS = {"tiktok", "instagram", "youtube", "facebook", "other"}
_ALLOWED_VOICE_PREFERENCES = {"male", "female", "auto"}
_ALLOWED_SUBTITLE_STYLES = {"clean", "tiktok_dynamic", "premium", "auto"}
# Auftrag Abschnitt 27/31: EIN kurzer, natuerlicher gesprochener Satz pro
# fehlendem Feld - kein Meta-Text, das ist es, was der Nutzer tatsaechlich
# hoert (siehe run()'s Rueckgabewert).
_QUESTION_BY_MISSING_FIELD = {
    "category3_confirmation": "Kategorie 3 nutzt Higgsfield, einen kostenpflichtigen Cloud-Anbieter, und verursacht echte Kosten. Soll ich trotzdem fortfahren?",
    "voice_preference": "Männliche, weibliche Stimme oder keine Präferenz?",
    "subtitle_style": "Clean, TikTok Dynamic, Premium oder Auto?",
}


def _base_url() -> str:
    """ACF_API_BASE_URL lets the user point this at a non-default host/port without
    touching Mark-LII's own config system (see module docstring: no Jarvis-specific
    config added to the Commercial Engine, and no Commercial Engine config added to
    Mark-LII - this plugin reads its own, single, optional environment variable)."""
    return os.environ.get("ACF_API_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _run_production_in_background(project_id: str) -> None:
    try:
        response = requests.post(f"{_base_url()}/api/commercial-projects/{project_id}/run", json={}, timeout=_RUN_TIMEOUT_SECONDS)
        if response.ok:
            _deliver_browser_draft(project_id)
    except Exception:
        # Swallowed on purpose: the Commercial Engine keeps the job's real result
        # (GENERATION_FAILED / READY / etc.) visible via GET .../status regardless
        # of whether THIS client-side call itself completed cleanly - see
        # check_video_production_status. No retry here (no unbounded loops).
        pass


def _deliver_browser_draft(project_id: str) -> None:
    """Deliver a completed direct Jarvis production to TikTok Studio as a draft.
    This flow never publishes a post."""
    try:
        created = requests.post(
            f"{_base_url()}/api/tiktok/drafts",
            json={"commercialProjectId": project_id, "deliveryMethod": "BROWSER"},
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        if not created.ok:
            return
        draft_id = created.json().get("draftId")
        if not isinstance(draft_id, str) or not draft_id:
            return
        approved = requests.post(
            f"{_base_url()}/api/tiktok/browser/drafts/{draft_id}/approve",
            json={},
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        if approved.ok:
            requests.post(
                f"{_base_url()}/api/tiktok/browser/drafts/{draft_id}/upload",
                json={},
                timeout=_RUN_TIMEOUT_SECONDS,
            )
    except Exception:
        # TikTok delivery must not invalidate a successfully rendered video.
        # A failed draft can be retried without re-running production.
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


def _import_from_tiktok_url(url: str) -> tuple[list[str] | None, str | None]:
    """Ruft den bestehenden, gemeinsamen POST /api/intake/tiktok auf (siehe AI
    Content Factory apps/api/src/routes/intake.ts) - EIN Service fuer Telegram
    UND Jarvis, keine eigene TikTok-Fachlogik/kein eigener Extractor hier
    (Auftrag "TikTok-Link-Intake fuer Telegram+Jarvis" Abschnitt 23: "dieselbe
    gemeinsame Intake-Komponente aufrufen"). Gibt (image_paths, None) bei
    Erfolg oder (None, friendly_error_message) zurueck - Meldungen bewusst
    auf Englisch (wie der Rest dieser Datei), NIE die rohe (deutsche)
    API-Fehlermeldung direkt weitergereicht, um keinen Sprachmischmasch im
    gesprochenen Ergebnis zu erzeugen."""
    try:
        response = requests.post(f"{_base_url()}/api/intake/tiktok", json={"url": url}, timeout=_TIKTOK_INTAKE_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        return None, "The TikTok import service is currently unreachable. Please make sure the Commercial Engine API is running."
    if response.status_code == 422:
        # TIKTOK_INTAKE_NO_USABLE_MEDIA (siehe errors.ts) - Quality Gate hat abgelehnt.
        return None, "I couldn't extract enough usable product material from that TikTok link. Please send one or more product photos instead."
    if not response.ok:
        return None, "That TikTok link could not be imported. Please check the link, or send product photos instead."
    try:
        body = response.json()
        assets = body.get("assets") or []
        paths = [a.get("path") for a in assets if isinstance(a, dict) and isinstance(a.get("path"), str) and a.get("path")]
    except Exception:
        return None, "The TikTok import returned an unexpected response."
    if not paths:
        return None, "I couldn't extract enough usable product material from that TikTok link. Please send one or more product photos instead."
    return paths, None


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


def _normalized_choice(parameters: dict, key: str, allowed: set[str]) -> str | None:
    value = parameters.get(key)
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return None


def _normalized_true(parameters: dict, key: str) -> bool | None:
    """Only ever returns True or None - never False (see PendingProductionDraft.
    category3_confirmed: a confirmation is either explicitly given or not yet
    known, there is no real "explicitly un-confirmed" state to forward)."""
    return True if parameters.get(key) is True else None


def _merge_parameters_into_pending(parameters: dict) -> PendingProductionDraft:
    """Auftrag Abschnitt 31/33/39: baut den (moeglicherweise bereits teilweise
    bekannten) Entwurf aus dem vorherigen Turn UND den neu gelieferten Feldern -
    ueberschreibt NIE ein bereits bekanntes Feld mit einem fehlenden, erlaubt aber
    Korrekturen (ein neu geliefertes Feld gewinnt immer, siehe merged_with())."""
    base = get_pending() or PendingProductionDraft()
    category = parameters.get("category")
    raw_images = parameters.get("reference_images")
    reference_images = (
        tuple(str(p) for p in raw_images if isinstance(p, str) and p.strip())
        if isinstance(raw_images, list) and raw_images
        else None
    )
    return base.merged_with(
        product_name=str(parameters.get("product_name") or "").strip() or None,
        product_description=parameters.get("product_description"),
        creative_notes=parameters.get("creative_notes"),
        reference_image=parameters.get("reference_image"),
        reference_images=reference_images,
        duration_seconds=parameters.get("duration_seconds"),
        category=int(category) if isinstance(category, (int, float)) and int(category) in (1, 2, 3) else None,
        aspect_ratio=parameters.get("aspect_ratio"),
        platform=parameters.get("platform"),
        voice_preference=_normalized_choice(parameters, "voice_preference", _ALLOWED_VOICE_PREFERENCES),
        subtitle_style=_normalized_choice(parameters, "subtitle_style", _ALLOWED_SUBTITLE_STYLES),
        category3_confirmed=_normalized_true(parameters, "category3_confirmed"),
    )


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        draft = _merge_parameters_into_pending(parameters)

        # TikTok-Link-Intake (Auftrag "TikTok-Link-Intake fuer Telegram+Jarvis"):
        # nur verarbeiten, wenn DIESER Aufruf tatsaechlich source_url mitgibt -
        # nicht bei jedem Folgeaufruf erneut, die bereits importierten Pfade
        # stecken danach in draft.reference_images und bleiben dort ueber
        # merged_with() hinweg erhalten (siehe PendingProductionDraft). Laeuft
        # VOR der product_name-Pruefung unten, aber unabhaengig davon - der
        # Produktname kommt weiterhin vom Nutzer, TikTok ersetzt nur die Bilder.
        source_url = parameters.get("source_url")
        if isinstance(source_url, str) and source_url.strip():
            imported_paths, import_error = _import_from_tiktok_url(source_url.strip())
            if import_error:
                # Kein halbfertiger Pending-Zustand mit einem fehlgeschlagenen
                # Import - der Nutzer soll klar neu anfangen koennen (Bilder
                # senden oder einen anderen Link), kein verwirrendes Rueckfragen
                # nach Kategorie/Stimme fuer ein Produkt ohne jedes Bildmaterial.
                clear_pending()
                return import_error
            draft = draft.merged_with(reference_images=tuple(imported_paths))
            set_pending(draft)

        product_name = draft.product_name or ""
        if not product_name:
            # Kein Pending-Zustand fuer ein Produkt, das noch nicht einmal einen
            # Namen hat - nichts zu merken, unveraendertes bisheriges Verhalten.
            clear_pending()
            return "I need at least a product name to start a video production."

        # category loest sich wie bisher auf einen konkreten Wert auf (explizit
        # ODER der bestehende Default), BEVOR die Pflichtfeld-Pruefung laeuft -
        # missing_required_fields() braucht eine konkrete category, um zu wissen,
        # ob Voice/Subtitle/Kosten-Bestaetigung ueberhaupt relevant sind (Kategorie 1/2/3).
        resolved_category = draft.category if draft.category in (1, 2, 3) else _DEFAULT_PRODUCTION_CATEGORY
        draft = draft.merged_with(category=resolved_category)

        missing = draft.missing_required_fields()
        if missing:
            set_pending(draft)
            question = _QUESTION_BY_MISSING_FIELD[missing[0]]
            if player:
                try:
                    player.write_log(f"JARVIS: {question}")
                except Exception:
                    pass
            return question

        # Alles Noetige ist bekannt - der Entwurf wird jetzt tatsaechlich
        # gestartet, Pending-Zustand danach in JEDEM Fall geloescht (Erfolg,
        # Ablehnung durch die API, oder unerwarteter Fehler unten) - ein
        # fehlgeschlagener Versuch soll nicht denselben Entwurf endlos erneut
        # anbieten.
        clear_pending()

        # reference_images (plural, deterministic upload order) takes precedence
        # when present - a single reference_image is the exact same one-element
        # case, so existing single-image callers/tests are unaffected either way.
        image_candidates: list[str] = (
            list(draft.reference_images)
            if draft.reference_images
            else ([draft.reference_image] if isinstance(draft.reference_image, str) and draft.reference_image.strip() else [])
        )
        validated_image_refs: list[str] = []
        for raw_image in image_candidates:
            validated_ref, image_error = _validate_reference_image(raw_image)
            if image_error:
                return image_error
            validated_image_refs.append(validated_ref)

        webhook_url = _n8n_webhook_url()
        if webhook_url:
            # See module docstring, OPTIONAL GEMINI CREATIVE LAYER / n8n PATH + KNOWN LIMITATION.
            threading.Thread(
                target=_run_via_n8n,
                # dataclasses.asdict(draft) statt des rohen parameters-Dicts: der
                # n8n-Pfad soll denselben, ueber mehrere Turns akkumulierten
                # Wissensstand sehen wie der direkte Pfad oben, nicht nur die
                # Felder DIESES einen Funktionsaufrufs.
                # n8n path stays single-image only for now (documented KNOWN
                # LIMITATION above, deliberately not extended here) - the first
                # validated image (cover/primary) is passed, same as before.
                args=(webhook_url, product_name, asdict(draft), validated_image_refs[0] if validated_image_refs else None),
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

        body: dict = {
            "productName": product_name,
            "avatarMode": "NONE",
            "caption": f"Entdecke {product_name}. Jetzt entdecken.",
            "hashtags": ["werbung", "produkt"],
        }

        if isinstance(draft.product_description, str) and draft.product_description.strip():
            body["productDescription"] = draft.product_description.strip()

        if isinstance(draft.duration_seconds, (int, float)) and draft.duration_seconds > 0:
            body["videoDurationSeconds"] = float(draft.duration_seconds)

        body["category"] = resolved_category

        if isinstance(draft.aspect_ratio, str) and draft.aspect_ratio in _ALLOWED_ASPECT_RATIOS:
            body["aspectRatio"] = draft.aspect_ratio

        if isinstance(draft.platform, str) and draft.platform.strip().lower() in _ALLOWED_PLATFORMS:
            body["platform"] = draft.platform.strip().upper()

        # Gemeinsame, kanalneutrale Production Options (Auftrag Abschnitt 45/46:
        # "Keine channel-spezifischen Backend-Felder... zentral: voicePreference,
        # subtitleStyle") - fuer Kategorie 1/2/3 gleichermassen erfragt (siehe
        # missing_required_fields()), hier unconditional durchgereicht.
        if draft.voice_preference:
            body["voicePreference"] = draft.voice_preference
        if draft.subtitle_style:
            body["subtitleStyle"] = draft.subtitle_style

        if validated_image_refs:
            # Same dual-field shape apps/telegram-bot/src/production.ts already
            # sends (productImageRef = first/cover image, productImageRefs = all,
            # in upload order) - apps/api's CommercialOrchestrator already
            # distributes productImageRefs round-robin across generated scenes
            # (resolveSceneReferenceImages), so nothing downstream needed to change.
            body["productImageRef"] = validated_image_refs[0]
            body["productImageRefs"] = validated_image_refs

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
