from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from ..auth import permission_required
from ..services.order_kpis import operational_today, summarize
from ..services.tenant import accessible_clients, user_can_access_client
from ..models import Division
from flask import abort

bp = Blueprint("kpi", __name__, url_prefix="/kpi")


def _payload():
    clients = accessible_clients(current_user)
    client_id = request.args.get("client_id", type=int)
    division_id = request.args.get("division_id", type=int)
    if client_id and not user_can_access_client(current_user, client_id):
        abort(404)
    if not current_user.is_admin() and not client_id:
        # Client mandatory unless global Admin
        client_ids = [c.id for c in clients]
    else:
        client_ids = None if current_user.is_admin() and not client_id else (
            [client_id] if client_id else [c.id for c in clients]
        )
    data = summarize(
        client_ids=client_ids,
        client_id=client_id,
        division_id=division_id,
        from_date=request.args.get("from_date"),
        to_date=request.args.get("to_date"),
        q=request.args.get("q", ""),
    )
    divisions = Division.query.filter_by(client_id=client_id).all() if client_id else []
    return data, clients, divisions, client_id, division_id


@bp.route("/orders")
@permission_required("REPORTS_VIEW")
def orders():
    data, clients, divisions, client_id, division_id = _payload()
    today = operational_today().isoformat()
    return render_template(
        "kpi/orders.html",
        rows=data["rows"],
        totals=data["totals"],
        overall=data["overall"],
        channels=data["channels"],
        options={"clients": clients, "divisions": divisions},
        filters={
            "client_id": str(client_id) if client_id else "",
            "division_id": str(division_id) if division_id else "",
            "from_date": request.args.get("from_date") or data["from_date"] or today,
            "to_date": request.args.get("to_date") or data["to_date"] or today,
            "q": request.args.get("q", ""),
        },
    )


@bp.route("/orders.json")
@permission_required("REPORTS_VIEW")
def orders_json():
    data, *_ = _payload()
    return jsonify(data)
