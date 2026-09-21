"""WMS V2 Flask application factory."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask

from .config import Config
from .extensions import csrf, db


def create_app(config: Config | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config or Config())
    os.makedirs(app.config["DOCUMENTS_DIR"], exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    from . import models  # noqa: F401

    from .auth import init_auth

    init_auth(app)

    with app.app_context():
        try:
            from .schema import ensure_v2_schema

            ensure_v2_schema()
            from .services.import_execution import inspect_orphaned_import_batches

            inspect_orphaned_import_batches()
        except Exception:
            pass

    from .blueprints.admin import bp as admin_bp
    from .blueprints.allocation import bp as allocation_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.health import bp as health_bp
    from .blueprints.imports import bp as imports_bp
    from .blueprints.inventory import bp as inventory_bp
    from .blueprints.kpi import bp as kpi_bp
    from .blueprints.orders import bp as orders_bp
    from .blueprints.processing import bp as processing_bp
    from .blueprints.reports import bp as reports_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(imports_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(kpi_bp)
    app.register_blueprint(admin_bp)

    csrf.exempt(health_bp)

    @app.errorhandler(403)
    def _forbidden(_e):
        from flask import render_template

        return render_template("403.html"), 403

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _csrf_failure(_e):
        from flask import jsonify, render_template, request

        # Safe client message only. Do not log tokens, cookies, or credentials.
        payload = {"error": "CSRF validation failed"}
        wants_json = (
            request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.accept_mimetypes.best_match(["application/json", "text/html"])
            == "application/json"
        )
        if wants_json:
            return jsonify(payload), 400
        return render_template("400.html"), 400

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

    @app.cli.command("process-inventory-import")
    @click.option("--batch-id", required=True, type=int)
    def process_inventory_import_cmd(batch_id):
        """Admin fallback: process one inventory import batch in this process."""
        from .services.inventory_import import process_import_batch

        batch = process_import_batch(batch_id)
        click.echo(f"Batch {batch.id} status={batch.status} units={batch.units_created}")

    @app.cli.command("process-order-import")
    @click.option("--batch-id", required=True, type=int)
    def process_order_import_cmd(batch_id):
        """Admin fallback: process one order import batch in this process."""
        from .services.order_import import process_import_batch

        batch = process_import_batch(batch_id)
        click.echo(
            f"Batch {batch.id} status={batch.status} "
            f"orders={batch.orders_created} lines={batch.order_lines_created}"
        )

    @app.cli.command("import-worker")
    def import_worker_cmd():
        """Run the durable import worker loop (inventory + orders)."""
        from .workers.import_worker import run_forever

        run_forever()

    @app.cli.command("inventory-import-worker")
    def inventory_import_worker_cmd():
        """Compatibility alias for the unified import worker."""
        from .workers.import_worker import run_forever

        run_forever()

    @app.cli.command("recover-imports")
    def recover_imports_cmd():
        """Emergency: advance stale/orphaned PROCESSING import batches."""
        from .services.import_execution import recover_stale_imports

        results = recover_stale_imports()
        if not results:
            click.echo("No PROCESSING import batches.")
            return
        for row in results:
            click.echo(
                f"batch={row.get('batch_id')} type={row.get('status')} "
                f"recovered={row.get('recovered')} reason={row.get('reason')} "
                f"progress={row.get('progress_percent')}"
            )

    @app.cli.command("pick-ticket-eligibility")
    @click.argument("wms_order_id")
    def pick_ticket_eligibility_cmd(wms_order_id):
        """Print allocation → Pick Ticket handoff fields. No customer PII."""
        from .models import Order
        from .services.fulfillment import pick_ticket_handoff_diagnostic

        order = Order.query.filter_by(wms_order_id=wms_order_id).one_or_none()
        if order is None:
            click.echo(f"Order {wms_order_id} was not found.")
            return
        snapshot = pick_ticket_handoff_diagnostic(order)
        for key, value in snapshot.items():
            click.echo(f"{key}={value}")
