import json
import threading
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class FakeDesk:
    def __init__(self):
        self.user = {"username": "admin.test", "role": "admin"}
        self.event = {"id": "event-1", "name": "Imagine Summit", "date": "2026-11-12", "venue": "Tunis", "status": "active"}
        self.people = [{"id": "p-existing", "name": "Amira Ben Salem", "email": "amira@example.tn", "company": "", "code": "IE-0001", "checkedIn": False, "deletedAt": ""}]

    def call(self, path, method="GET", payload=None):
        if path == "/api/events":
            return {"events": [self.event.copy()]}
        if path == "/api/events/event-1" and method == "PATCH":
            self.event.update(payload)
            return {"ok": True}
        if path == "/api/events/event-1/participants" and method == "GET":
            return {"participants": list(self.people)}
        if path == "/api/events/event-1/participants" and method == "POST":
            person = {"id": "p-" + uuid.uuid4().hex, **payload, "code": "IE-NEW", "checkedIn": payload.get("checkedIn", False), "deletedAt": ""}
            self.people.append(person)
            return {"participant": person}
        if path == "/api/events/event-1/sync" and method == "POST":
            op = payload["operations"][0]
            person = next(p for p in self.people if p["id"] == op["participantId"])
            person["checkedIn"] = True
            return {"results": [{"opId": op["opId"], "ok": True}]}
        raise AssertionError((path, method, payload))


class KioskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = (server.client, server.DESK_URL, server.DESK_USERNAME, server.DESK_PASSWORD, server.EVENT_ID)
        server.DESK_URL = "https://desk.example.test"
        server.DESK_USERNAME = "kiosk.test"
        server.DESK_PASSWORD = "not-a-real-password"
        server.EVENT_ID = "event-1"

    @classmethod
    def tearDownClass(cls):
        server.client, server.DESK_URL, server.DESK_USERNAME, server.DESK_PASSWORD, server.EVENT_ID = cls.previous

    def setUp(self):
        self.desk = FakeDesk()
        server.client = self.desk
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post(self, path, body, headers=None):
        raw = json.dumps(body).encode()
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(self.base + path, data=raw, headers=request_headers)
        try:
            response = urlopen(request)
        except HTTPError as exc:
            response = exc
        return response.code, json.loads(response.read())

    def test_event_config_is_limited_to_selected_event(self):
        with urlopen(self.base + "/api/config") as response:
            data = json.loads(response.read())
        self.assertTrue(data["ready"])
        self.assertEqual(data["event"]["name"], "Imagine Summit")

    def test_lookup_and_checkin_use_one_time_token(self):
        status, lookup = self.post("/api/lookup", {"query": "AMIRA@example.tn"})
        self.assertEqual(status, 200)
        self.assertNotIn("participantId", lookup)
        self.assertTrue(lookup["lookupToken"])
        status, checked = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
        self.assertEqual(status, 200)
        self.assertFalse(checked["already"])
        self.assertTrue(self.desk.people[0]["checkedIn"])
        status, reused = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
        self.assertEqual(status, 400)
        self.assertIn("expiré", reused["error"])

    def test_registration_creates_participant_and_prevents_duplicate_email(self):
        payload = {"name": "Mehdi Trabelsi", "email": "mehdi@example.tn", "company": "Demo", "consent": True}
        status, created = self.post("/api/register", payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["code"], "IE-NEW")
        self.assertTrue(self.desk.people[-1]["checkedIn"])
        status, duplicate = self.post("/api/register", payload)
        self.assertEqual(status, 409)
        self.assertIn("déjà inscrite", duplicate["error"])


    def test_admin_can_update_event_and_kiosk_settings(self):
        old_path = server.KIOSK_CONFIG_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.admin_sessions["test-admin-session"] = (self.desk, "admin.test", time.monotonic() + 60)
            status, saved = self.post("/api/admin/settings", {
                "eventId": "event-1", "name": "Forum Imagine", "date": "2026-12-01",
                "venue": "Tunis", "registrationsEnabled": False,
            }, {"Cookie": "imagine_kiosk_admin=test-admin-session"})
            self.assertEqual(status, 200)
            self.assertEqual(self.desk.event["name"], "Forum Imagine")
            self.assertFalse(saved["registrationsEnabled"])
            self.assertEqual(server.selected_event_id(), "event-1")
            self.assertFalse(server.kiosk_settings()["registrationsEnabled"])
            server.admin_sessions.pop("test-admin-session", None)
        server.KIOSK_CONFIG_PATH = old_path

    def test_registration_requires_consent(self):
        status, data = self.post("/api/register", {"name": "Test User", "email": "test@example.tn"})
        self.assertEqual(status, 400)
        self.assertIn("accord", data["error"])


if __name__ == "__main__":
    unittest.main()
