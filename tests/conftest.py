import os

import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db
from app.models import InventoryUnit, Order, OrderLine

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
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


# --- Factory helpers ---


def make_order(order_number="SO-1", customer="Acme", lines=None):
    order = Order(order_number=order_number, customer=customer)
    _db.session.add(order)
    _db.session.flush()
    for sku, qty in (lines or []):
        _db.session.add(OrderLine(order_id=order.id, sku=sku, quantity=qty))
    _db.session.commit()
    return order


def make_unit(barcode, sku, location="A-01"):
    unit = InventoryUnit(barcode=barcode, sku=sku, location=location)
    _db.session.add(unit)
    _db.session.commit()
    return unit


@pytest.fixture()
def factories():
    return {"make_order": make_order, "make_unit": make_unit}
