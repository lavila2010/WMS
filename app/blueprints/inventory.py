from flask import Blueprint, render_template

from ..auth import permission_required

bp = Blueprint("inventory", __name__, url_prefix="/inventory")


@bp.route("/")
@permission_required("INVENTORY_VIEW")
def overview():
    return render_template("v2/placeholder.html", title="Inventory", module="Inventory")
