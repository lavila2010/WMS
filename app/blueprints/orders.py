from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("orders", __name__, url_prefix="/orders")


@bp.route("/")
@permission_required("ORDERS_VIEW")
def index():
    return render_template("v2/placeholder.html", title="Orders", module="Orders")
