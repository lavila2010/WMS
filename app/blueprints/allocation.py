from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from io import BytesIO

from ..auth import permission_required
from ..constants import AllocationStatus
from ..extensions import db
from ..models import Allocation, Order
from ..services.allocation import (
    AllocationError,
    allocate_order,
    allocate_orders,
    approve_partial_allocation,
    release_allocation,
)
from ..services.allocation_exceptions import export_daily_exceptions, list_daily_exceptions, parse_exception_date
from ..services.allocation_overview import DEFAULT_PER_PAGE, allocation_overview_page, parse_page, parse_per_page
from ..services.fulfillment import order_quantities, pick_ticket_eligible
from ..services.tenant import accessible_clients, require_entity_client, user_can_access_client

bp = Blueprint("allocation", __name__, url_prefix="/allocation")


@bp.route("/")
@permission_required("ALLOCATION_VIEW")
def index():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    page_data = allocation_overview_page(
        client_id=client_id,
        accessible_client_ids=[c.id for c in clients],
        admin=current_user.is_admin(),
        page=parse_page(request.args.get("page")),
        per_page=parse_per_page(request.args.get("per_page", DEFAULT_PER_PAGE)),
    )
    return render_template(
        "allocation/index.html",
        rows=page_data["rows"],
        clients=clients,
        client_id=client_id,
        summary=None,
        page=page_data["page"],
        pages=page_data["pages"],
        per_page=page_data["per_page"],
        total=page_data["total"],
        per_page_options=page_data["per_page_options"],
    )


@bp.route("/bulk", methods=["POST"])
@permission_required("ALLOCATION_BULK_RUN")
def bulk():
    ids = request.form.getlist("order_ids", type=int)
    if not ids:
        flash("Select at least one order.", "error")
        return redirect(
            url_for(
                "allocation.index",
                client_id=request.form.get("client_id") or None,
                page=request.form.get("page") or None,
                per_page=request.form.get("per_page") or None,
            )
        )
    for oid in ids:
        order = db.session.get(Order, oid)
        require_entity_client(current_user, order)
    summary = allocate_orders(ids, user=current_user)
    flash(
        (
            f"Selected {summary['selected']}: fully {summary['fully_allocated']}, "
            f"partially {summary['partially_allocated']}, no inventory {summary['no_inventory']}, "
            f"failed {summary['failed']}."
        ),
        "success" if not summary["failed"] else "warning",
    )
    return redirect(
        url_for(
            "allocation.index",
            client_id=request.form.get("client_id") or None,
            page=request.form.get("page") or None,
            per_page=request.form.get("per_page") or None,
        )
    )


@bp.route("/exceptions")
@permission_required("ALLOCATION_REPORT_VIEW")
def exceptions():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    payload = list_daily_exceptions(
        day=parse_exception_date(request.args.get("date")),
        client_id=client_id,
        division_id=request.args.get("division_id", type=int),
        warehouse_id=request.args.get("warehouse_id", type=int),
        allocation_status=request.args.get("allocation_status", "").strip(),
        q=request.args.get("q", "").strip(),
        accessible_client_ids=[c.id for c in clients],
        admin=current_user.is_admin(),
    )
    from ..models import Division, Warehouse

    divisions = Division.query.filter_by(client_id=client_id).all() if client_id else []
    warehouses = Warehouse.query.filter_by(client_id=client_id).all() if client_id else []
    return render_template(
        "allocation/exceptions.html",
        payload=payload,
        clients=clients,
        divisions=divisions,
        warehouses=warehouses,
        client_id=client_id,
        division_id=request.args.get("division_id", type=int),
        warehouse_id=request.args.get("warehouse_id", type=int),
        allocation_status=request.args.get("allocation_status", ""),
        q=request.args.get("q", ""),
        date_str=payload["day"].isoformat(),
    )


@bp.route("/exceptions/export")
@permission_required("ALLOCATION_REPORT_EXPORT")
def exceptions_export():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    payload = list_daily_exceptions(
        day=parse_exception_date(request.args.get("date")),
        client_id=client_id,
        division_id=request.args.get("division_id", type=int),
        warehouse_id=request.args.get("warehouse_id", type=int),
        allocation_status=request.args.get("allocation_status", "").strip(),
        q=request.args.get("q", "").strip(),
        accessible_client_ids=[c.id for c in clients],
        admin=current_user.is_admin(),
    )
    data = export_daily_exceptions(payload)
    return send_file(
        BytesIO(data),
        as_attachment=True,
        download_name=f"daily-allocation-exceptions-{payload['day'].isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/<int:order_id>")
@permission_required("ALLOCATION_VIEW")
def detail(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    allocations = Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE).all()
    qty = order_quantities(order)
    return render_template(
        "allocation/detail.html",
        order=order,
        allocations=allocations,
        lines=qty["lines"],
        qty=qty,
        shortages=[row for row in qty["upc_rows"] if row["remaining_qty"] > 0],
        can_approve=qty["currently_allocated"] > 0 and qty["remaining"] > 0 and not pick_ticket_eligible(order, qty),
    )


@bp.route("/<int:order_id>/run", methods=["POST"])
@permission_required("ALLOCATION_EXECUTE")
def run(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    try:
        result = allocate_order(order, user=current_user)
    except (AllocationError, Exception) as exc:
        flash(str(exc), "error")
        return redirect(url_for("allocation.detail", order_id=order_id))
    if result["shortages"]:
        flash(
            f"Partial allocation: {result['reserved']} reserved. Shortages by UPC: "
            + ", ".join(f"{s['upc']}×{s['needed']}" for s in result["shortages"]),
            "warning",
        )
    else:
        flash(f"Allocation complete. Reserved {result['reserved']} units.", "success")
    return redirect(url_for("allocation.detail", order_id=order_id))


@bp.route("/<int:order_id>/approve-partial", methods=["GET", "POST"])
@permission_required("PARTIAL_ALLOCATION_APPROVE")
def approve_partial(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    qty = order_quantities(order)
    if request.method == "POST":
        try:
            approve_partial_allocation(order, user=current_user)
        except AllocationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("allocation.detail", order_id=order_id))
        flash("Partial allocation approved. A pick ticket may now be created for allocated units.", "success")
        return redirect(url_for("allocation.detail", order_id=order_id))
    return render_template("allocation/approve_partial.html", order=order, qty=qty)


@bp.route("/release/<int:allocation_id>", methods=["POST"])
@permission_required("ALLOCATION_RELEASE")
def release(allocation_id):
    allocation = db.session.get(Allocation, allocation_id)
    if allocation is None:
        abort(404)
    require_entity_client(current_user, allocation)
    try:
        release_allocation(allocation)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("Allocation released.", "success")
    return redirect(url_for("allocation.detail", order_id=allocation.order_id))
