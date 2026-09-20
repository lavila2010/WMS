from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from ..constants import OrderStatus
from ..models import Order
from ..services.allocation import allocation_progress
from ..services.imports import ImportError_, build_orders_workbook, import_orders
from ..workflow import WorkflowError, transition

bp = Blueprint("orders", __name__, url_prefix="/orders")


@bp.route("/")
def index():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template("orders/index.html", orders=orders)


@bp.route("/<int:order_id>")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    progress = allocation_progress(order)
    return render_template("orders/detail.html", order=order, progress=progress)


@bp.route("/<int:order_id>/validate", methods=["POST"])
def validate(order_id: int):
    order = Order.query.get_or_404(order_id)
    if not order.lines:
        flash("Cannot validate an order with no lines.", "error")
        return redirect(url_for("orders.detail", order_id=order.id))
    try:
        transition(order, OrderStatus.VALIDATED, "Order validated.")
        from ..extensions import db

        db.session.commit()
        flash(f"Order {order.order_number} validated.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/import", methods=["GET", "POST"])
def import_view():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Orders.xlsx file.", "error")
            return redirect(url_for("orders.import_view"))
        try:
            batch = import_orders(file.stream, file.filename)
        except ImportError_ as exc:
            flash(str(exc), "error")
            return redirect(url_for("orders.import_view"))
        flash(batch.message, "success")
        return redirect(url_for("orders.index"))
    return render_template("orders/import.html")


@bp.route("/template")
def template():
    buf = build_orders_workbook(
        [
            {"order_number": "SO-1001", "customer": "Acme", "sku": "SKU-A", "quantity": 2, "description": "Sample A"},
            {"order_number": "SO-1001", "customer": "Acme", "sku": "SKU-B", "quantity": 1, "description": "Sample B"},
        ]
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name="Orders.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
