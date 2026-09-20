from __future__ import annotations

from flask import Blueprint, render_template, request

from ..constants import OrderStatus, UnitStatus
from ..models import Box, InventoryUnit, Order, OrderException
from ..services.filters import apply_scope, parse_scope, scope_options

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    scope = parse_scope(request.args)

    def orders_q():
        return apply_scope(Order.query, Order, scope)

    def units_q():
        return apply_scope(InventoryUnit.query, InventoryUnit, scope)

    status_counts = {
        status: orders_q().filter(Order.status == status).count()
        for status in OrderStatus.ORDER
    }
    unit_counts = {
        "total": units_q().count(),
        "available": units_q().filter(InventoryUnit.status == UnitStatus.AVAILABLE).count(),
        "allocated": units_q().filter(InventoryUnit.status == UnitStatus.ALLOCATED).count(),
        "packed": units_q().filter(InventoryUnit.status == UnitStatus.PACKED).count(),
        "shipped": units_q().filter(InventoryUnit.status == UnitStatus.SHIPPED).count(),
    }

    box_q = Box.query.join(Order, Box.order_id == Order.id)
    box_q = apply_scope(box_q, Order, scope)
    exc_q = OrderException.query.filter_by(resolved=False)
    if any(scope.values()):
        exc_q = exc_q.join(Order, OrderException.order_id == Order.id)
        exc_q = apply_scope(exc_q, Order, scope)

    stats = {
        "orders_total": orders_q().count(),
        "open_exceptions": exc_q.count(),
        "boxes_total": box_q.count(),
    }
    recent_orders = orders_q().order_by(Order.created_at.desc()).limit(8).all()
    recent_exceptions = (
        OrderException.query.order_by(OrderException.created_at.desc()).limit(8).all()
    )
    return render_template(
        "dashboard.html",
        status_counts=status_counts,
        unit_counts=unit_counts,
        stats=stats,
        recent_orders=recent_orders,
        recent_exceptions=recent_exceptions,
        options=scope_options(scope["client_id"]),
        scope=scope,
    )
