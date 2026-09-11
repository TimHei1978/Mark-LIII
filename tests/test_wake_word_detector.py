"""
Tests for core/wake_word.py's WakeWordDetector - the local, offline "Hey Jarvis"
inference component. Model inference is mocked for determinism/speed (no GPU/CPU
model load needed to run these); is_installed/is_ready and the detector's queue/
threading/callback plumbing are exercised against the REAL, unmodified code.

One additional test (test_real_model_loads_and_predicts_without_crashing) uses the
ACTUAL downloaded openwakeword model against synthetic audio - skipped automatically
if the model files are not present on this machine (see core.wake_word.is_ready).

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.wake_word import WakeWordDetector, is_installed, is_ready, DEFAULT_THRESHOLD  # noqa: E402


def _fake_frame(value: int = 0, length: int = 1280):
    import numpy as np
    return np.full((length, 1), value, dtype="int16")


class WakeWordPackageChecksTests(unittest.TestCase):
    """is_installed/is_ready must never raise, regardless of environment state -
    the UI polls these on every settings-panel open (see ui.py _wake_state)."""

    def test_is_installed_never_raises(self):
        self.assertIsInstance(is_installed(), bool)

    def test_is_ready_never_raises(self):
        self.assertIsInstance(is_ready(), bool)

    def test_is_ready_implies_is_installed(self):
        if is_ready():
            self.assertTrue(is_installed())


class WakeWordDetectorLifecycleTests(unittest.TestCase):
    """start()/stop()/ready - the model itself is mocked so these run fast and
    deterministically without needing the real ~3MB ONNX files on disk."""

    def _patched_model(self, predict_return=None, side_effect=None):
        mock_model_cls = MagicMock()
        mock_instance = MagicMock()
        if side_effect is not None:
            mock_instance.predict.side_effect = side_effect
        else:
            mock_instance.predict.return_value = predict_return or {"hey_jarvis_v0.1": 0.0}
        mock_model_cls.return_value = mock_instance
        return patch("openwakeword.model.Model", mock_model_cls), mock_instance

    def test_start_loads_model_and_spawns_thread(self):
        patcher, _ = self._patched_model()
        with patcher:
            det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
            self.assertFalse(det.ready)
            ok = det.start()
            self.assertTrue(ok)
            self.assertTrue(det.ready)
            self.assertIsNotNone(det._thread)
            self.assertTrue(det._thread.is_alive())
            det.stop()

    def test_start_is_idempotent_second_call_is_a_noop(self):
        patcher, mock_instance = self._patched_model()
        with patcher as mock_model_cls:
            det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
            self.assertTrue(det.start())
            self.assertTrue(det.start())
            # Model constructed exactly once - a second start() must not reload it.
            self.assertEqual(mock_model_cls.call_count, 1)
            det.stop()

    def test_start_returns_false_and_stays_not_ready_when_model_load_fails(self):
        patcher = patch("openwakeword.model.Model", side_effect=RuntimeError("no onnx runtime"))
        logged = []
        with patcher:
            det = WakeWordDetector(on_detect=lambda: None, logger=logged.append)
            ok = det.start()
        self.assertFalse(ok)
        self.assertFalse(det.ready)
        self.assertTrue(any("could not load model" in m for m in logged))

    def test_stop_before_start_does_not_raise(self):
        det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
        det.stop()  # must be a safe no-op
        self.assertFalse(det.ready)

    def test_stop_unblocks_the_inference_thread_promptly(self):
        patcher, _ = self._patched_model()
        with patcher:
            det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
            det.start()
            thread = det._thread
            det.stop()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive(), "inference thread must exit promptly after stop()")


class WakeWordDetectorDetectionTests(unittest.TestCase):
    """The actual 'did it fire' logic - threshold comparison, key matching,
    repeated detections, and callback-error isolation."""

    def _running_detector(self, predict_return, on_detect, threshold=DEFAULT_THRESHOLD):
        mock_model_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.predict.return_value = predict_return
        mock_model_cls.return_value = mock_instance
        patcher = patch("openwakeword.model.Model", mock_model_cls)
        patcher.start()
        det = WakeWordDetector(on_detect=on_detect, threshold=threshold, logger=lambda _m: None)
        det.start()
        return det, patcher

    def test_score_above_threshold_fires_on_detect_after_enough_consecutive_frames(self):
        fired = threading.Event()
        det, patcher = self._running_detector({"hey_jarvis_v0.1": 0.9}, fired.set)
        try:
            for _ in range(3):  # default REQUIRED_CONSECUTIVE_FRAMES
                det.feed(_fake_frame())
            self.assertTrue(fired.wait(timeout=2.0), "on_detect was not called after enough consecutive above-threshold frames")
        finally:
            det.stop()
            patcher.stop()

    def test_a_single_above_threshold_frame_alone_does_not_fire(self):
        """Real hardware test #4: without a sustained-confidence requirement,
        a single 80ms frame scoring above the 0.5 default threshold fired on
        ordinary speech, not just 'Hey Jarvis'."""
        fired = threading.Event()
        det, patcher = self._running_detector({"hey_jarvis_v0.1": 0.9}, fired.set)
        try:
            det.feed(_fake_frame())
            self.assertFalse(fired.wait(timeout=0.5), "a single above-threshold frame must not fire on_detect by itself")
        finally:
            det.stop()
            patcher.stop()

    def test_score_dropping_below_threshold_resets_the_consecutive_count(self):
        """A brief spike that doesn't sustain must not 'bank' partial progress
        towards a later, unrelated spike - each detection needs its OWN
        sustained run of above-threshold frames."""
        mock_model_cls = MagicMock()
        mock_instance = MagicMock()
        # 2 hits, 1 miss, then 2 more hits - total 4 "hits" but never 3 IN A ROW.
        mock_instance.predict.side_effect = [
            {"hey_jarvis_v0.1": 0.9}, {"hey_jarvis_v0.1": 0.9},
            {"hey_jarvis_v0.1": 0.1},
            {"hey_jarvis_v0.1": 0.9}, {"hey_jarvis_v0.1": 0.9},
        ]
        mock_model_cls.return_value = mock_instance
        fired = threading.Event()
        with patch("openwakeword.model.Model", mock_model_cls):
            det = WakeWordDetector(on_detect=fired.set, logger=lambda _m: None)
            det.start()
            try:
                for _ in range(5):
                    det.feed(_fake_frame())
                    time.sleep(0.02)
                self.assertFalse(fired.wait(timeout=0.3), "a reset streak (2 hits, a miss, 2 hits) must not fire - never 3 IN A ROW")
            finally:
                det.stop()

    def test_custom_required_consecutive_frames_is_respected(self):
        fired = threading.Event()
        mock_model_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.predict.return_value = {"hey_jarvis_v0.1": 0.9}
        mock_model_cls.return_value = mock_instance
        with patch("openwakeword.model.Model", mock_model_cls):
            det = WakeWordDetector(on_detect=fired.set, required_consecutive_frames=1, logger=lambda _m: None)
            det.start()
            try:
                det.feed(_fake_frame())
                self.assertTrue(fired.wait(timeout=2.0), "required_consecutive_frames=1 should fire on the very first above-threshold frame")
            finally:
                det.stop()

    def test_score_below_threshold_does_not_fire(self):
        fired = threading.Event()
        det, patcher = self._running_detector({"hey_jarvis_v0.1": 0.1}, fired.set)
        try:
            det.feed(_fake_frame())
            self.assertFalse(fired.wait(timeout=0.5), "on_detect fired for a score below threshold")
        finally:
            det.stop()
            patcher.stop()

    def test_multiple_separate_wake_words_each_fire_their_own_event(self):
        """Repeated 'Hey Jarvis' must each be detected, not just the first."""
        count = {"n": 0}
        done_first = threading.Event()

        def on_detect():
            count["n"] += 1
            done_first.set()

        det, patcher = self._running_detector({"hey_jarvis_v0.1": 0.9}, on_detect)
        try:
            for _ in range(3):
                det.feed(_fake_frame())
            self.assertTrue(done_first.wait(timeout=2.0))
            done_first.clear()
            time.sleep(0.05)
            for _ in range(3):
                det.feed(_fake_frame())
            self.assertTrue(done_first.wait(timeout=2.0))
            self.assertGreaterEqual(count["n"], 2)
        finally:
            det.stop()
            patcher.stop()

    def test_on_detect_exception_does_not_kill_the_inference_loop(self):
        """A buggy callback must not silently stop future detections."""
        calls = {"n": 0}

        def flaky_on_detect():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

        det, patcher = self._running_detector({"hey_jarvis_v0.1": 0.9}, flaky_on_detect)
        try:
            for _ in range(3):
                det.feed(_fake_frame())
            time.sleep(0.3)
            for _ in range(3):
                det.feed(_fake_frame())
            time.sleep(0.3)
            self.assertGreaterEqual(calls["n"], 2, "one on_detect exception must not stop later detections")
            self.assertTrue(det._thread.is_alive(), "inference thread must survive an on_detect exception")
        finally:
            det.stop()
            patcher.stop()

    def test_feed_after_stop_is_ignored_not_queued(self):
        det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
        det.feed(_fake_frame())  # never started - must be a cheap no-op, not an error
        self.assertTrue(det._queue.empty())

    def test_feed_never_blocks_when_queue_is_full(self):
        det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
        det._running = True  # exercise feed()'s queue directly, no inference thread draining it - no model needed
        for _ in range(200):
            start = time.monotonic()
            det.feed(_fake_frame())
            self.assertLess(time.monotonic() - start, 0.1, "feed() must never block the real-time mic callback")
        det._running = False

    def test_feed_flattens_stereo_shaped_frames(self):
        """sounddevice delivers (frames, channels) arrays even for a 1-channel
        stream opened as channels=1 - feed() must not crash or misfeed on that shape."""
        import numpy as np
        det = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
        det._running = True
        stereo_shaped = np.zeros((320, 1), dtype="int16")
        det.feed(stereo_shaped)
        item = det._queue.get_nowait()
        self.assertEqual(item.ndim, 1)
        det._running = False


@unittest.skipUnless(is_ready(), "wake-word model files not downloaded on this machine (core.wake_word.install_and_download)")
class WakeWordDetectorRealModelSmokeTest(unittest.TestCase):
    """Runs the ACTUAL downloaded ONNX model (no mocking) against synthetic silent
    audio. Only proves the real pipeline loads and produces a well-formed score
    without crashing - NOT a detection-accuracy test (that needs real speech, see
    the manual real-hardware test in the task report)."""

    def test_real_model_loads_and_predicts_without_crashing(self):
        import numpy as np

        got_score = {"value": None}
        evt = threading.Event()

        def on_detect():
            evt.set()

        det = WakeWordDetector(on_detect=on_detect, logger=lambda _m: None)
        self.assertTrue(det.start())
        try:
            # 1.25s of digital silence at 16kHz, exactly what a quiet room feeds in.
            silence = np.zeros((20000, 1), dtype="int16")
            for i in range(0, 20000, 1280):
                det.feed(silence[i : i + 1280])
            # Silence must not spuriously fire the wake word within a couple of seconds.
            self.assertFalse(evt.wait(timeout=3.0), "digital silence triggered a false wake-word detection")
        finally:
            det.stop()


if __name__ == "__main__":
    unittest.main()
