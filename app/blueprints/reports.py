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

from ..auth import permission_required, record_audit
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
@permission_required("REPORTS_VIEW")
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


def _audit_pdf(doc, order_id=None):
    record_audit("PDF_GENERATED", module="Reports", entity_type="Document", entity_id=doc.id, detail=doc.type, commit=True)


@bp.route("/order/<int:order_id>/pick-ticket", methods=["POST"])
@permission_required("REPORTS_EXPORT")
def pick_ticket(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_pick_ticket(order)
    _audit_pdf(doc)
    flash(f"Pick Ticket generated for {order.order_number}.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/order/<int:order_id>/packing-report", methods=["POST"])
@permission_required("REPORTS_EXPORT")
def packing_report(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_packing_report(order)
    _audit_pdf(doc)
    flash(f"Packing Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/order/<int:order_id>/closure", methods=["POST"])
@permission_required("REPORTS_EXPORT")
def closure(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_order_closure(order)
    _audit_pdf(doc)
    flash(f"Order Closure Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/box/<int:box_id>/detail", methods=["POST"])
@permission_required("REPORTS_EXPORT")
def box_detail(box_id: int):
    box = Box.query.get_or_404(box_id)
    doc = generate_box_detail(box)
    _audit_pdf(doc)
    flash(f"Box Detail generated for {box.box_number}.", "success")
    return redirect(url_for("reports.index"))


@bp.route("/document/<int:document_id>")
@permission_required("DOCUMENT_REPRINT")
def download(document_id: int):
    doc = Document.query.get_or_404(document_id)
    import os

    if not os.path.exists(doc.path):
        abort(404)
    record_audit("DOCUMENT_REPRINT", module="Reports", entity_type="Document", entity_id=doc.id, detail=doc.filename, commit=True)
    return send_file(doc.path, as_attachment=True, download_name=doc.filename)
