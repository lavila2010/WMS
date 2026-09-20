import os
import re

import pytest
from sqlalchemy import text

from app import create_app
from app.config import Config
from app.extensions import db as _db
from app.schema import ensure_v2_schema

_CSRF_RE = re.compile(
    r'(?:name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']'
    r'|value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']'
    r'|name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\'])',
    re.I,
)

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://wms:wms@127.0.0.1:5432/wms_test"
)


@pytest.fixture()
def app(tmp_path):
    os.environ["DATABASE_URL"] = TEST_DB_URL
    os.environ.pop("WMS_V2_DATABASE_URL", None)
    os.environ["WMS_ENV"] = "test"
    config = Config()
    config.DOCUMENTS_DIR = str(tmp_path / "documents")
    config.OPERATIONAL_TIMEZONE = "UTC"
    application = create_app(config)
    with application.app_context():
        _db.drop_all()
        _db.create_all()
        ensure_v2_schema()
        _db.session.execute(text("ALTER SEQUENCE client_code_seq RESTART WITH 1"))
        _db.session.commit()
        from app.auth import seed_permissions

        seed_permissions()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


def create_user(username, role="USER", password="password123", perms=None,
                active=True, must_change=False, clients=None):
    from werkzeug.security import generate_password_hash

    from app.auth import grant_permissions, grant_user_defaults
    from app.models import User, UserClient

    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        full_name=username.title(),
        role=role,
        active=active,
        must_change_password=must_change,
    )
    _db.session.add(user)
    _db.session.commit()
    if role == "USER":
        if perms is None:
            grant_user_defaults(user)
        else:
            grant_permissions(user, perms)
    if clients:
        for cid in clients:
            _db.session.add(UserClient(user_id=user.id, client_id=cid))
        _db.session.commit()
    return user


def extract_csrf_token(html: str) -> str:
    match = _CSRF_RE.search(html)
    if not match:
        raise AssertionError("CSRF token not found in HTML")
    return next(group for group in match.groups() if group)


def csrf_token(client, path="/login"):
    """Return a signed CSRF token bound to this test client's session cookie.

    Flask test clients created while an app context is already pushed do not
    always persist Set-Cookie from a GET. session_transaction writes the
    cookie onto this client explicitly so a later POST can validate.
    """
    import hashlib

    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer

    if path:
        client.get(path, follow_redirects=True)
    with client.session_transaction() as sess:
        if "csrf_token" not in sess:
            sess["csrf_token"] = hashlib.sha1(os.urandom(64)).hexdigest()
        raw = sess["csrf_token"]
    return URLSafeTimedSerializer(current_app.secret_key, salt="wtf-csrf-token").dumps(raw)


def form_data(client, data=None, path="/"):
    payload = dict(data or {})
    payload["csrf_token"] = csrf_token(client, path)
    return payload


def login(client, username, password="password123"):
    return client.post(
        "/login",
        data=form_data(client, {"username": username, "password": password}, path="/login"),
        follow_redirects=False,
    )


@pytest.fixture()
def admin_user(app):
    return create_user("admin", role="ADMIN")


@pytest.fixture()
def regular_user(app):
    return create_user("worker", role="USER")


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin")
    return c
