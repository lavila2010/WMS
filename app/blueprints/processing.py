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
from ..models import Box, Order
from ..services.allocation import BarcodeError
from ..services.packing import (
    close_box,
    close_order,
    create_box,
    mark_processed,
    mark_ready_to_close,
    reconcile,
    scan_into_box,
)
from ..workflow import WorkflowError

bp = Blueprint("processing", __name__, url_prefix="/processing")

_PROCESSABLE = {
    OrderStatus.READY_TO_PICK,
    OrderStatus.PROCESSING,
    OrderStatus.PROCESSED,
    OrderStatus.READY_TO_CLOSE,
}


@bp.route("/")
def index():
    orders = (
        Order.query.filter(Order.status.in_(_PROCESSABLE))
        .order_by(Order.created_at.desc())
        .all()
    )
    return render_template("processing/index.html", orders=orders)


@bp.route("/<int:order_id>")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    rec = reconcile(order)
    return render_template("processing/detail.html", order=order, rec=rec)


@bp.route("/<int:order_id>/box", methods=["POST"])
def new_box(order_id: int):
    order = Order.query.get_or_404(order_id)

    def _f(name):
        val = request.form.get(name, "").strip()
        return float(val) if val else None

    box_number = request.form.get("box_number", "").strip()
    if not box_number:
        box_number = f"BOX-{len(order.boxes) + 1:03d}"
    try:
        create_box(order, box_number, _f("length_cm"), _f("width_cm"), _f("height_cm"))
        db.session.commit()
        flash(f"Box {box_number} created.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/box/<int:box_id>/scan", methods=["POST"])
def scan(box_id: int):
    box = Box.query.get_or_404(box_id)
    barcode = request.form.get("barcode", "")
    try:
        scan_into_box(box, barcode)
        db.session.commit()
        flash(f"Packed {barcode.strip()} into {box.box_number}.", "success")
    except BarcodeError as exc:
        db.session.commit()
        flash(f"{exc.exc_type}: {exc.message}", "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/box/<int:box_id>/close", methods=["POST"])
def close(box_id: int):
    box = Box.query.get_or_404(box_id)
    weight = request.form.get("weight_kg", "").strip()
    try:
        close_box(box, float(weight) if weight else None)
        db.session.commit()
        flash(f"Box {box.box_number} closed.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/<int:order_id>/processed", methods=["POST"])
def processed(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        mark_processed(order)
        db.session.commit()
        flash(f"Order {order.order_number} marked PROCESSED.", "success")
    except (ValueError, WorkflowError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>/ready-to-close", methods=["POST"])
def ready_to_close(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        mark_ready_to_close(order)
        db.session.commit()
        flash(f"Order {order.order_number} ready to close.", "success")
    except (ValueError, WorkflowError) as exc:
        db.session.commit()
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>/close", methods=["POST"])
def close_order_view(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        close_order(order)
        db.session.commit()
        flash(f"Order {order.order_number} CLOSED.", "success")
    except (ValueError, WorkflowError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))
