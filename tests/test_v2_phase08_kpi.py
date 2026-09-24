from datetime import datetime

from app.constants import OrderStatus
from app.extensions import db
from app.models import Order
from app.services.order_kpis import operational_today, summarize
from app.services.processing import close_order, request_close, scan_upc, set_weight
from tests.conftest import create_user, login
from tests.test_v2_phase06_processing import _processing_order


def test_p8_01_default_range_today(app):
    today = operational_today()
    data = summarize()
    assert data["from_date"] == today.isoformat()
    assert data["to_date"] == today.isoformat()


def test_p8_02_05_group_and_close_transition(app, db, admin_user):
    w, order, _, carton = _processing_order(db, admin_user, qty=2)
    before = summarize(client_id=w["celine"].id)
    assert before["overall"]["total_orders"] == 1
    assert before["overall"]["open_orders"] == 1
    assert before["rows"][0]["client"] == "01-CEL"
    assert before["rows"][0]["division"] == "01-CEL-ECOM"
    scan_upc(order, carton, "UPC-A", admin_user)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.0, user=admin_user)
    close_order(Order.query.get(order.id), admin_user)
    after = summarize(client_id=w["celine"].id)
    assert after["overall"]["total_orders"] == before["overall"]["total_orders"]
    assert after["overall"]["open_orders"] == 0
    assert after["overall"]["processed_orders"] == 1


def test_p8_03_04_channel_and_no_duplication(app, db, admin_user):
    _processing_order(db, admin_user, qty=3)
    data = summarize()
    assert data["channels"]["ecommerce"]["orders"] == 1
    assert data["channels"]["ecommerce"]["units"] == 3
    assert data["overall"]["total_units"] == 3
    assert data["overall"]["total_orders"] == 1


def test_p8_06_non_admin_filter(app, db, admin_user):
    w, *_ = _processing_order(db, admin_user)
    create_user("cel", perms=["REPORTS_VIEW", "DASHBOARD_VIEW"], clients=[w["celine"].id])
    create_user("dio", perms=["REPORTS_VIEW", "DASHBOARD_VIEW"], clients=[w["dior"].id])
    c = app.test_client()
    login(c, "dio")
    html = c.get("/kpi/orders").get_data(as_text=True)
    assert "01-CEL-1251" not in html
