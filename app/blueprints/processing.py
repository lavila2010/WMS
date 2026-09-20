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
from ..constants import OrderStatus, ProcessingEvent
from ..extensions import db
from ..models import Box, BoxContent, Document, Order
from ..services.allocation import BarcodeError
from ..services.packing import (
    close_box,
    close_order,
    create_box,
    mark_processed,
    mark_ready_to_close,
    reconcile,
    scan_into_box,
)
from ..services.processing import (
    ProcessingError,
    add_upc_to_recalled_carton,
    cancel_session,
    carton_content_rows,
    carton_index,
    closed_carton_summaries,
    confirm_dimensions,
    confirm_order,
    current_carton,
    dims_confirmed,
    finalize_order,
    lookup_pick_ticket,
    order_snapshot,
    ready_for_close_modal,
    recall_carton,
    remaining_unit_rows,
    remove_unit_from_carton,
    request_close_carton,
    save_carton_weight,
    scan_upc_into_box,
    station_kpis,
)
from ..workflow import WorkflowError

bp = Blueprint("processing", __name__, url_prefix="/processing")


def _packing_context(order: Order, focus_box_id=None, show_modal=False):
    current = current_carton(order, focus_box_id)
    return {
        "order": order,
        "snap": order_snapshot(order),
        "remaining": remaining_unit_rows(order),
        "current": current,
        "current_index": carton_index(current) if current else None,
        "dims_ready": dims_confirmed(current) if current else False,
        "contents": carton_content_rows(current) if current else [],
        "kpis": station_kpis(order, current),
        "cartons": closed_carton_summaries(order),
        "show_modal": show_modal,
    }


def _redirect_pack(order: Order, box_id=None, show_modal=False, **values):
    return redirect(
        url_for(
            "processing.detail",
            order_id=order.id,
            box_id=box_id,
            modal=1 if show_modal else None,
            **values,
        )
    )


@bp.route("/", methods=["GET"])
@permission_required("PROCESSING_VIEW")
def index():
    pdf_id = request.args.get("pdf")
    return render_template(
        "processing/index.html",
        pick_ticket_number=request.args.get("pt", ""),
        pdf_id=pdf_id,
    )


@bp.route("/find", methods=["POST"])
@permission_required("PROCESSING_VIEW")
def find():
    number = request.form.get("pick_ticket_number", "")
    try:
        ticket, order = lookup_pick_ticket(number, user=current_user)
        return render_template(
            "processing/confirm.html",
            ticket=ticket,
            order=order,
            snap=order_snapshot(order),
        )
    except ProcessingError as exc:
        flash(exc.message, "error")
        return render_template(
            "processing/index.html",
            pick_ticket_number=number,
            pdf_id=None,
        )


@bp.route("/<int:order_id>/confirm", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def confirm(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        confirm_order(order, user=current_user)
        record_audit(
            ProcessingEvent.PROCESSING_STARTED,
            module="Processing",
            entity_type="Order",
            entity_id=order.id,
            detail=order.order_number,
        )
        db.session.commit()
        return redirect(url_for("processing.detail", order_id=order.id))
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("processing.index"))


@bp.route("/<int:order_id>/cancel", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def cancel(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        cancel_session(order, user=current_user)
        record_audit(
            "PROCESSING_CANCELLED",
            module="Processing",
            entity_type="Order",
            entity_id=order.id,
            detail=order.order_number,
        )
        db.session.commit()
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return redirect(url_for("processing.index"))


@bp.route("/<int:order_id>")
@permission_required("PROCESSING_VIEW")
def detail(order_id: int):
    order = Order.query.get_or_404(order_id)
    if order.status == OrderStatus.CLOSED:
        flash(f"Order {order.order_number} is CLOSED.", "error")
        return redirect(url_for("processing.index"))
    if not order.processing_user_id:
        ticket = getattr(order, "pick_ticket", None)
        if ticket is None:
            flash("Enter a Pick Ticket number to start an order.", "error")
            return redirect(url_for("processing.index"))
        return render_template(
            "processing/confirm.html",
            ticket=ticket,
            order=order,
            snap=order_snapshot(order),
        )
    if order.processing_user_id != current_user.id and not current_user.is_admin():
        flash(
            f"Order {order.order_number} is locked by {order.processing_username}.",
            "error",
        )
        return redirect(url_for("processing.index"))
    rec = reconcile(order)
    focus = request.args.get("box_id", type=int)
    show_modal = request.args.get("modal") == "1" and ready_for_close_modal(order)
    ctx = _packing_context(order, focus_box_id=focus, show_modal=show_modal)
    ctx["rec"] = rec
    return render_template("processing/detail.html", **ctx)


@bp.route("/<int:order_id>/dimensions", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def dimensions(order_id: int):
    order = Order.query.get_or_404(order_id)
    box = Box.query.get_or_404(int(request.form.get("box_id") or 0))
    if box.order_id != order.id:
        abort(404)
    try:
        confirm_dimensions(
            box,
            request.form.get("length"),
            request.form.get("width"),
            request.form.get("height"),
            request.form.get("dimension_unit") or "in",
            user=current_user,
        )
        db.session.commit()
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return _redirect_pack(order, box_id=box.id)


@bp.route("/<int:order_id>/scan-upc", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def scan_upc(order_id: int):
    order = Order.query.get_or_404(order_id)
    box = Box.query.get_or_404(int(request.form.get("box_id") or 0))
    if box.order_id != order.id:
        abort(404)
    upc = request.form.get("upc", "")
    try:
        scan_upc_into_box(box, upc, user=current_user)
        record_audit(
            ProcessingEvent.UNIT_SCAN,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=upc.strip(),
        )
        db.session.commit()
    except (ProcessingError, BarcodeError) as exc:
        db.session.commit()
        flash(getattr(exc, "message", str(exc)), "error")
    return _redirect_pack(order, box_id=box.id)


@bp.route("/box/<int:box_id>/request-close", methods=["POST"])
@permission_required("BOX_CLOSE")
def request_close(box_id: int):
    box = Box.query.get_or_404(box_id)
    try:
        request_close_carton(box, user=current_user)
        record_audit(
            ProcessingEvent.CARTON_CLOSED,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=box.box_number,
        )
        db.session.commit()
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return _redirect_pack(box.order, box_id=box.id)


@bp.route("/box/<int:box_id>/weight", methods=["POST"])
@permission_required("BOX_CLOSE")
def weight(box_id: int):
    box = Box.query.get_or_404(box_id)
    try:
        save_carton_weight(
            box,
            request.form.get("weight"),
            request.form.get("weight_unit") or "lb",
            user=current_user,
        )
        record_audit(
            ProcessingEvent.CARTON_WEIGHT_RECORDED,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=request.form.get("weight"),
        )
        db.session.commit()
        db.session.refresh(box.order)
        show_modal = ready_for_close_modal(box.order)
        next_box = current_carton(box.order)
        return _redirect_pack(
            box.order,
            box_id=next_box.id if next_box else None,
            show_modal=show_modal,
        )
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return _redirect_pack(box.order, box_id=box.id)


@bp.route("/box/<int:box_id>/recall", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def recall(box_id: int):
    box = Box.query.get_or_404(box_id)
    try:
        recall_carton(box, user=current_user)
        record_audit(
            ProcessingEvent.CARTON_RECALLED,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=box.box_number,
        )
        db.session.commit()
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return _redirect_pack(box.order, box_id=box.id)


@bp.route("/box/<int:box_id>/add-upc", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def add_recalled(box_id: int):
    box = Box.query.get_or_404(box_id)
    upc = request.form.get("upc", "")
    try:
        add_upc_to_recalled_carton(box, upc, user=current_user)
        record_audit(
            ProcessingEvent.CARTON_UNIT_ADDED,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=upc.strip(),
        )
        db.session.commit()
    except (ProcessingError, BarcodeError) as exc:
        db.session.commit()
        flash(getattr(exc, "message", str(exc)), "error")
    return _redirect_pack(box.order, box_id=box.id)


@bp.route("/content/<int:content_id>/remove", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def remove_unit(content_id: int):
    content = BoxContent.query.get_or_404(content_id)
    box = content.box
    try:
        remove_unit_from_carton(content, user=current_user)
        record_audit(
            ProcessingEvent.CARTON_UNIT_REMOVED,
            module="Processing",
            entity_type="Box",
            entity_id=box.id,
            detail=str(content_id),
        )
        db.session.commit()
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
    return _redirect_pack(box.order, box_id=box.id)


@bp.route("/<int:order_id>/decision", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def decision(order_id: int):
    order = Order.query.get_or_404(order_id)
    choice = (request.form.get("choice") or "").strip()
    if choice != "yes":
        return redirect(url_for("processing.detail", order_id=order.id))
    if not current_user.has_permission("ORDER_CLOSE"):
        abort(403)
    try:
        _, document = finalize_order(order, user=current_user)
        record_audit(
            ProcessingEvent.ORDER_CLOSED,
            module="Processing",
            entity_type="Order",
            entity_id=order.id,
            detail=order.order_number,
        )
        record_audit(
            ProcessingEvent.PDF_GENERATED,
            module="Processing",
            entity_type="Document",
            entity_id=document.id,
            detail=document.filename,
        )
        db.session.commit()
        return redirect(url_for("processing.index", pdf=document.id))
    except ProcessingError as exc:
        db.session.rollback()
        flash(exc.message, "error")
        return redirect(url_for("processing.detail", order_id=order.id))
    except Exception as exc:
        db.session.rollback()
        flash(f"Order close failed and was rolled back: {exc}", "error")
        return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/document/<int:document_id>")
@permission_required("PROCESSING_VIEW")
def open_pdf(document_id: int):
    import os

    doc = Document.query.get_or_404(document_id)
    if not os.path.exists(doc.path):
        abort(404)
    if doc.order_id:
        from ..services.processing import record_event

        order = Order.query.get(doc.order_id)
        if order is not None:
            record_event(
                order,
                ProcessingEvent.PDF_PRINTED,
                detail=doc.filename,
                user=current_user,
            )
            record_audit(
                ProcessingEvent.PDF_PRINTED,
                module="Processing",
                entity_type="Document",
                entity_id=doc.id,
                detail=doc.filename,
            )
            db.session.commit()
    return send_file(doc.path, mimetype="application/pdf", download_name=doc.filename)


# --- Legacy barcode packing routes (regression / attribution) ---


@bp.route("/<int:order_id>/box", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def new_box(order_id: int):
    order = Order.query.get_or_404(order_id)

    def _f(name):
        val = request.form.get(name, "").strip()
        return float(val) if val else None

    box_number = request.form.get("box_number", "").strip()
    if not box_number:
        box_number = f"{order.order_number}-BOX{len(order.boxes) + 1:02d}"
    try:
        box = create_box(order, box_number, _f("length_cm"), _f("width_cm"), _f("height_cm"))
        record_audit("BOX_CREATE", module="Processing", entity_type="Box", entity_id=box.id, detail=box_number)
        db.session.commit()
        flash(f"Box {box_number} created.", "success")
    except WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/box/<int:box_id>/scan", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def scan(box_id: int):
    box = Box.query.get_or_404(box_id)
    barcode = request.form.get("barcode", "")
    try:
        scan_into_box(box, barcode)
        record_audit("UNIT_SCAN", module="Processing", entity_type="Box", entity_id=box.id, detail=barcode.strip())
        db.session.commit()
        flash(f"Packed {barcode.strip()} into {box.box_number}.", "success")
    except BarcodeError as exc:
        db.session.commit()
        flash(f"{exc.exc_type}: {exc.message}", "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/box/<int:box_id>/close", methods=["POST"])
@permission_required("BOX_CLOSE")
def close(box_id: int):
    box = Box.query.get_or_404(box_id)
    weight_value = request.form.get("weight_kg", "").strip()
    try:
        close_box(box, float(weight_value) if weight_value else None)
        record_audit("BOX_CLOSE", module="Processing", entity_type="Box", entity_id=box.id, detail=f"{weight_value}kg")
        db.session.commit()
        flash(f"Box {box.box_number} closed.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=box.order_id))


@bp.route("/<int:order_id>/processed", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def processed(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        mark_processed(order)
        db.session.commit()
        flash(f"Order {order.order_number} marked PROCESSED.", "success")
    except (ValueError, WorkflowError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>/ready-to-close", methods=["POST"])
@permission_required("PROCESSING_EXECUTE")
def ready_to_close(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        mark_ready_to_close(order)
        db.session.commit()
        flash(f"Order {order.order_number} ready to close.", "success")
    except (ValueError, WorkflowError) as exc:
        db.session.commit()
        flash(str(exc), "error")
    return redirect(url_for("processing.detail", order_id=order.id))


@bp.route("/<int:order_id>/close", methods=["POST"])
@permission_required("ORDER_CLOSE")
def close_order_view(order_id: int):
    order = Order.query.get_or_404(order_id)
    try:
        _, invoice = close_order(order, created_by=current_user.username)
        uid, uname = current_actor()
        order.closed_by_user_id = uid
        order.closed_by_username = uname
        record_audit("ORDER_CLOSE", module="Processing", entity_type="Order", entity_id=order.id, detail=invoice.invoice_number)
        db.session.commit()
        flash(
            f"Order {order.order_number} CLOSED. Invoice {invoice.invoice_number} created.",
            "success",
        )
    except (ValueError, WorkflowError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception as exc:
        db.session.rollback()
        flash(f"Order close failed and was rolled back: {exc}", "error")
    return redirect(url_for("processing.detail", order_id=order.id))
