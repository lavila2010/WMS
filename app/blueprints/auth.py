from __future__ import annotations

from datetime import datetime, timedelta

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import record_audit
from ..extensions import db
from ..models import User

bp = Blueprint("auth", __name__)

MAX_FAILED = 5
LOCK_MINUTES = 15


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()

        if user is None:
            record_audit("LOGIN_FAILED", module="Auth", detail=f"Unknown username '{username}'", commit=True)
            flash("Invalid username or password.", "error")
            return render_template("auth/login.html"), 401

        if user.locked_until and user.locked_until > datetime.utcnow():
            record_audit("LOGIN_FAILED", module="Auth", entity_type="User", entity_id=user.id, detail="Account locked", commit=True)
            flash("Account temporarily locked. Try again later.", "error")
            return render_template("auth/login.html"), 401

        if not user.active:
            record_audit("LOGIN_FAILED", module="Auth", entity_type="User", entity_id=user.id, detail="Inactive account", commit=True)
            flash("This account is disabled.", "error")
            return render_template("auth/login.html"), 401

        if not check_password_hash(user.password_hash, password):
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= MAX_FAILED:
                user.locked_until = datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)
            record_audit("LOGIN_FAILED", module="Auth", entity_type="User", entity_id=user.id, detail="Bad password")
            db.session.commit()
            flash("Invalid username or password.", "error")
            return render_template("auth/login.html"), 401

        # Success
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.utcnow()
        login_user(user)
        record_audit("LOGIN_SUCCESS", module="Auth", entity_type="User", entity_id=user.id)
        db.session.commit()

        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        nxt = request.args.get("next")
        if nxt and nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    record_audit("LOGOUT", module="Auth", entity_type="User", entity_id=current_user.id, commit=True)
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not current_user.must_change_password and not check_password_hash(current_user.password_hash, current):
            flash("Current password is incorrect.", "error")
            return render_template("auth/change_password.html")
        if len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
            return render_template("auth/change_password.html")
        if new != confirm:
            flash("New passwords do not match.", "error")
            return render_template("auth/change_password.html")
        current_user.password_hash = generate_password_hash(new)
        current_user.must_change_password = False
        record_audit("PASSWORD_CHANGED", module="Auth", entity_type="User", entity_id=current_user.id)
        db.session.commit()
        flash("Password updated.", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("auth/change_password.html")


@bp.route("/profile")
@login_required
def profile():
    return render_template("auth/profile.html", user=current_user)
