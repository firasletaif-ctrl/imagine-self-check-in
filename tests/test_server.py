import json
import io
import threading
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class FakeDesk:
    def __init__(self):
        self.user = {"username": "admin.test", "role": "admin"}
        self.event = {"id": "event-1", "name": "Imagine Summit", "date": "2026-11-12", "venue": "Tunis", "status": "active"}
        self.people = [{"id": "p-existing", "name": "Amira Ben Salem", "email": "amira@example.tn", "company": "", "code": "IE-0001", "checkedIn": False, "deletedAt": ""}]

    def call(self, path, method="GET", payload=None):
        if path == "/api/events":
            return {"events": [self.event.copy()]}
        if path == "/api/events/event-1/prices" and method == "GET":
            return {"prices": [{"id": "price-1", "label": "Participant", "category": "participant", "amount": 150, "active": True}]}
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
        with server.rate_lock:
            server.request_times.pop("127.0.0.1", None)
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

    def get_json(self, path, headers=None):
        request = Request(self.base + path, headers=headers or {})
        with urlopen(request) as response:
            return response.code, json.loads(response.read())

    def test_event_config_is_limited_to_selected_event(self):
        with urlopen(self.base + "/api/config") as response:
            data = json.loads(response.read())
        self.assertTrue(data["ready"])
        self.assertEqual(data["event"]["name"], "Imagine Summit")

    def test_pwa_shell_manifest_and_icons_are_served(self):
        with urlopen(self.base + "/service-worker.js") as response:
            self.assertIn("javascript", response.headers.get("Content-Type", ""))
            self.assertIn("/api/", response.read().decode("utf-8"))
        with urlopen(self.base + "/manifest.webmanifest?station=2") as response:
            manifest = json.loads(response.read())
        self.assertEqual(manifest["start_url"], "/2")
        self.assertEqual(manifest["id"], "/2")
        self.assertTrue(any(icon["sizes"] == "192x192" for icon in manifest["icons"]))
        self.assertTrue(any(icon["purpose"] == "maskable" for icon in manifest["icons"]))
        for path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "image/png")
                self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_lookup_and_checkin_use_one_time_token(self):
        status, lookup = self.post("/api/lookup", {"query": "AMIRA@example.tn"})
        self.assertEqual(status, 200)
        self.assertNotIn("participantId", lookup)
        self.assertTrue(lookup["lookupToken"])
        status, checked = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
        self.assertEqual(status, 200)
        self.assertFalse(checked["already"])
        self.assertEqual(checked["code"], "IE-0001")
        self.assertTrue(self.desk.people[0]["checkedIn"])
        status, reused = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
        self.assertEqual(status, 400)
        self.assertIn("expiré", reused["error"])

    def test_already_arrived_participant_still_gets_their_code(self):
        self.desk.people[0]["checkedIn"] = True
        status, lookup = self.post("/api/lookup", {"query": "amira@example.tn"})
        self.assertEqual(status, 200)
        self.assertEqual(lookup["code"], "IE-0001")
        status, result = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["already"])
        self.assertEqual(result["code"], "IE-0001")

    def test_registration_creates_participant_and_prevents_duplicate_email(self):
        payload = {"name": "Mehdi Trabelsi", "email": "mehdi@example.tn", "company": "Demo", "consent": True}
        status, created = self.post("/api/register", payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["code"], "IE-NEW")
        self.assertTrue(self.desk.people[-1]["checkedIn"])
        status, duplicate = self.post("/api/register", payload)
        self.assertEqual(status, 409)
        self.assertIn("déjà inscrite", duplicate["error"])


    def test_help_requests_are_visible_per_station_and_deduplicated_until_resolved(self):
        old_database_url = server.SETTINGS_DATABASE_URL
        with server.help_requests_lock:
            old_requests = list(server.help_requests_memory)
            server.help_requests_memory.clear()
        server.SETTINGS_DATABASE_URL = ""
        self.addCleanup(setattr, server, "SETTINGS_DATABASE_URL", old_database_url)
        self.addCleanup(self._restore_help_requests, old_requests)

        status, first = self.post("/api/help", {}, {"X-Kiosk-Station": "1"})
        self.assertEqual(status, 201)
        self.assertEqual(first["request"]["stationLabel"], "Borne 1")
        status, duplicate = self.post("/api/help", {}, {"X-Kiosk-Station": "1"})
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["request"]["id"], first["request"]["id"])
        status, second = self.post("/api/help", {}, {"X-Kiosk-Station": "2"})
        self.assertEqual(status, 201)
        self.assertEqual(second["request"]["stationLabel"], "Borne 2")

        self.assertIsNotNone(server.update_help_request(first["request"]["id"], "acknowledged", "agent"))
        self.assertEqual(server.list_help_requests()[0]["status"], "acknowledged")
        self.assertIsNotNone(server.update_help_request(first["request"]["id"], "resolved", "agent"))
        self.assertEqual([item["stationId"] for item in server.list_help_requests()], ["2"])

        with urlopen(self.base + "/help") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Appels au personnel", response.read())

    def _restore_help_requests(self, requests):
        with server.help_requests_lock:
            server.help_requests_memory[:] = requests

    def test_station_links_keep_independent_event_settings(self):
        old_path = server.KIOSK_CONFIG_PATH
        old_station = getattr(server.request_context, "station_id", "default")
        self.addCleanup(setattr, server, "KIOSK_CONFIG_PATH", old_path)
        self.addCleanup(setattr, server.request_context, "station_id", old_station)
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.save_kiosk_settings({"eventId": "event-1", "registrationsEnabled": False}, station_id="1")
            server.save_kiosk_settings({"eventId": "", "registrationsEnabled": True}, station_id="2")
            self.assertEqual(server.kiosk_settings("1")["eventId"], "event-1")
            self.assertFalse(server.kiosk_settings("1")["registrationsEnabled"])
            self.assertEqual(server.kiosk_settings("2")["eventId"], "")
            self.assertTrue(server.kiosk_settings("2")["registrationsEnabled"])

            status, first = self.get_json("/api/config", {"X-Kiosk-Station": "1"})
            self.assertEqual(status, 200)
            self.assertTrue(first["ready"])
            self.assertFalse(first["registrationsEnabled"])
            status, second = self.get_json("/api/config", {"X-Kiosk-Station": "2"})
            self.assertEqual(status, 200)
            self.assertFalse(second["ready"])
            self.assertTrue(second["registrationsEnabled"])

            with urlopen(self.base + "/1") as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"manifest.webmanifest?station=1", response.read())
            with urlopen(self.base + "/admin/1") as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"Administration de la borne", response.read())

    def test_admin_can_update_event_and_kiosk_settings(self):
        old_path = server.KIOSK_CONFIG_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.admin_sessions["test-admin-session"] = (self.desk, "admin.test", time.monotonic() + 60)
            status, saved = self.post("/api/admin/settings", {
                "eventId": "event-1", "name": "Forum Imagine", "date": "2026-12-01",
                "venue": "Tunis", "registrationsEnabled": False, "paymentRequired": True,
                "maxParticipants": 25, "newPaidMessage": "Dirigez-vous vers le côté gauche.",
                "paymentEmailSubject": "Reçu pour {{name}}", "paymentEmailBody": "Montant : {{price}} - {{participant_id}}",
            }, {"Cookie": "imagine_kiosk_admin=test-admin-session"})
            self.assertEqual(status, 200)
            self.assertFalse(saved["settings"]["registrationsEnabled"])
            self.assertTrue(saved["settings"]["paymentRequired"])
            self.assertEqual(saved["settings"]["maxParticipants"], 25)
            self.assertEqual(self.desk.event["name"], "Forum Imagine")
            self.assertEqual(server.selected_event_id(), "event-1")
            self.assertFalse(server.kiosk_settings()["registrationsEnabled"])
            self.assertEqual(server.kiosk_settings()["newPaidMessage"], "Dirigez-vous vers le côté gauche.")
            self.assertEqual(server.kiosk_settings()["paymentEmailSubject"], "Reçu pour {{name}}")
            server.admin_sessions.pop("test-admin-session", None)
        server.KIOSK_CONFIG_PATH = old_path


    def test_paid_event_registration_stays_unpaid_and_uses_admin_message(self):
        old_path = server.KIOSK_CONFIG_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.save_kiosk_settings({"eventId": "event-1", "paymentRequired": True,
                                       "newPaidMessage": "Dirigez-vous à droite.", "registrationsEnabled": True})
            status, data = self.post("/api/register", {"name": "Nour Test", "email": "nour@example.tn", "consent": True})
            self.assertEqual(status, 201)
            self.assertEqual(data["paymentStatus"], "unpaid")
            self.assertEqual(data["message"], "Dirigez-vous à droite.")
            self.assertEqual(self.desk.people[-1]["paymentStatus"], "unpaid")
        self.assertTrue(self.desk.people[-1]["checkedIn"])
        server.KIOSK_CONFIG_PATH = old_path

    def test_unpaid_attendee_can_record_arrival_before_agent_confirms_payment(self):
        old_path = server.KIOSK_CONFIG_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.save_kiosk_settings({"eventId": "event-1", "paymentRequired": True,
                                       "paymentRequiredMessage": "Confirmez votre arrivée puis allez régler.",
                                       "alreadyUnpaidMessage": "Arrivée déjà enregistrée; règlement à confirmer."})
            status, lookup = self.post("/api/lookup", {"query": "amira@example.tn"})
            self.assertEqual(status, 200)
            self.assertTrue(lookup["requiresPayment"])
            self.assertFalse(lookup["checkedIn"])
            self.assertEqual(lookup["message"], "Confirmez votre arrivée puis allez régler.")
            status, checked = self.post("/api/checkin", {"lookupToken": lookup["lookupToken"]})
            self.assertEqual(status, 200)
            self.assertFalse(checked["already"])
            self.assertEqual(checked["message"], server.kiosk_settings()["arrivedUnpaidMessage"])
            self.assertTrue(self.desk.people[0]["checkedIn"])
            self.assertNotEqual(self.desk.people[0].get("paymentStatus", "unpaid"), "paid")

            status, lookup = self.post("/api/lookup", {"query": "amira@example.tn"})
            self.assertEqual(status, 200)
            self.assertTrue(lookup["requiresPayment"])
            self.assertTrue(lookup["checkedIn"])
            self.assertEqual(lookup["message"], "Arrivée déjà enregistrée; règlement à confirmer.")

            # Simulate the agent confirming payment in Imagine Desk; arrival remains recorded.
            self.desk.people[0]["paymentStatus"] = "paid"
            status, lookup = self.post("/api/lookup", {"query": "amira@example.tn"})
            self.assertEqual(status, 200)
            self.assertFalse(lookup["requiresPayment"])
            self.assertTrue(lookup["checkedIn"])
        server.KIOSK_CONFIG_PATH = old_path

    def test_capacity_blocks_new_signup_with_admin_message(self):
        old_path = server.KIOSK_CONFIG_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            server.KIOSK_CONFIG_PATH = Path(tempdir) / "settings.json"
            server.save_kiosk_settings({"eventId": "event-1", "maxParticipants": 1,
                                       "capacityMessage": "Inscriptions terminées. Suivez la flèche bleue."})
            status, data = self.post("/api/register", {"name": "Nouvelle personne", "email": "new@example.tn", "consent": True})
            self.assertEqual(status, 409)
            self.assertEqual(data["error"], "Inscriptions terminées. Suivez la flèche bleue.")
        server.KIOSK_CONFIG_PATH = old_path

    def test_custom_message_template_substitutes_requested_fields(self):
        rendered = server.render_mail_template("{{name}} / {{price}} / {{participant_id}}", {
            "name": "Amira", "price": "150 TND", "participant_id": "p-42"})
        self.assertEqual(rendered, "Amira / 150 TND / p-42")


    def test_program_attachment_validation_and_email_mime(self):
        filename, content_type, content = server.validate_email_attachment("../programme.pdf", b"%PDF-1.7\nprogramme test")
        self.assertEqual(filename, "programme.pdf")
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(content, b"%PDF-1.7\nprogramme test")
        self.assertEqual(server.validate_email_attachment("photo.jpg", b"\xff\xd8\xffphoto")[1], "image/jpeg")
        self.assertEqual(server.validate_email_attachment("photo.png", b"\x89PNG\r\n\x1a\nphoto")[1], "image/png")
        self.assertEqual(server.validate_email_attachment("photo.webp", b"RIFF0000WEBPdata")[1], "image/webp")
        with self.assertRaises(ValueError):
            server.validate_email_attachment("programme.pdf", b"not a pdf")
        with self.assertRaises(ValueError):
            server.validate_email_attachment("programme.txt", b"plain text")
        with self.assertRaises(ValueError):
            server.validate_email_attachment("too-large.pdf", b"%PDF-" + b"x" * server.MAX_EMAIL_ATTACHMENT_BYTES)

        boundary = "arena-boundary"
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"programme.pdf\"\r\n"
               "Content-Type: application/pdf\r\n\r\n%PDF-1.7\r\n--arena-boundary--\r\n").encode()
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        parsed_name, parsed_content = handler.read_multipart_file()
        self.assertEqual(parsed_name, "programme.pdf")
        self.assertEqual(parsed_content, b"%PDF-1.7")

        message = server.build_email_message("person@example.com", "Programme", "Voici le programme.", [
            {"filename": filename, "contentType": content_type, "content": content}
        ])
        self.assertTrue(message.is_multipart())
        attachment = next(message.iter_attachments())
        self.assertEqual(attachment.get_filename(), "programme.pdf")
        self.assertEqual(attachment.get_content_type(), "application/pdf")
        self.assertEqual(attachment.get_payload(decode=True), content)

    def test_smtp_switches_to_secondary_when_primary_daily_cap_is_full(self):
        primary = {"key": "primary", "host": "smtp.primary.test", "port": 587, "username": "primary-user",
                   "password": "primary-secret", "from": "Imagine <mail@imagine.test>", "use_tls": True, "daily_limit": 300}
        secondary = {"key": "secondary", "host": "smtp.secondary.test", "port": 587, "username": "secondary-user",
                     "password": "secondary-secret", "from": "Imagine Events <mail@events.test>", "use_tls": True, "daily_limit": 300}
        with (
            patch.object(server, "smtp_configured", return_value=True),
            patch.object(server, "smtp_profiles", return_value=[primary, secondary]),
            patch.object(server, "reserve_smtp_daily_slot", side_effect=[False, True]),
            patch.object(server, "transmit_email") as transmit,
        ):
            server.send_email("participant@example.test", "Test", "Body")
        selected_profile, message = transmit.call_args.args
        self.assertEqual(selected_profile["key"], "secondary")
        self.assertEqual(message["From"], secondary["from"])

    def test_smtp_does_not_send_when_both_daily_caps_are_full(self):
        primary = {"key": "primary", "host": "smtp.primary.test", "port": 587, "username": "primary-user",
                   "password": "primary-secret", "from": "Imagine <mail@imagine.test>", "use_tls": True, "daily_limit": 300}
        secondary = {"key": "secondary", "host": "smtp.secondary.test", "port": 587, "username": "secondary-user",
                     "password": "secondary-secret", "from": "Imagine Events <mail@events.test>", "use_tls": True, "daily_limit": 300}
        with (
            patch.object(server, "smtp_configured", return_value=True),
            patch.object(server, "smtp_profiles", return_value=[primary, secondary]),
            patch.object(server, "reserve_smtp_daily_slot", return_value=False),
            patch.object(server, "transmit_email") as transmit,
        ):
            with self.assertRaisesRegex(RuntimeError, "quotas quotidiens"):
                server.send_email("participant@example.test", "Test", "Body")
        transmit.assert_not_called()

    def test_payment_email_only_follows_a_new_agent_confirmed_payment(self):
        settings = {"paymentEmailEnabled": True, "paymentEmailEnabledAt": "2026-10-25T10:00:00+00:00"}
        person = {"email": "person@example.tn", "paymentStatus": "unpaid", "paymentConfirmedAt": ""}
        self.assertFalse(server.payment_email_eligible(person, settings))
        person.update({"paymentStatus": "paid", "paymentConfirmedAt": "2026-10-25T10:01:00+00:00"})
        self.assertTrue(server.payment_email_eligible(person, settings))
        person["paymentConfirmedAt"] = "2026-10-25T09:59:00+00:00"
        self.assertFalse(server.payment_email_eligible(person, settings))

    def test_registration_requires_consent(self):
        status, data = self.post("/api/register", {"name": "Test User", "email": "test@example.tn"})
        self.assertEqual(status, 400)
        self.assertIn("accord", data["error"])


if __name__ == "__main__":
    unittest.main()
