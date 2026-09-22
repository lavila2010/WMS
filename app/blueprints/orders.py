from __future__ import annotations

import io
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func

from ..auth import permission_required, record_audit
from ..constants import ImportBatchStatus, OrderStatus, ShippingStatus, TrackingCarrier
from ..extensions import db
from ..models import Carton, Division, Document, ImportBatch, Order, OrderLine, PickTicket, Warehouse
from ..services.documents import get_store
from ..services.end_of_day import (
    build_eod_rows,
    closed_orders_query,
    eod_kpis,
    export_eod_excel,
    operational_today,
    parse_eod_date,
)
from ..services.order_import import (
    OrderImportError,
    analyze,
    batch_progress,
    begin_processing,
    cancel_validated_batch,
    cleanup_failed_import,
    clear_preview,
    load_preview,
    preview_from_batch,
    process_import_batch,
    resolve_context,
    retry_import,
    save_preview,
    template_bytes,
)
from ..services.order_visibility import apply_operational_order_visibility
from ..services.shipping_report import (
    ShippingReportAccessError,
    ShippingReportSelectionError,
    export_shipping_report,
    list_shipping_report_page,
    report_filter_choices,
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
    if client_id:
        warehouses = Warehouse.query.filter_by(client_id=client_id).order_by(Warehouse.warehouse_code).all()
    else:
        warehouses = (
            Warehouse.query.filter(Warehouse.client_id.in_(accessible_ids))
            .order_by(Warehouse.warehouse_code)
            .all()
        )
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
        query = apply_operational_order_visibility(Order.query.filter_by(client_id=scope["client_id"]))
        if scope["warehouse_id"]:
            query = query.filter(Order.warehouse_id == scope["warehouse_id"])
        if status:
            query = query.filter(Order.status == status)
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
        all_client = apply_operational_order_visibility(Order.query.filter_by(client_id=scope["client_id"]))
        if scope["warehouse_id"]:
            all_client = all_client.filter(Order.warehouse_id == scope["warehouse_id"])
        today = datetime.utcnow().date()
        start = datetime(today.year, today.month, today.day)
        kpis["orders_today"] = all_client.filter(Order.created_at >= start).count()
        kpis["open"] = all_client.filter(Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED])).count()
        kpis["allocated"] = all_client.filter(
            Order.status.in_([OrderStatus.ALLOCATED, OrderStatus.PICK_TICKET_READY, OrderStatus.PROCESSING])
        ).count()
        kpis["short"] = all_client.filter(Order.status == OrderStatus.PARTIALLY_ALLOCATED).count()
        kpis["pick_tickets"] = (
            PickTicket.query.join(Order, Order.id == PickTicket.order_id)
            .filter(Order.client_id == scope["client_id"])
            .count()
        )
        kpis["closed_today"] = all_client.filter(
            Order.status == OrderStatus.CLOSED, Order.closed_at >= start
        ).count()
        for status_name in OrderStatus.ALL:
            subset = all_client.filter(Order.status == status_name)
            count = subset.count()
            units = apply_operational_order_visibility(
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
    closure = Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").order_by(Document.id.asc()).first()
    packing = Document.query.filter_by(order_id=order.id, type="PACKING_LIST").order_by(Document.id.asc()).first()
    return render_template("orders/detail.html", order=order, closure=closure, packing=packing)


def _active_order_batch():
    batch_id = request.args.get("import_batch_id", type=int) or session.get("ord_import_batch_id")
    if not batch_id:
        return None
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        return None
    return batch


@bp.route("/upload", methods=["GET"])
@permission_required("ORDERS_UPLOAD")
def import_view():
    preview = None
    processing_batch = None
    token = session.get("ord_preview_token")
    if token:
        preview = load_preview(token)
        if preview and preview.get("batch_id"):
            batch = db.session.get(ImportBatch, preview["batch_id"])
            if batch is not None:
                preview = preview_from_batch(batch)
    batch = _active_order_batch()
    if batch is not None and batch.status in {
        ImportBatchStatus.PROCESSING,
        ImportBatchStatus.COMPLETED,
        ImportBatchStatus.FAILED,
    }:
        processing_batch = batch_progress(batch)
        preview = None
    return _page(
        "orders/upload.html",
        "upload",
        preview=preview,
        processing_batch=processing_batch,
        last_result=None,
    )


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
    session["ord_import_batch_id"] = preview["batch_id"]
    return redirect(url_for("orders.import_view", client_id=client.id, division_id=division.id))


@bp.route("/upload/confirm", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_confirm():
    token = session.get("ord_preview_token")
    preview = load_preview(token) if token else None
    batch_id = (preview or {}).get("batch_id") or session.get("ord_import_batch_id")
    if not batch_id:
        flash("Preview expired. Upload the file again.", "error")
        return redirect(url_for("orders.import_view"))
    try:
        batch = begin_processing(int(batch_id), user=current_user, run="async")
    except OrderImportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.import_view"))
    clear_preview(token)
    session.pop("ord_preview_token", None)
    session["ord_import_batch_id"] = batch.id
    return redirect(
        url_for(
            "orders.import_view",
            client_id=batch.client_id,
            division_id=batch.division_id,
            import_batch_id=batch.id,
        )
    )


@bp.route("/upload/cancel", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_cancel():
    token = session.pop("ord_preview_token", None)
    preview = load_preview(token) if token else None
    batch_id = (preview or {}).get("batch_id") or session.pop("ord_import_batch_id", None)
    clear_preview(token)
    if batch_id:
        try:
            cancel_validated_batch(int(batch_id), user=current_user)
        except OrderImportError as exc:
            flash(str(exc), "error")
            return redirect(url_for("orders.import_view"))
    flash("Order preview cancelled.", "success")
    return redirect(url_for("orders.import_view"))


@bp.route("/imports/<int:batch_id>/status")
@permission_required("ORDERS_UPLOAD")
def import_status(batch_id):
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        abort(404)
    return jsonify(batch_progress(batch))


@bp.route("/imports/<int:batch_id>/advance", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_advance(batch_id):
    """Admin-only recovery. The UI must not call this; the worker executes jobs."""
    if not current_user.is_admin():
        abort(403)
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        abort(404)
    try:
        resolve_context(current_user, batch.client_id, batch.division_id)
        if batch.status == ImportBatchStatus.PROCESSING:
            process_import_batch(batch.id, max_chunks=3)
            batch = db.session.get(ImportBatch, batch.id)
    except OrderImportError as exc:
        return jsonify({"error": str(exc), **batch_progress(batch)}), 400
    return jsonify(batch_progress(batch))


@bp.route("/imports/<int:batch_id>/retry", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_retry(batch_id):
    try:
        batch = retry_import(batch_id, user=current_user, run="async")
    except OrderImportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.import_view"))
    session["ord_import_batch_id"] = batch.id
    return redirect(
        url_for(
            "orders.import_view",
            client_id=batch.client_id,
            division_id=batch.division_id,
            import_batch_id=batch.id,
        )
    )


@bp.route("/imports/<int:batch_id>/cleanup", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_cleanup(batch_id):
    try:
        batch = cleanup_failed_import(batch_id, user=current_user)
    except OrderImportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.import_view"))
    flash("Failed import cleaned up. No operational orders remain from that file.", "success")
    session.pop("ord_import_batch_id", None)
    return redirect(
        url_for("orders.import_view", client_id=batch.client_id, division_id=batch.division_id)
    )


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
    from ..constants import PickTicketStatus
    from ..services.pick_tickets import list_pick_tickets

    ctx = _scope()
    eligible = []
    tickets = []
    q = request.args.get("q", "").strip()
    status = (request.args.get("status") or PickTicketStatus.OPEN).strip() or PickTicketStatus.OPEN
    date_str = request.args.get("date", "").strip()
    sort = request.args.get("sort", "created").strip() or "created"
    direction = request.args.get("dir", "desc").strip() or "desc"
    if ctx["scope"]["client_id"]:
        from ..services.pick_tickets import list_eligible_pick_ticket_orders

        eligible = list_eligible_pick_ticket_orders(
            client_id=ctx["scope"]["client_id"],
            division_id=ctx["scope"]["division_id"],
            warehouse_id=ctx["scope"]["warehouse_id"],
        )
        tickets = list_pick_tickets(
            client_id=ctx["scope"]["client_id"],
            division_id=ctx["scope"]["division_id"],
            warehouse_id=ctx["scope"]["warehouse_id"],
            status=status,
            date_str=date_str,
            q=q,
            sort=sort,
            direction=direction,
        )
    return _page(
        "orders/pick_tickets.html",
        "pick_tickets",
        tickets=tickets,
        eligible=eligible,
        q=q,
        status=status,
        date_str=date_str,
        sort=sort,
        direction=direction,
        ticket_statuses=["ALL", "OPEN", "CLOSED", "CANCELLED"],
    )


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
        return redirect(
            url_for(
                "orders.pick_tickets",
                client_id=order.client_id,
                division_id=order.division_id,
                warehouse_id=order.warehouse_id,
            )
        )
    flash(f"Pick ticket {ticket.pick_ticket_number} created.", "success")
    return redirect(url_for("orders.pick_ticket_preview", ticket_id=ticket.id))


@bp.route("/pick-tickets/<int:ticket_id>")
@permission_required("PICK_TICKET_VIEW")
def pick_ticket_preview(ticket_id):
    from ..services.pick_tickets import desired_ticket_status, ticket_lines

    ticket = db.session.get(PickTicket, ticket_id)
    if ticket is None:
        abort(404)
    require_entity_client(current_user, ticket)
    order = db.session.get(Order, ticket.order_id)
    return render_template(
        "orders/pick_ticket_preview.html",
        ticket=ticket,
        order=order,
        lines=ticket_lines(order, ticket),
        ticket_status=desired_ticket_status(order, ticket),
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
        as_attachment=False,
        download_name=f"{ticket.pick_ticket_number}.pdf",
        mimetype="application/pdf",
    )


@bp.route("/shipping")
@permission_required("SHIPPING_VIEW")
def shipping():
    from ..services.shipping import list_closed_shipping_orders

    ctx = _scope()
    q = request.args.get("q", "").strip()
    closed_date = request.args.get("closed_date", "").strip()
    shipping_status = request.args.get("shipping_status", "").strip()
    carrier = request.args.get("carrier", "").strip()
    orders = list_closed_shipping_orders(
        client_id=ctx["scope"]["client_id"],
        division_id=ctx["scope"]["division_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        closed_date=closed_date,
        shipping_status=shipping_status,
        carrier=carrier,
        q=q,
        accessible_client_ids=[c.id for c in ctx["clients"]],
        admin=current_user.is_admin(),
    )
    db.session.commit()
    rows = []
    for order in orders:
        ticket = PickTicket.query.filter_by(order_id=order.id).first()
        rows.append({"order": order, "ticket": ticket.pick_ticket_number if ticket else "—"})
    return _page(
        "orders/shipping.html",
        "shipping",
        rows=rows,
        q=q,
        closed_date=closed_date,
        shipping_status=shipping_status,
        carrier=carrier,
        shipping_statuses=ShippingStatus.ALL,
        carriers=TrackingCarrier.ALL,
    )


@bp.route("/shipping/<int:order_id>")
@permission_required("SHIPPING_VIEW")
def shipping_detail(order_id):
    from ..services.shipping import carton_display, sync_shipping_status

    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    require_entity_client(current_user, order)
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    cartons = Carton.query.filter_by(order_id=order.id).order_by(Carton.id).all()
    sync_shipping_status(order, cartons)
    db.session.commit()
    closure = Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").order_by(Document.id.asc()).first()
    packing = Document.query.filter_by(order_id=order.id, type="PACKING_LIST").order_by(Document.id.asc()).first()
    return render_template(
        "orders/shipping_detail.html",
        order=order,
        ticket=ticket,
        cartons=[carton_display(c) for c in cartons],
        carriers=TrackingCarrier.ALL,
        closure=closure,
        packing=packing,
    )


@bp.route("/shipping/<int:order_id>/tracking", methods=["POST"])
@permission_required("SHIPPING_EDIT")
def shipping_save_tracking(order_id):
    from ..services.shipping import ShippingError, save_carton_tracking

    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    require_entity_client(current_user, order)
    if order.status != OrderStatus.CLOSED:
        flash("Tracking can be entered only after the order is CLOSED.", "error")
        return redirect(url_for("orders.shipping_detail", order_id=order.id))
    try:
        for carton in Carton.query.filter_by(order_id=order.id).order_by(Carton.id):
            number = request.form.get(f"tracking_number_{carton.id}", "")
            carrier = request.form.get(f"tracking_carrier_{carton.id}", "")
            if not (number or "").strip() and not (carrier or "").strip():
                continue
            save_carton_tracking(
                order,
                carton,
                tracking_number=number,
                carrier=carrier,
                user=current_user,
                reason=request.form.get("reason", ""),
            )
        db.session.commit()
        flash("Tracking numbers saved.", "success")
    except ShippingError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("orders.shipping_detail", order_id=order.id))


@bp.route("/shipping/<int:order_id>/confirm", methods=["POST"])
@permission_required("SHIPPING_CONFIRM")
def shipping_confirm(order_id):
    from ..services.shipping import ShippingError, confirm_shipping

    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    require_entity_client(current_user, order)
    try:
        confirm_shipping(order, current_user)
        flash("Shipping confirmed. Tracking is complete.", "success")
    except ShippingError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("orders.shipping_detail", order_id=order.id))


def _shipping_report_args():
    keys = (
        "client_id",
        "division_id",
        "warehouse_id",
        "order_status",
        "shipping_status",
        "carrier",
        "created_from",
        "created_to",
        "closed_from",
        "closed_to",
        "q",
        "page",
        "per_page",
    )
    return {key: request.values.get(key) for key in keys if request.values.get(key)}


@bp.route("/shipping-report")
@permission_required("SHIPPING_REPORT_VIEW")
def shipping_report():
    ctx = _scope()
    filters = {
        "client_id": ctx["scope"]["client_id"],
        "division_id": ctx["scope"]["division_id"],
        "warehouse_id": ctx["scope"]["warehouse_id"],
        "order_status": request.args.get("order_status", "").strip(),
        "shipping_status": request.args.get("shipping_status", "").strip(),
        "carrier": request.args.get("carrier", "").strip(),
        "created_from": request.args.get("created_from", "").strip(),
        "created_to": request.args.get("created_to", "").strip(),
        "closed_from": request.args.get("closed_from", "").strip(),
        "closed_to": request.args.get("closed_to", "").strip(),
        "q": request.args.get("q", "").strip(),
    }
    try:
        page_data = list_shipping_report_page(
            current_user,
            filters,
            page=request.args.get("page", 1),
            per_page=request.args.get("per_page", 50),
        )
    except ShippingReportAccessError:
        abort(404)
    return _page(
        "orders/shipping_report.html",
        "shipping_report",
        rows=page_data["rows"],
        page=page_data["page"],
        pages=page_data["pages"],
        total=page_data["total"],
        per_page=page_data["per_page"],
        per_page_options=page_data["per_page_options"],
        q=filters["q"],
        order_status=filters["order_status"],
        shipping_status=filters["shipping_status"],
        carrier=filters["carrier"],
        created_from=filters["created_from"],
        created_to=filters["created_to"],
        closed_from=filters["closed_from"],
        closed_to=filters["closed_to"],
        **report_filter_choices(),
    )


@bp.route("/shipping-report/export", methods=["POST"])
@permission_required("SHIPPING_REPORT_EXPORT")
def shipping_report_export():
    order_ids = request.form.getlist("order_ids", type=int)
    redirect_args = _shipping_report_args()
    try:
        data, filename, _stats = export_shipping_report(current_user, order_ids)
        db.session.commit()
    except ShippingReportSelectionError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.shipping_report", **redirect_args))
    except ShippingReportAccessError:
        db.session.rollback()
        abort(404)
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/end-of-day")
@permission_required("END_OF_DAY_VIEW")
def end_of_day():
    ctx = _scope()
    day = parse_eod_date(request.args.get("date"))
    carrier = request.args.get("carrier", "").strip()
    closed_by = request.args.get("closed_by", "").strip()
    shipping_status = request.args.get("shipping_status", "").strip()
    query = closed_orders_query(
        day=day,
        client_id=ctx["scope"]["client_id"],
        division_id=ctx["scope"]["division_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        carrier=carrier,
        closed_by=closed_by,
        shipping_status=shipping_status,
        accessible_client_ids=[c.id for c in ctx["clients"]],
        admin=current_user.is_admin(),
    )
    rows = build_eod_rows(query.all())
    db.session.commit()
    return _page(
        "orders/end_of_day.html",
        "eod",
        rows=rows,
        kpis=eod_kpis(rows),
        day=day.isoformat(),
        carrier=carrier,
        closed_by=closed_by,
        shipping_status=shipping_status,
        shipping_statuses=ShippingStatus.ALL,
        default_day=operational_today().isoformat(),
    )


@bp.route("/end-of-day.xlsx")
@permission_required("END_OF_DAY_EXPORT")
def end_of_day_export():
    ctx = _scope()
    day = parse_eod_date(request.args.get("date"))
    query = closed_orders_query(
        day=day,
        client_id=ctx["scope"]["client_id"],
        division_id=ctx["scope"]["division_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        carrier=request.args.get("carrier", "").strip(),
        closed_by=request.args.get("closed_by", "").strip(),
        shipping_status=request.args.get("shipping_status", "").strip(),
        accessible_client_ids=[c.id for c in ctx["clients"]],
        admin=current_user.is_admin(),
    )
    rows = build_eod_rows(query.all())
    data = export_eod_excel(rows, client_id=ctx["scope"]["client_id"])
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=f"end-of-day-{day.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/documents/<int:document_id>")
def order_document(document_id):
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    allowed = (
        current_user.is_admin()
        or current_user.has_permission("SHIPPING_VIEW")
        or current_user.has_permission("END_OF_DAY_VIEW")
        or current_user.has_permission("ORDERS_VIEW")
        or current_user.has_permission("DOCUMENT_REPRINT")
        or current_user.has_permission("PROCESSING_VIEW")
    )
    if not allowed:
        abort(403)
    document = db.session.get(Document, document_id)
    if document is None:
        abort(404)
    require_entity_client(current_user, document)
    data = get_store().open(document.storage_key)
    return send_file(io.BytesIO(data), mimetype="application/pdf", download_name=document.filename)


@bp.route("/documents/<int:document_id>/print", methods=["POST"])
@permission_required("DOCUMENT_REPRINT")
def print_order_document(document_id):
    document = db.session.get(Document, document_id)
    if document is None:
        abort(404)
    require_entity_client(current_user, document)
    event = "PACKING_LIST_PRINTED" if document.type == "PACKING_LIST" else "PDF_PRINTED"
    record_audit(
        event,
        module="Orders",
        entity_type="document",
        entity_id=document.id,
        client_id=document.client_id,
        detail=document.filename,
    )
    if document.type == "PACKING_LIST":
        record_audit(
            "PDF_PRINTED",
            module="Orders",
            entity_type="document",
            entity_id=document.id,
            client_id=document.client_id,
            detail=f"reprint {document.filename}",
        )
    db.session.commit()
    return redirect(url_for("orders.order_document", document_id=document.id))


@bp.route("/pick-tickets/bulk-pdf", methods=["POST"])
@permission_required("PICK_TICKET_BULK_PRINT")
def bulk_pick_ticket_pdf():
    from ..services.bulk_pick_pdf import BulkPickPdfError, publish_bulk_pdf
    from ..services.tenant import require_entity_client

    ids = request.form.getlist("ticket_ids", type=int)
    if not ids:
        flash("Select at least one pick ticket.", "error")
        return redirect(url_for("orders.pick_tickets", client_id=request.form.get("client_id") or None))
    tickets = []
    for tid in ids:
        ticket = db.session.get(PickTicket, tid)
        if ticket is None:
            abort(404)
        require_entity_client(current_user, ticket)
        tickets.append(ticket)
    try:
        result = publish_bulk_pdf(
            tickets,
            sort=request.form.get("sort") or "client",
            direction=request.form.get("dir") or "asc",
            filter_context=request.query_string.decode()[:512],
        )
    except BulkPickPdfError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.pick_tickets", client_id=request.form.get("client_id") or None))
    return send_file(
        io.BytesIO(result["pdf"]),
        as_attachment=True,
        download_name=result["filename"],
        mimetype="application/pdf",
    )


@bp.route("/update-pick-ticket", methods=["GET", "POST"])
@permission_required("PICK_TICKET_UPDATE")
def update_pick_ticket():
    from ..services.pick_ticket_update import (
        PickTicketUpdateError,
        replacement_candidates,
        require_open_ticket,
        substitute_unit,
        ticket_units,
        units_for_upc,
    )
    from ..services.tenant import require_entity_client

    ctx = _scope()
    number = (request.values.get("pick_ticket_number") or "").strip()
    ticket = PickTicket.query.filter_by(pick_ticket_number=number).first() if number else None
    error = None
    units = []
    candidates = []
    selected_upc = (request.values.get("upc") or "").strip()
    selected_unit_id = request.values.get("unit_id", type=int)
    if ticket:
        try:
            require_entity_client(current_user, ticket)
            require_open_ticket(ticket)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            ticket = None
        else:
            units = ticket_units(ticket)
            if selected_upc:
                matches = units_for_upc(ticket, selected_upc)
                if not matches:
                    error = "UPC is not on this pick ticket."
                elif len(matches) == 1:
                    selected_unit_id = matches[0].id
    if ticket and request.method == "POST" and request.form.get("action") == "replace":
        from ..models import InventoryUnit

        original = db.session.get(InventoryUnit, request.form.get("unit_id", type=int))
        replacement = db.session.get(InventoryUnit, request.form.get("replacement_id", type=int))
        try:
            if original is None or replacement is None:
                raise PickTicketUpdateError("Select the original unit and a replacement.")
            result = substitute_unit(ticket, original, replacement, user=current_user)
        except PickTicketUpdateError as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"Unit replaced. {result['ticket'].pick_ticket_number} is now revision {result['revision']}.",
                "success",
            )
            return redirect(url_for("orders.update_pick_ticket", pick_ticket_number=ticket.pick_ticket_number))
    if ticket and selected_upc and selected_unit_id:
        from ..models import InventoryUnit, Order

        original = db.session.get(InventoryUnit, selected_unit_id)
        order = db.session.get(Order, ticket.order_id)
        if original:
            candidates = replacement_candidates(order, original.upc, exclude_id=original.id)
    return _page(
        "orders/update_pick_ticket.html",
        "update_pick_ticket",
        ticket=ticket,
        number=number,
        error=error,
        units=units,
        selected_upc=selected_upc,
        selected_unit_id=selected_unit_id,
        candidates=candidates,
    )
