import os

import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db
from app.models import InventoryUnit, Order, OrderLine
from app.services.scope import resolve_scope

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://wms:wms@127.0.0.1:5432/wms_test"
)


@pytest.fixture()
def app(tmp_path):
    os.environ["DATABASE_URL"] = TEST_DB_URL
    config = Config()
    config.DOCUMENTS_DIR = str(tmp_path / "documents")
    application = create_app(config)
    with application.app_context():
        _db.drop_all()
        _db.create_all()
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


# --- Auth fixtures ---

def create_user(username, role="USER", password="password123", perms=None,
                active=True, must_change=False):
    from werkzeug.security import generate_password_hash

    from app.auth import grant_permissions, grant_user_defaults
    from app.models import User

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
    return user


def login(client, username, password="password123"):
    return client.post(
        "/login", data={"username": username, "password": password},
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


@pytest.fixture()
def user_client(app, regular_user):
    c = app.test_client()
    login(c, "worker")
    return c


# --- Factory helpers ---

DEFAULT_SCOPE = ("ACME", "WH1", "B2C")


def make_scope(client="ACME", warehouse="WH1", order_type="B2C"):
    scope = resolve_scope(client, warehouse, order_type)
    _db.session.commit()
    return scope


def make_order(
    order_number="SO-1",
    customer="Acme",
    lines=None,
    client="ACME",
    warehouse="WH1",
    order_type="B2C",
    carrier="UPS",
    shipping_service="Ground",
):
    c, w, ot = resolve_scope(client, warehouse, order_type)
    order = Order(
        order_number=order_number,
        customer=customer,
        carrier=carrier,
        shipping_service=shipping_service,
        client_id=c.id,
        warehouse_id=w.id,
        order_type_id=ot.id,
    )
    _db.session.add(order)
    _db.session.flush()
    for sku, qty in (lines or []):
        _db.session.add(OrderLine(order_id=order.id, sku=sku, quantity=qty))
    _db.session.commit()
    return order


def make_unit(
    barcode,
    sku,
    location="A-01",
    client="ACME",
    warehouse="WH1",
    upc=None,
    **_ignored,
):
    """Create a physical unit. Inventory is scoped by Client + Warehouse only
    (Order Type is intentionally not an inventory dimension)."""
    from app.services.scope import get_or_create_client, get_or_create_warehouse

    c = get_or_create_client(client)
    w = get_or_create_warehouse(c, warehouse)
    unit = InventoryUnit(
        barcode=barcode,
        upc=upc or f"UPC-{sku}",
        sku=sku,
        location=location,
        client_id=c.id,
        warehouse_id=w.id,
    )
    _db.session.add(unit)
    _db.session.commit()
    return unit


@pytest.fixture()
def factories():
    return {"make_order": make_order, "make_unit": make_unit, "make_scope": make_scope}
