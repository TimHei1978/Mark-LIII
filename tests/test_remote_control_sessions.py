"""
Unit tests for the Remote Control dashboard's trusted-device persistence
(the "already-paired iPhone shouldn't need a new QR code after every JARVIS
restart" fix).

Root cause this guards against, confirmed in the code's own pre-existing
comment (dashboard/static/login.html): the auto-reconnect flow using a
device token stored in the phone's localStorage already existed, but the
server-side _device_sessions store backing it was RAM-only, so a restart
silently forgot every paired phone - the client-side code even said so:
"Device token rejected (server restarted) — clear it and show normal form".
The pairing key (_pending_keys) and live session token (_tokens) are
correctly RAM-only and short-lived by design and are NOT touched here.

Run with (from Mark-LII/, inside the project's own .venv):
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import dashboard.server as dashboard_server  # noqa: E402
from dashboard.server import DashboardServer  # noqa: E402


class _TempDeviceStore:
    """Points DEVICE_SESSIONS_PATH at a throwaway file for the duration of a
    test, so tests never read or write the developer's real, local
    config/remote_devices.json."""

    def __enter__(self):
        self._tmpdir = TemporaryDirectory()
        self._path = Path(self._tmpdir.name) / "remote_devices.json"
        self._patcher = patch.object(dashboard_server, "DEVICE_SESSIONS_PATH", self._path)
        self._patcher.start()
        return self._path

    def __exit__(self, *exc):
        self._patcher.stop()
        self._tmpdir.cleanup()


def _pair_a_device(client: TestClient, dashboard: DashboardServer) -> tuple[str, str]:
    """Runs a real /auto-login and returns (auth_token, device_token) parsed
    out of the HTML response the same way the phone's JS would."""
    key = dashboard.new_key()
    resp = client.get(f"/auto-login?key={key}")
    text = resp.text
    tok = text.split("jarvis_token','")[1].split("'")[0]
    dev_tok = text.split("jarvis_device_token','")[1].split("'")[0]
    return tok, dev_tok


class PersistsAcrossRestartTests(unittest.TestCase):
    """The actual acceptance criterion: pair once, simulate a full process
    restart (a brand-new DashboardServer instance, exactly like a real JARVIS
    restart creates), and confirm the SAME device token still works —
    without a new QR code or pairing key."""

    def test_device_session_survives_a_simulated_restart(self):
        with _TempDeviceStore():
            d1 = DashboardServer()
            client1 = TestClient(d1.app)
            _tok, dev_tok = _pair_a_device(client1, d1)

            # A real restart: a brand new instance, loading from the same
            # on-disk store — not the same Python object, no shared state
            # except what was actually written to disk.
            d2 = DashboardServer()
            client2 = TestClient(d2.app)
            resp = client2.post("/api/device-login", json={"device_token": dev_tok})

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["ok"])
            self.assertIn("token", body)

    def test_fresh_token_from_restarted_instance_authenticates_protected_route(self):
        with _TempDeviceStore():
            d1 = DashboardServer()
            _tok, dev_tok = _pair_a_device(TestClient(d1.app), d1)

            d2 = DashboardServer()
            client2 = TestClient(d2.app)
            login = client2.post("/api/device-login", json={"device_token": dev_tok}).json()
            fresh_tok = login["token"]

            resp = client2.post(
                "/api/command",
                json={"text": "hello"},
                headers={"Authorization": f"Bearer {fresh_tok}"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])

    def test_unknown_device_token_after_restart_is_rejected(self):
        """A device that was never actually paired must not be trusted just
        because SOME store file happens to exist."""
        with _TempDeviceStore():
            DashboardServer()  # creates an empty store on disk
            d2 = DashboardServer()
            resp = TestClient(d2.app).post(
                "/api/device-login", json={"device_token": "never-paired"}
            )
            self.assertEqual(resp.status_code, 401)


class NewPairingKeyDoesNotInvalidateExistingSessionTests(unittest.TestCase):
    def test_generating_a_new_qr_key_leaves_existing_device_session_intact(self):
        with _TempDeviceStore():
            d = DashboardServer()
            client = TestClient(d.app)
            _tok, dev_tok = _pair_a_device(client, d)

            d.new_key()  # e.g. user pressed "Remote Control" again for a 2nd phone

            resp = client.post("/api/device-login", json={"device_token": dev_tok})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])


class RevokeDevicesTests(unittest.TestCase):
    """The existing /api/revoke-devices endpoint is this app's "logout" for
    trusted devices — it must actually invalidate them, including after a
    restart (i.e. the revocation itself must persist too, not just the
    original pairing)."""

    def test_revoke_devices_invalidates_the_current_process(self):
        with _TempDeviceStore():
            d = DashboardServer()
            client = TestClient(d.app)
            tok, dev_tok = _pair_a_device(client, d)

            revoke = client.post(
                "/api/revoke-devices", headers={"Authorization": f"Bearer {tok}"}
            )
            self.assertEqual(revoke.status_code, 200)
            self.assertEqual(revoke.json()["revoked"], 1)

            resp = client.post("/api/device-login", json={"device_token": dev_tok})
            self.assertEqual(resp.status_code, 401)

    def test_revoked_device_stays_revoked_after_a_restart(self):
        """Regression guard for the persistence itself: clearing
        _device_sessions must write through to disk, or a revoked phone
        would silently regain trust on the next restart."""
        with _TempDeviceStore():
            d1 = DashboardServer()
            client1 = TestClient(d1.app)
            tok, dev_tok = _pair_a_device(client1, d1)
            client1.post("/api/revoke-devices", headers={"Authorization": f"Bearer {tok}"})

            d2 = DashboardServer()  # simulated restart, loads from disk
            resp = TestClient(d2.app).post(
                "/api/device-login", json={"device_token": dev_tok}
            )
            self.assertEqual(resp.status_code, 401)


class DeviceSessionExpiryTests(unittest.TestCase):
    def test_expired_device_session_is_rejected_and_evicted(self):
        with _TempDeviceStore():
            d = DashboardServer()
            client = TestClient(d.app)
            _tok, dev_tok = _pair_a_device(client, d)

            # Force it into the past, well beyond DEVICE_SESSION_LIFETIME_SECONDS.
            d._device_sessions[dev_tok]["created_at"] = (
                time.time() - dashboard_server.DEVICE_SESSION_LIFETIME_SECONDS - 1
            )

            resp = client.post("/api/device-login", json={"device_token": dev_tok})
            self.assertEqual(resp.status_code, 401)
            self.assertNotIn(dev_tok, d._device_sessions)  # evicted, not just rejected

    def test_record_with_no_created_at_is_treated_as_expired_not_trusted_forever(self):
        with _TempDeviceStore():
            d = DashboardServer()
            d._device_sessions["legacy-token"] = {"session_key": "ABC123"}  # pre-fix shape
            resp = TestClient(d.app).post(
                "/api/device-login", json={"device_token": "legacy-token"}
            )
            self.assertEqual(resp.status_code, 401)

    def test_freshly_paired_device_session_is_well_within_its_lifetime(self):
        with _TempDeviceStore():
            d = DashboardServer()
            client = TestClient(d.app)
            _tok, dev_tok = _pair_a_device(client, d)
            resp = client.post("/api/device-login", json={"device_token": dev_tok})
            self.assertEqual(resp.status_code, 200)


class TamperedTokenRejectedTests(unittest.TestCase):
    def test_made_up_device_token_is_rejected(self):
        with _TempDeviceStore():
            d = DashboardServer()
            resp = TestClient(d.app).post(
                "/api/device-login", json={"device_token": "not-a-real-token"}
            )
            self.assertEqual(resp.status_code, 401)

    def test_made_up_bearer_token_is_rejected_by_protected_route(self):
        with _TempDeviceStore():
            d = DashboardServer()
            resp = TestClient(d.app).post(
                "/api/command",
                json={"text": "hi"},
                headers={"Authorization": "Bearer totally-made-up"},
            )
            self.assertEqual(resp.status_code, 401)


class NoSecretsLoggedTests(unittest.TestCase):
    def test_pairing_and_device_login_never_print_any_token_or_session_key(self):
        with _TempDeviceStore():
            d = DashboardServer()
            client = TestClient(d.app)
            buf = io.StringIO()
            with redirect_stdout(buf):
                tok, dev_tok = _pair_a_device(client, d)
                login = client.post(
                    "/api/device-login", json={"device_token": dev_tok}
                ).json()
            output = buf.getvalue()
            for secret in (tok, dev_tok, login["token"], login["key"]):
                self.assertNotIn(secret, output)


class PersistenceFileShapeTests(unittest.TestCase):
    """Sanity check on the on-disk format itself — a corrupt or unexpected
    shape must fail closed (empty store), never crash startup."""

    def test_store_file_is_plain_json_object_of_device_token_to_record(self):
        with _TempDeviceStore() as path:
            d = DashboardServer()
            _pair_a_device(TestClient(d.app), d)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            self.assertEqual(len(data), 1)
            record = next(iter(data.values()))
            self.assertIn("session_key", record)
            self.assertIn("created_at", record)

    def test_corrupt_store_file_falls_back_to_empty_not_a_crash(self):
        with _TempDeviceStore() as path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ not valid json", encoding="utf-8")
            d = DashboardServer()  # must not raise
            self.assertEqual(d._device_sessions, {})


if __name__ == "__main__":
    unittest.main()
