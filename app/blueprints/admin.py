from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from werkzeug.security import generate_password_hash

from ..auth import (
    grant_user_defaults,
    permission_required,
    record_audit,
    set_user_permissions,
)
from ..extensions import db
from ..models import AuditEvent, Permission, User, UserPermission
from ..permissions import USER_DEFAULTS, Role, grouped_permissions

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _granted_codes(user: User) -> set[str]:
    if user.is_admin():
        from ..permissions import ALL_CODES

        return set(ALL_CODES)
    rows = (
        db.session.query(Permission.code)
        .join(UserPermission, UserPermission.permission_id == Permission.id)
        .filter(UserPermission.user_id == user.id, UserPermission.granted.is_(True))
        .all()
    )
    return {r[0] for r in rows}


@bp.route("/users")
@permission_required("USERS_VIEW")
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("admin/users.html", users=all_users, active_tab="users")


@bp.route("/users/new", methods=["GET"])
@permission_required("USERS_CREATE")
def new_user():
    return render_template(
        "admin/user_form.html",
        mode="create",
        user=None,
        groups=grouped_permissions(),
        granted=set(USER_DEFAULTS),
        roles=Role.ALL,
        active_tab="users",
    )


@bp.route("/users", methods=["POST"])
@permission_required("USERS_CREATE")
def create_user():
    username = (request.form.get("username") or "").strip()
    if not username or User.query.filter_by(username=username).first():
        flash("Username is required and must be unique.", "error")
        return redirect(url_for("admin.new_user"))
    password = request.form.get("password") or ""
    if len(password) < 8:
        flash("Temporary password must be at least 8 characters.", "error")
        return redirect(url_for("admin.new_user"))
    role = request.form.get("role") if request.form.get("role") in Role.ALL else Role.USER
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        full_name=(request.form.get("full_name") or "").strip() or None,
        email=(request.form.get("email") or "").strip() or None,
        role=role,
        active=bool(request.form.get("active")),
        must_change_password=True,
        created_by_user_id=current_user.id,
    )
    db.session.add(user)
    db.session.flush()
    if role == Role.USER:
        selected = [c for c in _all_codes() if request.form.get(f"perm_{c}")]
        set_user_permissions(user, selected, actor_id=current_user.id)
    record_audit("USER_CREATED", module="Administration", entity_type="User", entity_id=user.id, detail=f"role={role}")
    db.session.commit()
    flash(f"User {username} created.", "success")
    return redirect(url_for("admin.users"))


def _all_codes():
    from ..permissions import ALL_CODES

    return ALL_CODES


@bp.route("/users/<int:user_id>/edit", methods=["GET"])
@permission_required("USERS_EDIT")
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    return render_template(
        "admin/user_form.html",
        mode="edit",
        user=user,
        groups=grouped_permissions(),
        granted=_granted_codes(user),
        roles=Role.ALL,
        active_tab="users",
    )


@bp.route("/users/<int:user_id>/edit", methods=["POST"])
@permission_required("USERS_EDIT")
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    user.full_name = (request.form.get("full_name") or "").strip() or None
    user.email = (request.form.get("email") or "").strip() or None
    new_role = request.form.get("role")
    if new_role in Role.ALL:
        user.role = new_role
    user.updated_by_user_id = current_user.id
    record_audit("USER_UPDATED", module="Administration", entity_type="User", entity_id=user.id)
    db.session.commit()
    flash(f"User {user.username} updated.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@permission_required("USERS_DISABLE")
def toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("admin.users"))
    user.active = not user.active
    user.updated_by_user_id = current_user.id
    record_audit(
        "USER_ENABLED" if user.active else "USER_DISABLED",
        module="Administration", entity_type="User", entity_id=user.id,
    )
    db.session.commit()
    flash(f"User {user.username} {'enabled' if user.active else 'disabled'}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@permission_required("USERS_EDIT")
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new = request.form.get("new_password") or ""
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("admin.edit_user", user_id=user.id))
    user.password_hash = generate_password_hash(new)
    user.must_change_password = True
    user.updated_by_user_id = current_user.id
    record_audit("PASSWORD_RESET", module="Administration", entity_type="User", entity_id=user.id)
    db.session.commit()
    flash(f"Password reset for {user.username}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@bp.route("/users/<int:user_id>/permissions", methods=["POST"])
@permission_required("PERMISSIONS_ASSIGN")
def assign_permissions(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin():
        flash("ADMIN users automatically have all permissions.", "success")
        return redirect(url_for("admin.edit_user", user_id=user.id))
    selected = [c for c in _all_codes() if request.form.get(f"perm_{c}")]
    set_user_permissions(user, selected, actor_id=current_user.id)
    record_audit("PERMISSIONS_UPDATED", module="Administration", entity_type="User", entity_id=user.id, detail=f"{len(selected)} granted")
    db.session.commit()
    flash("Permissions updated.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@bp.route("/permissions")
@permission_required("PERMISSIONS_ASSIGN")
def permissions_tab():
    users_list = User.query.order_by(User.username).all()
    selected_id = request.args.get("user_id", type=int)
    user = User.query.get(selected_id) if selected_id else None
    return render_template(
        "admin/permissions.html",
        users=users_list,
        user=user,
        groups=grouped_permissions(),
        granted=_granted_codes(user) if user else set(),
        active_tab="permissions",
    )


@bp.route("/audit")
@permission_required("AUDIT_VIEW")
def audit():
    event_type = (request.args.get("event_type") or "").strip()
    q = AuditEvent.query
    if event_type:
        q = q.filter(AuditEvent.event_type == event_type)
    events = q.order_by(AuditEvent.created_at.desc()).limit(500).all()
    return render_template("admin/audit.html", events=events, active_tab="audit", event_type=event_type)
