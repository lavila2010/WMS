"""WMS V2 Flask application factory."""

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
    from . import models  # noqa: F401

    from .auth import init_auth

    init_auth(app)

    with app.app_context():
        try:
            from .schema import ensure_v2_schema

            ensure_v2_schema()
        except Exception:
            pass

    from .blueprints.admin import bp as admin_bp
    from .blueprints.allocation import bp as allocation_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.health import bp as health_bp
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.kpi import bp as kpi_bp
    from .blueprints.orders import bp as orders_bp
    from .blueprints.processing import bp as processing_bp
    from .blueprints.reports import bp as reports_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(kpi_bp)
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
    def init_db() -> None:
        from .auth import seed_permissions
        from .schema import ensure_v2_schema

        db.create_all()
        ensure_v2_schema()
        seed_permissions()
        print("V2 tables created and permissions seeded.")

    @app.cli.command("seed-permissions")
    def seed_permissions_cmd() -> None:
        from .auth import seed_permissions

        seed_permissions()
        print("Permissions seeded.")

    @app.cli.command("create-admin")
    @click.option("--username", default="leandro", show_default=True)
    @click.option("--full-name", default="")
    @click.option("--email", default="")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username, full_name, email, password):
        from werkzeug.security import generate_password_hash

        from .auth import seed_permissions
        from .models import User
        from .schema import ensure_v2_schema

        db.create_all()
        ensure_v2_schema()
        seed_permissions()
        if User.query.filter_by(username=username).first():
            click.echo(f"User '{username}' already exists.")
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
        click.echo(f"Admin user '{username}' created.")

    @app.cli.command("create-test-users")
    def create_test_users():
        """Create UAT users. Passwords come from environment, never hardcoded."""
        from werkzeug.security import generate_password_hash

        from .auth import seed_permissions
        from .models import User
        from .schema import ensure_v2_schema

        db.create_all()
        ensure_v2_schema()
        seed_permissions()
        specs = [
            ("WMS_UAT_ADMIN_PASSWORD", "uat-admin", "ADMIN"),
            ("WMS_UAT_USER_PASSWORD", "uat-user", "USER"),
        ]
        for env_name, username, role in specs:
            password = os.environ.get(env_name)
            if not password:
                click.echo(f"Skipped {username}: set {env_name}.")
                continue
            if User.query.filter_by(username=username).first():
                click.echo(f"User '{username}' already exists.")
                continue
            db.session.add(
                User(
                    username=username,
                    password_hash=generate_password_hash(password),
                    role=role,
                    active=True,
                    must_change_password=True,
                )
            )
            db.session.commit()
            click.echo(f"Created {username} ({role}).")
