from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..constants import APP_VERSION
from ..extensions import db

bp = Blueprint("health", __name__)


@bp.route("/health")
def health():
    db_status = "unavailable"
    http = 200
    try:
        db.session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unavailable"
        http = 503
    return (
        jsonify(
            {
                "status": "ok" if db_status == "ok" else "degraded",
                "database": db_status,
                "application_version": APP_VERSION,
            }
        ),
        http,
    )


@bp.route("/health/db")
def health_db():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "connected", "application_version": APP_VERSION})
    except Exception:
        return jsonify({"status": "error", "database": "unavailable", "application_version": APP_VERSION}), 503
