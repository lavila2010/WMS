from __future__ import annotations

import csv
import io
import os
import tempfile
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    Response,
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

from ..auth import permission_required, record_audit
from ..constants import InventoryExceptionType, OrderStatus, UnitStatus
from ..models import (
    Box,
    BoxContent,
    Client,
    ImportBatch,
    InventoryException,
    InventoryMovement,
    InventoryUnit,
    Order,
    Warehouse,
)
from ..services import inventory_query as invq
from ..services.filters import scope_options
from ..services.imports import build_inventory_workbook
from ..services.inventory_import import analyze, confirm_import

bp = Blueprint("inventory", __name__, url_prefix="/inventory")

_TMP_DIR = os.path.join(tempfile.gettempdir(), "wms_inventory_imports")
_ONHAND = [UnitStatus.AVAILABLE, UnitStatus.ALLOCATED, UnitStatus.PACKED]


def _scope():
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return _int(request.args.get("client_id")), _int(request.args.get("warehouse_id"))


def _ctx(active_tab, client_id, warehouse_id, **extra):
    ctx = {
        "active_tab": active_tab,
        "options": scope_options(client_id),
        "scope": {"client_id": client_id, "warehouse_id": warehouse_id},
    }
    ctx.update(extra)
    return ctx


@bp.route("/")
@permission_required("INVENTORY_VIEW")
def index():
    return redirect(url_for("inventory.overview", **request.args))


@bp.route("/overview")
@permission_required("INVENTORY_VIEW")
def overview():
    client_id, warehouse_id = _scope()
    kpis = invq.kpis(client_id, warehouse_id)
    by_upc = invq.inventory_by_upc(client_id, warehouse_id)
    comparison = None
    if client_id and not warehouse_id:
        comparison = invq.warehouse_comparison(client_id)
    return render_template(
        "inventory/overview.html",
        **_ctx("overview", client_id, warehouse_id, kpis=kpis, by_upc=by_upc, comparison=comparison),
    )


# ---------------------------------------------------------------- Upload / import

@bp.route("/upload")
@permission_required("INVENTORY_UPLOAD")
def upload():
    client_id, warehouse_id = _scope()
    preview = None
    pending = session.get("inv_import")
    if pending and os.path.exists(pending["path"]):
        with open(pending["path"], "rb") as fh:
            preview = analyze(fh, pending["filename"]).as_dict()
    return render_template(
        "inventory/upload.html",
        **_ctx("upload", client_id, warehouse_id, preview=preview, result=session.pop("inv_result", None)),
    )


@bp.route("/import/preview", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_preview():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose an Inventory.xlsx file.", "error")
        return redirect(url_for("inventory.upload"))
    os.makedirs(_TMP_DIR, exist_ok=True)
    # Clean up any previous pending temp file.
    _cleanup_pending()
    token = uuid.uuid4().hex
    path = os.path.join(_TMP_DIR, f"{token}.xlsx")
    file.save(path)
    session["inv_import"] = {"path": path, "filename": file.filename}
    record_audit("INVENTORY_PREVIEW", module="Inventory", detail=file.filename, commit=True)
    return redirect(url_for("inventory.upload"))


@bp.route("/import/confirm", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_confirm():
    pending = session.get("inv_import")
    if not pending or not os.path.exists(pending["path"]):
        flash("No pending import to confirm. Please upload a file first.", "error")
        return redirect(url_for("inventory.upload"))
    with open(pending["path"], "rb") as fh:
        result = confirm_import(fh, pending["filename"], actor=current_user.username)
    record_audit(
        "INVENTORY_IMPORT", module="Inventory", entity_type="ImportBatch",
        entity_id=result.get("batch_id"), detail=result["status"], commit=True,
    )
    _cleanup_pending()
    session["inv_result"] = {
        "status": result["status"],
        "rows_submitted": result["rows_submitted"],
        "rows_imported": result["rows_imported"],
        "rows_updated": result["rows_updated"],
        "rows_rejected": result["rows_rejected"],
        "warnings": result["warnings"],
        "created_by": result["created_by"],
        "timestamp": result["timestamp"].strftime("%Y-%m-%d %H:%M UTC"),
    }
    if result["status"] == "REJECTED":
        flash(f"Import rejected: {result['rows_rejected']} row(s) had blocking errors.", "error")
    else:
        flash(
            f"Import complete: {result['rows_imported']} imported, "
            f"{result['rows_updated']} updated.",
            "success",
        )
    return redirect(url_for("inventory.upload"))


@bp.route("/import/cancel", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_cancel():
    _cleanup_pending()
    flash("Pending import cancelled.", "success")
    return redirect(url_for("inventory.upload"))


def _cleanup_pending():
    pending = session.pop("inv_import", None)
    if pending:
        try:
            os.remove(pending["path"])
        except OSError:
            pass


@bp.route("/template")
@permission_required("INVENTORY_UPLOAD")
def template():
    buf = build_inventory_workbook(
        [
            {"client": "CELINE", "warehouse": "NY", "upc": "0001112223330", "sku": "SKU-A", "description": "Sample A", "barcode": "BC-0001", "location": "A-01"},
            {"client": "CELINE", "warehouse": "NJ", "upc": "0001112224447", "sku": "SKU-B", "description": "Sample B", "barcode": "BC-0002", "location": "B-02"},
        ]
    )
    return send_file(
        buf, as_attachment=True, download_name="Inventory.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------- Search / drill-down

@bp.route("/search")
@permission_required("INVENTORY_VIEW")
def search():
    client_id, warehouse_id = _scope()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    results = []
    barcode_hit = None
    upc_view = None

    if q:
        # Exact barcode match -> barcode detail.
        barcode_hit = InventoryUnit.query.filter_by(barcode=q).first()
        if barcode_hit:
            return redirect(url_for("inventory.barcode_detail", barcode=q))

        query = invq.scoped_units(client_id, warehouse_id)
        like = f"%{q}%"
        query = query.filter(
            (InventoryUnit.upc.ilike(like))
            | (InventoryUnit.sku.ilike(like))
            | (InventoryUnit.barcode.ilike(like))
            | (InventoryUnit.description.ilike(like))
            | (InventoryUnit.location.ilike(like))
        )
        if status:
            query = query.filter(InventoryUnit.status == status)
        # Order number search
        order_matches = Order.query.filter(Order.order_number.ilike(like)).all()
        order_ids = [o.id for o in order_matches]
        if order_ids:
            query = query.union(
                invq.scoped_units(client_id, warehouse_id).filter(
                    InventoryUnit.order_id.in_(order_ids)
                )
            )
        results = query.order_by(InventoryUnit.upc).limit(500).all()

    return render_template(
        "inventory/search.html",
        **_ctx("search", client_id, warehouse_id, q=q, status=status, results=results,
               statuses=[UnitStatus.AVAILABLE, UnitStatus.ALLOCATED, UnitStatus.PACKED, UnitStatus.SHIPPED]),
    )


@bp.route("/upc")
@permission_required("INVENTORY_VIEW")
def upc_view():
    client_id, warehouse_id = _scope()
    upc = (request.args.get("upc") or "").strip()
    summary = None
    if client_id and upc:
        summary = invq.upc_summary(client_id, warehouse_id, upc)
    client = Client.query.get(client_id) if client_id else None
    warehouse = Warehouse.query.get(warehouse_id) if warehouse_id else None
    return render_template(
        "inventory/upc.html",
        **_ctx("search", client_id, warehouse_id, upc=upc, summary=summary,
               client=client, warehouse=warehouse),
    )


@bp.route("/location")
@permission_required("INVENTORY_VIEW")
def location_view():
    client_id, warehouse_id = _scope()
    upc = (request.args.get("upc") or "").strip()
    location = (request.args.get("location") or "").strip()
    units = []
    if client_id and warehouse_id and upc and location:
        units = invq.location_units(client_id, warehouse_id, upc, location)
    return render_template(
        "inventory/location.html",
        **_ctx("search", client_id, warehouse_id, upc=upc, location=location, units=units),
    )


@bp.route("/barcode/<barcode>")
@permission_required("INVENTORY_VIEW")
def barcode_detail(barcode):
    client_id, warehouse_id = _scope()
    unit = InventoryUnit.query.filter_by(barcode=barcode).first()
    if unit is None:
        abort(404)
    last_movement = (
        InventoryMovement.query.filter_by(inventory_unit_id=unit.id)
        .order_by(InventoryMovement.created_at.desc())
        .first()
    )
    box_content = BoxContent.query.filter_by(inventory_unit_id=unit.id).first()
    box = box_content.box if box_content else None
    return render_template(
        "inventory/barcode.html",
        **_ctx("search", client_id, warehouse_id, unit=unit, last_movement=last_movement, box=box),
    )


# ---------------------------------------------------------------- Transactions

def _movement_query():
    client_id, warehouse_id = _scope()
    q = InventoryMovement.query
    if client_id:
        q = q.filter(InventoryMovement.client_id == client_id)
    if warehouse_id:
        q = q.filter(InventoryMovement.warehouse_id == warehouse_id)
    for field, col in (
        ("upc", InventoryMovement.upc),
        ("barcode", InventoryMovement.barcode),
        ("type", InventoryMovement.movement_type),
    ):
        val = (request.args.get(field) or "").strip()
        if val:
            q = q.filter(col.ilike(f"%{val}%"))
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    for val, op in ((date_from, "ge"), (date_to, "le")):
        if val:
            try:
                d = datetime.strptime(val, "%Y-%m-%d")
                if op == "ge":
                    q = q.filter(InventoryMovement.created_at >= d)
                else:
                    q = q.filter(InventoryMovement.created_at < d.replace(hour=23, minute=59, second=59))
            except ValueError:
                pass
    return q.order_by(InventoryMovement.created_at.desc())


@bp.route("/transactions")
@permission_required("INVENTORY_VIEW")
def transactions():
    client_id, warehouse_id = _scope()
    movements = _movement_query().limit(1000).all()
    return render_template(
        "inventory/transactions.html",
        **_ctx("transactions", client_id, warehouse_id,
               movements=movements,
               filters={k: request.args.get(k, "") for k in ("upc", "barcode", "type", "date_from", "date_to")}),
    )


@bp.route("/transactions.csv")
@permission_required("INVENTORY_EXPORT")
def transactions_csv():
    movements = _movement_query().limit(10000).all()
    record_audit("REPORT_EXPORT", module="Inventory", detail="transactions.csv", commit=True)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Timestamp", "Barcode", "UPC", "Client", "Warehouse", "Transaction",
        "From Status", "To Status", "From Location", "To Location", "Order", "Actor",
    ])
    for m in movements:
        writer.writerow([
            m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            m.barcode, m.upc,
            m.client.code if m.client else "",
            m.warehouse.code if m.warehouse else "",
            m.movement_type, m.from_status, m.to_status,
            m.from_location, m.to_location,
            m.order.order_number if m.order else "",
            m.actor or "",
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory_transactions.csv"},
    )


# ---------------------------------------------------------------- Import history

@bp.route("/import-history")
@permission_required("INVENTORY_VIEW")
def import_history():
    client_id, warehouse_id = _scope()
    batches = (
        ImportBatch.query.filter_by(type="INVENTORY")
        .order_by(ImportBatch.created_at.desc())
        .limit(200)
        .all()
    )
    return render_template(
        "inventory/import_history.html",
        **_ctx("import_history", client_id, warehouse_id, batches=batches),
    )


@bp.route("/import-history/<int:batch_id>")
@permission_required("INVENTORY_VIEW")
def import_detail(batch_id):
    client_id, warehouse_id = _scope()
    batch = ImportBatch.query.get_or_404(batch_id)
    return render_template(
        "inventory/import_detail.html",
        **_ctx("import_history", client_id, warehouse_id, batch=batch),
    )


# ---------------------------------------------------------------- Exceptions

@bp.route("/exceptions")
@permission_required("INVENTORY_VIEW")
def exceptions():
    client_id, warehouse_id = _scope()
    q = InventoryException.query
    if client_id:
        q = q.filter(InventoryException.client_id == client_id)
    if warehouse_id:
        q = q.filter(InventoryException.warehouse_id == warehouse_id)
    items = q.order_by(InventoryException.created_at.desc()).limit(500).all()
    return render_template(
        "inventory/exceptions.html",
        **_ctx("exceptions", client_id, warehouse_id, items=items,
               types=InventoryExceptionType.ALL),
    )
