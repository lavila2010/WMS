"""Unified import recovery endpoint. Mutation is POST-only."""

from __future__ import annotations

from flask import Blueprint, abort, jsonify
from flask_login import current_user

from ..constants import ImportType
from ..extensions import db
from ..models import ImportBatch
from ..services.import_execution import recover_import_batch
from ..services.tenant import user_can_access_client

bp = Blueprint("imports", __name__, url_prefix="/imports")


@bp.route("/<int:batch_id>/recover", methods=["POST"])
def recover(batch_id):
    if not current_user.is_authenticated:
        abort(401)
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None or not user_can_access_client(current_user, batch.client_id):
        abort(404)
    needed = (
        "INVENTORY_UPLOAD" if batch.type == ImportType.INVENTORY else "ORDERS_UPLOAD"
    )
    if not current_user.has_permission(needed):
        abort(403)
    try:
        payload = recover_import_batch(batch.id, user=current_user)
    except ValueError:
        abort(404)
    return jsonify(payload)
