from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("kpi", __name__, url_prefix="/kpi")


@bp.route("/orders")
@permission_required("REPORTS_VIEW")
def orders():
    return render_template("v2/placeholder.html", title="KPI Orders", module="KPI Orders")
