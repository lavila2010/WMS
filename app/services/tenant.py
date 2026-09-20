"""Server-side tenant authorization. Fail closed."""

from __future__ import annotations

from flask import abort

from ..models import Client, User, UserClient


def user_can_access_client(user: User | None, client_id: int | None) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if client_id is None:
        return False
    if user.is_admin():
        return Client.query.get(client_id) is not None
    return (
        UserClient.query.filter_by(user_id=user.id, client_id=client_id).first()
        is not None
    )


def accessible_clients(user: User):
    if user.is_admin():
        return Client.query.order_by(Client.client_code).all()
    return (
        Client.query.join(UserClient, UserClient.client_id == Client.id)
        .filter(UserClient.user_id == user.id)
        .order_by(Client.client_code)
        .all()
    )


def require_client_access(user: User, client_id: int | None, *, not_found=True):
    """Abort if the user may not use this client.

    Operational IDOR uses 404 (fail closed, no existence leak).
    """
    if user_can_access_client(user, client_id):
        return
    abort(404 if not_found else 403)


def require_entity_client(user: User, entity):
    if entity is None:
        abort(404)
    client_id = getattr(entity, "client_id", None)
    require_client_access(user, client_id)
    return entity
