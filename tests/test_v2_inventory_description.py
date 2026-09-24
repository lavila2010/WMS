"""Description is a first-class Client-scoped inventory attribute."""

from tests.pdf_support import extract_pdf_text


def _pdf_text(data: bytes) -> bytes:
    return extract_pdf_text(data).encode("latin-1", "replace")

from app.constants import OrderStatus
from app.models import InventoryUnit, Order, OrderLine
from app.services.allocation import allocate_order
from app.services.documents import render_closure_pdf
from app.services.inventory_import import analyze, commit_import
from app.services.inventory_ledger import create_available_unit
from app.services.inventory_query import unique_client_upc_description
from app.services.order_import import analyze as analyze_orders
from app.services.order_import import commit_import as commit_orders
from app.services.pick_tickets import create_pick_ticket, ticket_lines
from app.services.processing import carton_contents, remaining_rows, request_close, scan_upc, set_weight
from tests.test_v2_phase02_inventory import _masters, _row, _xlsx
from tests.test_v2_phase03_orders import _line, _xlsx as _order_xlsx
from tests.test_v2_phase04_allocation import _import_order, _stock, _world
from tests.test_v2_phase06_processing import _processing_order


def test_description_import_persists_and_expands(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=4, description="Celine Wallet")]), "inv.xlsx", celine, cel_ny)
    assert preview["has_blocking"] is False
    assert preview["rows"][0]["description"] == "Celine Wallet"
    commit_import(preview, user=admin_user)
    units = InventoryUnit.query.filter_by(client_id=celine.id).all()
    assert len(units) == 4
    assert {u.description for u in units} == {"Celine Wallet"}


def test_description_required_on_import(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=1, description="")]), "blank.xlsx", celine, cel_ny)
    assert preview["has_blocking"] is True
    assert any("Description is required" in e["message"] for e in preview["blocking"])
    assert InventoryUnit.query.count() == 0


def test_description_is_client_scoped(app, db, admin_user):
    celine, dior, cel_ny, _, dio_ny = _masters(db)
    commit_import(
        analyze(_xlsx([_row(upc="999", qty=1, description="Celine Bag")]), "c.xlsx", celine, cel_ny),
        user=admin_user,
    )
    commit_import(
        analyze(_xlsx([_row(upc="999", qty=1, description="Dior Bag")]), "d.xlsx", dior, dio_ny),
        user=admin_user,
    )
    assert unique_client_upc_description(celine.id, "999") == "Celine Bag"
    assert unique_client_upc_description(dior.id, "999") == "Dior Bag"
    assert InventoryUnit.query.filter_by(client_id=celine.id, upc="999").one().description == "Celine Bag"
    assert InventoryUnit.query.filter_by(client_id=dior.id, upc="999").one().description == "Dior Bag"


def test_order_line_inherits_client_upc_description(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 2, sku="SKU-A")
    create_available_unit(
        client_id=w["dior"].id,
        warehouse_id=w["dio_ny"].id,
        upc="UPC-A",
        location="Z-01",
        description="Dior Only",
    )
    for unit in InventoryUnit.query.filter_by(client_id=w["celine"].id, upc="UPC-A"):
        unit.description = "Celine Tote"
    db.session.commit()
    preview = analyze_orders(_order_xlsx([_line(upc="UPC-A", qty=1)]), "o.xlsx", w["celine"], w["cel_ecom"])
    assert preview["has_blocking"] is False
    commit_orders(preview, user=admin_user)
    line = OrderLine.query.one()
    assert line.description == "Celine Tote"
    assert "Dior" not in (line.description or "")


def test_order_keeps_explicit_description(app, db, admin_user):
    w = _world(db)
    create_available_unit(
        client_id=w["celine"].id,
        warehouse_id=w["cel_ny"].id,
        upc="UPC-A",
        location="A-01",
        description="Inventory Label",
    )
    db.session.commit()
    row = _line(upc="UPC-A", qty=1)
    row["Description"] = "Order Label"
    preview = analyze_orders(_order_xlsx([row]), "o.xlsx", w["celine"], w["cel_ecom"])
    commit_orders(preview, user=admin_user)
    assert OrderLine.query.one().description == "Order Label"


def test_pick_ticket_and_processing_show_description(app, db, admin_user):
    w, order, _, carton = _processing_order(db, admin_user, qty=1)
    for unit in InventoryUnit.query.filter_by(allocated_order_id=order.id):
        unit.description = "Packable Coat"
    order.lines[0].description = "Packable Coat"
    db.session.commit()
    create_pick_ticket(order)
    lines = ticket_lines(order)
    assert lines[0]["description"] == "Packable Coat"
    remaining = remaining_rows(order)
    assert remaining[0]["description"] == "Packable Coat"
    scan_upc(order, carton, "UPC-A", admin_user)
    contents = carton_contents(carton)
    assert contents[0]["description"] == "Packable Coat"


def test_closure_pdf_includes_description(app, db, admin_user):
    w, order, _, carton = _processing_order(db, admin_user, qty=1)
    for unit in InventoryUnit.query.filter_by(allocated_order_id=order.id):
        unit.description = "Closure Coat"
    db.session.commit()
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.1, user=admin_user)
    from app.services.processing import close_order

    close_order(Order.query.get(order.id), admin_user)
    pdf = render_closure_pdf(Order.query.get(order.id))
    assert b"Closure Coat" in _pdf_text(pdf)


def test_allocation_still_matches_upc_only(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 2, sku="OTHER")
    for unit in InventoryUnit.query.filter_by(client_id=w["celine"].id):
        unit.description = "Ignored By Allocation"
    db.session.commit()
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert Order.query.get(order.id).status == OrderStatus.ALLOCATED
    assert InventoryUnit.query.filter_by(status="RESERVED").count() == 2


def test_inventory_overview_http_shows_description(app, db, admin_user, admin_client):
    celine, _, cel_ny, _, _ = _masters(db)
    commit_import(
        analyze(_xlsx([_row(qty=1, description="Shown Coat")]), "s.xlsx", celine, cel_ny),
        user=admin_user,
    )
    html = admin_client.get(
        f"/inventory/?client_id={celine.id}&warehouse_id={cel_ny.id}"
    ).get_data(as_text=True)
    assert "Shown Coat" in html
    assert ">Description<" in html
