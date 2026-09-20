from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("processing", __name__, url_prefix="/processing")


@bp.route("/")
@permission_required("PROCESSING_VIEW")
def index():
    return render_template("v2/placeholder.html", title="Order Processing", module="Order Processing")
