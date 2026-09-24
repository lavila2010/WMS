"""Gate 12B — init-db / create-admin against an empty PostgreSQL database."""

import os

import psycopg2
from sqlalchemy import text
from werkzeug.security import check_password_hash

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import Permission, User
from app.permissions import ALL_CODES
from app.services.masters import create_client

BOOTSTRAP_URL = os.environ.get(
    "BOOTSTRAP_TEST_DATABASE_URL",
    "postgresql://wms:wms@127.0.0.1:5432/wms_bootstrap_test",
)
ADMIN_PASSWORD = "BootstrapAdmin-Pass-32"


def _ensure_empty_database(url: str) -> None:
    parsed_db = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (parsed_db,))
    if cur.fetchone() is None:
        cur.execute(f'CREATE DATABASE "{parsed_db}"')
    cur.close()
    admin.close()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    cur.execute("GRANT ALL ON SCHEMA public TO public")
    cur.close()
    conn.close()


def _public_table_count(url: str) -> int:
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def test_g12b_empty_postgres_init_db_and_create_admin(tmp_path, monkeypatch):
    _ensure_empty_database(BOOTSTRAP_URL)
    assert _public_table_count(BOOTSTRAP_URL) == 0

    monkeypatch.setenv("WMS_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", BOOTSTRAP_URL)
    monkeypatch.delenv("WMS_V2_DATABASE_URL", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    config = Config()
    config.DOCUMENTS_DIR = str(tmp_path / "documents")
    application = create_app(config)
    runner = application.test_cli_runner()

    first = runner.invoke(args=["init-db"])
    assert first.exit_code == 0, first.output
    assert "V2 tables created" in first.output
    assert ADMIN_PASSWORD not in first.output
    assert "postgresql://" not in first.output
    assert ":wms@" not in first.output

    second = runner.invoke(args=["init-db"])
    assert second.exit_code == 0, second.output

    created = runner.invoke(
        args=["create-admin", "--username", "leandro", "--full-name", "Leandro"],
        input=f"{ADMIN_PASSWORD}\n{ADMIN_PASSWORD}\n",
    )
    assert created.exit_code == 0, created.output
    assert "Admin user 'leandro' created." in created.output
    assert ADMIN_PASSWORD not in created.output

    duplicate = runner.invoke(
        args=["create-admin", "--username", "leandro"],
        input="OtherSecret-99\nOtherSecret-99\n",
    )
    assert duplicate.exit_code == 0, duplicate.output
    assert "already exists" in duplicate.output
    assert "OtherSecret-99" not in duplicate.output

    with application.app_context():
        admin = User.query.filter_by(username="leandro").one()
        assert admin.role == "ADMIN"
        assert admin.password_hash != ADMIN_PASSWORD
        assert check_password_hash(admin.password_hash, ADMIN_PASSWORD)
        assert not check_password_hash(admin.password_hash, "OtherSecret-99")
        assert Permission.query.count() == len(ALL_CODES)
        codes = {row.code for row in Permission.query.all()}
        assert codes == set(ALL_CODES)
        exists = db.session.execute(text("SELECT to_regclass('public.client_code_seq')")).scalar()
        assert exists == "client_code_seq"
        client = create_client("Celine", "CEL")
        db.session.commit()
        assert client.client_code == "01-CEL"
        assert client.sequence_number == 1
        assert User.query.count() == 1
