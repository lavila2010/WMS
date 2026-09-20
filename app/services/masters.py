"""Client / Division / Warehouse / mapping writes."""

from __future__ import annotations

import re

from sqlalchemy import text

from ..constants import OperationType
from ..extensions import db
from ..models import Client, Division, DivisionWarehouse, Warehouse


class MasterError(ValueError):
    pass


def normalize_initials(raw: str) -> str:
    initials = re.sub(r"[^A-Za-z]", "", raw or "").upper()
    if len(initials) < 2 or len(initials) > 8:
        raise MasterError("Initials must be 2–8 letters.")
    return initials


def normalize_symbol(raw: str) -> str:
    symbol = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not symbol or len(symbol) > 16:
        raise MasterError("Warehouse symbol is required (letters/digits, max 16).")
    return symbol


def next_client_sequence() -> int:
    value = db.session.execute(text("SELECT nextval('client_code_seq')")).scalar()
    if value is None:
        raise MasterError("Client sequence is unavailable.")
    return int(value)


def create_client(name: str, initials: str, *, actor_id=None) -> Client:
    name = (name or "").strip()
    if not name:
        raise MasterError("Client name is required.")
    initials = normalize_initials(initials)
    seq = next_client_sequence()
    code = f"{seq:02d}-{initials}"
    if Client.query.filter_by(client_code=code).first():
        raise MasterError(f"Client code {code} already exists.")
    client = Client(
        sequence_number=seq,
        client_code=code,
        name=name,
        initials=initials,
        active=True,
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.session.add(client)
    db.session.flush()
    return client


def update_client(client: Client, *, name=None, active=None, actor_id=None) -> Client:
    if name is not None:
        name = name.strip()
        if not name:
            raise MasterError("Client name is required.")
        client.name = name
    if active is not None:
        client.active = bool(active)
    client.updated_by = actor_id
    return client


def create_division(client: Client, name: str, operation_type: str, *, actor_id=None) -> Division:
    if not client.active:
        raise MasterError("Cannot add a division to an inactive client.")
    op = (operation_type or "").strip().upper()
    if op not in OperationType.ALL:
        raise MasterError("Operation type must be ECOM, RTL, or WHLS.")
    name = (name or "").strip() or op
    code = f"{client.client_code}-{op}"
    if Division.query.filter_by(code=code).first():
        raise MasterError(f"Division {code} already exists.")
    if Division.query.filter_by(client_id=client.id, operation_type=op).first():
        raise MasterError(f"Client already has a {op} division.")
    division = Division(
        client_id=client.id,
        code=code,
        name=name,
        operation_type=op,
        active=True,
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.session.add(division)
    db.session.flush()
    return division


def create_warehouse(client: Client, symbol: str, name: str | None = None, *, actor_id=None) -> Warehouse:
    if not client.active:
        raise MasterError("Cannot add a warehouse to an inactive client.")
    symbol = normalize_symbol(symbol)
    code = f"{client.client_code}-{symbol}"
    if Warehouse.query.filter_by(client_id=client.id, warehouse_symbol=symbol).first():
        raise MasterError(f"Warehouse {symbol} already exists for this client.")
    warehouse = Warehouse(
        client_id=client.id,
        warehouse_symbol=symbol,
        warehouse_code=code,
        name=(name or symbol).strip() or symbol,
        active=True,
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.session.add(warehouse)
    db.session.flush()
    return warehouse


def map_division_warehouse(division: Division, warehouse: Warehouse) -> DivisionWarehouse:
    if division.client_id != warehouse.client_id:
        raise MasterError("Division and warehouse must belong to the same client.")
    existing = DivisionWarehouse.query.filter_by(
        division_id=division.id, warehouse_id=warehouse.id
    ).first()
    if existing:
        existing.active = True
        return existing
    mapping = DivisionWarehouse(
        client_id=division.client_id,
        division_id=division.id,
        warehouse_id=warehouse.id,
        active=True,
    )
    db.session.add(mapping)
    db.session.flush()
    return mapping
