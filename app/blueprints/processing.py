from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from flask_login import current_user

from ..auth import current_actor, permission_required, record_audit
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
@permission_required("PROCESSING_VIEW")
def index():
    orders = (
        Order.query.filter(Order.status.in_(_PROCESSABLE))
        .order_by(Order.created_at.desc())
        .all()
    )
    return render_template("processing/index.html", orders=orders)


@bp.route("/<int:order_id>")
@permission_required("PROCESSING_VIEW")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    rec = reconcile(order)
    return render_template("processing/detail.html", order=order, rec=rec)


@bp.route("/<int:order_id>/box", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def new_box(order_id: int):
    order = Order.query.get_or_404(order_id)

    def _f(name):
        val = request.form.get(name, "").strip()
        return float(val) if val else None

    box_number = request.form.get("box_number", "").strip()
    if not box_number:
        box_number = f"{order.order_number}-BOX{len(order.boxes) + 1:02d}"
    try:
        box = create_box(order, box_number, _f("length_cm"), _f("width_cm"), _f("height_cm"))
        record_audit("BOX_CREATE", module="Processing", entity_type="Box", entity_id=box.id, detail=box_number)
        db.session.commit()
        flash(f"Box {box_number} created.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/box/<int:box_id>/scan", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def scan(box_id: int):
    box = Box.query.get_or_404(box_id)
    barcode = request.form.get("barcode", "")
    try:
        scan_into_box(box, barcode)
        record_audit("UNIT_SCAN", module="Processing", entity_type="Box", entity_id=box.id, detail=barcode.strip())
        db.session.commit()
        flash(f"Packed {barcode.strip()} into {box.box_number}.", "success")
    except BarcodeError as exc:
        db.session.commit()
        flash(f"{exc.exc_type}: {exc.message}", "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/box/<int:box_id>/close", methods=["POST"])
@permission_required("BOX_CLOSE")
def close(box_id: int):
    box = Box.query.get_or_404(box_id)
    weight = request.form.get("weight_kg", "").strip()
    try:
        close_box(box, float(weight) if weight else None)
        record_audit("BOX_CLOSE", module="Processing", entity_type="Box", entity_id=box.id, detail=f"{weight}kg")
        db.session.commit()
        flash(f"Box {box.box_number} closed.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/<int:order_id>/processed", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
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
@permission_required("PROCESSING_EXECUTE")
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
@permission_required("ORDER_CLOSE")
def close_order_view(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        _, invoice = close_order(order, created_by=current_user.username)
        uid, uname = current_actor()
        order.closed_by_user_id = uid
        order.closed_by_username = uname
        record_audit("ORDER_CLOSE", module="Processing", entity_type="Order", entity_id=order.id, detail=invoice.invoice_number)
        db.session.commit()
        flash(
            f"Order {order.order_number} CLOSED. Invoice {invoice.invoice_number} created.",
            "success",
        )
    except (ValueError, WorkflowError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception as exc:  # invoice creation failure -> full rollback
        db.session.rollback()
        flash(f"Order close failed and was rolled back: {exc}", "error")
    return redirect(url_for("processing.detail", order_id=order.id))
