from __future__ import annotations

import io

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from ..auth import permission_required, record_audit
from ..constants import CartonStatus
from ..extensions import db
from ..models import Carton, CartonContent, Document, Order, PickTicket
from ..services.documents import get_store
from ..services.processing import (
    ProcessingError,
    acquire_lock,
    assert_ticket_processable,
    can_close,
    can_close_short,
    carton_contents,
    close_order,
    close_order_short,
    dims_ready,
    ensure_open_carton,
    find_ticket,
    kpis,
    recall_carton,
    remaining_rows,
    release_lock,
    require_owner,
    set_dimensions,
    set_weight,
    shortage_summary,
    snapshot,
    SHORT_CLOSE_CONFIRM_TEXT,
)
from ..services.processing import remove_unit as remove_unit_svc
from ..services.processing import request_close as request_close_svc
from ..services.processing import scan_upc as scan_upc_svc
from ..services.tenant import require_entity_client

bp = Blueprint("processing", __name__, url_prefix="/processing")


def _ticket_and_order(order_id):
    order = db.session.get(Order, order_id)
    require_entity_client(current_user, order)
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket is None:
        abort(404)
    return order, ticket


@bp.route("/")
@permission_required("PROCESSING_VIEW")
def index():
    return render_template(
        "processing/index.html",
        pick_ticket_number="",
        pdf_id=request.args.get("pdf_id", type=int),
        packing_id=request.args.get("packing_id", type=int),
    )


@bp.route("/find", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def find():
    number = request.form.get("pick_ticket_number", "")
    try:
        ticket = find_ticket(number)
    except ProcessingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("processing.index"))
    require_entity_client(current_user, ticket)
    order = db.session.get(Order, ticket.order_id)
    return render_template(
        "processing/confirm.html",
        order=order,
        snap=snapshot(order, ticket),
    )


@bp.route("/<int:order_id>/confirm", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def confirm(order_id):
    order, ticket = _ticket_and_order(order_id)
    try:
        assert_ticket_processable(ticket, order)
        acquire_lock(order, current_user)
        ensure_open_carton(order, current_user)
        db.session.commit()
    except ProcessingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("processing.index"))
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>")
@permission_required("PROCESSING_VIEW")
def detail(order_id):
    order, ticket = _ticket_and_order(order_id)
    current = (
        Carton.query.filter_by(order_id=order.id)
        .filter(Carton.status != CartonStatus.CLOSED)
        .order_by(Carton.id.desc())
        .first()
    )
    cartons = []
    for index, carton in enumerate(Carton.query.filter_by(order_id=order.id).order_by(Carton.id), start=1):
        cartons.append(
            {
                "index": index,
                "box": carton,
                "status": carton.status,
                "units": CartonContent.query.filter_by(carton_id=carton.id).count(),
                "dims": f"{carton.length or '—'}×{carton.width or '—'}×{carton.height or '—'}",
                "weight": f"{carton.weight or '—'} {carton.weight_unit}",
            }
        )
    ready, _ = can_close(order)
    remaining = remaining_rows(order)
    short_ok, short_reason = can_close_short(order, current_user)
    return render_template(
        "processing/detail.html",
        order=order,
        snap=snapshot(order, ticket),
        kpis=kpis(order, current),
        remaining=remaining,
        current=current,
        current_index=cartons[-1]["index"] if current and cartons else 1,
        dims_ready=dims_ready(current) if current else False,
        contents=carton_contents(current) if current else [],
        cartons=cartons,
        show_modal=ready,
        short_ok=short_ok,
        short_reason=short_reason,
        remaining_count=sum(row["qty"] for row in remaining),
    )


@bp.route("/<int:order_id>/dimensions", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def dimensions(order_id):
    order, _ = _ticket_and_order(order_id)
    carton = db.session.get(Carton, request.form.get("box_id", type=int))
    if carton is None or carton.order_id != order.id:
        abort(404)
    try:
        require_owner(order, current_user)
        set_dimensions(
            carton,
            request.form.get("length"),
            request.form.get("width"),
            request.form.get("height"),
            request.form.get("dimension_unit") or "in",
        )
        db.session.commit()
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>/scan", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def scan_upc(order_id):
    order, _ = _ticket_and_order(order_id)
    carton = db.session.get(Carton, request.form.get("box_id", type=int))
    if carton is None:
        carton = ensure_open_carton(order, current_user)
    try:
        scan_upc_svc(order, carton, request.form.get("upc"), current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/carton/<int:box_id>/add", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def add_recalled(box_id):
    carton = db.session.get(Carton, box_id)
    if carton is None:
        abort(404)
    order = db.session.get(Order, carton.order_id)
    require_entity_client(current_user, order)
    try:
        scan_upc_svc(order, carton, request.form.get("upc"), current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/carton/<int:box_id>/close", methods=["POST"])
@permission_required("BOX_CLOSE")
def request_close(box_id):
    carton = db.session.get(Carton, box_id)
    if carton is None:
        abort(404)
    try:
        request_close_svc(carton, current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=carton.order_id))


@bp.route("/carton/<int:box_id>/weight", methods=["POST"])
@permission_required("BOX_CLOSE")
def weight(box_id):
    carton = db.session.get(Carton, box_id)
    if carton is None:
        abort(404)
    try:
        set_weight(carton, request.form.get("weight"), request.form.get("weight_unit") or "lb", current_user)
        order = db.session.get(Order, carton.order_id)
        if remaining_rows(order):
            ensure_open_carton(order, current_user)
            db.session.commit()
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=carton.order_id))


@bp.route("/content/<int:content_id>/remove", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def remove_unit(content_id):
    content = db.session.get(CartonContent, content_id)
    if content is None:
        abort(404)
    carton = db.session.get(Carton, content.carton_id)
    try:
        remove_unit_svc(content, current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=carton.order_id))


@bp.route("/carton/<int:box_id>/recall", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def recall(box_id):
    carton = db.session.get(Carton, box_id)
    if carton is None:
        abort(404)
    try:
        recall_carton(carton, current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=carton.order_id)    )


@bp.route("/<int:order_id>/short-close", methods=["GET", "POST"])
@permission_required("PROCESSING_VIEW")
def short_close(order_id):
    order, ticket = _ticket_and_order(order_id)
    summary = shortage_summary(order)
    if request.method == "GET":
        if summary["missing"]:
            record_audit(
                "PROCESSING_SHORTAGE_DETECTED",
                module="Processing",
                entity_type="order",
                entity_id=order.id,
                client_id=order.client_id,
                detail=f"{order.wms_order_id} expected={summary['expected']} packed={summary['packed']} missing={summary['missing']}",
            )
            db.session.commit()
        short_ok, short_reason = can_close_short(order, current_user)
        return render_template(
            "processing/short_close.html",
            order=order,
            ticket=ticket,
            snap=snapshot(order, ticket),
            summary=summary,
            short_ok=short_ok,
            short_reason=short_reason,
            confirm_text=SHORT_CLOSE_CONFIRM_TEXT,
            is_admin=current_user.is_admin(),
        )
    if not current_user.has_permission("ORDER_CLOSE_SHORT"):
        abort(403)
    if request.form.get("confirm_missing") != "1":
        flash("Close Short requires the confirmation checkbox.", "error")
        return redirect(url_for("processing.short_close", order_id=order.id))
    try:
        document = close_order_short(order, current_user, confirmed=True)
    except ProcessingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("processing.short_close", order_id=order.id))
    packing = Document.query.filter_by(order_id=order.id, type="PACKING_LIST").order_by(Document.id.asc()).first()
    return redirect(
        url_for(
            "processing.index",
            pdf_id=document.id if document else None,
            packing_id=packing.id if packing else None,
        )
    )


@bp.route("/<int:order_id>/cancel", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def cancel(order_id):
    order, _ = _ticket_and_order(order_id)
    try:
        release_lock(order, current_user, to_status="PICK_TICKET_READY")
        db.session.commit()
    except ProcessingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("processing.detail", order_id=order.id))
    flash("Processing lock released.", "success")
    return redirect(url_for("processing.index"))


@bp.route("/<int:order_id>/decision", methods=["POST"])
@permission_required("ORDER_CLOSE")
def decision(order_id):
    order, _ = _ticket_and_order(order_id)
    if request.form.get("choice") != "yes":
        return redirect(url_for("processing.detail", order_id=order.id))
    try:
        document = close_order(order, current_user)
    except ProcessingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("processing.detail", order_id=order.id))
    packing = Document.query.filter_by(order_id=order.id, type="PACKING_LIST").order_by(Document.id.asc()).first()
    return redirect(
        url_for(
            "processing.index",
            pdf_id=document.id,
            packing_id=packing.id if packing else None,
        )
    )


@bp.route("/documents/<int:document_id>")
@permission_required("PROCESSING_VIEW")
def open_pdf(document_id):
    document = db.session.get(Document, document_id)
    if document is None:
        abort(404)
    require_entity_client(current_user, document)
    data = get_store().open(document.storage_key)
    return send_file(io.BytesIO(data), mimetype="application/pdf", download_name=document.filename)


@bp.route("/documents/<int:document_id>/print", methods=["POST"])
@permission_required("PROCESSING_VIEW")
def print_pdf(document_id):
    document = db.session.get(Document, document_id)
    if document is None:
        abort(404)
    require_entity_client(current_user, document)
    if document.type == "PACKING_LIST":
        record_audit(
            "PACKING_LIST_PRINTED",
            module="Processing",
            entity_type="document",
            entity_id=document.id,
            client_id=document.client_id,
            detail=document.filename,
        )
    record_audit(
        "PDF_PRINTED",
        module="Processing",
        entity_type="document",
        entity_id=document.id,
        client_id=document.client_id,
        detail=document.filename,
    )
    db.session.commit()
    return redirect(url_for("processing.open_pdf", document_id=document.id))
