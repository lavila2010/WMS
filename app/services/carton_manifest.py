"""Read-only carton/product manifest shared by Shipping Report and End of Day."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func

from ..constants import CartonStatus, OrderStatus
from ..extensions import db
from ..models import Carton, CartonContent, InventoryUnit, OrderLine, PickTicket


FULFILLMENT_PARTIALLY = "PARTIALLY FULFILLED"
FULFILLMENT_CLOSED_COMPLETE = "CLOSED COMPLETE"
FULFILLMENT_CLOSED_SHORT = "CLOSED SHORT"
FULFILLMENT_STATUSES = [
    FULFILLMENT_PARTIALLY,
    FULFILLMENT_CLOSED_COMPLETE,
    FULFILLMENT_CLOSED_SHORT,
]


def display_status(value: str | None) -> str:
    return (value or "").replace("_", " ")


def fulfillment_status(order) -> str:
    if getattr(order, "status", None) == OrderStatus.CLOSED:
        return FULFILLMENT_CLOSED_SHORT if getattr(order, "short_closed", False) else FULFILLMENT_CLOSED_COMPLETE
    if getattr(order, "status", None) == OrderStatus.PARTIALLY_FULFILLED:
        return FULFILLMENT_PARTIALLY
    return display_status(getattr(order, "status", None))


def remaining_units(ordered, shipped, short) -> int:
    """Reporting remaining: ordered - shipped - short. Allocations are not shipped."""
    return max(int(ordered or 0) - int(shipped or 0) - int(short or 0), 0)


def line_totals(order_ids: list[int]) -> dict[int, dict[str, int]]:
    totals: dict[int, dict[str, int]] = {}
    if not order_ids:
        return totals
    rows = (
        db.session.query(
            OrderLine.order_id,
            func.coalesce(func.sum(OrderLine.qty_ordered), 0),
            func.coalesce(func.sum(OrderLine.qty_shipped), 0),
            func.coalesce(func.sum(OrderLine.qty_short), 0),
            func.coalesce(func.sum(OrderLine.qty_allocated), 0),
            func.coalesce(func.sum(OrderLine.qty_packed), 0),
        )
        .filter(OrderLine.order_id.in_(order_ids))
        .group_by(OrderLine.order_id)
        .all()
    )
    for order_id, ordered, shipped, short, allocated, packed in rows:
        ordered_n = int(ordered or 0)
        shipped_n = int(shipped or 0)
        short_n = int(short or 0)
        totals[int(order_id)] = {
            "ordered": ordered_n,
            "shipped": shipped_n,
            "short": short_n,
            "allocated": int(allocated or 0),
            "packed": int(packed or 0),
            "remaining": remaining_units(ordered_n, shipped_n, short_n),
        }
    return totals


def product_key(unit: InventoryUnit | None, upc: str):
    if unit is None:
        return (upc or "", "", "", "", "", "")
    return (
        unit.upc or upc or "",
        unit.sku or "",
        unit.description or "",
        unit.style or "",
        unit.color or "",
        unit.size or "",
    )


def _fmt_number(value, *, decimals=None):
    if value is None:
        return ""
    if decimals is None:
        return str(int(value))
    text = f"{float(value):.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_dim(value):
    if value is None:
        return ""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def carton_header_text(carton: Carton, pick_ticket_number: str | None) -> str:
    tracking = (carton.tracking_number or "").strip()
    carrier = (carton.tracking_carrier or "").strip()
    weight = _fmt_number(carton.weight, decimals=1)
    weight_unit = carton.weight_unit or "lb"
    length = _fmt_dim(carton.length)
    width = _fmt_dim(carton.width)
    height = _fmt_dim(carton.height)
    dim_unit = carton.dimension_unit or "in"
    if length and width and height:
        dimensions = f"{length} x {width} x {height} {dim_unit}"
    else:
        dimensions = ""
    weight_text = f"{weight} {weight_unit}".strip() if weight else ""
    return " | ".join(
        [
            f"Carton {carton.carton_number}",
            f"Pick Ticket: {pick_ticket_number or ''}",
            f"Carrier: {carrier}",
            f"Tracking: {tracking}",
            f"Weight: {weight_text}".rstrip(),
            f"Dimensions: {dimensions}".rstrip(),
            f"Status: {carton.status or ''}",
        ]
    )


def load_cartons_by_order(order_ids: list[int]) -> dict[int, list[Carton]]:
    buckets: dict[int, list[Carton]] = defaultdict(list)
    if not order_ids:
        return buckets
    cartons = Carton.query.filter(Carton.order_id.in_(order_ids)).order_by(Carton.id.asc()).all()
    for carton in cartons:
        buckets[carton.order_id].append(carton)
    return buckets


def is_processed_carton(carton: Carton) -> bool:
    return carton.status == CartonStatus.CLOSED and carton.closed_at is not None


def carton_in_day(carton: Carton, start, end) -> bool:
    if carton.closed_at is None:
        return False
    return start <= carton.closed_at < end


def load_carton_manifests(order_ids: list[int], *, carton_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """Bulk-load Carton → CartonContent → InventoryUnit products grouped per carton."""
    manifests: dict[int, list[dict]] = defaultdict(list)
    if not order_ids:
        return manifests
    query = Carton.query.filter(Carton.order_id.in_(order_ids))
    if carton_ids is not None:
        wanted = list(carton_ids) or [-1]
        query = query.filter(Carton.id.in_(wanted))
    cartons = query.order_by(Carton.id.asc()).all()
    if not cartons:
        return manifests
    ids = [carton.id for carton in cartons]
    ticket_ids = {carton.pick_ticket_id for carton in cartons if carton.pick_ticket_id}
    tickets = {
        int(row.id): row.pick_ticket_number
        for row in (
            db.session.query(PickTicket.id, PickTicket.pick_ticket_number)
            .filter(PickTicket.id.in_(ticket_ids or [-1]))
            .all()
            if ticket_ids
            else []
        )
    }
    contents = (
        db.session.query(CartonContent, InventoryUnit)
        .outerjoin(InventoryUnit, InventoryUnit.id == CartonContent.inventory_unit_id)
        .filter(CartonContent.carton_id.in_(ids))
        .all()
    )
    products_by_carton: dict[int, dict[tuple, dict]] = defaultdict(dict)
    for content, unit in contents:
        key = product_key(unit, content.upc)
        bucket = products_by_carton[content.carton_id]
        row = bucket.get(key)
        if row is None:
            bucket[key] = {
                "upc": key[0],
                "sku": key[1],
                "description": key[2],
                "style": key[3],
                "color": key[4],
                "size": key[5],
                "qty": 1,
            }
        else:
            row["qty"] += 1

    for carton in cartons:
        products = list(products_by_carton.get(carton.id, {}).values())
        products.sort(key=lambda row: (row["upc"], row["sku"], row["description"]))
        ticket_number = tickets.get(carton.pick_ticket_id) or ""
        manifests[carton.order_id].append(
            {
                "carton": carton,
                "header": carton_header_text(carton, ticket_number),
                "products": products,
                "pick_ticket_number": ticket_number,
                "tracking_number": (carton.tracking_number or "").strip(),
                "tracking_carrier": (carton.tracking_carrier or "").strip(),
                "unit_count": sum(int(row["qty"]) for row in products),
            }
        )
    return manifests
