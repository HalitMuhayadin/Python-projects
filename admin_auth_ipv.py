# routes/admin_auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_conn_cursor

admin_auth_bp = Blueprint("admin_auth", __name__, template_folder="../templates")

# -------- Helpers --------
def is_logged_in():
    return session.get("admin_logged_in") is True

def login_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in as admin to access this page", "warning")
            return redirect(url_for("admin_auth.login_admin"))
        return view_func(*args, **kwargs)
    return wrapper


# -------- Registration --------
@admin_auth_bp.route("/admin/register", methods=["GET", "POST"])
def register_admin():
    """Register a new IPV admin."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not password:
            flash("Username and password required", "danger")
            return render_template("admin_register.html")

        if password != confirm:
            flash("Passwords do not match", "warning")
            return render_template("admin_register.html")

        pw_hash = generate_password_hash(password)

        try:
            with get_conn_cursor(dict_cursor=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO ipv_admins (username, full_name, email, password_hash)
                    VALUES (%s, %s, %s, %s)
                """, (username, full_name, email, pw_hash))
            flash("Admin registered successfully", "success")
            return redirect(url_for("admin_auth.login_admin"))
        except Exception as e:
            current_app.logger.exception("Failed to register admin: %s", e)
            flash("Username already exists or database error", "danger")

    return render_template("admin_register.html")


# -------- Login --------
@admin_auth_bp.route("/admin/login", methods=["GET", "POST"])
def login_admin():
    """Admin login page."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            with get_conn_cursor(dict_cursor=True) as (conn, cur):
                cur.execute("SELECT * FROM ipv_admins WHERE username = %s AND is_active = TRUE", (username,))
                admin = cur.fetchone()
        except Exception as e:
            current_app.logger.exception("Login DB error: %s", e)
            flash("Database error", "danger")
            return render_template("admin_login.html")

        if not admin:
            flash("Invalid username or account inactive", "danger")
            return render_template("admin_login.html")

        if not check_password_hash(admin["password_hash"], password):
            flash("Incorrect password", "danger")
            return render_template("admin_login.html")

        # success
        session["admin_logged_in"] = True
        session["admin_id"] = admin["id"]
        session["admin_username"] = admin["username"]
        session["admin_name"] = admin.get("full_name") or admin["username"]
        flash(f"Welcome, {session['admin_name']}!", "success")
        return redirect(url_for("admin_auth.admin_dashboard"))

    return render_template("admin_login.html")


# -------- Dashboard (after login) --------
@admin_auth_bp.route("/admin/ipv/dashboard")
@login_required
def admin_dashboard():
    """Base page for logged-in admin."""
    return render_template("admin_dashboard.html", admin=session.get("admin_name"))


# -------- Logout --------
@admin_auth_bp.route("/admin/logout")
def logout_admin():
    session.clear()
    flash("Logged out successfully", "info")
    return redirect(url_for("admin_auth.login_admin"))
