"""Pick Ticket PDF warehouse readability — presentation only."""

from __future__ import annotations

import os
from io import BytesIO

from app.models import InventoryUnit, Order, PickTicket
from app.services.allocation import allocate_order
from app.services.bulk_pick_pdf import render_bulk_pdf
from app.services.document_pdf import pick_ticket_styles, styles
from app.services.inventory_ledger import create_available_unit
from app.services.pick_ticket_update import substitute_unit
from app.services.pick_tickets import create_pick_ticket, render_pdf, ticket_lines
from tests.pdf_support import extract_pdf_text, pdf_font_sizes, pdf_page_count
from tests.test_v2_phase03_orders import _line
from tests.test_v2_phase04_allocation import _import_order, _world

LONG_DESCRIPTION = (
    "Celine Triomphe Bag in shiny calfskin with adjustable leather strap "
    "and gold-finish hardware for warehouse pick verification"
)
LONG_UPC = "12345678901234567890"
LONG_SKU = "SKU-VERY-LONG-CODE-001"

REQUIRED_COLUMNS = ["Location", "UPC", "SKU", "Description", "Style", "Color", "Size", "Qty"]


def test_shared_document_styles_unchanged():
    shared = styles()
    assert shared["title"].fontSize == 13
    assert shared["number"].fontSize == 12
    assert shared["label"].fontSize == 6.5
    assert shared["value"].fontSize == 8
    assert shared["section"].fontSize == 8
    assert shared["body"].fontSize == 7.5
    assert shared["cell"].fontSize == 7
    assert shared["head"].fontSize == 7
    pick = pick_ticket_styles()
    assert pick["title"].fontSize == 17
    assert pick["number"].fontSize == 12
    assert pick["section"].fontSize == 11
    assert pick["label"].fontSize == 9
    assert pick["value"].fontSize == 10
    assert pick["head"].fontSize == 9.5
    assert pick["cell"].fontSize == 9.5
    assert pick["cell_right"].fontSize == 9.5
    assert pick["cell_right"].alignment != shared["cell"].alignment


def _stock_line(world, spec):
    qty = spec.get("qty", 1)
    for _ in range(qty):
        create_available_unit(
            client_id=world["celine"].id,
            warehouse_id=world["cel_ny"].id,
            upc=spec["upc"],
            location=spec.get("location", "A-01"),
            sku=spec.get("sku", "SKU-A"),
            description=spec.get("description", "Item"),
            style=spec.get("style", "ST"),
            color=spec.get("color", "BLK"),
            size=spec.get("size", "M"),
        )


def _ready_multiline(admin_user, world, order_number, specs):
    for spec in specs:
        _stock_line(world, spec)
    from app.extensions import db

    db.session.commit()
    order = _import_order(
        admin_user,
        world["celine"],
        world["cel_ecom"],
        [_line(order=str(order_number), warehouse="NY", upc=spec["upc"], qty=spec.get("qty", 1)) for spec in specs],
    )
    allocate_order(order, user=admin_user)
    return Order.query.get(order.id)


def _n_line_specs(count, prefix):
    return [
        {
            "upc": f"{prefix}-{i:03d}",
            "location": f"B-{i:02d}",
            "sku": f"SKU-{i:03d}",
            "description": f"Pick item {i} {prefix}",
            "style": f"ST{i}",
            "color": "NAVY" if i % 2 else "BLK",
            "size": "M" if i % 3 else "L",
            "qty": 1,
        }
        for i in range(1, count + 1)
    ]


def _assert_readable_ticket(pdf: bytes, ticket: PickTicket, *, expect_pages=None, extra=()):
    assert pdf.startswith(b"%PDF")
    text = extract_pdf_text(pdf)
    sizes = pdf_font_sizes(pdf)
    unique = set(sizes)
    assert 17 in unique, unique
    assert 12 in unique, unique
    assert 11 in unique, unique
    assert 9.5 in unique, unique
    assert 8 in unique, unique
    tiny = [size for size in unique if size < 8]
    assert tiny == [], tiny
    assert ticket.pick_ticket_number in text
    assert f"Rev {ticket.revision_number or 1}" in text
    assert "PICK TICKET" in text
    assert "ORDER INFORMATION" in text
    assert "PICKING SUMMARY" in text
    assert "PICKING DETAIL" in text
    for header in REQUIRED_COLUMNS:
        assert header in text
    for snippet in extra:
        assert snippet in text, snippet
    pages = pdf_page_count(pdf)
    assert pages >= 1
    if expect_pages is not None:
        assert pages >= expect_pages
    return text, pages, unique


def test_single_ticket_pdf_readability(app, db, admin_user):
    world = _world(db)
    cases = []

    one = _ready_multiline(admin_user, world, 8101, _n_line_specs(1, "ONE"))
    t1 = create_pick_ticket(one)
    cases.append(("1-line", t1, render_pdf(t1), 1, ["B-01", "ONE-001"]))

    ten = _ready_multiline(admin_user, world, 8102, _n_line_specs(10, "TEN"))
    t10 = create_pick_ticket(ten)
    cases.append(("10-line", t10, render_pdf(t10), 1, ["TEN-010", "B-10"]))

    twenty_five = _ready_multiline(admin_user, world, 8103, _n_line_specs(25, "QTR"))
    t25 = create_pick_ticket(twenty_five)
    cases.append(("25-line", t25, render_pdf(t25), 1, ["QTR-025", "B-25"]))

    fifty = _ready_multiline(admin_user, world, 8104, _n_line_specs(50, "FIF"))
    t50 = create_pick_ticket(fifty)
    cases.append(("50-line", t50, render_pdf(t50), 2, ["FIF-050", "B-50"]))

    long_desc = _ready_multiline(
        admin_user,
        world,
        8105,
        [
            {
                "upc": "LONG-DESC",
                "location": "C-01",
                "sku": "SKU-LD",
                "description": LONG_DESCRIPTION,
                "style": "BAG",
                "color": "TAN",
                "size": "OS",
                "qty": 1,
            }
        ],
    )
    tld = create_pick_ticket(long_desc)
    cases.append(("long-description", tld, render_pdf(tld), 1, ["C-01", "LONG-DESC", "Triomphe", "warehouse pick"]))

    long_ids = _ready_multiline(
        admin_user,
        world,
        8106,
        [
            {
                "upc": LONG_UPC,
                "location": "D-22",
                "sku": LONG_SKU,
                "description": "Long identifier unit",
                "style": "WIDE",
                "color": "RED",
                "size": "XL",
                "qty": 1,
            }
        ],
    )
    tid = create_pick_ticket(long_ids)
    cases.append(("long-upc-sku", tid, render_pdf(tid), 1, ["D-22", LONG_UPC, LONG_SKU]))

    rev_order = _ready_multiline(admin_user, world, 8107, _n_line_specs(1, "REV"))
    extra = create_available_unit(
        client_id=world["celine"].id,
        warehouse_id=world["cel_ny"].id,
        upc="REV-001",
        location="Z-99",
        sku="SKU-001",
        description="Pick item 1 REV",
        style="ST1",
        color="NAVY",
        size="M",
    )
    db.session.commit()
    trev = create_pick_ticket(rev_order)
    original = InventoryUnit.query.filter_by(allocated_order_id=rev_order.id, status="RESERVED").first()
    substitute_unit(trev, original, extra, user=admin_user)
    trev = PickTicket.query.get(trev.id)
    assert trev.revision_number == 2
    cases.append(("rev-2", trev, render_pdf(trev), 1, ["Rev 2", "Z-99"]))

    dump_dir = "/tmp/wms-pick-ticket-pdfs"
    os.makedirs(dump_dir, exist_ok=True)
    for name, ticket, pdf, min_pages, extra in cases:
        text, pages, unique = _assert_readable_ticket(pdf, ticket, expect_pages=min_pages, extra=extra)
        with open(os.path.join(dump_dir, f"{name}.pdf"), "wb") as handle:
            handle.write(pdf)
        assert ticket_lines(Order.query.get(ticket.order_id), ticket)
        if name == "50-line":
            assert pages >= 2
            assert ticket.pick_ticket_number in text
            assert f"Rev {ticket.revision_number or 1}" in text
        if name == "long-description":
            assert "Triomphe" in text
            compact = text.replace("\n", "").replace(" ", "")
            assert "warehousepick" in compact.lower() or "warehouse pick" in text.lower()
        assert 6.5 not in unique
        assert 7 not in unique


def _ticket_start_pages(pdf: bytes) -> list[int]:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        from PyPDF2 import PdfReader
    reader = PdfReader(BytesIO(pdf))
    starts = []
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if "BULK PICK TICKET PACKAGE" in text:
            continue
        if "PICK TICKET" in text and "Rev " in text:
            starts.append(index)
    return starts


def test_bulk_pick_ticket_pdf_same_typography(app, db, admin_user):
    world = _world(db)
    tickets = []
    for i in range(100):
        order = _ready_multiline(
            admin_user,
            world,
            8200 + i,
            [
                {
                    "upc": f"BLK-{i:03d}",
                    "location": "A-01",
                    "sku": "SKU-B",
                    "description": f"Bulk unit {i}",
                    "style": "ST",
                    "color": "BLK",
                    "size": "M",
                    "qty": 1,
                }
            ],
        )
        tickets.append(create_pick_ticket(order))

    for count in (10, 25, 100):
        batch = tickets[:count]
        pdf = render_bulk_pdf(batch, sort="ticket", direction="asc")
        assert pdf.startswith(b"%PDF")
        text = extract_pdf_text(pdf)
        sizes = set(pdf_font_sizes(pdf))
        assert 17 in sizes
        assert 9.5 in sizes
        assert 8 in sizes
        # Cover page may still use shared 6.5/7 chrome; ticket pages must include warehouse sizes.
        assert "BULK PICK TICKET PACKAGE" in text
        for ticket in batch:
            assert ticket.pick_ticket_number in text
        pages = pdf_page_count(pdf)
        assert pages >= 1 + count
        os.makedirs("/tmp/wms-pick-ticket-pdfs", exist_ok=True)
        with open(os.path.join("/tmp/wms-pick-ticket-pdfs", f"bulk-{count}.pdf"), "wb") as handle:
            handle.write(pdf)
        starts = _ticket_start_pages(pdf)
        assert len(starts) >= count
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)
        sample = render_pdf(batch[0])
        sample_sizes = set(pdf_font_sizes(sample))
        assert 17 in sample_sizes
        assert 9.5 in sample_sizes
        tiny = [size for size in sample_sizes if size < 8]
        assert tiny == [], tiny
