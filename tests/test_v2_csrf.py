"""CSRF certification: tokens required on every browser-originated mutation."""

from app.constants import OrderStatus, UnitStatus
from app.models import Client, InventoryUnit, Order
from app.services.processing import request_close, scan_upc, set_weight
from tests.conftest import create_user, csrf_token, extract_csrf_token, form_data, login
from tests.test_v2_phase06_processing import _processing_order


def test_csrf_01_valid_token_succeeds(app, db, admin_client):
    resp = admin_client.post(
        "/admin/clients/new",
        data=form_data(admin_client, {"name": "Celine", "initials": "CEL"}),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Client.query.filter_by(initials="CEL").one().client_code == "01-CEL"


def test_csrf_02_missing_token_rejected(app, db, admin_client):
    resp = admin_client.post("/admin/clients/new", data={"name": "Forged", "initials": "FRG"})
    assert resp.status_code in {400, 403}
    assert Client.query.count() == 0
    body = resp.get_data(as_text=True).lower()
    assert "postgres://" not in body
    assert "dev-insecure-secret-key" not in body
    assert "wms:wms@" not in body


def test_csrf_03_invalid_token_rejected(app, db, admin_client):
    resp = admin_client.post(
        "/admin/clients/new",
        data={"name": "Forged", "initials": "FRG", "csrf_token": "not-a-valid-csrf-token"},
    )
    assert resp.status_code in {400, 403}
    assert Client.query.count() == 0


def test_csrf_04_cross_client_still_enforced(app, db, admin_user, client):
    w, _order, _, _ = _processing_order(db, admin_user)
    create_user(
        "cel",
        perms=["ORDERS_VIEW", "DASHBOARD_VIEW", "CLIENTS_VIEW", "CLIENTS_EDIT", "CLIENTS_CREATE"],
        clients=[w["celine"].id],
    )
    login(client, "cel")
    assert client.get(f"/admin/clients/{w['dior'].id}/edit").status_code == 404
    resp = client.post(
        f"/admin/clients/{w['dior'].id}/edit",
        data=form_data(client, {"name": "Hijacked", "initials": "DIO", "active": "on"}),
    )
    assert resp.status_code == 404
    assert w["dior"].name != "Hijacked"


def test_csrf_05_session_alone_insufficient(app, db, admin_client):
    forged = app.test_client()
    login(forged, "admin")
    resp = forged.post("/admin/clients/new", data={"name": "SessionOnly", "initials": "SES"})
    assert resp.status_code in {400, 403}
    assert Client.query.filter_by(initials="SES").first() is None


def test_csrf_06_scan_with_form_token(app, db, admin_user, admin_client):
    _, order, _, carton = _processing_order(db, admin_user, qty=1)
    page = admin_client.get(f"/processing/{order.id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    token = extract_csrf_token(html)
    assert 'name="csrf_token"' in html
    assert 'id="scan-upc"' in html
    resp = admin_client.post(
        f"/processing/{order.id}/scan",
        data={"csrf_token": token, "box_id": carton.id, "upc": "UPC-A"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    packed = InventoryUnit.query.filter_by(allocated_order_id=order.id, status=UnitStatus.PACKED).count()
    assert packed == 1

    header_order_unit = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.RESERVED
    ).first()
    assert header_order_unit is None


def test_csrf_06b_scan_accepts_csrf_header(app, db, admin_user, admin_client):
    _, order, _, carton = _processing_order(db, admin_user, qty=1, upc="UPC-B")
    token = csrf_token(admin_client, f"/processing/{order.id}")
    resp = admin_client.post(
        f"/processing/{order.id}/scan",
        data={"box_id": carton.id, "upc": "UPC-B"},
        headers={"X-CSRFToken": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert InventoryUnit.query.filter_by(allocated_order_id=order.id, status=UnitStatus.PACKED).count() == 1


def test_csrf_07_order_close_cannot_be_forged(app, db, admin_user, admin_client):
    _, order, _, carton = _processing_order(db, admin_user, qty=1)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.1, user=admin_user)
    db.session.commit()
    oid = order.id
    forged = admin_client.post(
        f"/processing/{oid}/decision",
        data={"choice": "yes"},
    )
    assert forged.status_code in {400, 403}
    assert Order.query.get(oid).status != OrderStatus.CLOSED
    ok = admin_client.post(
        f"/processing/{oid}/decision",
        data=form_data(admin_client, {"choice": "yes"}),
        follow_redirects=True,
    )
    assert ok.status_code == 200
    assert Order.query.get(oid).status == OrderStatus.CLOSED


def test_csrf_08_admin_mutations_cannot_be_forged(app, db, admin_client):
    missing = admin_client.post(
        "/admin/users",
        data={"username": "forged-user", "password": "Password123", "role": "USER", "active": "1"},
    )
    assert missing.status_code in {400, 403}
    from app.models import User

    assert User.query.filter_by(username="forged-user").first() is None
    created = admin_client.post(
        "/admin/users",
        data=form_data(
            admin_client,
            {"username": "real-user", "password": "Password123", "role": "USER", "active": "1"},
        ),
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert User.query.filter_by(username="real-user").one().username == "real-user"


def test_csrf_login_required(client):
    missing = client.post("/login", data={"username": "admin", "password": "password123"})
    assert missing.status_code in {400, 403}
    invalid = client.post(
        "/login",
        data={"username": "admin", "password": "password123", "csrf_token": "bad-token"},
    )
    assert invalid.status_code in {400, 403}


def test_csrf_get_does_not_mutate(app, db, admin_client):
    before = Client.query.count()
    assert admin_client.get("/admin/clients/new").status_code == 200
    assert admin_client.get("/inventory/upload").status_code == 200
    assert Client.query.count() == before
    toggle = admin_client.get("/admin/users/1/toggle")
    assert toggle.status_code == 405


def test_csrf_health_unprotected_get(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] in {"ok", "degraded"}
