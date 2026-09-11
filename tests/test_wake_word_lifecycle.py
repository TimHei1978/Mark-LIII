"""
Tests for JARVIS's wake/sleep lifecycle in main.py - the REAL, unmodified
JarvisLive class (not a reimplementation), with only hardware (sounddevice) and
network (Gemini) boundaries mocked, per the task's own testing rule: mock only
where hardware/network access isn't sensibly automatable.

JarvisUI itself (a PyQt widget) is replaced with a MagicMock - constructing a
real one needs a running Qt application, which is not something an automated
suite should require. main.py's __init__ only ever ASSIGNS callables onto
self.ui and later CALLS a handful of methods on it (set_state/write_log/muted) -
a MagicMock satisfies both without needing PyQt's real shape.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Captured BEFORE any test patches main.asyncio.sleep - main.asyncio IS the
# asyncio module (sys.modules is process-wide), so patching main.asyncio.sleep
# patches asyncio.sleep everywhere, including inside AsyncSleepStub itself if
# it were to call asyncio.sleep(). Use this reference instead, never the name.
_real_asyncio_sleep = asyncio.sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as jarvis_main  # noqa: E402
from main import JarvisLive  # noqa: E402


def _fake_ui() -> MagicMock:
    ui = MagicMock()
    ui.muted = False
    return ui


def _make_jarvis(wake_enabled: bool) -> JarvisLive:
    with patch("main.get_wake_word_enabled", return_value=wake_enabled):
        return JarvisLive(ui=_fake_ui())


class WakeWordEnabledFalseIsTheFailsafeTests(unittest.TestCase):
    """Section 11 of the task: WAKE_WORD_ENABLED=false must reproduce the exact
    previous always-on behaviour - the safe fallback if wake word misbehaves."""

    def test_awake_is_true_from_construction_when_wake_word_disabled(self):
        j = _make_jarvis(wake_enabled=False)
        self.assertTrue(j._awake)
        self.assertFalse(j._wake_enabled)

    def test_run_sleep_watch_never_sleeps_jarvis_when_wake_word_is_disabled(self):
        j = _make_jarvis(wake_enabled=False)
        j._wake_sleep_timeout = 0.01
        j._last_user_speech = time.monotonic() - 999  # ancient, would trigger sleep if wake word were on

        async def _drive():
            task = asyncio.create_task(j._run_sleep_watch())
            await _real_asyncio_sleep(0.05)  # NOT asyncio.sleep - that name is patched for the duration below
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch("main.asyncio.sleep", new=AsyncSleepStub(0.01).sleep):
            asyncio.run(_drive())
        self.assertTrue(j._awake, "disabled wake word must never auto-sleep JARVIS")


class AsyncSleepStub:
    """Replaces the sleep-watch loop's fixed 5s poll interval with something a
    test can afford to wait out, WITHOUT touching the production 5s constant
    itself (that value is deliberately unit-test-independent)."""

    def __init__(self, seconds: float):
        self._seconds = seconds

    async def sleep(self, _ignored_seconds: float) -> None:
        await _real_asyncio_sleep(self._seconds)


class WakeSleepStateMachineTests(unittest.TestCase):
    """wake()/sleep()/_on_wake_detected() - the actual state transitions, run
    directly against the real methods (no mic, no network involved here)."""

    def test_starts_asleep_when_wake_word_enabled(self):
        j = _make_jarvis(wake_enabled=True)
        self.assertFalse(j._awake)

    def test_on_wake_detected_wakes_jarvis_and_updates_ui(self):
        j = _make_jarvis(wake_enabled=True)
        self.assertFalse(j._awake)
        j._on_wake_detected()
        self.assertTrue(j._awake)
        j.ui.set_state.assert_any_call("LISTENING")
        self.assertTrue(any("Awake" in str(c) for c in j.ui.write_log.call_args_list))

    def test_wake_while_already_awake_is_a_pure_noop(self):
        """No double 'wake' handling if the detector (or a stray UI click) fires
        again while JARVIS is already listening - directly protects against a
        second, redundant Gemini/session-affecting action."""
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="first")
        j.ui.reset_mock()
        first_speech_ts = j._last_user_speech
        j.wake(reason="second")
        self.assertEqual(j.ui.set_state.call_count, 0, "waking an already-awake JARVIS must not touch the UI again")
        self.assertEqual(j._last_user_speech, first_speech_ts)

    def test_multiple_separate_wake_detections_each_wake_from_asleep(self):
        j = _make_jarvis(wake_enabled=True)
        j._on_wake_detected()
        self.assertTrue(j._awake)
        j.sleep(reason="test")
        self.assertFalse(j._awake)
        j._on_wake_detected()
        self.assertTrue(j._awake, "a second, separate wake word must wake JARVIS again after it slept")

    def test_sleep_while_already_asleep_is_a_pure_noop(self):
        j = _make_jarvis(wake_enabled=True)
        self.assertFalse(j._awake)
        j.ui.reset_mock()
        j.sleep(reason="redundant")
        self.assertEqual(j.ui.set_state.call_count, 0)

    def test_sleep_clears_speaking_flag_and_updates_ui(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j.set_speaking(True)
        j.sleep(reason="test")
        self.assertFalse(j._awake)
        with j._speaking_lock:
            self.assertFalse(j._is_speaking)
        j.ui.set_state.assert_any_call("SLEEPING")

    def test_manual_ui_toggle_mirrors_wake_and_sleep(self):
        j = _make_jarvis(wake_enabled=True)
        j._ui_wake_manual()
        self.assertTrue(j._awake)
        j._ui_wake_manual()
        self.assertFalse(j._awake)

    def test_manual_toggle_is_a_noop_when_wake_word_disabled(self):
        j = _make_jarvis(wake_enabled=False)
        self.assertTrue(j._awake)
        j._ui_wake_manual()
        self.assertTrue(j._awake, "manual sleep/wake button must do nothing outside wake-word mode")


class SleepWatchAutoTimeoutTests(unittest.TestCase):
    """_run_sleep_watch(): the configurable idle timeout - must fire, but never
    while JARVIS is speaking, matching the task's explicit 'no aggressive
    shutdown mid-answer' requirement."""

    def _drive_sleep_watch(self, jarvis: JarvisLive, poll_seconds: float = 0.02, drive_seconds: float = 0.12):
        async def _drive():
            task = asyncio.create_task(jarvis._run_sleep_watch())
            await _real_asyncio_sleep(drive_seconds)  # NOT asyncio.sleep - that name is patched for the duration below
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch("main.asyncio.sleep", new=AsyncSleepStub(poll_seconds).sleep):
            asyncio.run(_drive())

    def test_auto_sleeps_after_the_configured_idle_timeout(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._wake_sleep_timeout = 0.001
        j._last_user_speech = time.monotonic() - 999
        self._drive_sleep_watch(j)
        self.assertFalse(j._awake, "JARVIS must auto-sleep once the idle timeout has elapsed")

    def test_does_not_sleep_while_jarvis_is_still_speaking(self):
        """The core 'no aggressive shutdown mid-answer' guarantee: a long answer
        must not be cut off just because the user has been silent (listening)."""
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j.set_speaking(True)
        j._wake_sleep_timeout = 0.001
        j._last_user_speech = time.monotonic() - 999
        self._drive_sleep_watch(j)
        self.assertTrue(j._awake, "JARVIS must not auto-sleep while it is actively speaking")

    def test_does_not_sleep_before_the_timeout_has_actually_elapsed(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._wake_sleep_timeout = 999  # effectively "never" for the duration of this test
        j._last_user_speech = time.monotonic()
        self._drive_sleep_watch(j)
        self.assertTrue(j._awake, "JARVIS must stay awake before its idle timeout elapses")


class PostResponseImmediateSleepTests(unittest.TestCase):
    """_schedule_post_response_sleep()/_cancel_pending_post_response_sleep() -
    real-user feedback after the first hardware test: a multi-minute open
    follow-up window was not wanted, every action should need a fresh
    'Hey Jarvis'. This is the mechanism that actually drives that now (the
    idle-timeout above is only the backstop)."""

    def test_sleeps_shortly_after_a_completed_response(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._post_response_sleep_delay = 0.03

        async def _drive():
            j._schedule_post_response_sleep()
            await j._post_response_sleep_task

        asyncio.run(_drive())
        self.assertFalse(j._awake, "JARVIS must return to standby shortly after a completed response")

    def test_does_not_sleep_while_still_speaking_when_the_timer_fires(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j.set_speaking(True)
        j._post_response_sleep_delay = 0.03

        async def _drive():
            j._schedule_post_response_sleep()
            await j._post_response_sleep_task

        asyncio.run(_drive())
        self.assertTrue(j._awake, "must not sleep while JARVIS is still actively speaking when the timer fires")

    def test_new_user_speech_cancels_the_pending_sleep(self):
        """A follow-up utterance that starts within the grace window must not
        have its own mic cut off mid-sentence by a stale scheduled sleep."""
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._post_response_sleep_delay = 0.05

        async def _drive():
            j._schedule_post_response_sleep()
            await _real_asyncio_sleep(0.01)
            j._cancel_pending_post_response_sleep()
            await _real_asyncio_sleep(0.08)  # well past the original delay

        asyncio.run(_drive())
        self.assertTrue(j._awake, "a cancelled sleep must not fire anyway")

    def test_rescheduling_replaces_the_previous_timer_not_stacks_it(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._post_response_sleep_delay = 0.05

        async def _drive():
            j._schedule_post_response_sleep()
            first_task = j._post_response_sleep_task
            await _real_asyncio_sleep(0.01)
            j._schedule_post_response_sleep()  # e.g. the vision-answer turn_complete
            await _real_asyncio_sleep(0)  # let the event loop actually process the cancel() request
            self.assertTrue(first_task.cancelled() or first_task.done())
            await j._post_response_sleep_task

        asyncio.run(_drive())
        self.assertFalse(j._awake)

    def test_is_a_noop_when_wake_word_disabled(self):
        j = _make_jarvis(wake_enabled=False)
        j._post_response_sleep_delay = 0.02
        j._schedule_post_response_sleep()
        self.assertIsNone(j._post_response_sleep_task, "must not schedule anything outside wake-word mode")

    def test_is_a_noop_when_already_asleep(self):
        j = _make_jarvis(wake_enabled=True)
        self.assertFalse(j._awake)
        j._schedule_post_response_sleep()
        self.assertIsNone(j._post_response_sleep_task)

    def test_play_audio_schedules_sleep_only_once_playback_genuinely_finishes(self):
        """The exact integration point a real hardware test caught as wrong the
        first time: turn_complete alone is NOT when a response is actually
        delivered - _play_audio()'s own empty-queue+turn-done detection (the
        real 'finished speaking' signal) is what must trigger this, not the
        raw Gemini turn_complete event, which can fire before playback of a
        longer response has even started."""
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._post_response_sleep_delay = 0.02
        j.audio_in_queue = asyncio.Queue()
        j._turn_done_event = asyncio.Event()

        fake_stream = MagicMock()

        async def _drive():
            with patch("main.sd.RawOutputStream", return_value=fake_stream), \
                 patch("main.get_output_device", return_value=None), \
                 patch.object(jarvis_main.audio_devices, "resolve", return_value=None):
                task = asyncio.create_task(j._play_audio())
                # A response is "in flight": queue has audio, turn not done yet.
                await j.audio_in_queue.put(b"\x00\x01" * 100)
                await _real_asyncio_sleep(0.05)
                self.assertIsNone(j._post_response_sleep_task, "must not schedule while a response is still being delivered")

                # Now the response completes: Gemini signals turn_complete, and
                # the queue drains (nothing left to play).
                j._turn_done_event.set()
                await _real_asyncio_sleep(0.25)  # >0.1s poll interval + grace delay

                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_drive())
        self.assertFalse(j._awake, "JARVIS must return to standby once playback of the response has actually finished")

    def test_does_not_schedule_while_a_tool_call_followup_is_still_expected(self):
        """Real hardware test #2's actual bug: a vision/tool-call round-trip has
        an intermediate 'turn done, queue empty' moment (the tool-ack) well
        before the real follow-up answer has even been sent. _awaiting_followup_turn
        (set wherever response.tool_call/a vision send happens in _receive_audio)
        must suppress scheduling at that intermediate point."""
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j._post_response_sleep_delay = 0.02
        j.audio_in_queue = asyncio.Queue()
        j._turn_done_event = asyncio.Event()
        j._awaiting_followup_turn = True  # simulates: response.tool_call was just seen

        fake_stream = MagicMock()

        async def _drive():
            with patch("main.sd.RawOutputStream", return_value=fake_stream), \
                 patch("main.get_output_device", return_value=None), \
                 patch.object(jarvis_main.audio_devices, "resolve", return_value=None):
                task = asyncio.create_task(j._play_audio())
                await j.audio_in_queue.put(b"\x00\x01" * 100)
                await _real_asyncio_sleep(0.05)
                # The tool-ack "turn" completes - but a follow-up is expected.
                j._turn_done_event.set()
                await _real_asyncio_sleep(0.15)
                self.assertIsNone(j._post_response_sleep_task, "must not schedule while a follow-up turn is still expected")
                self.assertTrue(j._awake, "must still be awake - the real answer has not played yet")

                # The real answer now arrives and plays out; _receive_audio()
                # would have cleared the flag once that turn_complete fires.
                j._awaiting_followup_turn = False
                await j.audio_in_queue.put(b"\x00\x01" * 100)
                await _real_asyncio_sleep(0.05)
                j._turn_done_event.set()
                await _real_asyncio_sleep(0.15)

                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_drive())
        self.assertFalse(j._awake, "JARVIS must sleep once the REAL follow-up answer has finished playing")


class MicCallbackGatingTests(unittest.TestCase):
    """The actual audio-path gate inside _listen_audio()'s callback - sounddevice
    itself is mocked (real hardware), everything else is the real method."""

    def _run_listen_audio_and_feed_one_frame(self, jarvis: JarvisLive):
        """Runs the REAL _listen_audio(), captures its mic callback, and invokes
        that callback with one synthetic frame WHILE the same event loop that
        _listen_audio() itself is running on is still alive - the callback
        closes over that loop (`loop = asyncio.get_event_loop()` inside
        _listen_audio itself) via `loop.call_soon_threadsafe(...)`, so it must
        be exercised from within this same asyncio.run(), not after it returns."""
        captured = {}

        def fake_input_stream(**kwargs):
            captured["callback"] = kwargs["callback"]
            return MagicMock()  # MagicMock supports __enter__/__exit__ out of the box

        async def _drive():
            with patch("main.sd.InputStream", side_effect=fake_input_stream), \
                 patch("main.get_input_device", return_value=None), \
                 patch.object(jarvis_main.audio_devices, "resolve", return_value=None):
                task = asyncio.create_task(jarvis._listen_audio())
                await _real_asyncio_sleep(0.05)
                self.assertIn("callback", captured, "_listen_audio() never reached sd.InputStream(...)")
                captured["callback"](self._fake_frame(), 1280, None, None)
                # Let call_soon_threadsafe's scheduled out_queue.put_nowait actually run.
                await _real_asyncio_sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_drive())

    def _fake_frame(self):
        import numpy as np
        return np.zeros((1280, 1), dtype="int16")

    def test_asleep_frames_go_only_to_the_local_detector_never_to_gemini(self):
        j = _make_jarvis(wake_enabled=True)
        self.assertFalse(j._awake)
        j.out_queue = MagicMock()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        j._wake_detector.feed.assert_called_once()
        j.out_queue.put_nowait.assert_not_called()

    def test_asleep_frames_still_go_only_to_detector_well_after_the_cooldown(self):
        """Sanity check that the cooldown is bounded, not a permanent gate -
        asleep=True + cooldown already elapsed must still feed the detector."""
        j = _make_jarvis(wake_enabled=True)
        j._asleep_since = time.monotonic() - 999
        j.out_queue = MagicMock()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        j._wake_detector.feed.assert_called_once()

    def test_no_detector_feed_during_the_cooldown_right_after_falling_asleep(self):
        """Real hardware test #3: JARVIS's own trailing voice/echo produced
        four rapid, high-confidence false 'Hey Jarvis' detections right after
        a real reply, because the detector started listening again the instant
        sleep() fired - before the speaker had necessarily finished draining."""
        j = _make_jarvis(wake_enabled=True)
        j._asleep_since = time.monotonic()  # sleep() "just" fired
        j._detector_cooldown_seconds = 10.0  # generous, so the test can't flake on timing
        j.out_queue = MagicMock()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        j._wake_detector.feed.assert_not_called()
        j.out_queue.put_nowait.assert_not_called()

    def test_sleep_resets_the_cooldown_window(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        before = time.monotonic()
        j.sleep(reason="test")
        self.assertGreaterEqual(j._asleep_since, before)

    def test_awake_frames_go_to_gemini_out_queue(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j.out_queue = asyncio.Queue()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        self.assertFalse(j.out_queue.empty(), "audio must reach Gemini's send queue once JARVIS is awake")
        j._wake_detector.feed.assert_not_called()

    def test_wake_word_disabled_frames_always_go_to_gemini_no_detector_involved(self):
        """Default behaviour (wake word off) must be pixel-for-pixel the old
        always-on path - the detector must never even be consulted."""
        j = _make_jarvis(wake_enabled=False)
        self.assertTrue(j._awake)
        j.out_queue = asyncio.Queue()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        self.assertFalse(j.out_queue.empty())
        j._wake_detector.feed.assert_not_called()

    def test_muted_awake_frames_are_dropped_not_sent_to_gemini_or_detector(self):
        j = _make_jarvis(wake_enabled=True)
        j.wake(reason="test")
        j.ui.muted = True
        j.out_queue = asyncio.Queue()
        j._wake_detector = MagicMock()

        self._run_listen_audio_and_feed_one_frame(j)

        self.assertTrue(j.out_queue.empty())
        j._wake_detector.feed.assert_not_called()

    def test_mic_open_failure_on_chosen_device_falls_back_to_default_not_silence(self):
        """Device unavailable (unplugged headset etc.) must fall back to the
        system default mic, never leave JARVIS unable to hear at all."""
        j = _make_jarvis(wake_enabled=False)
        attempts = []

        def flaky_input_stream(**kwargs):
            attempts.append(kwargs.get("device"))
            if len(attempts) == 1:
                raise OSError("device unavailable")
            return MagicMock()

        async def _drive():
            with patch("main.sd.InputStream", side_effect=flaky_input_stream), \
                 patch("main.get_input_device", return_value="Unplugged Headset"), \
                 patch.object(jarvis_main.audio_devices, "resolve", return_value="some-device-id"):
                task = asyncio.create_task(j._listen_audio())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_drive())
        self.assertEqual(len(attempts), 2, "must retry once with the default device after the chosen one fails")
        self.assertIsNone(attempts[1], "the retry must use the system default (device=None)")


if __name__ == "__main__":
    unittest.main()
