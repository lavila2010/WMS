from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ..constants import OrderStatus
from ..extensions import db
from ..models import Allocation, Order
from ..services.allocation import (
    BarcodeError,
    active_allocations,
    allocate_barcode,
    allocation_progress,
    is_fully_allocated,
    mark_allocated,
    release_allocation,
)
from ..workflow import WorkflowError, transition

bp = Blueprint("allocation", __name__, url_prefix="/allocation")

_ALLOCATABLE = {
    OrderStatus.VALIDATED,
    OrderStatus.ALLOCATING,
    OrderStatus.ALLOCATED,
}


@bp.route("/")
def index():
    orders = (
        Order.query.filter(Order.status.in_(_ALLOCATABLE))
        .order_by(Order.created_at.desc())
        .all()
    )
    return render_template("allocation/index.html", orders=orders)


@bp.route("/<int:order_id>")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    progress = allocation_progress(order)
    allocations = active_allocations(order)
    return render_template(
        "allocation/detail.html",
        order=order,
        progress=progress,
        allocations=allocations,
        fully_allocated=progress["fully_allocated"],
    )


@bp.route("/<int:order_id>/scan", methods=["POST"])
def scan(order_id: int):
    order = Order.query.get_or_404(order_id)
    barcode = request.form.get("barcode", "")
    try:
        allocate_barcode(order, barcode)
        db.session.commit()
        flash(f"Allocated {barcode.strip()}.", "success")
    except BarcodeError as exc:
        db.session.commit()  # persist the recorded exception
        flash(f"{exc.exc_type}: {exc.message}", "error")
    return redirect(url_for("allocation.detail", order_id=order.id))


@bp.route("/<int:order_id>/mark-allocated", methods=["POST"])
def mark_allocated_view(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        mark_allocated(order)
        db.session.commit()
        flash(f"Order {order.order_number} fully allocated.", "success")
    except (BarcodeError, WorkflowError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("allocation.detail", order_id=order.id))


@bp.route("/<int:order_id>/ready-to-pick", methods=["POST"])
def ready_to_pick(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        transition(order, OrderStatus.READY_TO_PICK, "Released for picking.")
        db.session.commit()
        flash(f"Order {order.order_number} is ready to pick.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("allocation.detail", order_id=order.id))


@bp.route("/release/<int:allocation_id>", methods=["POST"])
def release(allocation_id: int):
    allocation = Allocation.query.get_or_404(allocation_id)
    order_id = allocation.order_id
    release_allocation(allocation)
    db.session.commit()
    flash(f"Released {allocation.barcode}.", "success")
    return redirect(url_for("allocation.detail", order_id=order_id))
