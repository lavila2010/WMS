from __future__ import annotations

import io
from datetime import datetime, timedelta

import pandas as pd
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

from ..auth import permission_required
from ..constants import UnitStatus
from ..extensions import db
from ..models import Client, ImportBatch, InventoryUnit, Warehouse
from ..services.inventory_import import (
    ImportErrorClosed,
    analyze,
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
    batch_progress,
    template_bytes,
)
from ..services import inventory_query as iq
from ..services.inventory_visibility import apply_operational_visibility
from ..services.tenant import accessible_clients, user_can_access_client

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _scope():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int) or request.form.get("client_id", type=int)
    warehouse_id = request.args.get("warehouse_id", type=int) or request.form.get(
        "warehouse_id", type=int
    )
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    warehouses = []
    if client_id:
        warehouses = (
            Warehouse.query.filter_by(client_id=client_id)
            .order_by(Warehouse.warehouse_symbol)
            .all()
        )
        if warehouse_id and not any(w.id == warehouse_id for w in warehouses):
            warehouse_id = None
    elif warehouse_id:
        warehouse = db.session.get(Warehouse, warehouse_id)
        if warehouse is None or not user_can_access_client(current_user, warehouse.client_id):
            abort(404)
    client_ids = None if current_user.is_admin() and not client_id else (
        [client_id] if client_id else [c.id for c in clients]
    )
    return {
        "clients": clients,
        "warehouses": warehouses,
        "all_warehouses": Warehouse.query.order_by(Warehouse.warehouse_code).all()
        if current_user.is_admin()
        else Warehouse.query.filter(Warehouse.client_id.in_([c.id for c in clients] or [-1]))
        .order_by(Warehouse.warehouse_code)
        .all(),
        "scope": {"client_id": client_id, "warehouse_id": warehouse_id},
        "client_ids": client_ids,
    }


def _page(template, active_tab, **kwargs):
    ctx = _scope()
    return render_template(
        template,
        active_tab=active_tab,
        options={"clients": ctx["clients"], "warehouses": ctx["warehouses"] or ctx["all_warehouses"]},
        scope=ctx["scope"],
        **kwargs,
    )


@bp.route("/")
@permission_required("INVENTORY_VIEW")
def overview():
    ctx = _scope()
    scope = ctx["scope"]
    kpis = iq.status_counts(
        client_id=scope["client_id"],
        warehouse_id=scope["warehouse_id"],
        client_ids=ctx["client_ids"],
    )
    rows = iq.aggregate_rows(
        client_id=scope["client_id"],
        warehouse_id=scope["warehouse_id"],
        client_ids=ctx["client_ids"],
    )
    comparison = []
    if scope["client_id"] and not scope["warehouse_id"]:
        names = {w.id: w.warehouse_code for w in ctx["warehouses"]}
        comparison = [
            {**row, "warehouse": names.get(row["warehouse_id"], row["warehouse_id"])}
            for row in iq.warehouse_comparison(scope["client_id"])
        ]
    return _page(
        "inventory/overview.html",
        "overview",
        kpis=kpis,
        by_location=rows,
        comparison=comparison,
    )


def _active_batch():
    batch_id = request.args.get("import_batch_id", type=int) or session.get("inv_import_batch_id")
    if not batch_id:
        return None
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        return None
    return batch


@bp.route("/upload", methods=["GET", "POST"])
@permission_required("INVENTORY_UPLOAD")
def upload():
    preview = None
    processing_batch = None
    token = session.get("inv_preview_token")
    if token:
        preview = load_preview(token)
        if preview and preview.get("batch_id"):
            batch = db.session.get(ImportBatch, preview["batch_id"])
            if batch is not None:
                preview = preview_from_batch(batch)
    batch = _active_batch()
    if batch is not None and batch.status in {"PROCESSING", "COMPLETED", "FAILED"}:
        processing_batch = batch_progress(batch)
        preview = None
    return _page(
        "inventory/upload.html",
        "upload",
        preview=preview,
        processing_batch=processing_batch,
        result=None,
    )


@bp.route("/upload/preview", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_preview():
    file = request.files.get("file")
    try:
        client, warehouse = resolve_context(
            current_user,
            request.form.get("client_id", type=int),
            request.form.get("warehouse_id", type=int),
        )
    except ImportErrorClosed as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.upload"))
    if file is None or not file.filename:
        flash("Select an Excel file.", "error")
        return redirect(
            url_for(
                "inventory.upload",
                client_id=client.id,
                warehouse_id=warehouse.id,
            )
        )
    try:
        preview = analyze(file.stream, file.filename, client, warehouse, user=current_user)
    except ImportErrorClosed as exc:
        flash(str(exc), "error")
        return redirect(
            url_for("inventory.upload", client_id=client.id, warehouse_id=warehouse.id)
        )
    token = save_preview(preview)
    session["inv_preview_token"] = token
    session["inv_import_batch_id"] = preview["batch_id"]
    return redirect(url_for("inventory.upload", client_id=client.id, warehouse_id=warehouse.id))


@bp.route("/upload/confirm", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_confirm():
    token = session.get("inv_preview_token")
    preview = load_preview(token) if token else None
    batch_id = (preview or {}).get("batch_id") or session.get("inv_import_batch_id")
    if not batch_id:
        flash("Preview expired. Upload the file again.", "error")
        return redirect(url_for("inventory.upload"))
    try:
        batch = begin_processing(int(batch_id), user=current_user, run="async")
    except ImportErrorClosed as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.upload"))
    clear_preview(token)
    session.pop("inv_preview_token", None)
    session["inv_import_batch_id"] = batch.id
    return redirect(
        url_for(
            "inventory.upload",
            client_id=batch.client_id,
            warehouse_id=batch.warehouse_id,
            import_batch_id=batch.id,
        )
    )


@bp.route("/upload/cancel", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_cancel():
    token = session.pop("inv_preview_token", None)
    preview = load_preview(token) if token else None
    batch_id = (preview or {}).get("batch_id") or session.pop("inv_import_batch_id", None)
    clear_preview(token)
    if batch_id:
        try:
            cancel_validated_batch(int(batch_id), user=current_user)
        except ImportErrorClosed as exc:
            flash(str(exc), "error")
            return redirect(url_for("inventory.upload"))
    flash("Import preview cancelled.", "success")
    return redirect(url_for("inventory.upload"))


@bp.route("/imports/<int:batch_id>/status")
@permission_required("INVENTORY_UPLOAD")
def import_status(batch_id):
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        abort(404)
    return jsonify(batch_progress(batch))


@bp.route("/imports/<int:batch_id>/advance", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_advance(batch_id):
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        abort(404)
    try:
        resolve_context(current_user, batch.client_id, batch.warehouse_id)
        if batch.status == "PROCESSING":
            process_import_batch(batch.id, max_chunks=3)
            batch = db.session.get(ImportBatch, batch.id)
    except ImportErrorClosed as exc:
        return jsonify({"error": str(exc), **batch_progress(batch)}), 400
    return jsonify(batch_progress(batch))


@bp.route("/imports/<int:batch_id>/retry", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_retry(batch_id):
    try:
        batch = retry_import(batch_id, user=current_user, run="async")
    except ImportErrorClosed as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.upload"))
    session["inv_import_batch_id"] = batch.id
    return redirect(
        url_for(
            "inventory.upload",
            client_id=batch.client_id,
            warehouse_id=batch.warehouse_id,
            import_batch_id=batch.id,
        )
    )


@bp.route("/imports/<int:batch_id>/cleanup", methods=["POST"])
@permission_required("INVENTORY_UPLOAD")
def import_cleanup(batch_id):
    try:
        batch = cleanup_failed_import(batch_id, user=current_user)
    except ImportErrorClosed as exc:
        flash(str(exc), "error")
        return redirect(url_for("inventory.upload"))
    flash("Failed import cleaned up. No operational inventory remains from that file.", "success")
    session.pop("inv_import_batch_id", None)
    return redirect(
        url_for("inventory.upload", client_id=batch.client_id, warehouse_id=batch.warehouse_id)
    )


@bp.route("/template")
@permission_required("INVENTORY_UPLOAD")
def template():
    return send_file(
        io.BytesIO(template_bytes()),
        as_attachment=True,
        download_name="WMS_V2_Inventory_Template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/search")
@permission_required("INVENTORY_VIEW")
def search():
    ctx = _scope()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    results = []
    if q or status:
        results = iq.search_units(
            q,
            status=status or None,
            client_id=ctx["scope"]["client_id"],
            warehouse_id=ctx["scope"]["warehouse_id"],
            client_ids=ctx["client_ids"],
        )
    return _page(
        "inventory/search.html",
        "search",
        q=q,
        status=status,
        statuses=UnitStatus.ALL,
        results=results,
    )


@bp.route("/upc")
@permission_required("INVENTORY_VIEW")
def upc_view():
    ctx = _scope()
    upc = request.args.get("upc", "").strip()
    rows = []
    summary = None
    if upc:
        rows = [
            r
            for r in iq.aggregate_rows(
                client_id=ctx["scope"]["client_id"],
                warehouse_id=ctx["scope"]["warehouse_id"],
                client_ids=ctx["client_ids"],
            )
            if r["upc"] == upc
        ]
        if rows:
            summary = {
                "upc": upc,
                "sku": rows[0]["sku"],
                "description": rows[0]["description"],
                "style": rows[0]["style"],
                "available": sum(r["available"] for r in rows),
                "reserved": sum(r["reserved"] for r in rows),
                "packed": sum(r["packed"] for r in rows),
                "on_hand": sum(r["on_hand"] for r in rows),
                "num_locations": len({r["location"] for r in rows}),
                "locations": rows,
            }
    return _page("inventory/upc.html", "search", upc=upc, summary=summary, client=None, warehouse=None)


@bp.route("/location")
@permission_required("INVENTORY_VIEW")
def location_view():
    ctx = _scope()
    upc = request.args.get("upc", "").strip()
    location = request.args.get("location", "").strip()
    units = []
    if upc and location:
        units = (
            apply_operational_visibility(InventoryUnit.query.filter_by(upc=upc, location=location))
            .filter(
                InventoryUnit.client_id.in_(
                    [ctx["scope"]["client_id"]]
                    if ctx["scope"]["client_id"]
                    else (ctx["client_ids"] or [-1])
                )
            )
            .order_by(InventoryUnit.id)
            .all()
        )
        if ctx["scope"]["warehouse_id"]:
            units = [u for u in units if u.warehouse_id == ctx["scope"]["warehouse_id"]]
    return _page("inventory/location.html", "search", upc=upc, location=location, units=units)


@bp.route("/transactions")
@permission_required("INVENTORY_VIEW")
def transactions():
    ctx = _scope()
    filters = {
        "upc": request.args.get("upc", "").strip(),
        "type": request.args.get("type", "").strip(),
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
    }
    date_from = datetime.strptime(filters["date_from"], "%Y-%m-%d") if filters["date_from"] else None
    date_to = (
        datetime.strptime(filters["date_to"], "%Y-%m-%d") + timedelta(days=1)
        if filters["date_to"]
        else None
    )
    movements = iq.ledger_rows(
        client_id=ctx["scope"]["client_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        client_ids=ctx["client_ids"],
        upc=filters["upc"] or None,
        transaction_type=filters["type"] or None,
        date_from=date_from,
        date_to=date_to,
    )
    return _page("inventory/transactions.html", "transactions", filters=filters, movements=movements)


@bp.route("/transactions.csv")
@permission_required("INVENTORY_EXPORT")
def transactions_csv():
    ctx = _scope()
    movements = iq.ledger_rows(
        client_id=ctx["scope"]["client_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        client_ids=ctx["client_ids"],
        upc=request.args.get("upc") or None,
        transaction_type=request.args.get("type") or None,
    )
    frame = pd.DataFrame(
        [
            {
                "created_at": m.created_at,
                "unit_id": m.inventory_unit_id,
                "upc": m.upc,
                "client_id": m.client_id,
                "warehouse_id": m.warehouse_id,
                "location": m.location,
                "type": m.transaction_type,
                "from_status": m.from_status,
                "to_status": m.to_status,
                "reference": m.reference,
            }
            for m in movements
        ]
    )
    buffer = io.BytesIO()
    frame.to_csv(buffer, index=False)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="inventory_transactions.csv", mimetype="text/csv")


@bp.route("/export.xlsx")
@permission_required("INVENTORY_EXPORT")
def export_xlsx():
    ctx = _scope()
    rows = iq.aggregate_rows(
        client_id=ctx["scope"]["client_id"],
        warehouse_id=ctx["scope"]["warehouse_id"],
        client_ids=ctx["client_ids"],
    )
    clients = {c.id: c.client_code for c in Client.query.all()}
    warehouses = {w.id: w.warehouse_code for w in Warehouse.query.all()}
    frame = pd.DataFrame(
        [
            {
                "Client": clients.get(r["client_id"]),
                "Warehouse": warehouses.get(r["warehouse_id"]),
                "UPC": r["upc"],
                "SKU": r["sku"],
                "Description": r["description"],
                "Style": r["style"],
                "Color": r["color"],
                "Size": r["size"],
                "Location": r["location"],
                "Available": r["available"],
                "Reserved": r["reserved"],
                "Packed": r["packed"],
                "OnHand": r["on_hand"],
            }
            for r in rows
        ]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="inventory_on_hand.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/imports")
@permission_required("INVENTORY_VIEW")
def import_history():
    ctx = _scope()
    query = ImportBatch.query.filter_by(type="INVENTORY")
    if ctx["scope"]["client_id"]:
        query = query.filter_by(client_id=ctx["scope"]["client_id"])
    elif ctx["client_ids"] is not None:
        query = query.filter(ImportBatch.client_id.in_(ctx["client_ids"] or [-1]))
    if ctx["scope"]["warehouse_id"]:
        query = query.filter_by(warehouse_id=ctx["scope"]["warehouse_id"])
    batches = query.order_by(ImportBatch.created_at.desc()).all()
    return _page("inventory/import_history.html", "import_history", batches=batches)


@bp.route("/imports/<int:batch_id>")
@permission_required("INVENTORY_VIEW")
def import_detail(batch_id):
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        abort(404)
    if not user_can_access_client(current_user, batch.client_id):
        abort(404)
    return _page("inventory/import_detail.html", "import_history", batch=batch)
