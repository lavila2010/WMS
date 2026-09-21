"""Gate 12A — production configuration must refuse insecure defaults."""

from pathlib import Path

import pytest

from app.config import Config, normalize_database_url, redact_database_url

_AIVEN = "postgresql://wms:super-secret-pass@pg-wms-v2.example.aivencloud.com:25432/defaultdb"


def test_g12a_missing_secret_key_rejected(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("WMS_V2_DATABASE_URL", _AIVEN)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Config()


def test_g12a_insecure_secret_key_rejected(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "changeme")
    monkeypatch.setenv("WMS_V2_DATABASE_URL", _AIVEN)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Config()


def test_g12a_requires_wms_v2_database_url(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "unique-production-secret-key-value")
    monkeypatch.delenv("WMS_V2_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://wms:wms@127.0.0.1:5432/wms")
    with pytest.raises(RuntimeError, match="WMS_V2_DATABASE_URL"):
        Config()


def test_g12a_rejects_local_postgres_in_production(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "unique-production-secret-key-value")
    monkeypatch.setenv("WMS_V2_DATABASE_URL", "postgresql://wms:wms@127.0.0.1:5432/wms")
    with pytest.raises(RuntimeError, match="non-local"):
        Config()


def test_g12a_aiven_ssl_and_secure_cookies(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "unique-production-secret-key-value")
    monkeypatch.setenv("WMS_V2_DATABASE_URL", _AIVEN)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("WMS_TIMEZONE", "America/New_York")
    cfg = Config()
    assert cfg.SESSION_COOKIE_SECURE is True
    assert cfg.SESSION_COOKIE_HTTPONLY is True
    assert cfg.WTF_CSRF_SSL_STRICT is True
    assert cfg.OPERATIONAL_TIMEZONE == "America/New_York"
    assert "sslmode=require" in cfg.SQLALCHEMY_DATABASE_URI
    assert "postgresql+psycopg2" in cfg.SQLALCHEMY_DATABASE_URI


def test_g12a_redact_never_includes_credentials():
    label = redact_database_url(_AIVEN)
    assert "super-secret-pass" not in label
    assert "wms:" not in label
    assert "pg-wms-v2.example.aivencloud.com" in label
    assert "defaultdb" in label
    normalized = normalize_database_url(_AIVEN, require_ssl=True)
    assert "sslmode=require" in normalized
    assert redact_database_url(normalized).count("super-secret-pass") == 0


def test_g12a_render_yaml_keeps_separate_v2_service():
    text = Path("render.yaml").read_text()
    assert "  - type: web\n    name: wms\n" in text
    assert "  - type: web\n    name: wms-v2\n" in text
    assert "  - type: worker\n    name: wms-v2-import-worker\n" in text
    assert "python -m app.workers.inventory_import_worker" in text
    assert "WMS_V2_DATABASE_URL" in text
    assert "SESSION_COOKIE_SECURE" in text
    assert 'value: "3.12.6"' in text
    assert "gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120" in text
    assert "pip install -r requirements.txt" in text
