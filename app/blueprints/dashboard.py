from __future__ import annotations

from flask import Blueprint, render_template

from ..constants import OrderStatus, UnitStatus
from ..extensions import db
from ..models import InventoryUnit, Order, OrderException, Box

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    status_counts = {
        status: Order.query.filter_by(status=status).count()
        for status in OrderStatus.ORDER
    }
    unit_counts = {
        "total": InventoryUnit.query.count(),
        "in_stock": InventoryUnit.query.filter_by(status=UnitStatus.IN_STOCK).count(),
        "allocated": InventoryUnit.query.filter_by(status=UnitStatus.ALLOCATED).count(),
        "packed": InventoryUnit.query.filter_by(status=UnitStatus.PACKED).count(),
        "shipped": InventoryUnit.query.filter_by(status=UnitStatus.SHIPPED).count(),
    }
    stats = {
        "orders_total": Order.query.count(),
        "open_exceptions": OrderException.query.filter_by(resolved=False).count(),
        "boxes_total": Box.query.count(),
    }
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(8).all()
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
    )
