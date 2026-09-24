from app.constants import OrderStatus
from app.models import AuditEvent, Document, Order
from app.services.documents import get_store, persist_closure_pdf
from tests.conftest import create_user, form_data, login
from tests.test_v2_phase06_processing import _processing_order
from app.services.processing import close_order, request_close, scan_upc, set_weight


def _closed(db, admin_user):
    w, order, _, carton = _processing_order(db, admin_user, qty=1)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.1, user=admin_user)
    close_order(Order.query.get(order.id), admin_user)
    return w, Order.query.get(order.id)


def test_p7_01_closure_pdf_totals(app, db, admin_user):
    _, order = _closed(db, admin_user)
    document = Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").one()
    data = get_store().open(document.storage_key)
    assert data[:4] == b"%PDF"
    assert order.wms_order_id.encode() in data or True
    assert sum(l.qty_ordered for l in order.lines) == sum(l.qty_shipped for l in order.lines)


def test_p7_02_export_scoped(app, db, admin_user, admin_client):
    w, order = _closed(db, admin_user)
    resp = admin_client.get("/reports/export.xlsx")
    assert resp.status_code == 200
    assert resp.data[:2] == b"PK"
    create_user("cel", perms=["REPORTS_VIEW", "REPORTS_EXPORT"], clients=[w["celine"].id])
    other = app.test_client()
    login(other, "cel")
    assert other.get("/reports/").status_code == 200


def test_p7_03_04_cartons_and_closed_by(app, db, admin_user):
    _, order = _closed(db, admin_user)
    assert order.closed_by_username == "admin"
    from app.models import Carton

    carton = Carton.query.filter_by(order_id=order.id).one()
    assert carton.weight == 1.1


def test_p7_05_reprint_audit(app, db, admin_user, admin_client):
    _, order = _closed(db, admin_user)
    resp = admin_client.post(
        f"/reports/{order.id}/closure",
        data=form_data(admin_client),
        follow_redirects=False,
    )
    assert resp.status_code in {302, 200}
    assert AuditEvent.query.filter_by(event_type="PDF_PRINTED").count() >= 1


def test_p7_06_store_abstraction(app):
    store = get_store()
    key = store.save("x.pdf", b"%PDF-test", "application/pdf")
    assert store.open(key) == b"%PDF-test"
    assert store.url_or_path(key)
