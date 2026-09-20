"""V2 configuration. Prefers WMS_V2_DATABASE_URL. Rejects insecure production secrets."""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_INSECURE_KEYS = {"", "dev-insecure-secret-key", "changeme", "secret"}


def normalize_database_url(raw_url: str) -> str:
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
    if host not in _LOCAL_HOSTS and "sslmode" not in query:
        query["sslmode"] = "require"
    return urlunparse(parsed._replace(scheme=scheme, query=urlencode(query)))


def _is_production() -> bool:
    return os.environ.get("WMS_ENV", os.environ.get("FLASK_ENV", "")).lower() in {
        "production",
        "prod",
    }


class Config:
    def __init__(self) -> None:
        secret = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")
        if _is_production() and secret.strip() in _INSECURE_KEYS:
            raise RuntimeError(
                "Production SECRET_KEY is missing or insecure. Set a unique SECRET_KEY."
            )
        self.SECRET_KEY = secret
        raw_db = os.environ.get("WMS_V2_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
        self.SQLALCHEMY_DATABASE_URI = normalize_database_url(raw_db)
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        self.SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
        self.DOCUMENTS_DIR = os.environ.get(
            "DOCUMENTS_DIR", os.path.join(os.getcwd(), "instance", "documents")
        )
        self.MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self.PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
        self.OPERATIONAL_TIMEZONE = os.environ.get("WMS_TIMEZONE", "America/New_York")
        self.WMS_ENV = os.environ.get("WMS_ENV", "development")
        self.DOCUMENT_STORE = os.environ.get("DOCUMENT_STORE", "local")
