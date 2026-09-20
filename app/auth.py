"""Authentication, authorization, auditing, and seeding helpers."""

from __future__ import annotations

from functools import wraps

from flask import abort, g, redirect, request, url_for
from flask_login import LoginManager, current_user
from sqlalchemy import event

from .extensions import db
from .models import (
    AuditEvent,
    Box,
    Document,
    ImportBatch,
    InventoryMovement,
    Invoice,
    OrderException,
    Permission,
    Transaction,
    User,
    UserPermission,
)
from .permissions import PERMISSIONS, USER_DEFAULTS

login_manager = LoginManager()

# Endpoints reachable without authentication.
PUBLIC_ENDPOINTS = {"auth.login", "health.health", "health.health_db", "static"}


@login_manager.user_loader
def load_user(user_id: str):
    user = db.session.get(User, int(user_id)) if user_id and user_id.isdigit() else None
    # A disabled account immediately ends the session.
    if user is None or not user.active:
        return None
    return user


def _stamp_created_by(uid_attr, uname_attr):
    def _listener(mapper, connection, target):
        uid, uname = current_actor()
        if getattr(target, uid_attr, None) is None:
            setattr(target, uid_attr, uid)
        if getattr(target, uname_attr, None) is None:
            setattr(target, uname_attr, uname)

    return _listener


_ATTRIBUTION = [
    (Transaction, "user_id", "username"),
    (InventoryMovement, "created_by_user_id", "created_by_username"),
    (Box, "created_by_user_id", "created_by_username"),
    (Invoice, "created_by_user_id", "created_by_username"),
    (Document, "created_by_user_id", "created_by_username"),
    (ImportBatch, "created_by_user_id", "created_by_username"),
    (OrderException, "created_by_user_id", "created_by_username"),
]

_listeners_registered = False


def register_attribution_listeners():
    """Auto-populate created_by/user fields from the authenticated user on
    insert. Explicitly-set values win; outside a request the actor is SYSTEM."""
    global _listeners_registered
    if _listeners_registered:
        return
    for model, uid_attr, uname_attr in _ATTRIBUTION:
        event.listen(model, "before_insert", _stamp_created_by(uid_attr, uname_attr))
    _listeners_registered = True


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    register_attribution_listeners()

    @app.before_request
    def _require_auth():
        # Ensure the authenticated user is resolved per-request (not cached in a
        # long-lived app context, e.g. under the test harness).
        g.pop("_login_user", None)
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint == "":
            return None
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.path))
        # Force password change before any other use.
        if getattr(current_user, "must_change_password", False) and endpoint not in {
            "auth.change_password", "auth.logout", "static",
        }:
            return redirect(url_for("auth.change_password"))
        return None

    @app.context_processor
    def _inject_helpers():
        from .permissions import NAV_MODULES

        def can(code: str) -> bool:
            return current_user.is_authenticated and current_user.has_permission(code)

        return {"can": can, "nav_modules": NAV_MODULES}


def permission_required(code: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.path))
            if not current_user.has_permission(code):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def current_actor():
    """Return (user_id, username) for the authenticated user, or (None, 'SYSTEM')."""
    try:
        if current_user and current_user.is_authenticated:
            return current_user.id, current_user.username
    except Exception:
        pass
    return None, "SYSTEM"


def record_audit(event_type, *, module=None, entity_type=None, entity_id=None, detail=None, commit=False):
    uid, uname = current_actor()
    ip = None
    try:
        ip = request.remote_addr
    except Exception:
        pass
    event = AuditEvent(
        user_id=uid, username=uname, event_type=event_type, module=module,
        entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None,
        ip_address=ip, detail=detail,
    )
    db.session.add(event)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return event


# --- Seeding ---

def seed_permissions():
    """Idempotently create the permission catalog."""
    for code, desc, module in PERMISSIONS:
        perm = Permission.query.filter_by(code=code).first()
        if perm is None:
            db.session.add(Permission(code=code, description=desc, module=module))
        else:
            perm.description = desc
            perm.module = module
    db.session.commit()


def grant_permissions(user: User, codes, actor_id=None):
    """Grant the given permission codes to a user (idempotent)."""
    for code in codes:
        perm = Permission.query.filter_by(code=code).first()
        if perm is None:
            continue
        link = UserPermission.query.filter_by(user_id=user.id, permission_id=perm.id).first()
        if link is None:
            db.session.add(
                UserPermission(user_id=user.id, permission_id=perm.id, granted=True, created_by_user_id=actor_id)
            )
        else:
            link.granted = True
    db.session.commit()


def set_user_permissions(user: User, granted_codes, actor_id=None):
    """Set a user's permission set exactly to ``granted_codes``."""
    granted = set(granted_codes)
    for code, _desc, _mod in PERMISSIONS:
        perm = Permission.query.filter_by(code=code).first()
        if perm is None:
            continue
        link = UserPermission.query.filter_by(user_id=user.id, permission_id=perm.id).first()
        want = code in granted
        if link is None:
            if want:
                db.session.add(
                    UserPermission(user_id=user.id, permission_id=perm.id, granted=True, created_by_user_id=actor_id)
                )
        else:
            link.granted = want
    db.session.commit()


def grant_user_defaults(user: User, actor_id=None):
    grant_permissions(user, USER_DEFAULTS, actor_id=actor_id)
