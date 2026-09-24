from __future__ import annotations

from flask import Blueprint, render_template, request
from flask_login import current_user

from ..auth import permission_required
from ..constants import OrderStatus, UnitStatus
from ..models import Client, InventoryUnit, Order, Warehouse
from ..services.inventory_visibility import apply_operational_visibility
from ..services.tenant import accessible_clients, user_can_access_client

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@permission_required("DASHBOARD_VIEW")
def index():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    warehouse_id = request.args.get("warehouse_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        client_id = None
    warehouses = []
    if client_id:
        warehouses = Warehouse.query.filter_by(client_id=client_id).order_by(Warehouse.warehouse_symbol).all()
        if warehouse_id and not any(w.id == warehouse_id for w in warehouses):
            warehouse_id = None

    units = apply_operational_visibility(InventoryUnit.query)
    orders = Order.query
    if client_id:
        units = units.filter(InventoryUnit.client_id == client_id)
        orders = orders.filter_by(client_id=client_id)
    elif not current_user.is_admin():
        ids = [c.id for c in clients]
        units = units.filter(InventoryUnit.client_id.in_(ids or [-1]))
        orders = orders.filter(Order.client_id.in_(ids or [-1]))
    if warehouse_id:
        units = units.filter(InventoryUnit.warehouse_id == warehouse_id)
        orders = orders.filter_by(warehouse_id=warehouse_id)

    unit_counts = {
        "total": units.count(),
        "available": units.filter(InventoryUnit.status == UnitStatus.AVAILABLE).count(),
        "reserved": units.filter(InventoryUnit.status == UnitStatus.RESERVED).count(),
        "packed": units.filter(InventoryUnit.status == UnitStatus.PACKED).count(),
        "shipped": units.filter(InventoryUnit.status == UnitStatus.SHIPPED).count(),
    }
    status_counts = {s: orders.filter_by(status=s).count() for s in OrderStatus.ALL}
    return render_template(
        "dashboard.html",
        clients=clients,
        warehouses=warehouses,
        client_id=client_id,
        warehouse_id=warehouse_id,
        unit_counts=unit_counts,
        status_counts=status_counts,
        stats={"orders_total": orders.count(), "boxes_total": 0, "open_exceptions": 0},
    )
