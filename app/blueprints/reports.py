from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    send_file,
    url_for,
)

from ..models import Box, Document, Order
from ..services.documents import (
    generate_box_detail,
    generate_order_closure,
    generate_packing_report,
    generate_pick_ticket,
)

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
def index():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    documents = Document.query.order_by(Document.created_at.desc()).limit(50).all()
    return render_template("reports/index.html", orders=orders, documents=documents)


@bp.route("/order/<int:order_id>/pick-ticket", methods=["POST"])
def pick_ticket(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_pick_ticket(order)
    flash(f"Pick Ticket generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/order/<int:order_id>/packing-report", methods=["POST"])
def packing_report(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_packing_report(order)
    flash(f"Packing Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/order/<int:order_id>/closure", methods=["POST"])
def closure(order_id: int):
    order = Order.query.get_or_404(order_id)
    doc = generate_order_closure(order)
    flash(f"Order Closure Report generated for {order.order_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/box/<int:box_id>/detail", methods=["POST"])
def box_detail(box_id: int):
    box = Box.query.get_or_404(box_id)
    doc = generate_box_detail(box)
    flash(f"Box Detail generated for {box.box_number}.", "success")
    return redirect(url_for("reports.download", document_id=doc.id))


@bp.route("/document/<int:document_id>")
def download(document_id: int):
    doc = Document.query.get_or_404(document_id)
    import os

    if not os.path.exists(doc.path):
        abort(404)
    return send_file(doc.path, as_attachment=True, download_name=doc.filename)
