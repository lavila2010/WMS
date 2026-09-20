from __future__ import annotations

import os
import tempfile
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
from sqlalchemy import func, or_

from ..auth import current_actor, permission_required, record_audit
from ..constants import OrderStatus, PrintSource
from ..extensions import db
from ..models import Order, OrderLine, PickTicket
from ..services.allocation import (
    active_allocations,
    allocation_progress,
    run_allocation_batch,
)
from ..services.filters import scope_options
from ..services.imports import ImportError_, build_orders_workbook
from ..services.order_import import analyze, confirm_import
from ..services.packing import packed_count
from ..services import pick_tickets as pts
from ..workflow import WorkflowError, transition

bp = Blueprint("orders", __name__, url_prefix="/orders")

_TMP_DIR = os.path.join(tempfile.gettempdir(), "wms_order_imports")
_OPEN = [s for s in OrderStatus.ORDER if s != OrderStatus.CLOSED]
_STATUS_PILLS = [
    ("open", "Open"),
    ("closed", "Closed"),
    ("all", "All"),
    (OrderStatus.NEW, "New"),
    (OrderStatus.VALIDATED, "Validated"),
    (OrderStatus.ALLOCATING, "Allocating"),
    (OrderStatus.ALLOCATED, "Allocated"),
    (OrderStatus.READY_TO_PICK, "Ready to Pick"),
    (OrderStatus.PROCESSING, "Processing"),
    (OrderStatus.READY_TO_CLOSE, "Ready to Close"),
]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _client_id():
    return _int(request.args.get("client_id") or request.form.get("client_id"))


def _warehouse_id():
    return _int(request.args.get("warehouse_id") or request.form.get("warehouse_id"))


def _scope_q():
    return {
        "client_id": _client_id(),
        "warehouse_id": _warehouse_id(),
    }


def _layout(active_tab, **extra):
    scope = extra.pop("scope", _scope_q())
    ctx = {
        "active_tab": active_tab,
        "options": scope_options(scope.get("client_id")),
        "scope": scope,
    }
    ctx.update(extra)
    return ctx


def _base_orders(client_id, warehouse_id=None, date_str=None):
    q = Order.query.filter(Order.client_id == client_id)
    if warehouse_id:
        q = q.filter(Order.warehouse_id == warehouse_id)
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
            q = q.filter(func.date(Order.created_at) == day)
        except ValueError:
            pass
    return q


def _apply_search(query, qtext):
    qtext = (qtext or "").strip()
    if not qtext:
        return query
    like = f"%{qtext}%"
    return (
        query.outerjoin(OrderLine, OrderLine.order_id == Order.id)
        .filter(
            or_(
                Order.order_number.ilike(like),
                Order.customer.ilike(like),
                Order.carrier.ilike(like),
                OrderLine.sku.ilike(like),
            )
        )
        .distinct()
    )


def _apply_status(query, status):
    status = (status or "open").strip() or "open"
    if status == "open":
        return query.filter(Order.status != OrderStatus.CLOSED)
    if status == "closed":
        return query.filter(Order.status == OrderStatus.CLOSED)
    if status == "all":
        return query
    return query.filter(Order.status == status)


def _row_for(order: Order) -> dict:
    allocated = len(active_allocations(order))
    units = order.ordered_quantity
    return {
        "order": order,
        "units": units,
        "allocated": allocated,
        "short": max(0, units - allocated),
        "processed": packed_count(order),
        "boxes": len(order.boxes),
        "exceptions": sum(1 for e in order.exceptions if not e.resolved),
        "pick_ticket": order.pick_ticket.pick_ticket_number if order.pick_ticket else "—",
    }


def _kpis(client_id, warehouse_id=None, date_str=None):
    base = _base_orders(client_id, warehouse_id, date_str)
    today = datetime.utcnow().date()
    orders_today = _base_orders(client_id, warehouse_id).filter(func.date(Order.created_at) == today).count()
    closed_today = (
        _base_orders(client_id, warehouse_id)
        .filter(Order.status == OrderStatus.CLOSED, func.date(Order.updated_at) == today)
        .count()
    )
    open_q = base.filter(Order.status != OrderStatus.CLOSED)
    allocated = base.filter(
        Order.status.in_([
            OrderStatus.ALLOCATED,
            OrderStatus.READY_TO_PICK,
            OrderStatus.PROCESSING,
            OrderStatus.PROCESSED,
            OrderStatus.READY_TO_CLOSE,
        ])
    ).count()
    short = 0
    for o in open_q.all():
        units = o.ordered_quantity
        got = len(active_allocations(o))
        if 0 < got < units:
            short += 1
    tickets = (
        PickTicket.query.join(Order, Order.id == PickTicket.order_id)
        .filter(Order.client_id == client_id)
    )
    if warehouse_id:
        tickets = tickets.filter(Order.warehouse_id == warehouse_id)
    return {
        "orders_today": orders_today,
        "open": open_q.count(),
        "allocated": allocated,
        "short": short,
        "pick_tickets": tickets.count(),
        "closed_today": closed_today,
    }


def _daily_summary(client_id, warehouse_id=None):
    today = datetime.utcnow().date()
    rows = []
    for offset in range(5):
        day = today - timedelta(days=offset)
        count = (
            _base_orders(client_id, warehouse_id)
            .filter(func.date(Order.created_at) == day)
            .count()
        )
        rows.append({"label": day.strftime("%b %d"), "count": count, "date": day.isoformat()})
    return rows


def _status_breakdown(client_id, warehouse_id=None, date_str=None):
    rows = []
    for status in [
        OrderStatus.NEW,
        OrderStatus.ALLOCATING,
        OrderStatus.ALLOCATED,
        OrderStatus.READY_TO_PICK,
        OrderStatus.PROCESSING,
        OrderStatus.READY_TO_CLOSE,
        OrderStatus.CLOSED,
    ]:
        q = _base_orders(client_id, warehouse_id, date_str).filter(Order.status == status)
        orders = q.all()
        rows.append({
            "status": status,
            "orders": len(orders),
            "units": sum(o.ordered_quantity for o in orders),
        })
    return rows


@bp.route("/")
@permission_required("ORDERS_VIEW")
def index():
    scope = _scope_q()
    client_id = scope["client_id"]
    warehouse_id = scope["warehouse_id"]
    date_str = (request.args.get("date") or "").strip()
    qtext = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "open").strip() or "open"

    rows = []
    kpis = None
    daily = []
    breakdown = []
    if client_id:
        query = _apply_status(_base_orders(client_id, warehouse_id, date_str), status)
        query = _apply_search(query, qtext)
        orders = query.order_by(Order.created_at.desc()).all()
        rows = [_row_for(o) for o in orders]
        kpis = _kpis(client_id, warehouse_id, date_str)
        daily = _daily_summary(client_id, warehouse_id)
        breakdown = _status_breakdown(client_id, warehouse_id, date_str)

    return render_template(
        "orders/index.html",
        **_layout(
            "management",
            scope=scope,
            rows=rows,
            kpis=kpis,
            daily=daily,
            breakdown=breakdown,
            date_str=date_str,
            q=qtext,
            status=status,
            status_pills=_STATUS_PILLS,
            client_selected=bool(client_id),
        ),
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
        db.session.commit()
        flash(f"Order {order.order_number} validated.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/import", methods=["GET", "POST"])
@bp.route("/upload", methods=["GET", "POST"])
@permission_required("ORDERS_UPLOAD")
def import_view():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Orders.xlsx file.", "error")
            return redirect(url_for("orders.import_view"))
        try:
            batch = confirm_import(analyze(file.stream, file.filename))
        except ImportError_ as exc:
            flash(str(exc), "error")
            return redirect(url_for("orders.import_view"))
        record_audit(
            "ORDER_IMPORT",
            module="Orders",
            entity_type="ImportBatch",
            entity_id=batch.id,
            detail=batch.message,
            commit=True,
        )
        flash(batch.message, "success")
        return redirect(url_for("orders.index"))
    return render_template(
        "orders/upload.html",
        **_layout("upload", preview=None, last_result=None),
    )


@bp.route("/import/preview", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_preview():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose an Orders.xlsx file.", "error")
        return redirect(url_for("orders.import_view"))
    os.makedirs(_TMP_DIR, exist_ok=True)
    path = os.path.join(_TMP_DIR, f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}")
    file.save(path)
    preview = analyze(path, file.filename)
    session["order_import_path"] = path
    session["order_import_filename"] = file.filename
    record_audit(
        "ORDER_PREVIEW",
        module="Orders",
        entity_type="ImportBatch",
        detail=file.filename,
        commit=True,
    )
    return render_template(
        "orders/upload.html",
        **_layout("upload", preview=preview.as_dict(), last_result=None),
    )


@bp.route("/import/confirm", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_confirm():
    path = session.get("order_import_path")
    filename = session.get("order_import_filename") or "Orders.xlsx"
    if not path or not os.path.exists(path):
        flash("No preview is ready to confirm. Upload the file again.", "error")
        return redirect(url_for("orders.import_view"))
    preview = analyze(path, filename)
    try:
        batch = confirm_import(preview)
    except ImportError_ as exc:
        flash(str(exc), "error")
        return render_template(
            "orders/upload.html",
            **_layout("upload", preview=preview.as_dict(), last_result=None),
        )
    session.pop("order_import_path", None)
    session.pop("order_import_filename", None)
    try:
        os.remove(path)
    except OSError:
        pass
    record_audit(
        "ORDER_IMPORT",
        module="Orders",
        entity_type="ImportBatch",
        entity_id=batch.id,
        detail=batch.message,
        commit=True,
    )
    return render_template(
        "orders/upload.html",
        **_layout("upload", preview=None, last_result=batch),
    )


@bp.route("/import/cancel", methods=["POST"])
@permission_required("ORDERS_UPLOAD")
def import_cancel():
    path = session.pop("order_import_path", None)
    session.pop("order_import_filename", None)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    flash("Import cancelled. Nothing was written.", "success")
    return redirect(url_for("orders.import_view"))


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


@bp.route("/allocation-report", methods=["GET", "POST"])
@permission_required("ALLOCATION_VIEW")
def allocation_report():
    scope = _scope_q()
    client_id = scope["client_id"]
    warehouse_id = scope["warehouse_id"]
    summary = session.get("allocation_report")
    rows = []
    if request.method == "POST":
        from flask_login import current_user

        if not current_user.has_permission("ALLOCATION_EXECUTE"):
            abort(403)
        if not client_id:
            flash("Select a client before running allocation.", "error")
            return redirect(url_for("orders.allocation_report"))
        orders = (
            _base_orders(client_id, warehouse_id)
            .filter(Order.status != OrderStatus.CLOSED)
            .order_by(Order.created_at.asc())
            .all()
        )
        summary = run_allocation_batch(orders)
        record_audit(
            "ALLOCATION",
            module="Orders",
            entity_type="Client",
            entity_id=client_id,
            detail=f"Evaluated {summary['evaluated']} order(s); full={summary['full']}",
        )
        db.session.commit()
        session["allocation_report"] = {
            k: (v if k != "rows" else None) for k, v in summary.items()
        }
        session["allocation_report_rows"] = summary["rows"]
        flash(
            f"Allocation complete: {summary['full']} full, {summary['partial']} partial, "
            f"{summary['no_inventory']} no inventory, {summary['exceptions']} exceptions.",
            "success",
        )
        return redirect(url_for(
            "orders.allocation_report",
            client_id=client_id,
            warehouse_id=warehouse_id or "",
        ))

    if client_id:
        if session.get("allocation_report_rows"):
            rows = session.get("allocation_report_rows")
            summary = session.get("allocation_report")
        else:
            orders = (
                _base_orders(client_id, warehouse_id)
                .filter(Order.status != OrderStatus.CLOSED)
                .order_by(Order.created_at.desc())
                .all()
            )
            rows = []
            for o in orders:
                progress = allocation_progress(o)
                if progress["fully_allocated"]:
                    result, reason = "FULL", "Demand fully covered."
                elif progress["total_allocated"] == 0:
                    result, reason = "NO INVENTORY", "No units allocated."
                else:
                    result, reason = "PARTIAL", "Insufficient available inventory."
                rows.append({
                    "order_id": o.id,
                    "order_number": o.order_number,
                    "units": progress["total_ordered"],
                    "allocated": progress["total_allocated"],
                    "short": max(0, progress["total_ordered"] - progress["total_allocated"]),
                    "result": result,
                    "reason": reason,
                })
            evaluated = len(rows)
            full = sum(1 for r in rows if r["result"] == "FULL")
            summary = {
                "evaluated": evaluated,
                "full": full,
                "partial": sum(1 for r in rows if r["result"] == "PARTIAL"),
                "no_inventory": sum(1 for r in rows if r["result"] == "NO INVENTORY"),
                "exceptions": 0,
                "rate": round((full / evaluated) * 100, 1) if evaluated else 0.0,
            }

    return render_template(
        "orders/allocation_report.html",
        **_layout(
            "allocation",
            scope=scope,
            summary=summary,
            rows=rows,
            client_selected=bool(client_id),
        ),
    )


def _pt_sort_key(row, sort):
    mapping = {
        "order_number": row["order"].order_number,
        "pick_ticket_number": row["ticket"].pick_ticket_number,
        "print_count": row["print_count"],
        "last_printed": row["last_printed"] or datetime.min,
        "last_printed_by": row["last_printed_by"] or "",
    }
    return mapping.get(sort, row["ticket"].pick_ticket_number)


@bp.route("/pick-tickets")
@permission_required("PICK_TICKET_VIEW")
def pick_tickets():
    scope = _scope_q()
    client_id = scope["client_id"]
    warehouse_id = scope["warehouse_id"]
    print_status = (request.args.get("print_status") or "never").strip() or "never"
    printed_by = (request.args.get("printed_by") or "").strip()
    qtext = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "pick_ticket_number").strip()
    direction = (request.args.get("dir") or "asc").strip()

    rows = []
    kpis = None
    printers = []
    counts = {"never": 0, "printed": 0, "all": 0}
    if client_id:
        pts.sync_eligible_tickets(client_id, warehouse_id)
        db.session.commit()
        tickets = (
            PickTicket.query.join(Order, Order.id == PickTicket.order_id)
            .filter(Order.client_id == client_id, Order.status != OrderStatus.CLOSED)
        )
        if warehouse_id:
            tickets = tickets.filter(Order.warehouse_id == warehouse_id)
        all_rows = [pts.ticket_row(t) for t in tickets.all()]
        eligible = [r for r in all_rows if pts.is_eligible(r["order"])]
        counts["all"] = len(eligible)
        counts["printed"] = sum(1 for r in eligible if r["printed"])
        counts["never"] = sum(1 for r in eligible if not r["printed"])
        printers = pts.printed_by_usernames(client_id, warehouse_id)

        rows = eligible
        if print_status == "never":
            rows = [r for r in rows if not r["printed"]]
        elif print_status == "printed":
            rows = [r for r in rows if r["printed"]]
            if printed_by:
                rows = [r for r in rows if r["last_printed_by"] == printed_by]
        if qtext:
            like = qtext.lower()
            rows = [
                r for r in rows
                if like in r["order"].order_number.lower()
                or like in r["ticket"].pick_ticket_number.lower()
            ]
        reverse = direction == "desc"
        rows.sort(key=lambda r: _pt_sort_key(r, sort), reverse=reverse)

        today = datetime.utcnow().date()
        reprinted_today = sum(
            1
            for r in eligible
            if r["print_count"] >= 2 and r["last_printed"] and r["last_printed"].date() == today
        )
        kpis = {
            "never": counts["never"],
            "printed": counts["printed"],
            "eligible": counts["all"],
            "reprinted_today": reprinted_today,
            "total_units": sum(r["units"] for r in eligible),
        }

    return render_template(
        "orders/pick_tickets.html",
        **_layout(
            "pick_tickets",
            scope=scope,
            rows=rows,
            kpis=kpis,
            counts=counts,
            printers=printers,
            print_status=print_status,
            printed_by=printed_by,
            q=qtext,
            sort=sort,
            direction=direction,
            client_selected=bool(client_id),
        ),
    )


@bp.route("/pick-tickets/generate", methods=["POST"])
@permission_required("PICK_TICKET_GENERATE")
def pick_tickets_generate():
    ids = request.form.getlist("ticket_id")
    if not ids:
        flash("Select at least one pick ticket.", "error")
        return redirect(request.referrer or url_for("orders.pick_tickets"))
    from ..services.documents import generate_pick_ticket

    generated = []
    for raw in ids:
        ticket = PickTicket.query.get(_int(raw))
        if ticket is None or not pts.is_eligible(ticket.order):
            continue
        doc = generate_pick_ticket(ticket.order, pick_ticket=ticket)
        ticket.document_id = doc.id
        pts.record_print(ticket, PrintSource.BATCH)
        record_audit(
            "PDF_GENERATED",
            module="Orders",
            entity_type="PickTicket",
            entity_id=ticket.id,
            detail=ticket.pick_ticket_number,
        )
        generated.append(ticket)
    db.session.commit()
    if not generated:
        flash("No eligible pick tickets were generated.", "error")
        return redirect(request.referrer or url_for("orders.pick_tickets"))
    flash(f"Generated {len(generated)} pick ticket PDF(s).", "success")
    if len(generated) == 1:
        return redirect(url_for("orders.pick_ticket_preview", ticket_id=generated[0].id))
    return redirect(request.referrer or url_for("orders.pick_tickets"))


@bp.route("/pick-tickets/<int:ticket_id>")
@permission_required("PICK_TICKET_VIEW")
def pick_ticket_preview(ticket_id: int):
    ticket = PickTicket.query.get_or_404(ticket_id)
    lines = pts.pick_lines(ticket.order)
    return render_template(
        "orders/pick_ticket_preview.html",
        **_layout(
            "pick_tickets",
            ticket=ticket,
            order=ticket.order,
            lines=lines,
            locations=pts.unique_locations(ticket.order),
            row=pts.ticket_row(ticket),
        ),
    )


@bp.route("/pick-tickets/<int:ticket_id>/pdf")
@permission_required("PICK_TICKET_VIEW")
def pick_ticket_pdf(ticket_id: int):
    ticket = PickTicket.query.get_or_404(ticket_id)
    from ..services.documents import generate_pick_ticket

    if ticket.document is None or not os.path.exists(ticket.document.path):
        doc = generate_pick_ticket(ticket.order, pick_ticket=ticket)
        ticket.document_id = doc.id
        db.session.commit()
    return send_file(ticket.document.path, download_name=ticket.document.filename)


@bp.route("/pick-tickets/<int:ticket_id>/print", methods=["POST"])
@permission_required("PICK_TICKET_PRINT")
def pick_ticket_print(ticket_id: int):
    ticket = PickTicket.query.get_or_404(ticket_id)
    from ..services.documents import generate_pick_ticket

    if ticket.document is None or not os.path.exists(getattr(ticket.document, "path", "")):
        doc = generate_pick_ticket(ticket.order, pick_ticket=ticket)
        ticket.document_id = doc.id
    source = PrintSource.REPRINT if ticket.print_count else PrintSource.SCREEN
    pts.record_print(ticket, source)
    record_audit(
        "PICK_TICKET_PRINT",
        module="Orders",
        entity_type="PickTicket",
        entity_id=ticket.id,
        detail=f"{ticket.pick_ticket_number} {source}",
        commit=True,
    )
    return send_file(ticket.document.path, download_name=ticket.document.filename)
