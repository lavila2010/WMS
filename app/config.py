"""V2 configuration. Prefers WMS_V2_DATABASE_URL. Rejects insecure production secrets."""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_INSECURE_KEYS = {"", "dev-insecure-secret-key", "changeme", "secret"}
_SSL_OK = {"require", "verify-ca", "verify-full"}


def redact_database_url(raw_url: str) -> str:
    """Return a host/db label with username and password removed."""
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    host = parsed.hostname or "unknown-host"
    port = f":{parsed.port}" if parsed.port else ""
    dbname = (parsed.path or "").lstrip("/") or "unknown-db"
    return f"{host}{port}/{dbname}"


def normalize_database_url(raw_url: str, *, require_ssl: bool = False) -> str:
    if not raw_url:
        raise RuntimeError(
            "WMS_V2_DATABASE_URL or DATABASE_URL must be set to a PostgreSQL URI."
        )
    parsed = urlparse(raw_url)
    scheme = parsed.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+psycopg2"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    host = (parsed.hostname or "").lower()
    if require_ssl or host not in _LOCAL_HOSTS:
        if query.get("sslmode") not in _SSL_OK:
            query["sslmode"] = "require"
    return urlunparse(parsed._replace(scheme=scheme, query=urlencode(query)))


def _is_production() -> bool:
    return os.environ.get("WMS_ENV", os.environ.get("FLASK_ENV", "")).lower() in {
        "production",
        "prod",
    }


def _production_database_url() -> str:
    raw_db = (os.environ.get("WMS_V2_DATABASE_URL") or "").strip()
    if not raw_db:
        raise RuntimeError(
            "Production requires WMS_V2_DATABASE_URL. "
            "Silent DATABASE_URL or local PostgreSQL fallback is not allowed."
        )
    host = (urlparse(raw_db).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        raise RuntimeError(
            "Production WMS_V2_DATABASE_URL must point at Aiven (non-local host)."
        )
    return raw_db


class Config:
    def __init__(self) -> None:
        production = _is_production()
        if production:
            if "SECRET_KEY" not in os.environ or os.environ.get("SECRET_KEY", "").strip() in _INSECURE_KEYS:
                raise RuntimeError(
                    "Production SECRET_KEY is missing or insecure. Set a unique SECRET_KEY."
                )
            secret = os.environ["SECRET_KEY"]
            raw_db = _production_database_url()
            secure_default = "true"
        else:
            secret = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")
            raw_db = os.environ.get("WMS_V2_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
            secure_default = ""

        self.SECRET_KEY = secret
        self.SQLALCHEMY_DATABASE_URI = normalize_database_url(raw_db, require_ssl=production)
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        self.SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
        self.DOCUMENTS_DIR = os.environ.get(
            "DOCUMENTS_DIR", os.path.join(os.getcwd(), "instance", "documents")
        )
        self.MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", secure_default).lower() in (
            "1",
            "true",
            "yes",
        )
        self.PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
        self.WTF_CSRF_ENABLED = True
        self.WTF_CSRF_TIME_LIMIT = int(os.environ.get("WTF_CSRF_TIME_LIMIT", str(8 * 3600)))
        self.WTF_CSRF_SSL_STRICT = bool(self.SESSION_COOKIE_SECURE)
        self.WTF_CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
        self.WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]
        self.OPERATIONAL_TIMEZONE = os.environ.get("WMS_TIMEZONE", "America/New_York")
        self.WMS_ENV = os.environ.get("WMS_ENV", "development")
        self.DOCUMENT_STORE = os.environ.get("DOCUMENT_STORE", "local")
