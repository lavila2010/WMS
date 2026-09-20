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

    from .auth import init_auth
    init_auth(app)

    from .blueprints.health import bp as health_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.orders import bp as orders_bp
    from .blueprints.allocation import bp as allocation_bp
    from .blueprints.processing import bp as processing_bp
    from .blueprints.reports import bp as reports_bp
    from .blueprints.admin import bp as admin_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)

    @app.errorhandler(403)
    def _forbidden(_e):
        from flask import render_template

        return render_template("403.html"), 403

    _register_cli(app)
    return app


def _register_cli(app):
    import click

    @app.cli.command("init-db")
    def init_db() -> None:  # pragma: no cover - invoked via CLI
        """Create all database tables and seed permissions."""
        from .auth import seed_permissions

        db.create_all()
        seed_permissions()
        print("Database tables created and permissions seeded.")

    @app.cli.command("seed-permissions")
    def seed_permissions_cmd() -> None:  # pragma: no cover
        from .auth import seed_permissions

        seed_permissions()
        print("Permissions seeded.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--full-name", prompt="Full name", default="")
    @click.option("--email", prompt=True, default="")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username, full_name, email, password):  # pragma: no cover - CLI
        """Create the first ADMIN user (no hard-coded credentials)."""
        from werkzeug.security import generate_password_hash

        from .auth import seed_permissions
        from .models import User

        seed_permissions()
        if User.query.filter_by(username=username).first():
            print(f"User '{username}' already exists.")
            return
        admin = User(
            username=username,
            password_hash=generate_password_hash(password),
            full_name=full_name or None,
            email=email or None,
            role="ADMIN",
            active=True,
            must_change_password=False,
        )
        db.session.add(admin)
        db.session.commit()
        print(f"Admin user '{username}' created.")
