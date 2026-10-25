#!/usr/bin/env python3
"""Touch-first public registration/check-in kiosk for Imagine Desk.

The browser talks only to this small server. Imagine Desk credentials stay in
server-side environment variables and are never sent to the kiosk browser.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import secrets
from http.cookies import SimpleCookie
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DESK_URL = os.getenv("IMAGINE_DESK_URL", "").strip().rstrip("/")
DESK_USERNAME = os.getenv("IMAGINE_DESK_USERNAME", "").strip()
DESK_PASSWORD = os.getenv("IMAGINE_DESK_PASSWORD", "")
EVENT_ID = os.getenv("IMAGINE_DESK_EVENT_ID", "").strip()
KIOSK_CONFIG_PATH = Path(os.getenv("IMAGINE_KIOSK_CONFIG_PATH", ROOT / "data" / "kiosk_settings.json"))
SETTINGS_DATABASE_URL = os.getenv("SELF_CHECKIN_DATABASE_URL", "").strip()
settings_schema_ready = False
ADMIN_SESSION_TTL = 8 * 60 * 60
MAX_BODY = 16_000
RATE_LIMIT = 20
RATE_WINDOW = 60


def required_config() -> list[str]:
    missing = []
    for name, value in (("IMAGINE_DESK_URL", DESK_URL), ("IMAGINE_DESK_USERNAME", DESK_USERNAME),
                        ("IMAGINE_DESK_PASSWORD", DESK_PASSWORD)):
        if not value:
            missing.append(name)
    return missing


class DeskClient:
    """Server-side API client; keeps Imagine Desk's session cookie off-device."""
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        self.username = DESK_USERNAME if username is None else username
        self.password = DESK_PASSWORD if password is None else password
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.lock = threading.RLock()
        self.logged_in = False
        self.user = {}

    def _request(self, path: str, method: str = "GET", payload: dict | None = None):
        raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "Imagine-Self-Check-In/1.0"}
        if raw is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = Request(DESK_URL + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=12) as response:
                body = response.read(2_000_000)
                return response.status, json.loads(body.decode("utf-8")) if body else {}
        except HTTPError as exc:
            body = exc.read(64_000)
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                data = {}
            return exc.code, data
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Impossible de joindre Imagine Desk. Vérifiez la connexion réseau.") from exc

    def _login(self) -> None:
        parsed = urlsplit(DESK_URL)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not local_http:
            raise RuntimeError("IMAGINE_DESK_URL doit utiliser HTTPS (HTTP est réservé à localhost en développement).")
        status, data = self._request("/api/auth/login", "POST", {"username": self.username, "password": self.password})
        if status not in (200, 201):
            self.logged_in = False
            raise RuntimeError(data.get("error") or "Connexion au compte Imagine Desk impossible.")
        self.user = data.get("user") or {}
        self.logged_in = True

    def call(self, path: str, method: str = "GET", payload: dict | None = None):
        with self.lock:
            if not self.logged_in:
                self._login()
            status, data = self._request(path, method, payload)
            if status == 401:
                self.logged_in = False
                self._login()
                status, data = self._request(path, method, payload)
            if status < 200 or status >= 300:
                message = data.get("error") or "Une erreur est survenue dans Imagine Desk."
                raise RuntimeError(message)
            return data


client = DeskClient()
request_times: dict[str, deque[float]] = defaultdict(deque)
rate_lock = threading.Lock()
registration_lock = threading.Lock()
lookup_tokens: dict[str, tuple[str, str, float]] = {}
lookup_lock = threading.Lock()
settings_lock = threading.RLock()
admin_sessions: dict[str, tuple[DeskClient, str, float]] = {}
admin_sessions_lock = threading.RLock()


def settings_database_connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Le pilote PostgreSQL manque. Installez les dépendances de requirements.txt.") from exc
    return psycopg.connect(SETTINGS_DATABASE_URL, connect_timeout=8)


def ensure_settings_schema() -> None:
    global settings_schema_ready
    if not SETTINGS_DATABASE_URL or settings_schema_ready:
        return
    with settings_lock:
        if settings_schema_ready:
            return
        try:
            with settings_database_connect() as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS self_checkin_settings (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    event_id TEXT NOT NULL DEFAULT '',
                    registrations_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
            settings_schema_ready = True
        except Exception as exc:
            raise RuntimeError("Impossible de joindre PostgreSQL pour les réglages de la borne.") from exc


def kiosk_settings() -> dict:
    defaults = {"eventId": EVENT_ID, "registrationsEnabled": True}
    if SETTINGS_DATABASE_URL:
        ensure_settings_schema()
        try:
            with settings_database_connect() as connection:
                row = connection.execute("SELECT event_id, registrations_enabled FROM self_checkin_settings WHERE id=1").fetchone()
            if row:
                defaults.update({"eventId": row[0], "registrationsEnabled": row[1]})
        except Exception as exc:
            raise RuntimeError("Impossible de lire les réglages de la borne dans PostgreSQL.") from exc
    else:
        with settings_lock:
            try:
                stored = json.loads(KIOSK_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    defaults.update({k: stored[k] for k in ("eventId", "registrationsEnabled") if k in stored})
            except (OSError, ValueError):
                pass
    defaults["eventId"] = clean(defaults.get("eventId"), 100)
    defaults["registrationsEnabled"] = bool(defaults.get("registrationsEnabled", True))
    return defaults


def selected_event_id() -> str:
    return kiosk_settings().get("eventId") or EVENT_ID


def save_kiosk_settings(settings: dict) -> None:
    safe = {"eventId": clean(settings.get("eventId"), 100),
            "registrationsEnabled": bool(settings.get("registrationsEnabled", True))}
    if SETTINGS_DATABASE_URL:
        ensure_settings_schema()
        try:
            with settings_database_connect() as connection:
                connection.execute("""INSERT INTO self_checkin_settings(id,event_id,registrations_enabled,updated_at)
                    VALUES(1,%s,%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET event_id=EXCLUDED.event_id,
                    registrations_enabled=EXCLUDED.registrations_enabled,updated_at=CURRENT_TIMESTAMP""",
                    (safe["eventId"], safe["registrationsEnabled"]))
        except Exception as exc:
            raise RuntimeError("Impossible d’enregistrer les réglages dans PostgreSQL.") from exc
        return
    with settings_lock:
        KIOSK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = KIOSK_CONFIG_PATH.with_suffix(KIOSK_CONFIG_PATH.suffix + ".tmp")
        temp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, KIOSK_CONFIG_PATH)


def admin_client_for(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except Exception:
        return None
    morsel = cookie.get("imagine_kiosk_admin")
    if not morsel:
        return None
    token = morsel.value
    with admin_sessions_lock:
        session = admin_sessions.get(token)
        if not session:
            return None
        if session[2] < time.monotonic():
            admin_sessions.pop(token, None)
            return None
        return session[0]


def clean(value, limit=180) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def normalized(value) -> str:
    return clean(value, 240).casefold()


def event_info() -> dict:
    missing = required_config()
    event_id = selected_event_id()
    if not event_id:
        missing = missing + ["IMAGINE_DESK_EVENT_ID"]
    if missing:
        return {"ready": False, "event": None, "missing": missing, "registrationsEnabled": kiosk_settings()["registrationsEnabled"]}
    data = client.call("/api/events")
    event = next((item for item in data.get("events", []) if str(item.get("id")) == event_id), None)
    if not event:
        return {"ready": False, "event": None, "missing": [], "message": "L’événement configuré est introuvable ou n’est pas accessible."}
    if event.get("status") == "archived":
        return {"ready": False, "event": None, "missing": [], "message": "Cet événement est archivé dans Imagine Desk."}
    settings = kiosk_settings()
    return {"ready": True, "event": {"id": event_id, "name": clean(event.get("name"), 120), "date": clean(event.get("date"), 30), "venue": clean(event.get("venue"), 120)}, "registrationsEnabled": settings["registrationsEnabled"]}


def participants(event_id: str | None = None) -> list[dict]:
    event_id = event_id or selected_event_id()
    data = client.call(f"/api/events/{quote(event_id, safe='')}/participants")
    return [p for p in data.get("participants", []) if not p.get("deletedAt")]


def find_person(query: str, people: list[dict]) -> dict | None:
    key = normalized(query)
    if not key:
        return None
    # Exact email or badge only; never reveal the attendee list through fuzzy search.
    return next((p for p in people if key in (normalized(p.get("email")), normalized(p.get("code")))), None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ImagineKiosk/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging URLs, request bodies, emails or other visitor data.
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {args[1] if len(args) > 1 else ''}")

    def security_headers(self, content_type="application/json; charset=utf-8"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def json_response(self, status: int, data: dict, extra_headers: dict | None = None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            raise ValueError("Requête invalide.")
        if length < 1 or length > MAX_BODY:
            raise ValueError("Formulaire invalide ou trop volumineux.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Le formulaire n’a pas pu être lu.")
        if not isinstance(data, dict):
            raise ValueError("Formulaire invalide.")
        return data

    def rate_limited(self) -> bool:
        ip = self.client_address[0]
        now = time.monotonic()
        with rate_lock:
            q = request_times[ip]
            while q and now - q[0] > RATE_WINDOW:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                return True
            q.append(now)
            return False

    def do_GET(self):
        if self.path == "/api/health":
            try:
                if SETTINGS_DATABASE_URL:
                    ensure_settings_schema()
                    with settings_database_connect() as connection:
                        connection.execute("SELECT 1")
                self.json_response(200, {"ok": True, "settingsStore": "postgres" if SETTINGS_DATABASE_URL else "file"})
            except RuntimeError as exc:
                self.json_response(503, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/admin/session":
            admin = admin_client_for(self)
            if not admin:
                self.json_response(401, {"authenticated": False})
            else:
                self.json_response(200, {"authenticated": True, "username": clean(admin.user.get("username"), 80)})
            return
        if self.path == "/api/admin/events":
            admin = admin_client_for(self)
            if not admin:
                self.json_response(401, {"error": "Connectez-vous avec un compte administrateur Imagine Desk."})
                return
            try:
                data = admin.call("/api/events")
                self.json_response(200, {"events": data.get("events", [])})
            except RuntimeError as exc:
                self.json_response(502, {"error": str(exc)})
            return
        if self.path == "/api/config":
            try:
                self.json_response(200, event_info())
            except RuntimeError as exc:
                self.json_response(503, {"ready": False, "event": None, "message": str(exc)})
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/app.js", "/styles.css", "/icon.svg", "/logo.png", "/admin", "/admin.html", "/admin.js", "/admin.css", "/manifest.webmanifest"):
            filename = "index.html" if path in ("/", "/index.html") else ("admin.html" if path == "/admin" else path.lstrip("/"))
            file = (PUBLIC / filename).resolve()
            if not file.is_relative_to(PUBLIC.resolve()) or not file.is_file():
                self.send_error(404)
                return
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json; charset=utf-8", ".png": "image/png"}
            raw = file.read_bytes()
            self.send_response(200)
            self.security_headers(types.get(file.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self.send_error(404)
            return
        if self.rate_limited():
            self.json_response(429, {"error": "Trop de tentatives. Patientez une minute puis réessayez."})
            return
        try:
            body = self.read_body()
            if self.path == "/api/admin/login":
                if not DESK_URL:
                    self.json_response(503, {"error": "Configurez d’abord l’URL d’Imagine Desk sur le serveur."})
                    return
                username = clean(body.get("username"), 80)
                password = body.get("password") if isinstance(body.get("password"), str) else ""
                if not username or not password:
                    raise ValueError("Saisissez votre identifiant et votre mot de passe Imagine Desk.")
                admin = DeskClient(username, password)
                admin._login()
                if admin.user.get("role") != "admin":
                    self.json_response(403, {"error": "Ce compte n’a pas le rôle administrateur dans Imagine Desk."})
                    return
                token = secrets.token_urlsafe(32)
                with admin_sessions_lock:
                    admin_sessions[token] = (admin, username, time.monotonic() + ADMIN_SESSION_TTL)
                cookie = f"imagine_kiosk_admin={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ADMIN_SESSION_TTL}"
                forwarded = self.headers.get("X-Forwarded-Proto", "").lower()
                origin = self.headers.get("Origin", "").lower()
                if os.environ.get("COOKIE_SECURE") == "1" or "https" in forwarded or origin.startswith("https://"):
                    cookie += "; Secure"
                self.json_response(200, {"authenticated": True, "username": clean(admin.user.get("username"), 80)}, {"Set-Cookie": cookie})
                return
            if self.path == "/api/admin/logout":
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                    old = cookie.get("imagine_kiosk_admin")
                    if old:
                        with admin_sessions_lock:
                            admin_sessions.pop(old.value, None)
                except Exception:
                    pass
                clear_cookie = "imagine_kiosk_admin=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                self.json_response(200, {"ok": True}, {"Set-Cookie": clear_cookie})
                return
            if self.path == "/api/admin/settings":
                admin = admin_client_for(self)
                if not admin:
                    self.json_response(401, {"error": "Votre session administrateur a expiré. Reconnectez-vous."})
                    return
                event_id = clean(body.get("eventId"), 100)
                name = clean(body.get("name"), 180)
                event_date = clean(body.get("date"), 20)
                venue = clean(body.get("venue"), 180)
                if not event_id or not name:
                    raise ValueError("Choisissez un événement et indiquez son nom.")
                if event_date:
                    try:
                        date.fromisoformat(event_date)
                    except ValueError:
                        raise ValueError("La date doit être au format AAAA-MM-JJ.")
                data = admin.call("/api/events")
                event = next((item for item in data.get("events", []) if str(item.get("id")) == event_id), None)
                if not event:
                    raise ValueError("Événement introuvable. Actualisez la page et réessayez.")
                if event.get("status") == "archived":
                    raise ValueError("Réactivez cet événement dans Imagine Desk avant de le sélectionner.")
                admin.call(f"/api/events/{quote(event_id, safe='')}", "PATCH", {"name": name, "date": event_date, "venue": venue})
                save_kiosk_settings({"eventId": event_id, "registrationsEnabled": body.get("registrationsEnabled") is True})
                self.json_response(200, {"ok": True, "event": {"id": event_id, "name": name, "date": event_date, "venue": venue}, "registrationsEnabled": body.get("registrationsEnabled") is True})
                return
            missing = required_config()
            if not selected_event_id():
                missing.append("IMAGINE_DESK_EVENT_ID")
            if missing:
                self.json_response(503, {"error": "La borne n’est pas encore reliée à Imagine Desk. Connectez-vous à /admin pour la configurer."})
                return
            event = event_info()
            if not event.get("ready"):
                self.json_response(503, {"error": event.get("message") or "L’événement n’est pas disponible."})
                return
            event_id = selected_event_id()
            participants_path = f"/api/events/{quote(event_id, safe='')}/participants"
            if self.path == "/api/lookup":
                query = clean(body.get("query"), 240)
                if len(query) < 3:
                    self.json_response(400, {"error": "Saisissez votre adresse e-mail ou votre code de badge."})
                    return
                person = find_person(query, participants(event_id))
                if not person:
                    self.json_response(404, {"error": "Inscription introuvable. Vérifiez l’adresse e-mail ou le code de votre badge."})
                    return
                token = secrets.token_urlsafe(32)
                with lookup_lock:
                    now = time.monotonic()
                    for key, value in list(lookup_tokens.items()):
                        if value[2] < now:
                            lookup_tokens.pop(key, None)
                    lookup_tokens[token] = (str(person.get("id")), self.client_address[0], now + 180)
                self.json_response(200, {"lookupToken": token, "name": clean(person.get("name"), 180), "checkedIn": bool(person.get("checkedIn")), "code": clean(person.get("code"), 40)})
                return
            if self.path == "/api/checkin":
                token = clean(body.get("lookupToken"), 100)
                with lookup_lock:
                    token_data = lookup_tokens.pop(token, None) if token else None
                if not token_data or token_data[1] != self.client_address[0] or token_data[2] < time.monotonic():
                    raise ValueError("Votre recherche a expiré. Recommencez la recherche.")
                person_id = token_data[0]
                # Re-fetch and validate the attendee on the server; don't trust a client-supplied ID alone.
                person = next((p for p in participants(event_id) if str(p.get("id")) == person_id), None)
                if not person:
                    self.json_response(404, {"error": "Cette inscription est introuvable. Recommencez la recherche."})
                    return
                if person.get("checkedIn"):
                    self.json_response(200, {"ok": True, "already": True, "name": clean(person.get("name"), 180)})
                    return
                op = {"opId": "kiosk-" + uuid.uuid4().hex, "action": "checkin", "participantId": person_id}
                sync_path = f"/api/events/{quote(event_id, safe='')}/sync"
                result = client.call(sync_path, "POST", {"operations": [op], "deviceId": "self-check-in"})
                if not result.get("results") or not result["results"][0].get("ok"):
                    raise RuntimeError("Le check-in n’a pas pu être confirmé. Demandez de l’aide à l’accueil.")
                self.json_response(200, {"ok": True, "already": False, "name": clean(person.get("name"), 180)})
                return
            if self.path == "/api/register":
                if not kiosk_settings()["registrationsEnabled"]:
                    self.json_response(403, {"error": "Les inscriptions sur place sont momentanément fermées. Adressez-vous à l’accueil."})
                    return
                name = clean(body.get("name"), 180)
                email = clean(body.get("email"), 180)
                company = clean(body.get("company"), 180)
                consent = body.get("consent") is True
                if not name or len(name) < 2:
                    raise ValueError("Indiquez votre nom et prénom.")
                if not email or len(email) > 180 or "@" not in email or email.startswith("@") or email.endswith("@"):
                    raise ValueError("Saisissez une adresse e-mail valide.")
                if not consent:
                    raise ValueError("Veuillez confirmer votre accord pour l’utilisation de ces informations.")
                if body.get("website"):
                    self.json_response(200, {"ok": True, "name": name, "code": ""})
                    return
                with registration_lock:
                    people = participants(event_id)
                    if any(normalized(p.get("email")) == normalized(email) for p in people if p.get("email")):
                        self.json_response(409, {"error": "Cette adresse e-mail est déjà inscrite. Utilisez le parcours « Déjà inscrit·e » pour confirmer votre arrivée."})
                        return
                    data = client.call(participants_path, "POST", {"name": name, "email": email, "company": company, "category": "participant", "checkedIn": True})
                person = data.get("participant") or {}
                self.json_response(201, {"ok": True, "name": clean(person.get("name"), 180) or name, "code": clean(person.get("code"), 40)})
                return
            self.json_response(404, {"error": "Action inconnue."})
        except ValueError as exc:
            self.json_response(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.json_response(502, {"error": str(exc)})
        except Exception:
            self.json_response(500, {"error": "Une erreur inattendue est survenue. Réessayez ou demandez de l’aide à l’accueil."})


def main():
    missing = required_config()
    print(f"Imagine Self Check-in listening on http://{HOST}:{PORT}")
    if missing:
        print("Integration is not configured. Missing: " + ", ".join(missing))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
