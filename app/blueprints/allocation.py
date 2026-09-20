from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("allocation", __name__, url_prefix="/allocation")


@bp.route("/")
@permission_required("ALLOCATION_VIEW")
def index():
    return render_template("v2/placeholder.html", title="Allocation", module="Allocation")
