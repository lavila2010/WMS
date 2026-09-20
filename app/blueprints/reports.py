from __future__ import annotations

import io
from datetime import datetime, timedelta

import pandas as pd
from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from ..auth import permission_required, record_audit
from ..constants import OrderStatus
from ..extensions import db
from ..models import Carton, CartonContent, Division, Document, Invoice, Order, Warehouse
from ..services.documents import get_store, persist_closure_pdf
from ..services.tenant import accessible_clients, require_entity_client, user_can_access_client

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _filters():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    warehouse_id = request.args.get("warehouse_id", type=int)
    division_id = request.args.get("division_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    if not current_user.is_admin() and not client_id and len(clients) == 1:
        client_id = clients[0].id
    warehouses = Warehouse.query.filter_by(client_id=client_id).all() if client_id else []
    divisions = Division.query.filter_by(client_id=client_id).all() if client_id else []
    return {
        "clients": clients,
        "warehouses": warehouses,
        "divisions": divisions,
        "client_id": client_id,
        "warehouse_id": warehouse_id,
        "division_id": division_id,
    }


def _query(scope, filters):
    query = Order.query.filter_by(status=OrderStatus.CLOSED)
    if scope["client_id"]:
        query = query.filter_by(client_id=scope["client_id"])
    elif not current_user.is_admin():
        query = query.filter(Order.client_id.in_([c.id for c in scope["clients"]] or [-1]))
    if scope["warehouse_id"]:
        query = query.filter_by(warehouse_id=scope["warehouse_id"])
    if scope["division_id"]:
        query = query.filter_by(division_id=scope["division_id"])
    if filters.get("carrier"):
        query = query.filter(Order.carrier.ilike(f"%{filters['carrier']}%"))
    if filters.get("closed_by"):
        query = query.filter(Order.closed_by_username.ilike(f"%{filters['closed_by']}%"))
    if filters.get("date_from"):
        query = query.filter(Order.closed_at >= datetime.strptime(filters["date_from"], "%Y-%m-%d"))
    if filters.get("date_to"):
        query = query.filter(
            Order.closed_at < datetime.strptime(filters["date_to"], "%Y-%m-%d") + timedelta(days=1)
        )
    if filters.get("order_number"):
        like = f"%{filters['order_number']}%"
        query = query.filter(db.or_(Order.wms_order_id.ilike(like), Order.client_order_number.ilike(like)))
    return query.order_by(Order.closed_at.desc())


@bp.route("/")
@permission_required("REPORTS_VIEW")
def index():
    scope = _filters()
    filters = {
        "order_number": request.args.get("order_number", ""),
        "invoice_number": request.args.get("invoice_number", ""),
        "customer": request.args.get("customer", ""),
        "carrier": request.args.get("carrier", ""),
        "shipping_service": request.args.get("shipping_service", ""),
        "status": request.args.get("status", OrderStatus.CLOSED),
        "date": request.args.get("date", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "closed_by": request.args.get("closed_by", ""),
    }
    orders = _query(scope, filters).limit(200).all()
    docs = Document.query
    if scope["client_id"]:
        docs = docs.filter_by(client_id=scope["client_id"])
    elif not current_user.is_admin():
        docs = docs.filter(Document.client_id.in_([c.id for c in scope["clients"]] or [-1]))
    documents = docs.order_by(Document.created_at.desc()).limit(50).all()
    return render_template(
        "reports/index.html",
        orders=orders,
        documents=documents,
        filters=filters,
        statuses=[OrderStatus.CLOSED],
        options={"clients": scope["clients"], "warehouses": scope["warehouses"], "divisions": scope["divisions"]},
        scope=scope,
    )


@bp.route("/export.xlsx")
@permission_required("REPORTS_EXPORT")
def export_completed():
    scope = _filters()
    filters = {
        "carrier": request.args.get("carrier", ""),
        "closed_by": request.args.get("closed_by", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "order_number": request.args.get("order_number", ""),
    }
    orders = _query(scope, filters).all()
    rows = []
    for order in orders:
        invoice = Invoice.query.filter_by(order_id=order.id).first()
        cartons = Carton.query.filter_by(order_id=order.id).all()
        rows.append(
            {
                "Client": order.client.client_code,
                "Division": order.division.code,
                "Warehouse": order.warehouse.warehouse_code,
                "OrderNumber": order.client_order_number,
                "WmsOrderId": order.wms_order_id,
                "Customer": order.customer,
                "Carrier": order.carrier,
                "ShippingService": order.shipping_service,
                "ClosedAt": order.closed_at,
                "ClosedBy": order.closed_by_username,
                "Ordered": sum(l.qty_ordered for l in order.lines),
                "Allocated": sum(l.qty_allocated for l in order.lines),
                "Packed": sum(l.qty_packed for l in order.lines),
                "Shipped": sum(l.qty_shipped for l in order.lines),
                "Cartons": len(cartons),
                "Invoice": invoice.invoice_number if invoice else "",
            }
        )
    buffer = io.BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="completed_orders.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/<int:order_id>/closure", methods=["POST"])
@permission_required("DOCUMENT_REPRINT")
def closure(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    document = persist_closure_pdf(order)
    record_audit(
        "PDF_PRINTED",
        module="Reports",
        entity_type="document",
        entity_id=document.id,
        client_id=order.client_id,
        detail="reprint closure",
    )
    db.session.commit()
    return redirect(url_for("reports.download", document_id=document.id))


@bp.route("/documents/<int:document_id>")
@permission_required("DOCUMENT_REPRINT")
def download(document_id):
    document = db.session.get(Document, document_id)
    if document is None:
        abort(404)
    require_entity_client(current_user, document)
    data = get_store().open(document.storage_key)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=document.filename, mimetype="application/pdf")
