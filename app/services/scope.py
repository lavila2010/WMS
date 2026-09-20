"""Master-data resolution for the Client + Warehouse + Order Type scope.

Import flows reference clients, warehouses, and order types by code. These
helpers resolve an existing master row or create it on first use, keeping
warehouses and order types scoped to their owning client.
"""

from __future__ import annotations

from ..extensions import db
from ..models import Client, OrderType, Warehouse


def get_or_create_client(code: str, name: str | None = None) -> Client:
    code = (code or "").strip()
    if not code:
        raise ValueError("Client code is required.")
    client = Client.query.filter_by(code=code).first()
    if client is None:
        client = Client(code=code, name=(name or code).strip() or code)
        db.session.add(client)
        db.session.flush()
    return client


def get_or_create_warehouse(client: Client, code: str, name: str | None = None) -> Warehouse:
    code = (code or "").strip()
    if not code:
        raise ValueError("Warehouse code is required.")
    wh = Warehouse.query.filter_by(client_id=client.id, code=code).first()
    if wh is None:
        wh = Warehouse(
            client_id=client.id, code=code, name=(name or code).strip() or code
        )
        db.session.add(wh)
        db.session.flush()
    return wh


def get_or_create_order_type(client: Client, code: str, name: str | None = None) -> OrderType:
    code = (code or "").strip()
    if not code:
        raise ValueError("Order type code is required.")
    ot = OrderType.query.filter_by(client_id=client.id, code=code).first()
    if ot is None:
        ot = OrderType(
            client_id=client.id, code=code, name=(name or code).strip() or code
        )
        db.session.add(ot)
        db.session.flush()
    return ot


def resolve_scope(client_code: str, warehouse_code: str, order_type_code: str):
    """Resolve/create a (Client, Warehouse, OrderType) triple by codes."""
    client = get_or_create_client(client_code)
    warehouse = get_or_create_warehouse(client, warehouse_code)
    order_type = get_or_create_order_type(client, order_type_code)
    return client, warehouse, order_type
