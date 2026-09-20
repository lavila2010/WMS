from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
@permission_required("REPORTS_VIEW")
def index():
    return render_template("v2/placeholder.html", title="Order Reports", module="Order Reports")
