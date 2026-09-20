from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user

from ..auth import permission_required
from ..services.order_kpis import get_order_kpis, kpi_filter_options, operational_today

bp = Blueprint("kpi", __name__, url_prefix="/kpi")


def _filters():
    today = operational_today().isoformat()
    args = request.args
    applied = any(args.get(k) for k in ("from_date", "to_date", "client_id", "division_id", "q"))
    return {
        "client_id": args.get("client_id") or "",
        "division_id": args.get("division_id") or "",
        "from_date": args.get("from_date") or today,
        "to_date": args.get("to_date") or today,
        "q": args.get("q") or "",
        "applied": applied,
    }


def _payload(filters):
    return get_order_kpis(
        from_date=filters["from_date"],
        to_date=filters["to_date"],
        client_id=filters["client_id"] or None,
        division_id=filters["division_id"] or None,
        search=filters["q"] or None,
    )


@bp.route("/orders")
@permission_required("REPORTS_VIEW")
def orders():
    filters = _filters()
    data = _payload(filters)
    client_id = filters["client_id"] or None
    return render_template(
        "kpi/orders.html",
        filters=filters,
        options=kpi_filter_options(client_id),
        overall=data["overall"],
        rows=data["rows"],
        totals=data["totals"],
        channels=data["channels"],
        today=operational_today().isoformat(),
    )


@bp.route("/orders.json")
@permission_required("REPORTS_VIEW")
def orders_json():
    if not current_user.has_permission("REPORTS_VIEW"):
        abort(403)
    filters = _filters()
    payload = _payload(filters)
    payload["from_date"] = payload["from_date"].isoformat()
    payload["to_date"] = payload["to_date"].isoformat()
    return jsonify(payload)
