from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..auth import permission_required
from ..constants import AllocationStatus, OrderStatus
from ..extensions import db
from ..models import Allocation, Order, OrderLine
from ..services.allocation import AllocationError, allocate_order, release_allocation
from ..services.tenant import accessible_clients, require_entity_client, user_can_access_client

bp = Blueprint("allocation", __name__, url_prefix="/allocation")


@bp.route("/")
@permission_required("ALLOCATION_VIEW")
def index():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    query = Order.query.filter(Order.status.in_([OrderStatus.UNALLOCATED, OrderStatus.PARTIALLY_ALLOCATED]))
    if client_id:
        query = query.filter_by(client_id=client_id)
    elif not current_user.is_admin():
        query = query.filter(Order.client_id.in_([c.id for c in clients] or [-1]))
    orders = query.order_by(Order.created_at.desc()).limit(200).all()
    return render_template(
        "allocation/index.html",
        orders=orders,
        clients=clients,
        client_id=client_id,
    )


@bp.route("/<int:order_id>")
@permission_required("ALLOCATION_VIEW")
def detail(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    allocations = Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE).all()
    lines = OrderLine.query.filter_by(order_id=order.id).order_by(OrderLine.id).all()
    shortages = [
        {
            "upc": line.upc,
            "needed": max(line.qty_ordered - line.qty_allocated, 0),
            "ordered": line.qty_ordered,
            "allocated": line.qty_allocated,
        }
        for line in lines
        if line.qty_allocated < line.qty_ordered
    ]
    return render_template(
        "allocation/detail.html",
        order=order,
        allocations=allocations,
        lines=lines,
        shortages=shortages,
    )


@bp.route("/<int:order_id>/run", methods=["POST"])
@permission_required("ALLOCATION_EXECUTE")
def run(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    try:
        result = allocate_order(order, user=current_user)
    except (AllocationError, Exception) as exc:
        flash(str(exc), "error")
        return redirect(url_for("allocation.detail", order_id=order_id))
    if result["shortages"]:
        flash(
            f"Partial allocation: {result['reserved']} reserved. Shortages by UPC: "
            + ", ".join(f"{s['upc']}×{s['needed']}" for s in result["shortages"]),
            "warning",
        )
    else:
        flash(f"Allocation complete. Reserved {result['reserved']} units.", "success")
    return redirect(url_for("allocation.detail", order_id=order_id))


@bp.route("/release/<int:allocation_id>", methods=["POST"])
@permission_required("ALLOCATION_RELEASE")
def release(allocation_id):
    allocation = db.session.get(Allocation, allocation_id)
    if allocation is None:
        abort(404)
    require_entity_client(current_user, allocation)
    try:
        release_allocation(allocation)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("Allocation released.", "success")
    return redirect(url_for("allocation.detail", order_id=allocation.order_id))
