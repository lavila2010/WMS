from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from ..models import InventoryUnit
from ..services.imports import (
    ImportError_,
    build_inventory_workbook,
    import_inventory,
)

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    query = InventoryUnit.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            (InventoryUnit.barcode.ilike(like))
            | (InventoryUnit.sku.ilike(like))
            | (InventoryUnit.description.ilike(like))
        )
    units = query.order_by(InventoryUnit.created_at.desc()).limit(500).all()
    return render_template("inventory/index.html", units=units, q=q)


@bp.route("/import", methods=["GET", "POST"])
def import_view():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Inventory.xlsx file.", "error")
            return redirect(url_for("inventory.import_view"))
        try:
            batch = import_inventory(file.stream, file.filename)
        except ImportError_ as exc:
            flash(str(exc), "error")
            return redirect(url_for("inventory.import_view"))
        flash(batch.message, "success")
        return redirect(url_for("inventory.index"))
    return render_template("inventory/import.html")


@bp.route("/template")
def template():
    buf = build_inventory_workbook(
        [
            {"barcode": "BC-0001", "sku": "SKU-A", "description": "Sample A", "location": "A-01"},
            {"barcode": "BC-0002", "sku": "SKU-B", "description": "Sample B", "location": "B-02"},
        ]
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name="Inventory.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
