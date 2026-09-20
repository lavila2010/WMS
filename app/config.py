"""Application configuration, driven entirely by environment variables.

The production database is Aiven PostgreSQL, reached through an external
``DATABASE_URL`` with SSL required. Local development/testing may point
``DATABASE_URL`` at a local PostgreSQL instance (SSL is not forced for
localhost hosts).
"""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def normalize_database_url(raw_url: str) -> str:
    """Normalize a database URL for SQLAlchemy + psycopg2.

    - Rewrites the legacy ``postgres://`` scheme to ``postgresql+psycopg2://``.
    - Forces ``sslmode=require`` for non-local hosts (Aiven requires SSL),
      unless an ``sslmode`` is already present in the URL.
    """
    if not raw_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it to your Aiven PostgreSQL "
            "connection string (or a local PostgreSQL URL for development)."
        )

    parsed = urlparse(raw_url)
    scheme = parsed.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+psycopg2"

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    host = (parsed.hostname or "").lower()
    is_local = host in _LOCAL_HOSTS
    if not is_local and "sslmode" not in query:
        query["sslmode"] = "require"

    new_query = urlencode(query)
    return urlunparse(parsed._replace(scheme=scheme, query=new_query))


class Config:
    def __init__(self) -> None:
        self.SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")
        self.SQLALCHEMY_DATABASE_URI = normalize_database_url(
            os.environ.get("DATABASE_URL", "")
        )
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        self.SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
        }
        # Where generated PDF documents are written.
        self.DOCUMENTS_DIR = os.environ.get(
            "DOCUMENTS_DIR", os.path.join(os.getcwd(), "instance", "documents")
        )
        self.MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB upload cap

        # Session / login security.
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        # Enable secure cookies in production (behind HTTPS on Render).
        self.SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in (
            "1", "true", "yes",
        )
        self.PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
