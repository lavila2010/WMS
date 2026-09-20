"""Helpers for Client + Warehouse + Order Type scope filtering in the UI."""

from __future__ import annotations

from ..models import Client, OrderType, Warehouse


def scope_options(selected_client_id: int | None = None) -> dict:
    clients = Client.query.order_by(Client.code).all()
    wh_query = Warehouse.query
    ot_query = OrderType.query
    if selected_client_id:
        wh_query = wh_query.filter_by(client_id=selected_client_id)
        ot_query = ot_query.filter_by(client_id=selected_client_id)
    return {
        "clients": clients,
        "warehouses": wh_query.order_by(Warehouse.code).all(),
        "order_types": ot_query.order_by(OrderType.code).all(),
    }


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_scope(args) -> dict:
    return {
        "client_id": _to_int(args.get("client_id")),
        "warehouse_id": _to_int(args.get("warehouse_id")),
        "order_type_id": _to_int(args.get("order_type_id")),
    }


def apply_scope(query, model, scope: dict):
    if scope.get("client_id"):
        query = query.filter(model.client_id == scope["client_id"])
    if scope.get("warehouse_id"):
        query = query.filter(model.warehouse_id == scope["warehouse_id"])
    if scope.get("order_type_id"):
        query = query.filter(model.order_type_id == scope["order_type_id"])
    return query
