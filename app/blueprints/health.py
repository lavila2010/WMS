from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..extensions import db

bp = Blueprint("health", __name__)


@bp.route("/health")
def health():
    return jsonify({"status": "ok"})


@bp.route("/health/db")
def health_db():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "connected"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "database": "unavailable", "detail": str(exc)}), 503
