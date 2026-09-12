"""
Unit tests for dashboard/server.py's HTTPS self-test / fallback logic.

Real-hardware root cause this guards against: local security software
(consumer AV HTTPS/SSL-scanning) intercepts and resets self-signed TLS
connections on this machine, even on 127.0.0.1 - invisible to a pure Python
ssl-module client (curl.exe/Windows SChannel-based clients see it, Python's
own OpenSSL-based client does not), which previously left the dashboard
advertising a QR code and a manual-entry address that neither actually
worked. See HANDOVER.md / Obsidian COMPONENTS/jarvis-remote-control.md for
the full real-hardware diagnostic trail.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import dashboard.server as dashboard_server  # noqa: E402
from dashboard.server import DashboardServer, PORT  # noqa: E402


def _make_dashboard() -> DashboardServer:
    d = DashboardServer()
    d._ip = "192.168.178.122"  # deterministic for assertions; the real
    # value is exercised separately by LocalIpTests below.
    return d


class SingleSourceOfTruthTests(unittest.TestCase):
    """get_url()/get_manual_url() must always agree on which port is real -
    the original bug (UI showed :8001, QR code pointed at :8000, neither
    worked) came from _ssl_enabled() trusting cert-file presence instead of a
    real, tested result."""

    def test_before_any_self_test_https_verified_is_unset(self):
        d = _make_dashboard()
        self.assertIsNone(d._https_verified)

    def test_verified_true_uses_https_and_the_dedicated_alias_port(self):
        d = _make_dashboard()
        d._https_verified = True
        self.assertEqual(d.get_url(), f"https://{d._ip}:{PORT}")
        self.assertEqual(d.get_manual_url(), f"{d._ip}:{PORT + 1}")

    def test_verified_false_uses_plain_http_and_the_same_port_everywhere(self):
        d = _make_dashboard()
        d._https_verified = False
        self.assertEqual(d.get_url(), f"http://{d._ip}:{PORT}")
        self.assertEqual(d.get_manual_url(), f"{d._ip}:{PORT}")
        # the actual regression: QR (get_url) and manual entry (get_manual_url)
        # must reference the identical port when not on HTTPS - no second,
        # independently-hardcoded port value anywhere in this path.
        self.assertIn(f":{PORT}", d.get_url())
        self.assertIn(f":{PORT}", d.get_manual_url())

    def test_falls_back_to_file_existence_check_before_the_real_test_has_run(self):
        d = _make_dashboard()
        # no assertion on the specific boolean (depends on this machine's
        # config/certs/ state) - only that it never raises and returns a bool
        # when _https_verified is still None (pre-serve() call).
        self.assertIsInstance(d._ssl_enabled(), bool)


class VerifyHttpsSelfTestTests(unittest.IsolatedAsyncioTestCase):
    """_verify_https() is what actually distinguishes 'cert files exist' from
    'HTTPS really works on this machine'. It must prefer curl.exe (the tool
    that reproduces the real local-AV interception; a pure Python ssl-module
    probe does not, since it bypasses Windows SChannel entirely) and must
    never raise regardless of what the subprocess does."""

    async def test_curl_http_200_reports_verified(self):
        d = _make_dashboard()
        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(return_value=(b"200", b""))
        with patch.object(dashboard_server.shutil, "which", return_value="curl.exe"), \
             patch.object(dashboard_server.asyncio, "create_subprocess_exec",
                           AsyncMock(return_value=fake_proc)):
            self.assertTrue(await d._verify_https())

    async def test_curl_http_000_reports_not_verified(self):
        """HTTP 000 is curl's own code for 'connected but got no real response' -
        exactly the reset-after-handshake signature the local AV interception
        produces."""
        d = _make_dashboard()
        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(return_value=(b"000", b""))
        with patch.object(dashboard_server.shutil, "which", return_value="curl.exe"), \
             patch.object(dashboard_server.asyncio, "create_subprocess_exec",
                           AsyncMock(return_value=fake_proc)):
            self.assertFalse(await d._verify_https())

    async def test_curl_subprocess_failure_reports_not_verified_never_raises(self):
        d = _make_dashboard()
        with patch.object(dashboard_server.shutil, "which", return_value="curl.exe"), \
             patch.object(dashboard_server.asyncio, "create_subprocess_exec",
                           AsyncMock(side_effect=OSError("spawn failed"))):
            self.assertFalse(await d._verify_https())

    async def test_curl_missing_falls_back_to_python_ssl_probe_without_raising(self):
        d = _make_dashboard()
        with patch.object(dashboard_server.shutil, "which", return_value=None):
            # No real server listening on PORT in this test process - the
            # fallback probe must fail closed (False), not raise.
            self.assertFalse(await d._verify_https())


class AutoLoginPairingKeyTests(unittest.TestCase):
    """The QR code's actual target - validates the whole visible bug is fixed
    end-to-end at the route level, not just in the URL-string helpers.

    /auto-login also writes a trusted-device record to disk (see
    tests/test_remote_control_sessions.py for that feature's own coverage) -
    DEVICE_SESSIONS_PATH is redirected to a throwaway file for every test
    here so this file never touches the real, local
    config/remote_devices.json on the machine running the suite."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self._device_store_patcher = patch.object(
            dashboard_server, "DEVICE_SESSIONS_PATH",
            Path(self._tmpdir.name) / "remote_devices.json",
        )
        self._device_store_patcher.start()
        self.addCleanup(self._device_store_patcher.stop)
        self.addCleanup(self._tmpdir.cleanup)
        self.d = _make_dashboard()
        self.client = TestClient(self.d.app)

    def test_valid_key_is_accepted(self):
        key = self.d.new_key()
        resp = self.client.get(f"/auto-login?key={key}")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Link Expired", resp.text)
        self.assertIn("Connecting to JARVIS", resp.text)

    def test_key_is_single_use(self):
        key = self.d.new_key()
        self.client.get(f"/auto-login?key={key}")
        second = self.client.get(f"/auto-login?key={key}")
        self.assertIn("Link Expired", second.text)

    def test_expired_key_is_rejected(self):
        key = self.d.new_key(expiry_secs=600)
        self.d._pending_keys[key] = time.time() - 1  # force it into the past
        resp = self.client.get(f"/auto-login?key={key}")
        self.assertIn("Link Expired", resp.text)

    def test_unknown_key_is_rejected(self):
        resp = self.client.get("/auto-login?key=ZZZZZZ")
        self.assertIn("Link Expired", resp.text)

    def test_missing_key_is_rejected(self):
        resp = self.client.get("/auto-login")
        self.assertIn("Link Expired", resp.text)

    def test_new_key_is_never_printed_to_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            key = self.d.new_key()
        self.assertNotIn(key, buf.getvalue())


class LocalIpTests(unittest.TestCase):
    def test_local_ip_never_raises_and_returns_an_ipv4_looking_string(self):
        ip = dashboard_server._local_ip()
        parts = ip.split(".")
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))

    def test_local_ip_is_not_a_loopback_address_when_a_real_adapter_exists(self):
        # On any machine with a real network adapter (the normal case this
        # feature exists for), _local_ip() must not silently settle for the
        # loopback address - that would make LAN/phone access impossible.
        ip = dashboard_server._local_ip()
        self.assertFalse(ip.startswith("127."))


class ListenBindingTests(unittest.TestCase):
    def test_serve_binds_0_0_0_0_not_localhost_only(self):
        """Regression guard: LAN remote control requires binding all
        interfaces, not just the loopback interface. Source-level check
        (rather than actually starting a server) keeps this test fast and
        network-free while still catching an accidental revert to
        '127.0.0.1' in either the primary or alias server."""
        import inspect
        source = inspect.getsource(dashboard_server.DashboardServer.serve) + \
            inspect.getsource(dashboard_server.DashboardServer._serve_alias)
        self.assertIn('host="0.0.0.0"', source)
        self.assertNotIn('host="127.0.0.1"', source)
        self.assertNotIn("host='127.0.0.1'", source)


if __name__ == "__main__":
    unittest.main()
