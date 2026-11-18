# routes/admin_ipv.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from werkzeug.exceptions import abort
from db import get_conn_cursor
from datetime import datetime

admin_ipv_bp = Blueprint("admin_ipv", __name__,static_folder="static")

# ---------- Helper validators ----------
def parse_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default

# ---------- Templates CRUD ----------
@admin_ipv_bp.route("/admin/ipv/templates")
def list_templates():
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("SELECT * FROM ipv_ai_templates ORDER BY model, stage, subtask")
            rows = cur.fetchall()
        return render_template("admin_ipv_templates_list.html", templates=rows)
    except Exception as e:
        current_app.logger.exception("Failed to list templates: %s", e)
        flash("Failed to load templates", "danger")
        return render_template("admin_ipv_templates_list.html", templates=[]), 500

@admin_ipv_bp.route("/admin/ipv/templates/new", methods=["GET", "POST"])
def create_template():
    if request.method == "POST":
        model = request.form.get("model", "").strip()
        stage = request.form.get("stage", "").strip()
        subtask = parse_int(request.form.get("subtask"))
        required_orings = parse_int(request.form.get("required_orings"))
        description = request.form.get("description", "").strip()
        sample_image = request.form.get("sample_image", "").strip()
        class_to_find = request.form.get("class_to_find", "").strip()
        class_index = parse_int(request.form.get("class_index"))
        model_yolo = request.form.get("model_yolo", "").strip()

        # Basic validation
        if not model or subtask is None:
            flash("Model and Subtask are required", "warning")
            return render_template("admin_ipv_template_edit.html", template={}, mode="create")

        try:
            with get_conn_cursor(dict_cursor=True) as (conn, cur):
                cur.execute("""
                    INSERT INTO ipv_ai_templates
                      (model, subtask, required_orings, stage, description, sample_image, class_to_find, class_index, model_yolo)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (model, subtask, required_orings, stage, description, sample_image, class_to_find, class_index, model_yolo))
                new_id = cur.fetchone()["id"]
            flash(f"Template created (id={new_id})", "success")
            return redirect(url_for("admin_ipv.list_templates"))
        except Exception as e:
            current_app.logger.exception("Failed to create template: %s", e)
            flash("Failed to create template. Maybe already exists (unique constraint).", "danger")
            return render_template("admin_ipv_template_edit.html", template=request.form, mode="create"), 500

    # GET
    return render_template("admin_ipv_template_edit.html", template={}, mode="create")


@admin_ipv_bp.route("/admin/ipv/templates/<int:tpl_id>/edit", methods=["GET", "POST"])
def edit_template(tpl_id):
    if request.method == "POST":
        # collect form
        model = request.form.get("model", "").strip()
        stage = request.form.get("stage", "").strip()
        subtask = parse_int(request.form.get("subtask"))
        required_orings = parse_int(request.form.get("required_orings"))
        description = request.form.get("description", "").strip()
        sample_image = request.form.get("sample_image", "").strip()
        class_to_find = request.form.get("class_to_find", "").strip()
        class_index = parse_int(request.form.get("class_index"))
        model_yolo = request.form.get("model_yolo", "").strip()

        if not model or subtask is None:
            flash("Model and Subtask required", "warning")
            return redirect(url_for("admin_ipv.edit_template", tpl_id=tpl_id))

        try:
            with get_conn_cursor(dict_cursor=True) as (conn, cur):
                cur.execute("""
                    UPDATE ipv_ai_templates
                    SET model=%s, subtask=%s, required_orings=%s, stage=%s, description=%s,
                        sample_image=%s, class_to_find=%s, class_index=%s, model_yolo=%s, 
                        -- don't modify id
                        id = id
                    WHERE id = %s
                """, (model, subtask, required_orings, stage, description, sample_image, class_to_find, class_index, model_yolo, tpl_id))
            flash("Template updated", "success")
            return redirect(url_for("admin_ipv.list_templates"))
        except Exception as e:
            current_app.logger.exception("Failed to update template: %s", e)
            flash("Failed to update template", "danger")
            return redirect(url_for("admin_ipv.edit_template", tpl_id=tpl_id)), 500

    # GET: load
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("SELECT * FROM ipv_ai_templates WHERE id = %s LIMIT 1", (tpl_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
        return render_template("admin_ipv_template_edit.html", template=row, mode="edit")
    except Exception as e:
        current_app.logger.exception("Failed to load template: %s", e)
        flash("Failed to load template", "danger")
        return redirect(url_for("admin_ipv.list_templates")), 500

@admin_ipv_bp.route("/admin/ipv/templates/<int:tpl_id>/delete", methods=["POST"])
def delete_template(tpl_id):
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("DELETE FROM ipv_ai_templates WHERE id = %s", (tpl_id,))
        flash("Template deleted", "success")
    except Exception as e:
        current_app.logger.exception("Failed to delete template: %s", e)
        flash("Failed to delete template", "danger")
    return redirect(url_for("admin_ipv.list_templates"))


# ---------- Logs CRUD / view ----------
@admin_ipv_bp.route("/admin/ipv/logs")
def list_logs():
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("""
                SELECT l.*, t.description AS template_description
                FROM ipv_ai_logs l
                LEFT JOIN ipv_ai_templates t
                  ON t.model = l.model AND t.stage = l.stage AND t.subtask = l.subtask
                ORDER BY l.created_at DESC
                LIMIT 1000
            """)
            rows = cur.fetchall()
        return render_template("admin_ipv_logs_list.html", logs=rows)
    except Exception as e:
        current_app.logger.exception("Failed to list logs: %s", e)
        flash("Failed to load logs", "danger")
        return render_template("admin_ipv_logs_list.html", logs=[]), 500

@admin_ipv_bp.route("/admin/ipv/logs/<int:log_id>")
def view_log(log_id):
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("SELECT * FROM ipv_ai_logs WHERE id = %s LIMIT 1", (log_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
        return render_template("admin_ipv_log_view.html", log=row)
    except Exception as e:
        current_app.logger.exception("Failed to load log: %s", e)
        flash("Failed to load log", "danger")
        return redirect(url_for("admin_ipv.list_logs")), 500

@admin_ipv_bp.route("/admin/ipv/logs/<int:log_id>/edit", methods=["GET", "POST"])
def edit_log(log_id):
    if request.method == "POST":
        # fields allowed to edit
        status = request.form.get("status")
        current_quantity = parse_int(request.form.get("current_quantity"))
        required_quantity = parse_int(request.form.get("required_quantity"))
        predicted_image_path = request.form.get("predicted_image_path", "")
        image_path = request.form.get("image_path", "")

        try:
            with get_conn_cursor(dict_cursor=True) as (conn, cur):
                cur.execute("""
                    UPDATE ipv_ai_logs
                    SET status=%s, current_quantity=%s, required_quantity=%s, predicted_image_path=%s, image_path=%s
                    WHERE id = %s
                """, (status, current_quantity, required_quantity, predicted_image_path, image_path, log_id))
            flash("Log updated", "success")
            return redirect(url_for("admin_ipv.view_log", log_id=log_id))
        except Exception as e:
            current_app.logger.exception("Failed to update log: %s", e)
            flash("Failed to update log", "danger")
            return redirect(url_for("admin_ipv.edit_log", log_id=log_id)), 500

    # GET
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("SELECT * FROM ipv_ai_logs WHERE id = %s LIMIT 1", (log_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
        return render_template("admin_ipv_log_edit.html", log=row)
    except Exception as e:
        current_app.logger.exception("Failed to load log: %s", e)
        flash("Failed to load log", "danger")
        return redirect(url_for("admin_ipv.list_logs")), 500

@admin_ipv_bp.route("/admin/ipv/logs/<int:log_id>/delete", methods=["POST"])
def delete_log(log_id):
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("DELETE FROM ipv_ai_logs WHERE id = %s", (log_id,))
        flash("Log deleted", "success")
    except Exception as e:
        current_app.logger.exception("Failed to delete log: %s", e)
        flash("Failed to delete log", "danger")
    return redirect(url_for("admin_ipv.list_logs"))

# ---------- small JSON helpers (optional) ----------
@admin_ipv_bp.route("/admin/ipv/templates/json")
def templates_json():
    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute("SELECT * FROM ipv_ai_templates ORDER BY model, stage, subtask")
            rows = cur.fetchall()
        return jsonify(rows)
    except Exception as e:
        current_app.logger.exception("templates_json error: %s", e)
        return jsonify([]), 500
