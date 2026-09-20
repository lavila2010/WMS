"""WMS Flask application factory."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask

from .config import Config
from .extensions import db


def create_app(config: Config | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config or Config())

    os.makedirs(app.config["DOCUMENTS_DIR"], exist_ok=True)

    db.init_app(app)

    # Import models so they are registered on the metadata.
    from . import models  # noqa: F401

    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.orders import bp as orders_bp
    from .blueprints.allocation import bp as allocation_bp
    from .blueprints.processing import bp as processing_bp
    from .blueprints.reports import bp as reports_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(reports_bp)

    @app.cli.command("init-db")
    def init_db() -> None:  # pragma: no cover - invoked via CLI
        """Create all database tables."""
        db.create_all()
        print("Database tables created.")

    return app
