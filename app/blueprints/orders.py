from __future__ import annotations

import io
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func

from ..auth import permission_required
from ..constants import OrderStatus
from ..extensions import db
from ..models import Division, ImportBatch, Order, OrderLine, PickTicket, Warehouse
from ..services.order_import import (
    OrderImportError,
    analyze,
    clear_preview,
    commit_import,
    load_preview,
    resolve_context,
    save_preview,
    template_bytes,
)
from ..services.tenant import accessible_clients, require_entity_client, user_can_access_client

bp = Blueprint("orders", __name__, url_prefix="/orders")


def _scope():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int) or request.form.get("client_id", type=int)
    warehouse_id = request.args.get("warehouse_id", type=int) or request.form.get(
        "warehouse_id", type=int
    )
    division_id = request.args.get("division_id", type=int) or request.form.get("division_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    accessible_ids = [c.id for c in clients] or [-1]
    warehouses = []
    if client_id:
        warehouses = Warehouse.query.filter_by(client_id=client_id).order_by(Warehouse.warehouse_symbol).all()
        if warehouse_id and not any(w.id == warehouse_id for w in warehouses):
            warehouse_id = None
    if current_user.is_admin():
        divisions = Division.query.order_by(Division.code).all()
    else:
        divisions = (
            Division.query.filter(Division.client_id.in_(accessible_ids)).order_by(Division.code).all()
        )
    if division_id and not any(d.id == division_id for d in divisions):
        division_id = None
    return {
        "clients": clients,
        "warehouses": warehouses,
        "divisions": divisions,
        "scope": {
            "client_id": client_id,
            "warehouse_id": warehouse_id,
            "division_id": division_id,
        },
    }


def _page(template, active_tab, **kwargs):
    ctx = _scope()
    return render_template(
        template,
        active_tab=active_tab,
        options={
            "clients": ctx["clients"],
            "warehouses": ctx["warehouses"],
            "divisions": ctx["divisions"],
        },
        scope=ctx["scope"],
        **kwargs,
    )


@bp.route("/")
@permission_required("ORDERS_VIEW")
def index():
    ctx = _scope()
    scope = ctx["scope"]
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    date_str = request.args.get("date", "").strip()
    client_selected = bool(scope["client_id"])
    rows = []
    kpis = {"orders_today": 0, "open": 0, "allocated": 0, "short": 0, "pick_tickets": 0, "closed_today": 0}
    daily = []
    breakdown = []
    if client_selected:
        query = Order.query.filter_by(client_id=scope["client_id"])
        if scope["warehouse_id"]:
            query = query.filter_by(warehouse_id=scope["warehouse_id"])
        if status:
            query = query.filter_by(status=status)
        if date_str:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            query = query.filter(Order.created_at >= day, Order.created_at < day + timedelta(days=1))
        if q:
            like = f"%{q}%"
            query = query.filter(
                db.or_(
                    Order.client_order_number.ilike(like),
                    Order.wms_order_id.ilike(like),
                    Order.customer.ilike(like),
                    Order.carrier.ilike(like),
                )
            )
        orders = query.order_by(Order.created_at.desc()).limit(200).all()
        for order in orders:
            units = sum(line.qty_ordered for line in order.lines)
            allocated = sum(line.qty_allocated for line in order.lines)
            ticket = PickTicket.query.filter_by(order_id=order.id).first()
            rows.append(
                {
                    "order": order,
                    "units": units,
                    "allocated": allocated,
                    "short": max(units - allocated, 0),
                    "pick_ticket": ticket.pick_ticket_number if ticket else "—",
                }
            )
        all_client = Order.query.filter_by(client_id=scope["client_id"])
        if scope["warehouse_id"]:
            all_client = all_client.filter_by(warehouse_id=scope["warehouse_id"])
        today = datetime.utcnow().date()
        start = datetime(today.year, today.month, today.day)
        kpis["orders_today"] = all_client.filter(Order.created_at >= start).count()
        kpis["open"] = all_client.filter(Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED])).count()
        kpis["allocated"] = all_client.filter(
            Order.status.in_([OrderStatus.ALLOCATED, OrderStatus.PICK_TICKET_READY, OrderStatus.PROCESSING])
        ).count()
        kpis["short"] = all_client.filter_by(status=OrderStatus.PARTIALLY_ALLOCATED).count()
        kpis["pick_tickets"] = (
            PickTicket.query.join(Order, Order.id == PickTicket.order_id)
            .filter(Order.client_id == scope["client_id"])
            .count()
        )
        kpis["closed_today"] = all_client.filter(
            Order.status == OrderStatus.CLOSED, Order.closed_at >= start
        ).count()
        for status_name in OrderStatus.ALL:
            subset = all_client.filter_by(status=status_name)
            count = subset.count()
            units = (
                db.session.query(func.coalesce(func.sum(OrderLine.qty_ordered), 0))
                .join(Order, Order.id == OrderLine.order_id)
                .filter(Order.client_id == scope["client_id"], Order.status == status_name)
            )
            if scope["warehouse_id"]:
                units = units.filter(Order.warehouse_id == scope["warehouse_id"])
            breakdown.append({"status": status_name, "orders": count, "units": int(units.scalar() or 0)})
    return _page(
        "orders/index.html",
        "management",
        client_selected=client_selected,
        q=q,
        status=status,
        date_str=date_str,
        rows=rows,
        kpis=kpis,
        daily=daily,
        breakdown=breakdown,
        status_pills=[("", "All")] + [(s, s.replace("_", " ").title()) for s in OrderStatus.ALL],
    )


@bp.route("/<int:order_id>")
@permission_required("ORDERS_VIEW")
def detail(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    return render_template("orders/detail.html", order=order)


@bp.route("/upload", methods=["GET"])
@permission_required("ORDERS_UPLOAD")
def import_view():
    token = session.get("ord_preview_token")
    preview = load_preview(token) if token else None
    return _page("orders/upload.html", "upload", preview=preview, last_result=None)


@bp.route("/upload/preview", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_preview():
    file = request.files.get("file")
    try:
        client, division = resolve_context(
            current_user,
            request.form.get("client_id", type=int),
            request.form.get("division_id", type=int),
        )
    except OrderImportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.import_view"))
    if file is None or not file.filename:
        flash("Select an Excel file.", "error")
        return redirect(url_for("orders.import_view", client_id=client.id, division_id=division.id))
    try:
        preview = analyze(file.stream, file.filename, client, division)
    except OrderImportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.import_view", client_id=client.id, division_id=division.id))
    session["ord_preview_token"] = save_preview(preview)
    return redirect(url_for("orders.import_view", client_id=client.id, division_id=division.id))


@bp.route("/upload/confirm", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_confirm():
    token = session.get("ord_preview_token")
    preview = load_preview(token) if token else None
    if not preview:
        flash("Preview expired. Upload the file again.", "error")
        return redirect(url_for("orders.import_view"))
    try:
        resolve_context(current_user, preview["client_id"], preview["division_id"])
        if preview["has_blocking"]:
            raise OrderImportError("Confirm is disabled while blocking errors exist.")
        batch = commit_import(preview, user=current_user)
    except (OrderImportError, Exception) as exc:
        flash(str(exc), "error")
        return redirect(
            url_for("orders.import_view", client_id=preview["client_id"], division_id=preview["division_id"])
        )
    clear_preview(token)
    session.pop("ord_preview_token", None)
    flash(f"Imported {batch.rows_imported} orders from {batch.filename}.", "success")
    return redirect(url_for("orders.index", client_id=batch.client_id))


@bp.route("/upload/cancel", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_cancel():
    token = session.pop("ord_preview_token", None)
    clear_preview(token)
    flash("Order preview cancelled.", "success")
    return redirect(url_for("orders.import_view"))


@bp.route("/template")
@permission_required("ORDERS_UPLOAD")
def template():
    return send_file(
        io.BytesIO(template_bytes()),
        as_attachment=True,
        download_name="WMS_V2_Orders_Template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/pick-tickets")
@permission_required("PICK_TICKET_VIEW")
def pick_tickets():
    ctx = _scope()
    eligible = []
    tickets = []
    if ctx["scope"]["client_id"]:
        eligible = (
            Order.query.filter_by(client_id=ctx["scope"]["client_id"], status=OrderStatus.ALLOCATED)
            .order_by(Order.created_at.desc())
            .all()
        )
        tickets = (
            PickTicket.query.filter_by(client_id=ctx["scope"]["client_id"])
            .order_by(PickTicket.created_at.desc())
            .all()
        )
    return _page("orders/pick_tickets.html", "pick_tickets", tickets=tickets, eligible=eligible)


@bp.route("/allocation-report")
@permission_required("ALLOCATION_VIEW")
def allocation_report():
    return _page("orders/allocation_report.html", "allocation", results=[])


@bp.route("/<int:order_id>/pick-ticket", methods=["POST"])
@permission_required("PICK_TICKET_GENERATE")
def generate_pick_ticket(order_id):
    from ..services.pick_tickets import PickTicketError, create_pick_ticket

    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    try:
        ticket = create_pick_ticket(order)
    except PickTicketError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.pick_tickets", client_id=order.client_id))
    flash(f"Pick ticket {ticket.pick_ticket_number} created.", "success")
    return redirect(url_for("orders.pick_ticket_preview", ticket_id=ticket.id))


@bp.route("/pick-tickets/<int:ticket_id>")
@permission_required("PICK_TICKET_VIEW")
def pick_ticket_preview(ticket_id):
    from ..services.pick_tickets import ticket_lines

    ticket = db.session.get(PickTicket, ticket_id)
    if ticket is None:
        abort(404)
    require_entity_client(current_user, ticket)
    order = db.session.get(Order, ticket.order_id)
    return render_template(
        "orders/pick_ticket_preview.html",
        ticket=ticket,
        order=order,
        lines=ticket_lines(order),
    )


@bp.route("/pick-tickets/<int:ticket_id>/print", methods=["POST"])
@permission_required("PICK_TICKET_PRINT")
def pick_ticket_print(ticket_id):
    from ..services.pick_tickets import record_print

    ticket = db.session.get(PickTicket, ticket_id)
    if ticket is None:
        abort(404)
    require_entity_client(current_user, ticket)
    record_print(ticket, source="UI")
    flash(f"Recorded reprint of {ticket.pick_ticket_number}.", "success")
    return redirect(url_for("orders.pick_ticket_preview", ticket_id=ticket.id))


@bp.route("/pick-tickets/<int:ticket_id>/pdf")
@permission_required("PICK_TICKET_VIEW")
def pick_ticket_pdf(ticket_id):
    from ..services.pick_tickets import render_pdf

    ticket = db.session.get(PickTicket, ticket_id)
    if ticket is None:
        abort(404)
    require_entity_client(current_user, ticket)
    return send_file(
        io.BytesIO(render_pdf(ticket)),
        as_attachment=True,
        download_name=f"{ticket.pick_ticket_number}.pdf",
        mimetype="application/pdf",
    )
