import io

from werkzeug.security import check_password_hash

from app.constants import OrderStatus
from app.models import AuditEvent, Document, ImportBatch, Transaction, User
from app.services.allocation import allocate_barcode, mark_allocated
from app.services.imports import build_inventory_workbook, build_orders_workbook
from app.services.packing import create_box, scan_into_box, close_box, mark_processed, mark_ready_to_close
from app.workflow import transition
from tests.conftest import create_user, login, make_order, make_unit


# ============================== AUTHENTICATION ==============================

def test_login_success(client, admin_user):
    resp = login(client, "admin")
    assert resp.status_code == 302
    assert "/login" not in resp.headers["Location"]
    assert client.get("/").status_code == 200


def test_login_wrong_password(client, admin_user):
    resp = login(client, "admin", "nope")
    assert resp.status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 302


def test_unknown_username_rejected(client, app):
    assert login(client, "ghost", "x").status_code == 401


def test_inactive_user_rejected(client, app):
    create_user("frozen", role="USER", active=True)
    u = User.query.filter_by(username="frozen").first()
    u.active = False
    from app.extensions import db as _db
    _db.session.commit()
    assert login(client, "frozen").status_code == 401


def test_password_stored_hashed(app, admin_user):
    u = User.query.filter_by(username="admin").first()
    assert u.password_hash != "password123"
    assert check_password_hash(u.password_hash, "password123")


def test_create_admin_cli_defaults_and_hidden_password(app, db):
    secret = "CliSecret-NotStored!"
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin"], input=f"{secret}\n{secret}\n")
    assert result.exit_code == 0, result.output
    assert "leandro" in result.output
    assert secret not in result.output
    assert "Password" not in result.output or secret not in result.output
    user = User.query.filter_by(username="leandro").first()
    assert user is not None
    assert user.role == "ADMIN"
    assert user.active is True
    assert user.must_change_password is False
    assert user.password_hash
    assert secret not in (user.password_hash or "")
    assert check_password_hash(user.password_hash, secret)


def test_create_admin_cli_does_not_overwrite_existing(app, db):
    runner = app.test_cli_runner()
    first = runner.invoke(args=["create-admin"], input="FirstPass-1!\nFirstPass-1!\n")
    assert first.exit_code == 0
    user = User.query.filter_by(username="leandro").first()
    original_hash = user.password_hash
    second = runner.invoke(args=["create-admin"], input="OtherPass-2!\nOtherPass-2!\n")
    assert second.exit_code == 0
    assert "already exists" in second.output
    assert "OtherPass-2!" not in second.output
    db.session.refresh(user)
    assert user.password_hash == original_hash
    assert check_password_hash(user.password_hash, "FirstPass-1!")


def test_logout_works(admin_client):
    assert admin_client.post("/logout").status_code == 302
    assert "/login" in admin_client.get("/", follow_redirects=False).headers["Location"]


def test_anonymous_route_redirects(client):
    r = client.get("/inventory/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_health_public(client):
    assert client.get("/health").status_code == 200


def test_session_persists_across_pages(admin_client):
    for path in ["/", "/inventory/overview", "/orders/", "/reports/"]:
        assert admin_client.get(path, follow_redirects=True).status_code == 200


# ============================== ADMIN ==============================

def test_admin_sees_all_modules(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    for label in ["Inventory", "Orders", "Allocation", "Order Processing", "Order Reports", "KPI Orders", "Administration"]:
        assert label in html


def test_admin_inventory_upload_access(admin_client):
    assert admin_client.get("/inventory/upload").status_code == 200


def test_admin_order_upload_access(admin_client):
    assert admin_client.get("/orders/import").status_code == 200


def test_admin_capabilities(admin_user):
    for code in ["ALLOCATION_EXECUTE", "PROCESSING_EXECUTE", "ORDER_CLOSE", "BOX_CLOSE", "USERS_VIEW", "PERMISSIONS_ASSIGN", "AUDIT_VIEW"]:
        assert admin_user.has_permission(code)


def test_admin_administration_access(admin_client):
    assert admin_client.get("/admin/users").status_code == 200


# ============================== REGULAR USER ==============================

def test_user_can_view_inventory(user_client):
    assert user_client.get("/inventory/overview").status_code == 200


def test_user_cannot_upload_inventory(user_client):
    assert user_client.get("/inventory/upload").status_code == 403
    assert user_client.post("/inventory/import/preview").status_code == 403


def test_user_can_view_orders(user_client):
    assert user_client.get("/orders/").status_code == 200


def test_user_cannot_upload_orders(user_client):
    assert user_client.get("/orders/import").status_code == 403


def test_user_execute_capabilities(regular_user):
    assert regular_user.has_permission("ALLOCATION_EXECUTE")
    assert regular_user.has_permission("PROCESSING_EXECUTE")


def test_user_cannot_manage_users(user_client):
    assert user_client.get("/admin/users").status_code == 403


def test_unauthorized_post_returns_403(user_client):
    assert user_client.post("/orders/import").status_code == 403


# ============================== PERMISSIONS ==============================

def test_grant_inventory_upload_allows(app, regular_user):
    from app.auth import grant_permissions
    grant_permissions(regular_user, ["INVENTORY_UPLOAD"])
    c = app.test_client()
    login(c, "worker")
    assert c.get("/inventory/upload").status_code == 200


def test_revoke_inventory_upload_blocks(app, regular_user):
    from app.auth import grant_permissions, set_user_permissions
    grant_permissions(regular_user, ["INVENTORY_UPLOAD"])
    set_user_permissions(regular_user, [])  # revoke all
    c = app.test_client()
    login(c, "worker")
    assert c.get("/inventory/upload").status_code == 403


def test_grant_users_view_allows_admin_page(app, regular_user):
    from app.auth import grant_permissions
    grant_permissions(regular_user, ["USERS_VIEW"])
    c = app.test_client()
    login(c, "worker")
    assert c.get("/admin/users").status_code == 200


# ============================== USER ADMIN ==============================

def _create_user_via_http(admin_client, username="newbie", role="USER"):
    return admin_client.post("/admin/users", data={
        "username": username, "full_name": "New Bie", "email": "n@x.com",
        "password": "password123", "role": role, "active": "1",
        "perm_INVENTORY_VIEW": "1",
    }, follow_redirects=True)


def test_admin_creates_user(admin_client, db):
    _create_user_via_http(admin_client)
    assert User.query.filter_by(username="newbie").first() is not None


def test_admin_edits_user(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    admin_client.post(f"/admin/users/{u.id}/edit", data={"full_name": "Edited", "email": "e@x.com", "role": "USER"}, follow_redirects=True)
    assert User.query.get(u.id).full_name == "Edited"


def test_admin_disables_user(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    admin_client.post(f"/admin/users/{u.id}/toggle", follow_redirects=True)
    assert User.query.get(u.id).active is False


def test_disabled_user_cannot_login(admin_client, app, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    admin_client.post(f"/admin/users/{u.id}/toggle", follow_redirects=True)
    c = app.test_client()
    assert login(c, "newbie").status_code == 401


def test_admin_resets_password(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    old_hash = u.password_hash
    admin_client.post(f"/admin/users/{u.id}/reset-password", data={"new_password": "brandnew123"}, follow_redirects=True)
    u = User.query.get(u.id)
    assert u.password_hash != old_hash and u.must_change_password is True


def test_admin_assigns_permissions(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    admin_client.post(f"/admin/users/{u.id}/permissions", data={"perm_INVENTORY_VIEW": "1", "perm_ORDERS_VIEW": "1"}, follow_redirects=True)
    assert u.has_permission("ORDERS_VIEW")
    assert not u.has_permission("INVENTORY_UPLOAD")


def test_passwords_never_exposed(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    resp = admin_client.get(f"/admin/users/{u.id}/edit")
    assert u.password_hash.encode() not in resp.data
    assert b"password123" not in resp.data


# ============================== AUDIT ==============================

def test_login_success_audited(client, admin_user):
    login(client, "admin")
    assert AuditEvent.query.filter_by(event_type="LOGIN_SUCCESS", username="admin").count() >= 1


def test_login_failed_audited(client, admin_user):
    login(client, "admin", "bad")
    assert AuditEvent.query.filter_by(event_type="LOGIN_FAILED").count() >= 1


def test_logout_audited(admin_client):
    admin_client.post("/logout")
    assert AuditEvent.query.filter_by(event_type="LOGOUT", username="admin").count() >= 1


def test_user_created_audited(admin_client, db):
    _create_user_via_http(admin_client)
    assert AuditEvent.query.filter_by(event_type="USER_CREATED").count() >= 1


def test_permissions_updated_audited(admin_client, db):
    _create_user_via_http(admin_client)
    u = User.query.filter_by(username="newbie").first()
    admin_client.post(f"/admin/users/{u.id}/permissions", data={"perm_ORDERS_VIEW": "1"}, follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="PERMISSIONS_UPDATED").count() >= 1


def _import_inventory_http(admin_client):
    wb = build_inventory_workbook([{"client": "ACME", "warehouse": "WH1", "upc": "U1", "sku": "SKU-A", "barcode": "AU-1", "location": "A-01"}])
    admin_client.post("/inventory/import/preview", data={"file": (io.BytesIO(wb.read()), "Inventory.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    admin_client.post("/inventory/import/confirm", follow_redirects=True)


def test_inventory_import_stores_user(admin_client, db):
    _import_inventory_http(admin_client)
    assert AuditEvent.query.filter_by(event_type="INVENTORY_IMPORT", username="admin").count() >= 1
    assert ImportBatch.query.filter_by(type="INVENTORY").first().created_by_username == "admin"


def test_order_import_stores_user(admin_client, db):
    wb = build_orders_workbook([{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "AO-1", "sku": "SKU-A", "quantity": 1}])
    admin_client.post("/orders/import", data={"file": (io.BytesIO(wb.read()), "Orders.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="ORDER_IMPORT", username="admin").count() >= 1
    assert ImportBatch.query.filter_by(type="ORDERS").first().created_by_username == "admin"


def _validated_order_with_units(db, n=2, prefix="AL"):
    order = make_order(f"{prefix}-ORD", lines=[("SKU-A", n)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    for i in range(1, n + 1):
        make_unit(f"{prefix}-{i}", "SKU-A")
    return order


def test_allocation_stores_user(admin_client, db):
    order = _validated_order_with_units(db, 1, "ALLOC")
    admin_client.post(f"/allocation/{order.id}/scan", data={"barcode": "ALLOC-1"}, follow_redirects=True)
    txn = Transaction.query.filter_by(type="ALLOCATE").first()
    assert txn.username == "admin"
    assert AuditEvent.query.filter_by(event_type="ALLOCATION", username="admin").count() >= 1


def _order_ready_to_pick(db, prefix):
    order = make_order(f"{prefix}-ORD", lines=[("SKU-A", 1)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit(f"{prefix}-1", "SKU-A")
    allocate_barcode(order, f"{prefix}-1")
    db.session.commit()
    mark_allocated(order)
    transition(order, OrderStatus.READY_TO_PICK)
    db.session.commit()
    return order


def test_unit_scan_box_close_order_close_pdf_store_user(admin_client, db):
    order = _order_ready_to_pick(db, "PROC")
    # create box
    admin_client.post(f"/processing/{order.id}/box", data={"box_number": "BOX-1", "length_cm": "10", "width_cm": "10", "height_cm": "10"}, follow_redirects=True)
    from app.models import Box
    box = Box.query.filter_by(order_id=order.id).first()
    # scan
    admin_client.post(f"/processing/box/{box.id}/scan", data={"barcode": "PROC-1"}, follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="UNIT_SCAN", username="admin").count() >= 1
    # close box
    admin_client.post(f"/processing/box/{box.id}/close", data={"weight_kg": "2.0"}, follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="BOX_CLOSE", username="admin").count() >= 1
    assert Box.query.get(box.id).closed_by_username == "admin"
    # processed -> ready-to-close -> close
    admin_client.post(f"/processing/{order.id}/processed", follow_redirects=True)
    admin_client.post(f"/processing/{order.id}/ready-to-close", follow_redirects=True)
    admin_client.post(f"/processing/{order.id}/close", follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="ORDER_CLOSE", username="admin").count() >= 1
    from app.models import Order, Invoice
    assert Order.query.get(order.id).closed_by_username == "admin"
    assert Invoice.query.filter_by(order_id=order.id).first().created_by_username == "admin"
    # PDF generation
    admin_client.post(f"/reports/order/{order.id}/closure", follow_redirects=True)
    assert AuditEvent.query.filter_by(event_type="PDF_GENERATED", username="admin").count() >= 1
    assert Document.query.filter_by(order_id=order.id).first().created_by_username == "admin"


# ============================== MULTI-USER ==============================

def test_two_sessions_distinct_identities(app, admin_user, regular_user):
    c_admin = app.test_client(); login(c_admin, "admin")
    c_user = app.test_client(); login(c_user, "worker")
    assert b"admin" in c_admin.get("/profile").data
    assert b"worker" in c_user.get("/profile").data


def test_actions_attributed_to_acting_user(app, db, admin_user, regular_user):
    # regular user needs allocation execute (default) — allocate on order A
    order_a = _validated_order_with_units(db, 1, "UA")
    order_b = _validated_order_with_units(db, 1, "UB")
    c_admin = app.test_client(); login(c_admin, "admin")
    c_user = app.test_client(); login(c_user, "worker")
    c_admin.post(f"/allocation/{order_a.id}/scan", data={"barcode": "UA-1"}, follow_redirects=True)
    c_user.post(f"/allocation/{order_b.id}/scan", data={"barcode": "UB-1"}, follow_redirects=True)
    ta = Transaction.query.filter_by(type="ALLOCATE", order_id=order_a.id).first()
    tb = Transaction.query.filter_by(type="ALLOCATE", order_id=order_b.id).first()
    assert ta.username == "admin"
    assert tb.username == "worker"
