"""Bulk allocation list/overview queries. No per-line COUNT loops."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import joinedload

from ..constants import AllocationStatus, OrderStatus, PickTicketStatus, UnitStatus
from ..extensions import db
from ..models import Order
from .order_visibility import apply_operational_order_visibility

PER_PAGE_OPTIONS = (25, 50, 100)
DEFAULT_PER_PAGE = 50
CANDIDATE_WINDOW = 500
ACTIVE_UNIT_STATUSES = (UnitStatus.RESERVED, UnitStatus.PACKED)
OPEN_TICKET_STATUSES = tuple(PickTicketStatus.LEGACY_OPEN)
HIDDEN_FROM_ALLOCATION = (
    OrderStatus.CLOSED,
    OrderStatus.CANCELLED,
    OrderStatus.PROCESSING,
)


def parse_per_page(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    if parsed in PER_PAGE_OPTIONS:
        return parsed
    return DEFAULT_PER_PAGE


def parse_page(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _id_sql(ids: list[int]) -> str:
    cleaned = [str(int(i)) for i in ids]
    return ",".join(cleaned) if cleaned else "NULL"


def _empty_page(page: int, per_page: int) -> dict:
    return {
        "rows": [],
        "page": page,
        "per_page": per_page,
        "total": 0,
        "pages": 1,
        "per_page_options": PER_PAGE_OPTIONS,
    }


def _overview_sql(candidate_sql: str) -> str:
    reserved, packed = UnitStatus.RESERVED, UnitStatus.PACKED
    active = AllocationStatus.ACTIVE
    open_sql = ",".join(f"'{status}'" for status in OPEN_TICKET_STATUSES)
    return f"""
        WITH candidates AS ({candidate_sql}),
        lines AS (
            SELECT order_id,
                   COALESCE(SUM(qty_ordered), 0)::int AS ordered,
                   COALESCE(SUM(qty_shipped), 0)::int AS shipped,
                   COALESCE(SUM(qty_short), 0)::int AS short
            FROM order_lines
            WHERE order_id IN (SELECT id FROM candidates)
            GROUP BY order_id
        ),
        live AS (
            SELECT a.order_id, COUNT(*)::int AS currently
            FROM allocations a
            JOIN inventory_units u ON u.id = a.inventory_unit_id
            WHERE a.order_id IN (SELECT id FROM candidates)
              AND a.status = '{active}'
              AND u.status IN ('{reserved}', '{packed}')
            GROUP BY a.order_id
        ),
        unticketed AS (
            SELECT a.order_id, COUNT(*)::int AS qty
            FROM allocations a
            JOIN orders o ON o.id = a.order_id
            WHERE a.order_id IN (SELECT id FROM candidates)
              AND a.status = '{active}'
              AND a.pick_ticket_id IS NULL
              AND (
                    COALESCE(o.current_wave_number, 0) = 0
                    OR a.wave_number = o.current_wave_number
              )
            GROUP BY a.order_id
        ),
        open_tix AS (
            SELECT order_id, COUNT(*)::int AS qty
            FROM pick_tickets
            WHERE order_id IN (SELECT id FROM candidates)
              AND status IN ({open_sql})
            GROUP BY order_id
        )
        SELECT o.id AS order_id,
               COALESCE(l.ordered, 0) AS ordered,
               COALESCE(l.shipped, 0) AS shipped,
               COALESCE(l.short, 0) AS short,
               COALESCE(lv.currently, 0) AS currently_allocated,
               GREATEST(
                   COALESCE(l.ordered, 0)
                   - COALESCE(l.shipped, 0)
                   - COALESCE(l.short, 0)
                   - COALESCE(lv.currently, 0),
                   0
               ) AS remaining,
               COALESCE(u.qty, 0) AS unticketed,
               COALESCE(t.qty, 0) AS open_tickets,
               o.current_wave_number,
               o.partial_approved_wave,
               o.status,
               o.created_at
        FROM orders o
        JOIN candidates c ON c.id = o.id
        LEFT JOIN lines l ON l.order_id = o.id
        LEFT JOIN live lv ON lv.order_id = o.id
        LEFT JOIN unticketed u ON u.order_id = o.id
        LEFT JOIN open_tix t ON t.order_id = o.id
    """


def _row_flags(row) -> dict:
    remaining = int(row.remaining)
    currently = int(row.currently_allocated)
    approved = bool(row.partial_approved_wave and row.partial_approved_wave == row.current_wave_number)
    ticket_ok = (
        currently > 0
        and int(row.unticketed) > 0
        and (remaining == 0 or approved)
        and row.status not in {OrderStatus.CLOSED, OrderStatus.CANCELLED}
    )
    if approved:
        partial = "APPROVED"
    elif currently > 0 and remaining > 0:
        partial = "PENDING"
    else:
        partial = "—"
    return {
        "ordered": int(row.ordered),
        "shipped": int(row.shipped),
        "short": int(getattr(row, "short", 0) or 0),
        "currently_allocated": currently,
        "remaining": remaining,
        "unticketed": int(row.unticketed),
        "open_tickets": int(row.open_tickets),
        "eligible": remaining > 0
        and not ticket_ok
        and row.status not in {OrderStatus.CLOSED, OrderStatus.CANCELLED},
        "partial_approval": partial,
        "can_approve": currently > 0 and remaining > 0 and not ticket_ok,
        "pick_ticket_eligible": ticket_ok,
        "pick_ticket_ready": ticket_ok,
    }


def _load_orders(ids: list[int]) -> dict[int, Order]:
    if not ids:
        return {}
    orders = (
        Order.query.options(
            joinedload(Order.client),
            joinedload(Order.division),
            joinedload(Order.warehouse),
        )
        .filter(Order.id.in_(ids))
        .all()
    )
    return {order.id: order for order in orders}


def allocation_overview_page(
    *,
    client_id=None,
    accessible_client_ids=None,
    admin=False,
    page=1,
    per_page=DEFAULT_PER_PAGE,
) -> dict:
    per_page = parse_per_page(per_page)
    page = parse_page(page)
    query = apply_operational_order_visibility(
        Order.query.filter(Order.status.notin_(HIDDEN_FROM_ALLOCATION))
    )
    if client_id:
        query = query.filter(Order.client_id == int(client_id))
    elif not admin:
        query = query.filter(Order.client_id.in_(list(accessible_client_ids) or [-1]))
    candidate_ids = [
        row[0]
        for row in query.with_entities(Order.id).order_by(Order.created_at.desc()).limit(CANDIDATE_WINDOW).all()
    ]
    if not candidate_ids:
        return _empty_page(page, per_page)
    sql = _overview_sql(f"SELECT id FROM orders WHERE id IN ({_id_sql(candidate_ids)})")
    sql += """
        WHERE COALESCE(t.qty, 0) = 0
          AND (
                GREATEST(
                    COALESCE(l.ordered, 0)
                    - COALESCE(l.shipped, 0)
                    - COALESCE(l.short, 0)
                    - COALESCE(lv.currently, 0),
                    0
                ) > 0
                OR (
                    COALESCE(lv.currently, 0) > 0
                    AND COALESCE(u.qty, 0) > 0
                    AND (
                        GREATEST(
                            COALESCE(l.ordered, 0)
                            - COALESCE(l.shipped, 0)
                            - COALESCE(l.short, 0)
                            - COALESCE(lv.currently, 0),
                            0
                        ) = 0
                        OR (
                            o.partial_approved_wave IS NOT NULL
                            AND o.partial_approved_wave = o.current_wave_number
                        )
                    )
                )
          )
        ORDER BY o.created_at DESC
    """
    matched = list(db.session.execute(text(sql)))
    total = len(matched)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page
    sliced = matched[offset : offset + per_page]
    orders = _load_orders([int(row.order_id) for row in sliced])
    rows = []
    for row in sliced:
        order = orders.get(int(row.order_id))
        if order is None:
            continue
        payload = _row_flags(row)
        payload["order"] = order
        payload["order_id"] = order.id
        payload["wms_order_id"] = order.wms_order_id
        payload["client_order_number"] = order.client_order_number
        payload["client_code"] = order.client.client_code if order.client else None
        payload["division_code"] = order.division.code if order.division else None
        payload["warehouse_code"] = order.warehouse.warehouse_code if order.warehouse else None
        payload["customer"] = order.customer
        payload["status"] = order.status
        payload["created_at"] = order.created_at
        rows.append(payload)
    return {
        "rows": rows,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "per_page_options": PER_PAGE_OPTIONS,
    }


def bulk_order_quantity_rows(order_ids: list[int]) -> dict[int, dict]:
    ids = [int(i) for i in order_ids if i]
    if not ids:
        return {}
    sql = _overview_sql(f"SELECT id FROM orders WHERE id IN ({_id_sql(ids)})")
    result = {}
    for row in db.session.execute(text(sql)):
        result[int(row.order_id)] = _row_flags(row)
    return result


def bulk_upc_rows(order_ids: list[int]) -> dict[int, list[dict]]:
    ids = [int(i) for i in order_ids if i]
    if not ids:
        return {}
    in_sql = _id_sql(ids)
    line_sql = text(
        f"""
        SELECT ol.order_id, ol.upc, ol.sku, ol.description,
               ol.qty_ordered, ol.qty_shipped, COALESCE(ol.qty_short, 0)::int AS qty_short,
               COUNT(u.id) FILTER (
                   WHERE a.status = '{AllocationStatus.ACTIVE}'
                     AND u.status IN ('{UnitStatus.RESERVED}', '{UnitStatus.PACKED}')
               )::int AS allocated_qty
        FROM order_lines ol
        LEFT JOIN allocations a ON a.order_line_id = ol.id AND a.status = '{AllocationStatus.ACTIVE}'
        LEFT JOIN inventory_units u ON u.id = a.inventory_unit_id
        WHERE ol.order_id IN ({in_sql})
        GROUP BY ol.id, ol.order_id, ol.upc, ol.sku, ol.description,
                 ol.qty_ordered, ol.qty_shipped, ol.qty_short
        ORDER BY ol.order_id, ol.id
        """
    )
    meta_sql = text(
        f"""
        SELECT DISTINCT ON (allocated_order_id, upc)
               allocated_order_id, upc, style, color, size, sku, description
        FROM inventory_units
        WHERE allocated_order_id IN ({in_sql})
        ORDER BY allocated_order_id, upc, id
        """
    )
    meta = {}
    for row in db.session.execute(meta_sql):
        meta[(int(row.allocated_order_id), row.upc)] = row
    grouped: dict[int, list[dict]] = {oid: [] for oid in ids}
    for row in db.session.execute(line_sql):
        live = int(row.allocated_qty or 0)
        short = int(row.qty_short or 0)
        remaining = max(int(row.qty_ordered) - int(row.qty_shipped or 0) - short - live, 0)
        extra = meta.get((int(row.order_id), row.upc))
        grouped.setdefault(int(row.order_id), []).append(
            {
                "upc": row.upc,
                "sku": row.sku or (extra.sku if extra else None),
                "description": row.description or (extra.description if extra else None),
                "style": extra.style if extra else None,
                "color": extra.color if extra else None,
                "size": extra.size if extra else None,
                "ordered_qty": int(row.qty_ordered),
                "shipped_qty": int(row.qty_shipped or 0),
                "short_qty": short,
                "allocated_qty": live,
                "remaining_qty": remaining,
            }
        )
    return grouped
