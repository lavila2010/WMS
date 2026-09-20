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

from datetime import datetime

from sqlalchemy import func

from ..models import Box, Document, Invoice, Order
from ..services.documents import (
    generate_box_detail,
    generate_order_closure,
    generate_packing_report,
    generate_pick_ticket,
)
from ..services.filters import apply_scope, parse_scope, scope_options

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
def index():
    args = request.args
    scope = parse_scope(args)
    query = apply_scope(Order.query.outerjoin(Invoice, Invoice.order_id == Order.id), Order, scope)

    def like(field, value):
        nonlocal query
        value = (value or "").strip()
        if value:
            query = query.filter(field.ilike(f"%{value}%"))

    like(Order.order_number, args.get("order_number"))
    like(Order.customer, args.get("customer"))
    like(Order.carrier, args.get("carrier"))
    like(Order.shipping_service, args.get("shipping_service"))

    invoice_number = (args.get("invoice_number") or "").strip()
    if invoice_number:
        query = query.filter(Invoice.invoice_number.ilike(f"%{invoice_number}%"))

    status = (args.get("status") or "").strip()
    if status:
        query = query.filter(Order.status == status)

    date_str = (args.get("date") or "").strip()
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
            query = query.filter(func.date(Order.created_at) == day)
        except ValueError:
            pass

    orders = query.order_by(Order.created_at.desc()).all()
    documents = Document.query.order_by(Document.created_at.desc()).limit(50).all()
    from ..constants import OrderStatus

    return render_template(
        "reports/index.html",
        orders=orders,
        documents=documents,
        options=scope_options(scope["client_id"]),
        scope=scope,
        statuses=OrderStatus.ORDER,
        filters={
            "order_number": args.get("order_number", ""),
            "invoice_number": invoice_number,
            "customer": args.get("customer", ""),
            "carrier": args.get("carrier", ""),
            "shipping_service": args.get("shipping_service", ""),
            "status": status,
            "date": date_str,
        },
    )


@bp.route("/order/<int:order_id>/pick-ticket", methods=["POST"])
def pick_ticket(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_pick_ticket(order)
    flash(f"Pick Ticket generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/order/<int:order_id>/packing-report", methods=["POST"])
def packing_report(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_packing_report(order)
    flash(f"Packing Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/order/<int:order_id>/closure", methods=["POST"])
def closure(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_order_closure(order)
    flash(f"Order Closure Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/box/<int:box_id>/detail", methods=["POST"])
def box_detail(box_id: int):
    box = Box.query.get_or_404(box_id)
    doc = generate_box_detail(box)
    flash(f"Box Detail generated for {box.box_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/document/<int:document_id>")
def download(document_id: int):
    doc = Document.query.get_or_404(document_id)
    import os

    if not os.path.exists(doc.path):
        abort(404)
    return send_file(doc.path, as_attachment=True, download_name=doc.filename)
