from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from ..auth import permission_required, record_audit, set_user_permissions
from ..constants import OperationType, Role
from ..extensions import db
from ..models import (
    AuditEvent,
    Client,
    Division,
    DivisionWarehouse,
    Permission,
    User,
    UserClient,
    UserPermission,
    Warehouse,
)
from ..permissions import USER_DEFAULTS, grouped_permissions
from ..services.masters import (
    MasterError,
    create_client,
    create_division,
    create_warehouse,
    map_division_warehouse,
    update_client,
)
from ..services.tenant import accessible_clients, require_client_access, user_can_access_client

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


def _all_codes():
    from ..permissions import ALL_CODES

    return ALL_CODES


def _visible_client(client_id: int) -> Client:
    client = Client.query.get_or_404(client_id)
    require_client_access(current_user, client.id)
    return client


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
        module="Administration",
        entity_type="User",
        entity_id=user.id,
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
    record_audit(
        "PERMISSIONS_UPDATED",
        module="Administration",
        entity_type="User",
        entity_id=user.id,
        detail=f"{len(selected)} granted",
    )
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


@bp.route("/client-access", methods=["GET", "POST"])
@permission_required("PERMISSIONS_ASSIGN")
def client_access():
    users_list = User.query.order_by(User.username).all()
    clients = accessible_clients(current_user)
    selected_id = request.values.get("user_id", type=int)
    user = User.query.get(selected_id) if selected_id else None
    if request.method == "POST" and user and not user.is_admin():
        wanted = []
        for client in clients:
            if request.form.get(f"client_{client.id}"):
                wanted.append(client.id)
        UserClient.query.filter_by(user_id=user.id).delete()
        for cid in wanted:
            db.session.add(UserClient(user_id=user.id, client_id=cid, created_by_user_id=current_user.id))
        record_audit(
            "CLIENT_ACCESS_UPDATED",
            module="Administration",
            entity_type="User",
            entity_id=user.id,
            detail=f"{len(wanted)} clients",
        )
        db.session.commit()
        flash("Client access updated.", "success")
        return redirect(url_for("admin.client_access", user_id=user.id))
    granted_ids = {link.client_id for link in user.client_links} if user else set()
    return render_template(
        "admin/client_access.html",
        users=users_list,
        user=user,
        clients=clients,
        granted_ids=granted_ids,
        active_tab="client_access",
    )


@bp.route("/audit")
@permission_required("AUDIT_VIEW")
def audit():
    event_type = (request.args.get("event_type") or "").strip()
    q = AuditEvent.query
    if event_type:
        q = q.filter(AuditEvent.event_type == event_type)
    if not current_user.is_admin():
        ids = [c.id for c in accessible_clients(current_user)]
        q = q.filter((AuditEvent.client_id.in_(ids or [-1])) | (AuditEvent.client_id.is_(None)))
    events = q.order_by(AuditEvent.created_at.desc()).limit(500).all()
    return render_template("admin/audit.html", events=events, active_tab="audit", event_type=event_type)


@bp.route("/clients")
@permission_required("CLIENTS_VIEW")
def clients():
    items = accessible_clients(current_user)
    return render_template("admin/clients.html", clients=items, active_tab="clients")


@bp.route("/clients/new", methods=["GET", "POST"])
@permission_required("CLIENTS_CREATE")
def new_client():
    if request.method == "POST":
        try:
            client = create_client(
                request.form.get("name"),
                request.form.get("initials"),
                actor_id=current_user.id,
            )
            record_audit(
                "CLIENT_CREATED",
                module="Administration",
                entity_type="Client",
                entity_id=client.id,
                client_id=client.id,
                detail=client.client_code,
            )
            db.session.commit()
            flash(f"Client {client.client_code} created.", "success")
            return redirect(url_for("admin.clients"))
        except MasterError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("admin/client_form.html", active_tab="clients")


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@permission_required("CLIENTS_EDIT")
def edit_client(client_id):
    client = _visible_client(client_id)
    if request.method == "POST":
        try:
            update_client(
                client,
                name=request.form.get("name"),
                active=bool(request.form.get("active")),
                actor_id=current_user.id,
            )
            record_audit(
                "CLIENT_UPDATED",
                module="Administration",
                entity_type="Client",
                entity_id=client.id,
                client_id=client.id,
            )
            db.session.commit()
            flash("Client updated. Client code is immutable.", "success")
            return redirect(url_for("admin.clients"))
        except MasterError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    return render_template("admin/client_form.html", client=client, active_tab="clients")


@bp.route("/divisions")
@permission_required("DIVISIONS_VIEW")
def divisions():
    q = Division.query.join(Client)
    if not current_user.is_admin():
        ids = [c.id for c in accessible_clients(current_user)]
        q = q.filter(Division.client_id.in_(ids or [-1]))
    items = q.order_by(Client.client_code, Division.code).all()
    return render_template(
        "admin/divisions.html",
        divisions=items,
        clients=accessible_clients(current_user),
        operation_types=OperationType.ALL,
        active_tab="divisions",
    )


@bp.route("/divisions", methods=["POST"])
@permission_required("DIVISIONS_CREATE")
def create_division_view():
    client = _visible_client(int(request.form.get("client_id") or 0))
    try:
        division = create_division(
            client,
            request.form.get("name"),
            request.form.get("operation_type"),
            actor_id=current_user.id,
        )
        record_audit(
            "DIVISION_CREATED",
            module="Administration",
            entity_type="Division",
            entity_id=division.id,
            client_id=client.id,
            detail=division.code,
        )
        db.session.commit()
        flash(f"Division {division.code} created.", "success")
    except (MasterError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.divisions"))


@bp.route("/warehouses")
@permission_required("WAREHOUSES_VIEW")
def warehouses():
    q = Warehouse.query.join(Client)
    if not current_user.is_admin():
        ids = [c.id for c in accessible_clients(current_user)]
        q = q.filter(Warehouse.client_id.in_(ids or [-1]))
    items = q.order_by(Client.client_code, Warehouse.warehouse_symbol).all()
    return render_template(
        "admin/warehouses.html",
        warehouses=items,
        clients=accessible_clients(current_user),
        active_tab="warehouses",
    )


@bp.route("/warehouses", methods=["POST"])
@permission_required("WAREHOUSES_CREATE")
def create_warehouse_view():
    client = _visible_client(int(request.form.get("client_id") or 0))
    try:
        warehouse = create_warehouse(
            client,
            request.form.get("warehouse_symbol"),
            request.form.get("name"),
            actor_id=current_user.id,
        )
        record_audit(
            "WAREHOUSE_CREATED",
            module="Administration",
            entity_type="Warehouse",
            entity_id=warehouse.id,
            client_id=client.id,
            detail=warehouse.warehouse_code,
        )
        db.session.commit()
        flash(f"Warehouse {warehouse.warehouse_code} created.", "success")
    except (MasterError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.warehouses"))


@bp.route("/mappings")
@permission_required("DIVISIONS_VIEW")
def mappings():
    q = DivisionWarehouse.query
    if not current_user.is_admin():
        ids = [c.id for c in accessible_clients(current_user)]
        q = q.filter(DivisionWarehouse.client_id.in_(ids or [-1]))
    items = q.order_by(DivisionWarehouse.id.desc()).all()
    clients = accessible_clients(current_user)
    return render_template(
        "admin/mappings.html",
        mappings=items,
        clients=clients,
        divisions=Division.query.filter(Division.client_id.in_([c.id for c in clients] or [-1])).all(),
        warehouses=Warehouse.query.filter(Warehouse.client_id.in_([c.id for c in clients] or [-1])).all(),
        active_tab="mappings",
    )


@bp.route("/mappings", methods=["POST"])
@permission_required("DIVISIONS_EDIT")
def create_mapping_view():
    division = Division.query.get_or_404(int(request.form.get("division_id") or 0))
    warehouse = Warehouse.query.get_or_404(int(request.form.get("warehouse_id") or 0))
    require_client_access(current_user, division.client_id)
    require_client_access(current_user, warehouse.client_id)
    try:
        mapping = map_division_warehouse(division, warehouse)
        record_audit(
            "MAPPING_CREATED",
            module="Administration",
            entity_type="DivisionWarehouse",
            entity_id=mapping.id,
            client_id=mapping.client_id,
            detail=f"{division.code} ↔ {warehouse.warehouse_code}",
        )
        db.session.commit()
        flash("Mapping saved.", "success")
    except MasterError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.mappings"))
