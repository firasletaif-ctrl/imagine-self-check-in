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
import re
import smtplib
import ssl
from http.cookies import SimpleCookie
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
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
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
except ValueError:
    SMTP_PORT = 587
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1").strip().lower() not in {"0", "false", "no"}
settings_schema_ready = False
ADMIN_SESSION_TTL = 8 * 60 * 60
MAX_BODY = 32_000
MAX_EMAIL_ATTACHMENT_FILES = 5
MAX_EMAIL_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES = 12 * 1024 * 1024
MAX_EMAIL_UPLOAD_REQUEST_BYTES = MAX_EMAIL_ATTACHMENT_BYTES + 64 * 1024
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def message_defaults() -> dict:
    return {
        "newPaidMessage": "Votre inscription et votre arrivée sont enregistrées. Le règlement reste à confirmer. Veuillez vous diriger vers la gauche/droite; il n’est pas nécessaire de revenir à la borne.",
        "newFreeMessage": "Votre inscription et votre arrivée sont confirmées. Bienvenue !",
        "arrivedPaidMessage": "Votre arrivée est confirmée. Bienvenue à l’événement !",
        "arrivedUnpaidMessage": "Votre arrivée est confirmée. Si un règlement est requis, il reste à confirmer : veuillez vous diriger vers la gauche/droite.",
        "paymentRequiredMessage": "Votre inscription est trouvée, mais le règlement n’est pas confirmé. Confirmez votre arrivée ci-dessous, puis dirigez-vous vers la gauche/droite; vous n’aurez pas à revenir à la borne.",
        "arrivedExemptMessage": "Votre arrivée est confirmée. Bienvenue à l’événement !",
        "alreadyPaidMessage": "Votre arrivée était déjà confirmée et votre paiement est enregistré. Bienvenue !",
        "alreadyUnpaidMessage": "Votre arrivée est déjà enregistrée. Si le règlement reste à confirmer, veuillez vous diriger vers la gauche/droite; aucun retour à la borne n’est nécessaire.",
        "alreadyExemptMessage": "Votre arrivée était déjà confirmée. Bienvenue !",
        "capacityMessage": "Les inscriptions sur place sont complètes. Adressez-vous à l’équipe d’accueil.",
        "registrationClosedMessage": "Les inscriptions sur place sont fermées. Adressez-vous à l’équipe d’accueil.",
        "notFoundMessage": "Inscription introuvable. Vérifiez votre e-mail ou votre code, ou demandez de l’aide à l’accueil.",
        "duplicateEmailMessage": "Cette adresse e-mail est déjà inscrite. Choisissez le parcours « Déjà inscrit·e » pour confirmer votre arrivée.",
    }


def default_settings() -> dict:
    return {
        "eventId": EVENT_ID,
        "registrationsEnabled": True,
        "paymentRequired": False,
        "maxParticipants": 0,
        **message_defaults(),
        "paymentEmailEnabled": False,
        "paymentEmailEnabledAt": "",
        "paymentEmailSubject": "Confirmation de paiement — {{event_name}}",
        "paymentEmailBody": "Bonjour {{name}},\n\nNous confirmons la réception de votre paiement de {{price}} pour {{event_name}}.\nTarif : {{tariff}}\nIdentifiant d’inscription : {{participant_id}}\nCode participant : {{code}}\n\nÀ bientôt !",
    }


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
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    settings_json TEXT NOT NULL DEFAULT '{}'
                )""")
                connection.execute("ALTER TABLE self_checkin_settings ADD COLUMN IF NOT EXISTS settings_json TEXT NOT NULL DEFAULT '{}' ")
                connection.execute("""CREATE TABLE IF NOT EXISTS self_checkin_payment_emails (
                    event_id TEXT NOT NULL,
                    participant_id TEXT NOT NULL,
                    payment_confirmed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMPTZ,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(event_id, participant_id, payment_confirmed_at)
                )""")
                connection.execute("""CREATE TABLE IF NOT EXISTS self_checkin_email_attachments (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content BYTEA NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
            settings_schema_ready = True
        except Exception as exc:
            raise RuntimeError("Impossible de joindre PostgreSQL pour les réglages de la borne.") from exc


def kiosk_settings() -> dict:
    defaults = default_settings()
    if SETTINGS_DATABASE_URL:
        ensure_settings_schema()
        try:
            with settings_database_connect() as connection:
                row = connection.execute("SELECT event_id, registrations_enabled, settings_json FROM self_checkin_settings WHERE id=1").fetchone()
            if row:
                defaults["eventId"] = row[0]
                defaults["registrationsEnabled"] = row[1]
                try:
                    saved = json.loads(row[2] or "{}")
                    if isinstance(saved, dict):
                        defaults.update(saved)
                except (TypeError, ValueError):
                    pass
                # The canonical columns always win over the JSON copy.
                defaults["eventId"] = row[0]
                defaults["registrationsEnabled"] = row[1]
        except Exception as exc:
            raise RuntimeError("Impossible de lire les réglages de la borne dans PostgreSQL.") from exc
    else:
        with settings_lock:
            try:
                stored = json.loads(KIOSK_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    defaults.update(stored)
            except (OSError, ValueError):
                pass
    defaults["eventId"] = clean(defaults.get("eventId"), 100)
    defaults["registrationsEnabled"] = bool(defaults.get("registrationsEnabled", True))
    defaults["paymentRequired"] = bool(defaults.get("paymentRequired", False))
    defaults["paymentEmailEnabled"] = bool(defaults.get("paymentEmailEnabled", False))
    try:
        defaults["maxParticipants"] = max(0, min(100000, int(defaults.get("maxParticipants", 0))))
    except (TypeError, ValueError):
        defaults["maxParticipants"] = 0
    for key, value in message_defaults().items():
        defaults[key] = str(defaults.get(key, value) or value)[:1000]
    defaults["paymentEmailSubject"] = str(defaults.get("paymentEmailSubject") or default_settings()["paymentEmailSubject"])[:200]
    defaults["paymentEmailBody"] = str(defaults.get("paymentEmailBody") or default_settings()["paymentEmailBody"])[:8000]
    defaults["paymentEmailEnabledAt"] = str(defaults.get("paymentEmailEnabledAt") or "")[:64]
    return defaults


def selected_event_id() -> str:
    return kiosk_settings().get("eventId") or EVENT_ID


def clean_multiline(value, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text.strip()[:limit]


def sanitized_settings(settings: dict) -> dict:
    safe = default_settings()
    safe["eventId"] = clean(settings.get("eventId"), 100)
    safe["registrationsEnabled"] = bool(settings.get("registrationsEnabled", True))
    safe["paymentRequired"] = bool(settings.get("paymentRequired", False))
    try:
        safe["maxParticipants"] = max(0, min(100000, int(settings.get("maxParticipants", 0))))
    except (TypeError, ValueError):
        raise ValueError("La capacité doit être un nombre entier positif (0 = sans limite).")
    for key in message_defaults():
        safe[key] = clean_multiline(settings.get(key, safe[key]), 1000) or message_defaults()[key]
    safe["paymentEmailEnabled"] = bool(settings.get("paymentEmailEnabled", False))
    safe["paymentEmailEnabledAt"] = clean(settings.get("paymentEmailEnabledAt"), 64)
    safe["paymentEmailSubject"] = clean_multiline(settings.get("paymentEmailSubject", safe["paymentEmailSubject"]), 200) or safe["paymentEmailSubject"]
    safe["paymentEmailBody"] = clean_multiline(settings.get("paymentEmailBody", safe["paymentEmailBody"]), 8000) or safe["paymentEmailBody"]
    return safe


def save_kiosk_settings(settings: dict) -> None:
    safe = sanitized_settings(settings)
    encoded = json.dumps(safe, ensure_ascii=False)
    if SETTINGS_DATABASE_URL:
        ensure_settings_schema()
        try:
            with settings_database_connect() as connection:
                connection.execute("""INSERT INTO self_checkin_settings(id,event_id,registrations_enabled,updated_at,settings_json)
                    VALUES(1,%s,%s,CURRENT_TIMESTAMP,%s)
                    ON CONFLICT(id) DO UPDATE SET event_id=EXCLUDED.event_id,
                    registrations_enabled=EXCLUDED.registrations_enabled,updated_at=CURRENT_TIMESTAMP,
                    settings_json=EXCLUDED.settings_json""",
                    (safe["eventId"], safe["registrationsEnabled"], encoded))
        except Exception as exc:
            raise RuntimeError("Impossible d’enregistrer les réglages dans PostgreSQL.") from exc
        return
    with settings_lock:
        KIOSK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = KIOSK_CONFIG_PATH.with_suffix(KIOSK_CONFIG_PATH.suffix + ".tmp")
        temp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, KIOSK_CONFIG_PATH)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and 1 <= SMTP_PORT <= 65535 and SMTP_FROM and (bool(SMTP_USERNAME) == bool(SMTP_PASSWORD)))


def render_mail_template(template: str, values: dict) -> str:
    return re.sub(r"{{\s*([a-zA-Z0-9_]+)\s*}}", lambda match: str(values.get(match.group(1), "")), str(template or ""))


def email_variables(person: dict, event: dict) -> dict:
    try:
        amount = float(person.get("paymentAmount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    currency = clean(event.get("currency") or "TND", 8)
    price = f"{amount:.3f}".rstrip("0").rstrip(".")
    return {
        "name": clean(person.get("name"), 180),
        "price": f"{price} {currency}",
        "amount": price,
        "currency": currency,
        "participant_id": clean(person.get("id"), 100),
        "code": clean(person.get("code"), 40),
        "email": clean(person.get("email"), 180),
        "event_name": clean(event.get("name"), 180),
        "event_date": clean(event.get("date"), 40),
        "venue": clean(event.get("venue"), 180),
        "tariff": clean(person.get("paymentTariff"), 180),
        "payment_method": clean(person.get("paymentMethod"), 80),
        "payment_reference": clean(person.get("paymentReference"), 120),
    }


def validate_email_attachment(filename: str, content: bytes) -> tuple[str, str, bytes]:
    """Validate a program file using its extension and signature, not browser MIME metadata."""
    if not isinstance(content, bytes) or not content:
        raise ValueError("Le fichier est vide ou illisible.")
    if len(content) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise ValueError("Chaque pièce jointe doit faire 5 Mo maximum.")
    raw_name = Path(str(filename or "").replace("\\", "/")).name
    extension = Path(raw_name).suffix.lower()
    allowed = {
        ".pdf": ("application/pdf", lambda data: data.startswith(b"%PDF-")),
        ".jpg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
        ".jpeg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
        ".png": ("image/png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
        ".webp": ("image/webp", lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"),
    }
    file_type = allowed.get(extension)
    if not file_type or not file_type[1](content):
        raise ValueError("Formats acceptés : PDF, JPG, JPEG, PNG et WebP (fichier valide requis).")
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", raw_name).strip(" .")[:120]
    if not safe_name:
        safe_name = "programme" + extension
    return safe_name, file_type[0], content


def list_email_attachments(event_id: str) -> list[dict]:
    if not SETTINGS_DATABASE_URL:
        raise RuntimeError("Une base PostgreSQL est nécessaire pour conserver les pièces jointes du programme.")
    ensure_settings_schema()
    try:
        with settings_database_connect() as connection:
            rows = connection.execute("""SELECT id, filename, content_type, size_bytes, created_at
                FROM self_checkin_email_attachments WHERE event_id=%s ORDER BY created_at, filename""", (event_id,)).fetchall()
        return [{"id": row[0], "filename": row[1], "contentType": row[2], "sizeBytes": row[3],
                 "createdAt": row[4].isoformat() if row[4] else ""} for row in rows]
    except Exception as exc:
        raise RuntimeError("Impossible de lire les pièces jointes du programme dans PostgreSQL.") from exc


def load_email_attachments(event_id: str) -> list[dict]:
    if not SETTINGS_DATABASE_URL:
        return []
    ensure_settings_schema()
    try:
        with settings_database_connect() as connection:
            rows = connection.execute("""SELECT filename, content_type, content
                FROM self_checkin_email_attachments WHERE event_id=%s ORDER BY created_at, filename""", (event_id,)).fetchall()
        return [{"filename": row[0], "contentType": row[1], "content": bytes(row[2])} for row in rows]
    except Exception as exc:
        raise RuntimeError("Impossible de charger les pièces jointes du programme.") from exc


def store_email_attachment(event_id: str, filename: str, content: bytes) -> dict:
    if not SETTINGS_DATABASE_URL:
        raise RuntimeError("Une base PostgreSQL est nécessaire pour conserver les pièces jointes du programme.")
    safe_name, content_type, content = validate_email_attachment(filename, content)
    ensure_settings_schema()
    attachment_id = uuid.uuid4().hex
    try:
        with settings_database_connect() as connection:
            usage = connection.execute("""SELECT COUNT(*), COALESCE(SUM(size_bytes),0)
                FROM self_checkin_email_attachments WHERE event_id=%s""", (event_id,)).fetchone()
            if usage[0] >= MAX_EMAIL_ATTACHMENT_FILES:
                raise ValueError("Vous pouvez ajouter jusqu’à 5 pièces jointes par événement.")
            if usage[1] + len(content) > MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES:
                raise ValueError("La taille totale des pièces jointes ne peut pas dépasser 12 Mo par événement.")
            connection.execute("""INSERT INTO self_checkin_email_attachments
                (id,event_id,filename,content_type,content,size_bytes)
                VALUES(%s,%s,%s,%s,%s,%s)""",
                (attachment_id, event_id, safe_name, content_type, content, len(content)))
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError("Impossible d’enregistrer la pièce jointe dans PostgreSQL.") from exc
    return {"id": attachment_id, "filename": safe_name, "contentType": content_type, "sizeBytes": len(content)}


def delete_email_attachment(event_id: str, attachment_id: str) -> bool:
    if not SETTINGS_DATABASE_URL:
        raise RuntimeError("Une base PostgreSQL est nécessaire pour gérer les pièces jointes du programme.")
    ensure_settings_schema()
    try:
        with settings_database_connect() as connection:
            cursor = connection.execute("DELETE FROM self_checkin_email_attachments WHERE event_id=%s AND id=%s", (event_id, attachment_id))
            return cursor.rowcount == 1
    except Exception as exc:
        raise RuntimeError("Impossible de supprimer la pièce jointe dans PostgreSQL.") from exc


def build_email_message(to_address: str, subject: str, body: str, attachments: list[dict] | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = SMTP_FROM or "Imagine Events <noreply@example.com>"
    message["To"] = to_address
    message["Subject"] = subject[:200]
    message.set_content(body[:10000])
    for attachment in attachments or []:
        content_type = attachment.get("contentType", "application/octet-stream")
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(bytes(attachment.get("content") or b""), maintype=maintype,
                               subtype=subtype or "octet-stream", filename=attachment.get("filename") or "programme")
    return message


def send_email(to_address: str, subject: str, body: str, attachments: list[dict] | None = None) -> None:
    if not smtp_configured():
        raise RuntimeError("Le serveur e-mail SMTP n’est pas configuré.")
    message = build_email_message(to_address, subject, body, attachments)
    context = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=context) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
            return
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=context)
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError("Impossible d’envoyer l’e-mail. Vérifiez les paramètres SMTP et l’adresse de l’expéditeur.") from exc


def payment_email_claim(event_id: str, person_id: str, confirmed_at: str) -> bool:
    if not SETTINGS_DATABASE_URL:
        return False
    ensure_settings_schema()
    with settings_database_connect() as connection:
        cursor = connection.execute("""INSERT INTO self_checkin_payment_emails
            (event_id,participant_id,payment_confirmed_at,status,attempted_at,last_error)
            VALUES(%s,%s,%s,'sending',CURRENT_TIMESTAMP,'')
            ON CONFLICT(event_id,participant_id,payment_confirmed_at) DO UPDATE SET
            status='sending',attempted_at=CURRENT_TIMESTAMP,last_error=''
            WHERE self_checkin_payment_emails.status='failed'
               OR (self_checkin_payment_emails.status='sending'
                   AND self_checkin_payment_emails.attempted_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes')""",
            (event_id, person_id, confirmed_at))
        return cursor.rowcount == 1


def finish_payment_email(event_id: str, person_id: str, confirmed_at: str, success: bool) -> None:
    if not SETTINGS_DATABASE_URL:
        return
    try:
        with settings_database_connect() as connection:
            if success:
                connection.execute("""UPDATE self_checkin_payment_emails SET status='sent',sent_at=CURRENT_TIMESTAMP,last_error=''
                    WHERE event_id=%s AND participant_id=%s AND payment_confirmed_at=%s""", (event_id, person_id, confirmed_at))
            else:
                connection.execute("""UPDATE self_checkin_payment_emails SET status='failed',last_error='smtp_send_failed'
                    WHERE event_id=%s AND participant_id=%s AND payment_confirmed_at=%s""", (event_id, person_id, confirmed_at))
    except Exception:
        pass


def parse_timestamp(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def payment_email_eligible(person: dict, settings: dict) -> bool:
    if person.get("paymentStatus") != "paid" or not clean(person.get("email"), 180):
        return False
    enabled_at = parse_timestamp(settings.get("paymentEmailEnabledAt", ""))
    confirmed_at = parse_timestamp(person.get("paymentConfirmedAt", ""))
    return bool(settings.get("paymentEmailEnabled") and enabled_at and confirmed_at and confirmed_at >= enabled_at)


def poll_payment_emails_once() -> None:
    settings = kiosk_settings()
    event_id = settings.get("eventId") or EVENT_ID
    if not event_id or not settings.get("paymentEmailEnabled") or not smtp_configured() or not SETTINGS_DATABASE_URL:
        return
    if not parse_timestamp(settings.get("paymentEmailEnabledAt", "")):
        return
    event_data = client.call("/api/events")
    event = next((item for item in event_data.get("events", []) if str(item.get("id")) == str(event_id)), None)
    if not event or event.get("status") == "archived":
        return
    attachments = load_email_attachments(event_id)
    for person in participants(event_id):
        confirmed_at = str(person.get("paymentConfirmedAt") or "")
        email = clean(person.get("email"), 180)
        if not payment_email_eligible(person, settings):
            continue
        person_id = str(person.get("id") or "")
        if not person_id or not payment_email_claim(event_id, person_id, confirmed_at):
            continue
        variables = email_variables(person, event)
        try:
            send_email(email,
                       render_mail_template(settings["paymentEmailSubject"], variables),
                       render_mail_template(settings["paymentEmailBody"], variables), attachments)
            finish_payment_email(event_id, person_id, confirmed_at, True)
        except Exception:
            finish_payment_email(event_id, person_id, confirmed_at, False)
            print("Payment confirmation email failed (SMTP error).", flush=True)


def payment_email_worker() -> None:
    while True:
        try:
            poll_payment_emails_once()
        except Exception:
            print("Payment confirmation email polling failed.", flush=True)
        time.sleep(30)


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


def admin_event_exists(admin, event_id: str) -> bool:
    data = admin.call("/api/events")
    return any(str(event.get("id")) == str(event_id) for event in data.get("events", []))


def clean(value, limit=180) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def normalized(value) -> str:
    return clean(value, 240).casefold()


def public_settings(settings: dict | None = None) -> dict:
    settings = settings or kiosk_settings()
    keys = ("registrationsEnabled", "paymentRequired", "maxParticipants", *message_defaults().keys())
    return {key: settings[key] for key in keys}


def event_info() -> dict:
    settings = kiosk_settings()
    missing = required_config()
    event_id = selected_event_id()
    if not event_id:
        missing = missing + ["IMAGINE_DESK_EVENT_ID"]
    if missing:
        return {"ready": False, "event": None, "missing": missing, "settings": public_settings(settings), "registrationsEnabled": settings["registrationsEnabled"]}
    data = client.call("/api/events")
    event = next((item for item in data.get("events", []) if str(item.get("id")) == event_id), None)
    if not event:
        return {"ready": False, "event": None, "missing": [], "message": "L’événement configuré est introuvable ou n’est pas accessible.", "settings": public_settings(settings)}
    if event.get("status") == "archived":
        return {"ready": False, "event": None, "missing": [], "message": "Cet événement est archivé dans Imagine Desk.", "settings": public_settings(settings)}
    return {
        "ready": True,
        "event": {"id": event_id, "name": clean(event.get("name"), 120), "date": clean(event.get("date"), 30),
                  "venue": clean(event.get("venue"), 120), "currency": clean(event.get("currency") or "TND", 8),
                  "total": int(event.get("total") or 0)},
        "registrationsEnabled": settings["registrationsEnabled"],
        "settings": public_settings(settings),
    }


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

    def read_multipart_file(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("Le formulaire de fichier est invalide.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            raise ValueError("Requête invalide.")
        if length < 1 or length > MAX_EMAIL_UPLOAD_REQUEST_BYTES:
            raise ValueError("Le fichier dépasse la taille maximale autorisée de 5 Mo.")
        raw = self.rfile.read(length)
        envelope = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", "replace") + raw)
        parsed = BytesParser(policy=policy.default).parsebytes(envelope)
        if not parsed.is_multipart():
            raise ValueError("Le formulaire de fichier est invalide.")
        files = []
        for part in parsed.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if name == "file" and filename:
                files.append((filename, part.get_payload(decode=True) or b""))
        if len(files) != 1:
            raise ValueError("Sélectionnez un seul fichier à importer à la fois.")
        return files[0]

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
        if self.path == "/api/admin/settings":
            admin = admin_client_for(self)
            if not admin:
                self.json_response(401, {"error": "Connectez-vous avec un compte administrateur Imagine Desk."})
                return
            try:
                settings = kiosk_settings()
                self.json_response(200, {"settings": settings, "smtpConfigured": smtp_configured()})
            except RuntimeError as exc:
                self.json_response(503, {"error": str(exc)})
            return
        attachment_list_match = re.fullmatch(r"/api/admin/events/([^/]+)/email-attachments", self.path)
        if attachment_list_match:
            admin = admin_client_for(self)
            if not admin:
                self.json_response(401, {"error": "Connectez-vous avec un compte administrateur Imagine Desk."})
                return
            event_id = clean(unquote(attachment_list_match.group(1)), 100)
            try:
                if not event_id or not admin_event_exists(admin, event_id):
                    self.json_response(404, {"error": "Événement introuvable."})
                    return
                self.json_response(200, {"attachments": list_email_attachments(event_id),
                                         "maxFiles": MAX_EMAIL_ATTACHMENT_FILES,
                                         "maxFileBytes": MAX_EMAIL_ATTACHMENT_BYTES,
                                         "maxTotalBytes": MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES})
            except RuntimeError as exc:
                self.json_response(503, {"error": str(exc)})
            return
        price_match = re.fullmatch(r"/api/admin/events/([^/]+)/prices", self.path)
        if price_match:
            admin = admin_client_for(self)
            if not admin:
                self.json_response(401, {"error": "Connectez-vous avec un compte administrateur Imagine Desk."})
                return
            try:
                data = admin.call(f"/api/events/{quote(price_match.group(1), safe='')}/prices")
                self.json_response(200, {"prices": data.get("prices", [])})
            except RuntimeError as exc:
                self.json_response(502, {"error": str(exc)})
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
            attachment_upload_match = re.fullmatch(r"/api/admin/events/([^/]+)/email-attachments", self.path)
            if attachment_upload_match:
                admin = admin_client_for(self)
                if not admin:
                    self.json_response(401, {"error": "Votre session administrateur a expiré. Reconnectez-vous."})
                    return
                event_id = clean(unquote(attachment_upload_match.group(1)), 100)
                if not event_id or not admin_event_exists(admin, event_id):
                    self.json_response(404, {"error": "Événement introuvable."})
                    return
                filename, content = self.read_multipart_file()
                attachment = store_email_attachment(event_id, filename, content)
                self.json_response(201, {"ok": True, "attachment": attachment})
                return
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
                old = kiosk_settings()
                updated = dict(old)
                updated.update(body)
                updated.update({"eventId": event_id,
                                "registrationsEnabled": body.get("registrationsEnabled") is True,
                                "paymentRequired": body.get("paymentRequired") is True,
                                "paymentEmailEnabled": body.get("paymentEmailEnabled") is True})
                updated["maxParticipants"] = body.get("maxParticipants", 0)
                if updated["paymentEmailEnabled"]:
                    if not smtp_configured():
                        raise ValueError("Configurez d’abord le serveur SMTP dans les variables Render avant d’activer les e-mails.")
                    if not SETTINGS_DATABASE_URL:
                        raise ValueError("La base PostgreSQL est requise pour éviter les e-mails en double.")
                    if not old.get("paymentEmailEnabled"):
                        updated["paymentEmailEnabledAt"] = utc_now()
                else:
                    updated["paymentEmailEnabledAt"] = ""
                updated = sanitized_settings(updated)
                admin.call(f"/api/events/{quote(event_id, safe='')}", "PATCH", {"name": name, "date": event_date, "venue": venue})
                updated["eventId"] = event_id
                save_kiosk_settings(updated)
                self.json_response(200, {"ok": True, "event": {"id": event_id, "name": name, "date": event_date, "venue": venue}, "settings": updated, "smtpConfigured": smtp_configured()})
                return
            attachment_delete_match = re.fullmatch(r"/api/admin/events/([^/]+)/email-attachments/([a-f0-9]{32})/delete", self.path)
            if attachment_delete_match:
                admin = admin_client_for(self)
                if not admin:
                    self.json_response(401, {"error": "Votre session administrateur a expiré. Reconnectez-vous."})
                    return
                event_id = clean(unquote(attachment_delete_match.group(1)), 100)
                attachment_id = attachment_delete_match.group(2)
                if not event_id or not admin_event_exists(admin, event_id):
                    self.json_response(404, {"error": "Événement introuvable."})
                    return
                if not delete_email_attachment(event_id, attachment_id):
                    self.json_response(404, {"error": "Cette pièce jointe n’existe plus."})
                    return
                self.json_response(200, {"ok": True})
                return
            if self.path == "/api/admin/test-email":
                admin = admin_client_for(self)
                if not admin:
                    self.json_response(401, {"error": "Votre session administrateur a expiré. Reconnectez-vous."})
                    return
                if not smtp_configured():
                    raise ValueError("Le serveur SMTP n’est pas encore configuré dans Render.")
                recipient = clean(body.get("to"), 180)
                if not recipient or "@" not in recipient:
                    raise ValueError("Saisissez une adresse e-mail de test valide.")
                settings = kiosk_settings()
                event_id = settings.get("eventId") or EVENT_ID
                event_data = admin.call("/api/events")
                event = next((item for item in event_data.get("events", []) if str(item.get("id")) == str(event_id)), {})
                variables = {"name": "Participant de test", "price": "150 TND", "amount": "150", "currency": "TND",
                             "participant_id": "TEST-001", "code": "IE-TEST", "email": recipient,
                             "event_name": clean(event.get("name"), 180) or "Événement test", "event_date": clean(event.get("date"), 40),
                             "venue": clean(event.get("venue"), 180), "tariff": "Tarif test", "payment_method": "test", "payment_reference": "TEST"}
                subject_template = clean_multiline(body.get("subjectTemplate", settings["paymentEmailSubject"]), 200)
                body_template = clean_multiline(body.get("bodyTemplate", settings["paymentEmailBody"]), 8000)
                attachments = load_email_attachments(event_id)
                send_email(recipient, render_mail_template(subject_template, variables),
                           render_mail_template(body_template, variables), attachments)
                self.json_response(200, {"ok": True})
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
                    self.json_response(404, {"error": kiosk_settings()["notFoundMessage"]})
                    return
                token = secrets.token_urlsafe(32)
                with lookup_lock:
                    now = time.monotonic()
                    for key, value in list(lookup_tokens.items()):
                        if value[2] < now:
                            lookup_tokens.pop(key, None)
                    lookup_tokens[token] = (str(person.get("id")), self.client_address[0], now + 180)
                settings = kiosk_settings()
                payment_status = person.get("paymentStatus") or "unpaid"
                already_key = {"paid": "alreadyPaidMessage", "exempt": "alreadyExemptMessage"}.get(payment_status, "alreadyUnpaidMessage")
                requires_payment = bool(settings["paymentRequired"] and payment_status not in ("paid", "exempt"))
                self.json_response(200, {"lookupToken": token, "name": clean(person.get("name"), 180),
                                         "checkedIn": bool(person.get("checkedIn")), "code": clean(person.get("code"), 40),
                                         "paymentStatus": payment_status, "requiresPayment": requires_payment,
                                         "message": (settings["alreadyUnpaidMessage"] if requires_payment and person.get("checkedIn") else
                                                     settings["paymentRequiredMessage"] if requires_payment else
                                                     settings[already_key] if person.get("checkedIn") else "")})
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
                settings = kiosk_settings()
                payment_status = person.get("paymentStatus") or "unpaid"
                if person.get("checkedIn"):
                    message_key = {"paid": "alreadyPaidMessage", "exempt": "alreadyExemptMessage"}.get(payment_status, "alreadyUnpaidMessage")
                    self.json_response(200, {"ok": True, "already": True, "name": clean(person.get("name"), 180), "message": settings[message_key]})
                    return
                op = {"opId": "kiosk-" + uuid.uuid4().hex, "action": "checkin", "participantId": person_id}
                sync_path = f"/api/events/{quote(event_id, safe='')}/sync"
                result = client.call(sync_path, "POST", {"operations": [op], "deviceId": "self-check-in"})
                if not result.get("results") or not result["results"][0].get("ok"):
                    raise RuntimeError("Le check-in n’a pas pu être confirmé. Demandez de l’aide à l’accueil.")
                message_key = {"paid": "arrivedPaidMessage", "exempt": "arrivedExemptMessage"}.get(payment_status, "arrivedUnpaidMessage")
                self.json_response(200, {"ok": True, "already": False, "name": clean(person.get("name"), 180), "message": settings[message_key]})
                return
            if self.path == "/api/register":
                settings = kiosk_settings()
                if not settings["registrationsEnabled"]:
                    self.json_response(403, {"error": settings["registrationClosedMessage"]})
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
                    if settings["maxParticipants"] and len(people) >= settings["maxParticipants"]:
                        self.json_response(409, {"error": settings["capacityMessage"]})
                        return
                    if any(normalized(p.get("email")) == normalized(email) for p in people if p.get("email")):
                        self.json_response(409, {"error": settings["duplicateEmailMessage"]})
                        return
                    payment_status = "unpaid" if settings["paymentRequired"] else "exempt"
                    data = client.call(participants_path, "POST", {"name": name, "email": email, "company": company,
                                                                    "category": "participant", "checkedIn": True,
                                                                    "paymentStatus": payment_status})
                person = data.get("participant") or {}
                success_message = settings["newPaidMessage"] if settings["paymentRequired"] else settings["newFreeMessage"]
                self.json_response(201, {"ok": True, "name": clean(person.get("name"), 180) or name,
                                         "code": clean(person.get("code"), 40), "paymentStatus": payment_status,
                                         "message": success_message})
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
    threading.Thread(target=payment_email_worker, name="payment-email-worker", daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
