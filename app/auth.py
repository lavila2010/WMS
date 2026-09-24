"""Authentication, RBAC, tenant helpers, and permission seeding."""

from __future__ import annotations

from functools import wraps

from flask import abort, g, redirect, request, url_for
from flask_login import LoginManager, current_user

from .extensions import db
from .models import AuditEvent, Permission, User, UserPermission
from .permissions import PERMISSIONS, USER_DEFAULTS

login_manager = LoginManager()
PUBLIC_ENDPOINTS = {"auth.login", "health.health", "health.health_db", "static"}


@login_manager.user_loader
def load_user(user_id: str):
    user = db.session.get(User, int(user_id)) if user_id and user_id.isdigit() else None
    if user is None or not user.active:
        return None
    return user


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @app.before_request
    def _require_auth():
        g.pop("_login_user", None)
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint == "":
            return None
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.path))
        if getattr(current_user, "must_change_password", False) and endpoint not in {
            "auth.change_password",
            "auth.logout",
            "static",
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
    try:
        if current_user and current_user.is_authenticated:
            return current_user.id, current_user.username
    except Exception:
        pass
    return None, "SYSTEM"


def record_audit(
    event_type,
    *,
    module=None,
    entity_type=None,
    entity_id=None,
    detail=None,
    client_id=None,
    commit=False,
):
    uid, uname = current_actor()
    ip = None
    try:
        ip = request.remote_addr
    except Exception:
        ip = None
    event = AuditEvent(
        user_id=uid,
        username=uname,
        client_id=client_id,
        event_type=event_type,
        module=module,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        ip_address=ip,
        detail=detail,
    )
    db.session.add(event)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return event


def seed_permissions():
    for code, desc, module in PERMISSIONS:
        perm = Permission.query.filter_by(code=code).first()
        if perm is None:
            db.session.add(Permission(code=code, description=desc, module=module))
        else:
            perm.description = desc
            perm.module = module
    db.session.commit()


def grant_permissions(user: User, codes, actor_id=None):
    for code in codes:
        perm = Permission.query.filter_by(code=code).first()
        if perm is None:
            continue
        link = UserPermission.query.filter_by(user_id=user.id, permission_id=perm.id).first()
        if link is None:
            db.session.add(
                UserPermission(
                    user_id=user.id,
                    permission_id=perm.id,
                    granted=True,
                    created_by_user_id=actor_id,
                )
            )
        else:
            link.granted = True
    db.session.commit()


def set_user_permissions(user: User, granted_codes, actor_id=None):
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
                    UserPermission(
                        user_id=user.id,
                        permission_id=perm.id,
                        granted=True,
                        created_by_user_id=actor_id,
                    )
                )
        else:
            link.granted = want
    db.session.commit()


def grant_user_defaults(user: User, actor_id=None):
    grant_permissions(user, USER_DEFAULTS, actor_id=actor_id)
