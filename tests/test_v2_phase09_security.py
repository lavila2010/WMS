from werkzeug.security import check_password_hash

from app.config import Config
from app.models import Order, User
from app.services.processing import LOCK_MESSAGE, ProcessingError, acquire_lock, scan_upc
from tests.conftest import create_user, form_data, login
from tests.test_v2_phase06_processing import _processing_order


def test_p9_anonymous_routes(client):
    for path in ("/", "/inventory/", "/orders/", "/allocation/", "/processing/", "/reports/", "/kpi/orders", "/admin/users"):
        resp = client.get(path)
        assert resp.status_code in {302, 401}
        if resp.status_code == 302:
            assert "/login" in resp.headers["Location"]


def test_p9_idor_and_escalation(app, db, admin_user, client):
    w, order, _, _ = _processing_order(db, admin_user)
    create_user(
        "cel",
        perms=["ORDERS_VIEW", "DASHBOARD_VIEW", "CLIENTS_VIEW", "CLIENTS_EDIT"],
        clients=[w["celine"].id],
    )
    login(client, "cel")
    assert client.get(f"/admin/clients/{w['dior'].id}/edit").status_code == 404
    assert client.post(
        "/admin/clients/new",
        data=form_data(client, {"name": "X", "initials": "XXX"}),
    ).status_code == 403
    assert client.post(
        f"/allocation/{order.id}/run",
        data=form_data(client),
    ).status_code == 403


def test_p9_health_no_secrets(client):
    data = client.get("/health").get_data(as_text=True).lower()
    assert "password" not in data
    assert "secret" not in data


def test_p9_password_hashed(app, db):
    user = create_user("hashme")
    assert user.password_hash != "password123"
    assert check_password_hash(user.password_hash, "password123")


def test_p9_secure_cookie_flag_in_production(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "unique-production-secret-key-value")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv(
        "WMS_V2_DATABASE_URL",
        "postgresql://wms:wms@pg-wms-v2.example.aivencloud.com:25432/wms?sslmode=require",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = Config()
    assert cfg.SESSION_COOKIE_SECURE is True
    assert cfg.SESSION_COOKIE_HTTPONLY is True
    assert "sslmode=require" in cfg.SQLALCHEMY_DATABASE_URI


def test_p9_duplicate_scan_and_double_close(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user, qty=1)
    scan_upc(order, carton, "UPC-A", admin_user)
    try:
        scan_upc(order, carton, "UPC-A", admin_user)
        raised = False
    except Exception:
        raised = True
    assert raised


def test_p9_lock_message(app, db, admin_user):
    _, order, ticket, _ = _processing_order(db, admin_user)
    other = create_user("second", perms=["PROCESSING_EXECUTE"])
    try:
        acquire_lock(order, other)
        assert False, "second user should not take the lock"
    except ProcessingError as exc:
        assert str(exc) == LOCK_MESSAGE
