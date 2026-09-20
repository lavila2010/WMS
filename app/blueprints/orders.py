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

from flask_login import current_user

from ..auth import current_actor, permission_required, record_audit
from ..constants import OrderStatus
from ..models import Order
from ..services.allocation import active_allocations, allocation_progress
from ..services.filters import apply_scope, parse_scope, scope_options
from ..services.imports import ImportError_, build_orders_workbook, import_orders
from ..services.packing import packed_count
from ..workflow import WorkflowError, transition

bp = Blueprint("orders", __name__, url_prefix="/orders")


@bp.route("/")
@permission_required("ORDERS_VIEW")
def index():
    scope = parse_scope(request.args)
    query = apply_scope(Order.query, Order, scope)
    orders = query.order_by(Order.created_at.desc()).all()
    rows = []
    for o in orders:
        rows.append(
            {
                "order": o,
                "units": o.ordered_quantity,
                "allocated": len(active_allocations(o)),
                "processed": packed_count(o),
                "boxes": len(o.boxes),
                "exceptions": sum(1 for e in o.exceptions if not e.resolved),
            }
        )
    return render_template(
        "orders/index.html",
        rows=rows,
        options=scope_options(scope["client_id"]),
        scope=scope,
    )


@bp.route("/<int:order_id>")
@permission_required("ORDERS_VIEW")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    progress = allocation_progress(order)
    return render_template("orders/detail.html", order=order, progress=progress)


@bp.route("/<int:order_id>/validate", methods=["POST"])
@permission_required("ORDERS_VIEW")
def validate(order_id: int):
    order = Order.query.get_or_404(order_id)
    if not order.lines:
        flash("Cannot validate an order with no lines.", "error")
        return redirect(url_for("orders.detail", order_id=order.id))
    try:
        transition(order, OrderStatus.VALIDATED, "Order validated.")
        uid, uname = current_actor()
        order.validated_by_user_id = uid
        order.validated_by_username = uname
        from ..extensions import db

        db.session.commit()
        flash(f"Order {order.order_number} validated.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/import", methods=["GET", "POST"])
@permission_required("ORDERS_UPLOAD")
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
        record_audit("ORDER_IMPORT", module="Orders", entity_type="ImportBatch", entity_id=batch.id, detail=batch.message, commit=True)
        flash(batch.message, "success")
        return redirect(url_for("orders.index"))
    return render_template("orders/import.html")


@bp.route("/template")
@permission_required("ORDERS_UPLOAD")
def template():
    buf = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-1001", "customer": "Acme Retail", "sku": "SKU-A", "quantity": 2, "description": "Sample A", "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-1001", "customer": "Acme Retail", "sku": "SKU-B", "quantity": 1, "description": "Sample B", "carrier": "UPS", "shipping_service": "Ground"},
        ]
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name="Orders.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
