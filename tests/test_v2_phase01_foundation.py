import threading

import pytest
from sqlalchemy import text

from app.config import Config
from app.constants import APP_VERSION
from app.extensions import db
from app.models import Client, Division, DivisionWarehouse, Warehouse
from app.services.masters import (
    MasterError,
    create_client,
    create_division,
    create_warehouse,
    map_division_warehouse,
    update_client,
)
from tests.conftest import create_user, form_data, login


def test_p1_01_client_sequence_concurrency(app, db):
    results = []
    errors = []

    def worker(idx):
        with app.app_context():
            try:
                client = create_client(f"Brand{idx}", "CEL" if idx % 2 == 0 else "DIO")
                db.session.commit()
                results.append((client.sequence_number, client.client_code))
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    seqs = sorted(r[0] for r in results)
    assert seqs == sorted(set(seqs))
    assert len(seqs) == 4
    assert seqs == list(range(min(seqs), min(seqs) + 4))


def test_p1_02_client_code_unique_upper_immutable(app, db):
    a = create_client("Celine", "cel")
    db.session.commit()
    assert a.initials == "CEL"
    assert a.client_code == "01-CEL"
    b = create_client("Celine Paris", "cel")
    db.session.commit()
    assert b.client_code == "02-CEL"
    assert a.client_code != b.client_code
    update_client(a, name="Celine SA")
    db.session.commit()
    assert Client.query.get(a.id).client_code == "01-CEL"


def test_p1_03_division_tenant_ownership(app, db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    div = create_division(celine, "Ecommerce", "ECOM")
    db.session.commit()
    assert div.code == "01-CEL-ECOM"
    assert div.client_id == celine.id
    with pytest.raises(MasterError):
        create_division(celine, "Ecom 2", "ECOM")
    other = create_division(dior, "Ecommerce", "ECOM")
    db.session.commit()
    assert other.code == "02-DIO-ECOM"
    assert Division.query.filter_by(id=div.id).first().client_id != dior.id


def test_p1_04_warehouse_symbol_repeat_across_clients(app, db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    ny1 = create_warehouse(celine, "ny", "New York")
    ny2 = create_warehouse(dior, "NY", "New York")
    db.session.commit()
    assert ny1.warehouse_code == "01-CEL-NY"
    assert ny2.warehouse_code == "02-DIO-NY"
    with pytest.raises(MasterError):
        create_warehouse(celine, "NY")


def test_p1_05_mapping_rejects_cross_client(app, db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    div = create_division(celine, "Ecom", "ECOM")
    wh = create_warehouse(dior, "NY")
    db.session.commit()
    with pytest.raises(MasterError, match="same client"):
        map_division_warehouse(div, wh)
    db.session.rollback()
    with pytest.raises(Exception):
        db.session.add(
            DivisionWarehouse(
                client_id=celine.id,
                division_id=div.id,
                warehouse_id=wh.id,
                active=True,
            )
        )
        db.session.commit()
    db.session.rollback()
    own = create_warehouse(celine, "NY")
    mapping = map_division_warehouse(div, own)
    db.session.commit()
    assert mapping.client_id == celine.id


def test_p1_06_user_without_clients_sees_no_masters(app, db, client):
    create_client("Celine", "CEL")
    db.session.commit()
    create_user("nobody", perms=["CLIENTS_VIEW", "DASHBOARD_VIEW"])
    login(client, "nobody")
    html = client.get("/admin/clients").get_data(as_text=True)
    assert "01-CEL" not in html
    dash = client.get("/").get_data(as_text=True)
    assert "01-CEL" not in dash


def test_p1_07_idor_denied(app, db, client):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    create_user(
        "alice",
        perms=["CLIENTS_VIEW", "CLIENTS_EDIT", "DASHBOARD_VIEW"],
        clients=[celine.id],
    )
    login(client, "alice")
    ok = client.get(f"/admin/clients/{celine.id}/edit")
    assert ok.status_code == 200
    denied = client.get(f"/admin/clients/{dior.id}/edit")
    assert denied.status_code == 404


def test_p1_08_admin_sees_all_clients(app, db, admin_client):
    create_client("Celine", "CEL")
    create_client("Dior", "DIO")
    db.session.commit()
    html = admin_client.get("/admin/clients").get_data(as_text=True)
    assert "01-CEL" in html and "02-DIO" in html


def test_p1_09_rbac_denies_client_create(app, db, client):
    create_user("limited", perms=["DASHBOARD_VIEW", "CLIENTS_VIEW"])
    login(client, "limited")
    assert client.get("/admin/clients/new").status_code == 403
    resp = client.post("/admin/clients/new", data=form_data(client, {"name": "X", "initials": "XXX"}))
    assert resp.status_code == 403
    assert Client.query.count() == 0


def test_p1_10_admin_crud_masters(app, db, admin_client):
    resp = admin_client.post(
        "/admin/clients/new",
        data=form_data(admin_client, {"name": "Valentino", "initials": "val"}),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    client_row = Client.query.filter_by(initials="VAL").one()
    assert client_row.client_code == "01-VAL"
    admin_client.post(
        "/admin/divisions",
        data=form_data(admin_client, {"client_id": client_row.id, "name": "Ecom", "operation_type": "ECOM"}),
        follow_redirects=True,
    )
    admin_client.post(
        "/admin/warehouses",
        data=form_data(admin_client, {"client_id": client_row.id, "warehouse_symbol": "ny", "name": "NYC"}),
        follow_redirects=True,
    )
    div = Division.query.one()
    wh = Warehouse.query.one()
    assert div.code == "01-VAL-ECOM"
    assert wh.warehouse_code == "01-VAL-NY"
    admin_client.post(
        "/admin/mappings",
        data=form_data(admin_client, {"division_id": div.id, "warehouse_id": wh.id}),
        follow_redirects=True,
    )
    assert DivisionWarehouse.query.count() == 1
    create_user("pat", role="USER")
    user = db.session.execute(text("SELECT id FROM users WHERE username='pat'")).scalar()
    admin_client.post(
        "/admin/client-access",
        data=form_data(admin_client, {"user_id": user, f"client_{client_row.id}": "on"}),
        follow_redirects=True,
    )
    from app.models import UserClient

    assert UserClient.query.filter_by(user_id=user, client_id=client_row.id).first()


def test_p1_11_production_rejects_insecure_secret(monkeypatch):
    monkeypatch.setenv("WMS_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-insecure-secret-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://wms:wms@127.0.0.1:5432/wms_test")
    monkeypatch.delenv("WMS_V2_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Config()


def test_p1_12_health_has_no_secrets(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["application_version"] == APP_VERSION
    assert data["database"] in {"ok", "unavailable"}
    blob = resp.get_data(as_text=True).lower()
    assert "password" not in blob
    assert "secret" not in blob
    assert "postgres://" not in blob
