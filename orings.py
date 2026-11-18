# routes/orings.py
import os
import zipfile
import base64
import shutil
from io import BytesIO, StringIO
from datetime import datetime
from flask import (
    Blueprint, render_template, request, session, redirect, url_for,
    send_file, jsonify, send_from_directory, current_app
)
from azure.storage.blob import BlobClient, ContainerClient
from dateutil.parser import parse
from collections import defaultdict
import statistics
from fpdf import FPDF
from ultralytics import YOLO
from PIL import Image
import tempfile
import requests
import logging
import csv
import numpy as np
from myemail import send_email_via_functionapp
# DB helpers you already designed (keep these available in project)
from oring_db_helpers import (
    get_orings_data_from_db as get_orings_data,
    read_logs_from_db as read_logs,
    get_required_quantity_from_db as get_required_quantity,
    get_all_required_subtasks_from_db as get_all_required_subtasks,
    insert_log_db,
    insert_predicted_image_record,is_stage_fully_completed
)

logger = logging.getLogger(__name__)
orings_bp = Blueprint('orings', __name__)

# Azure blob containers (read from env)
PRED_CONTAINER = os.environ.get('AZURE_BLOB_CONTAINER_URL_PREDICT_IMAGE_IPV_AI', '').rstrip('/')
PRED_SAS = os.environ.get('AZURE_BLOB_SAS_PREDICT_IMAGE_IPV_AI', '')
ORIG_CONTAINER = os.environ.get('AZURE_BLOB_CONTAINER_URL_ORGINAL_IMAGE_IPV_AI', '').rstrip('/')
ORIG_SAS = os.environ.get('AZURE_BLOB_SAS_ORGINAL_IMAGE_IPV_AI', '')

# Small util
def safe_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default

# For YOLO model loading we still expect the .pt files local (models/<name>).
# If you store model metadata in DB we can look up the model file path from there.
# ---- Live detection YOLO cache ----
LIVE_YOLO_MODELS = {}

def get_live_model(model_name):
    if model_name not in LIVE_YOLO_MODELS:
        LIVE_YOLO_MODELS[model_name] = load_yolo_model(model_name)
    return LIVE_YOLO_MODELS[model_name]


def load_yolo_model(model_filename):
    model_path = os.path.join('models', model_filename)
    if not os.path.exists(model_path):
        logger.error("YOLO model file not found: %s", model_path)
        raise FileNotFoundError(model_path)
    return YOLO(model_path)

# analyze_logs unchanged logic
def analyze_logs(logs):
    subtask_data = defaultdict(lambda: {"initial_time": None, "end_time": None, "delay": None})
    grouped_logs = defaultdict(list)
    for log in logs:
        grouped_logs[(log["stage"], log["subtask"])].append(log)

    for (stage, subtask), entries in grouped_logs.items():
        entries.sort(key=lambda x: parse(x["date_time"]))
        initial_time, end_time = None, None
        for entry in entries:
            timestamp = parse(entry["date_time"])
            if entry["status"].lower() == "fail" and initial_time is None:
                initial_time = timestamp
            elif entry["status"].lower() == "pass" and initial_time is not None:
                end_time = timestamp
                break

        if initial_time and not end_time:
            for next_entries in grouped_logs.values():
                for next_entry in next_entries:
                    try:
                        next_time = parse(next_entry["date_time"])
                    except Exception:
                        continue
                    if next_time > initial_time:
                        end_time = next_time
                        break
                if end_time:
                    break

        if initial_time and end_time:
            delay_hours = round((end_time - initial_time).total_seconds() / 3600, 2)
            subtask_data[(stage, subtask)] = {
                "initial_time": initial_time,
                "end_time": end_time,
                "delay": delay_hours
            }

    return subtask_data

# ----------------------------- helpers for blob upload/download -----------------------------
def upload_bytes_to_blob(container_url, sas_token, blob_name, data_bytes, content_type='application/octet-stream'):
    """
    Upload raw bytes to container_url/blob_name?{sas_token}
    Returns the blob URL without SAS (public path).
    """
    if not container_url or not sas_token:
        raise ValueError("Container URL or SAS token missing")
    full_url = f"{container_url}/{blob_name}?{sas_token}"
    blob_client = BlobClient.from_blob_url(full_url)
    blob_client.upload_blob(data_bytes, overwrite=True, length=len(data_bytes), content_settings=None)
    return f"{container_url}/{blob_name}"

def download_blob_bytes(container_url, sas_token, blob_name):
    full_url = f"{container_url}/{blob_name}?{sas_token}"
    blob_client = BlobClient.from_blob_url(full_url)
    data = blob_client.download_blob().readall()
    return data

# ----------------------------- Routes -----------------------------

@orings_bp.route('/oring-internal-logs')
def summary():
    logs = read_logs()
    orings_data = get_orings_data()
    summary_data = {}
    stage_analysis = defaultdict(lambda: defaultdict(lambda: {
            "total_orders": set(),
            "ok_orders": set(),
            "nok_orders": set(),
            "times": []
    }))
    subtask_bottlenecks = defaultdict(lambda: {"failed_count": 0, "total_count": 0, "delays": []})
    
    logs.sort(key=lambda x: parse(x['date_time']), reverse=True)
    
    for log in logs:
        order_number = log['order_number']
        model = log['model']
        stage = log['stage']
        employee_id = log['employee_id']
        subtask = log['subtask']
        status = log['status']
        date_time = parse(log['date_time'])
    
        if order_number not in summary_data:
            summary_data[order_number] = {
                'order_number': order_number,
                'model': model,
                'stage': stage,
                'employees': set(),
                'completed_subtasks': set(),
                'failed_subtasks': set(),
                'subtask_statuses':{},
                'required_subtasks': {sub['subtask'] for sub in orings_data.get(model, {}).get(stage, [])},
                'start_time': date_time,
                'end_time': date_time,
                'overall_status': 'OK'
            }
    
        summary_data[order_number]['employees'].add(employee_id)
    
        if status.lower() == "pass":
            summary_data[order_number]['completed_subtasks'].add(subtask)
            summary_data[order_number]['subtask_statuses'][str(subtask)]='COMPLETED'
        elif status.lower()=="fail":
            summary_data[order_number]['failed_subtasks'].add(subtask)
            summary_data[order_number]['subtask_statuses'][str(subtask)]='DETECTION_FAILED'
            subtask_bottlenecks[subtask]["failed_count"] += 1
    
        summary_data[order_number]['start_time'] = min(summary_data[order_number]['start_time'], date_time)
        summary_data[order_number]['end_time'] = max(summary_data[order_number]['end_time'], date_time)
    
        stage_analysis[model][stage]["total_orders"].add(order_number)
        duration = (summary_data[order_number]['end_time'] - summary_data[order_number]['start_time']).total_seconds()
        stage_analysis[model][stage]["times"].append(duration)
    
        subtask_bottlenecks[subtask]["total_count"] += 1
        subtask_bottlenecks[subtask]["delays"].append(duration)
    
    for order in summary_data.values():
            order['completed_subtasks'] = sorted(int(subtask) for subtask in order['completed_subtasks'])
            order['required_subtasks'] = sorted(int(subtask) for subtask in order['required_subtasks'])
    
            if set(order['completed_subtasks']) == set(order['required_subtasks']):
                order['overall_status'] = 'OK'
                stage_analysis[order['model']][order['stage']]["ok_orders"].add(order["order_number"])
            else:
                order['overall_status'] = 'NOK'
                stage_analysis[order['model']][order['stage']]["nok_orders"].add(order["order_number"])
    
    sorted_summary = sorted(summary_data.values(), key=lambda x: x['end_time'], reverse=True)
    
    for model, stages in stage_analysis.items():
            for stage, data in stages.items():
                data["total_orders"] = len(data["total_orders"])
                data["ok_orders"] = len(data["ok_orders"])
                data["nok_orders"] = len(data["nok_orders"])
                data["avg_time"] = round(statistics.mean(data["times"]) / 60, 2) if data["times"] else 0
    
    for subtask, data in subtask_bottlenecks.items():
            data["avg_delay"] = round(statistics.mean(data["delays"]) / 60, 2) if data["delays"] else 0
    
    return render_template(
            'summary_log.html',
            summary_data=sorted_summary,
            stage_analysis=stage_analysis,
            logs=logs
    )

@orings_bp.route('/ipv', methods=['GET', 'POST'])
def oring_sml_index():
    #oring
    orings_data = get_orings_data()
    return render_template('index-oring.html', orings_data=orings_data)

@orings_bp.route('/nxt_task/<int:subtask>/<string:order_number>/<string:employee_id>/<string:model_name>/<stage>', methods=['GET'])
def nxt_task(subtask, order_number, employee_id, model_name, stage):
    # Get O-ring data for the model and stage
    orings_data = get_orings_data()

    # Read the logs and filter for this order, model, and stage
    logs = read_logs()

    filtered_logs = [log for log in logs if log['order_number'] == order_number 
                     and log['model'] == model_name and log['stage'] == stage]

    # Get completed subtasks from logs and ensure they are integers
    completed_subtasks = {int(log['subtask']) for log in filtered_logs if log['status'].lower() == 'pass'}

    # Remove completed subtasks directly from orings_data
    if model_name in orings_data and stage in orings_data[model_name]:
        orings_data[model_name][stage] = [
            task for task in orings_data[model_name][stage] if int(task['subtask']) not in completed_subtasks
        ]

    return render_template('success.html', 
                           subtask=subtask, order_number=order_number, 
                           employee_id=employee_id, model_name=model_name, 
                           orings_data=orings_data, stage=stage)

@orings_bp.route('/task_complete/<string:order_number>/<string:employee_id>/<string:model_name>', methods=['GET'])
def task_complete(order_number, employee_id, model_name):
    # Render a template where the user can generate a PDF or see completion status
    return render_template('task_complete.html', order_number=order_number, employee_id=employee_id, model_name=model_name)

@orings_bp.route('/submit_task_oring_sml', methods=['POST'])
def submit_task():
    """
    Main submit path:
    - Accepts base64 or multipart-file
    - Runs YOLO on in-memory image
    - Uploads original & annotated image to Azure Blob
    - Logs to DB
    """
    orings_data = get_orings_data()

    # Form fields
    order_number = request.form.get('order_number', '').upper().replace(" ", "")
    employee_id = str(request.form.get('employee_id', ''))
    model_name = request.form.get('model_name')
    subtask = safe_int(request.form.get('subtask'))
    stage = request.form.get('stage')
    current_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')

    if not any(s in order_number.upper() for s in ["S1", "S2", "S3"]):
        order_number = f"{order_number}-{stage}"

    # Acquire image bytes (either base64 capture or uploaded file)
    image_bytes = None
    image_field = request.form.get('image_data')
    if image_field:
        # base64 string expected
        if 'base64,' in image_field:
            image_field = image_field.split('base64,')[1]
        image_bytes = base64.b64decode(image_field)
    else:
        # file upload
        if 'image' not in request.files:
            return "No image data received", 400
        file = request.files['image']
        if file.filename == '':
            return "No image uploaded", 400
        image_bytes = file.read()

    # open PIL image from bytes
    try:
        pil_img = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        logger.exception("Failed to open uploaded image: %s", e)
        return "Invalid image", 400

    # Get template data
    if model_name in orings_data and stage in orings_data[model_name]:
        subtask_data = next((item for item in orings_data[model_name][stage] if item['subtask'] == subtask), None)
        if not subtask_data:
            return "Invalid subtask", 400
    else:
        return "Invalid model or stage", 400

    find_class = subtask_data['class']
    oring_class_index = subtask_data['class_index']
    yolo_model_name = subtask_data['model_yolo']

    # Load YOLO model (local .pt expected)
    try:
        model_oring = load_yolo_model(yolo_model_name)
    except Exception as e:
        logger.exception("Failed to load YOLO model: %s", e)
        return render_template('download_models.html')

    # Convert PIL -> numpy array for ultralytics
    img_arr = np.array(pil_img)

    # Run prediction (in-memory)
    try:
        # conf threshold and other params can be customized
        results = model_oring.predict(source=img_arr, conf=0.65, save=False, verbose=False)
    except Exception as e:
        logger.exception("YOLO predict failed: %s", e)
        return "Prediction error", 500

    # Build blob names
    orig_blob_name = f"ORING-{model_name}-{stage}/{order_number}_{model_name}_{stage}_{subtask}_{employee_id}_{current_time}.jpg"
    pred_blob_name = f"ORING-PRED-{model_name}-{stage}/{order_number}_{model_name}_{stage}_{subtask}_{employee_id}_{current_time}_pred.jpg"

    # Upload original image bytes to ORIG container
    uploaded_image_blob_url = None
    try:
        uploaded_image_blob_url = upload_bytes_to_blob(ORIG_CONTAINER, ORIG_SAS, orig_blob_name, image_bytes, content_type='image/jpeg')
    except Exception:
        logger.exception("Failed to upload original image to blob")
        uploaded_image_blob_url = None

    # Create annotated image in-memory from results[0].plot()
    annotated_bytes = None
    try:
        annotated_arr = results[0].plot()  # returns numpy array with boxes drawn
        annotated_pil = Image.fromarray(annotated_arr.astype('uint8'))
        buf = BytesIO()
        annotated_pil.save(buf, format='JPEG')
        annotated_bytes = buf.getvalue()
        buf.close()
    except Exception as e:
        logger.exception("Failed to create annotated image bytes: %s", e)
        annotated_bytes = None

    # Upload annotated bytes to PRED container
    predicted_image_blob_url = None
    try:
        if annotated_bytes:
            predicted_image_blob_url = upload_bytes_to_blob(PRED_CONTAINER, PRED_SAS, pred_blob_name, annotated_bytes, content_type='image/jpeg')
            # record predicted image in DB
            try:
                insert_predicted_image_record(blob_path=predicted_image_blob_url, local_path=None)
            except Exception:
                logger.exception("Failed to insert predicted image record")
    except Exception:
        logger.exception("Failed to upload predicted image to blob")
        predicted_image_blob_url = None

    # Count occurrences of the class in the detected results
    # safe one-liner (counts only boxes with conf >= 0.65 and matching class)
    find_class_index = next((int(ci) for ci,cn in results[0].names.items() if cn == find_class), None)
    if find_class_index is not None and getattr(results[0], "boxes", None):
        current_quantity = int(sum(1 for conf,cls in zip(results[0].boxes.conf, results[0].boxes.cls) if float(conf) >= 0.65 and int(cls) == find_class_index))
    else:
        current_quantity = 0

    # required quantity from DB
    required_quantity = get_required_quantity(model_name, subtask, stage)

    # Log to DB and handle pass/fail flow
    if required_quantity is not None and current_quantity == required_quantity:
        status = 'pass'
        session['nok_count'] = 0
        try:
            insert_log_db(
                order_number=order_number,
                employee_id=employee_id,
                model=model_name,
                stage=stage,
                subtask=subtask,
                current_quantity=current_quantity,
                required_quantity=required_quantity,
                status=status,
                image_path=uploaded_image_blob_url,
                predicted_image_path=predicted_image_blob_url
            )
        except Exception:
            logger.exception("Failed to insert pass log into DB")

        next_subtask = subtask + 1
        if not get_required_quantity(model_name, next_subtask, stage):
            return redirect(url_for('orings.task_complete', order_number=order_number, employee_id=employee_id, model_name=model_name))
        else:
            return redirect(url_for('orings.nxt_task', subtask=next_subtask, order_number=order_number, employee_id=employee_id, model_name=model_name, stage=stage))
    else:
        status = 'fail'
        session['nok_count'] = session.get('nok_count', 0) + 1

        # Alerts
        try:
            if session['nok_count'] >= 5 and employee_id != "test":
                send_email_via_functionapp(
                    "⚠️ ALERT: Action Needed! More than 4 consecutive failures detected! In Internal Parts Verification System",
                    f"Order Number: {order_number}, Model: {model_name}, Stage: {stage}, Sub-Task Id: {subtask}",
                    ["svc.z327496@zf.com","manikandan.krishnaswamy@zf.com"],
                    None
                )
            elif session['nok_count'] >= 3 and employee_id != "test":
                send_email_via_functionapp(
                    "⚠️ ALERT: Action Needed! More than 2 consecutive failures detected! In Internal Parts Verification System",
                    f"Order Number: {order_number}, Model: {model_name}, Stage: {stage}, Sub-Task Id: {subtask}",
                    ["svc.z327496@zf.com"],
                    None
                )
                return redirect(url_for(
                                'orings.hil_review',
                                order_number=order_number,
                                employee_id=employee_id,
                                model=model_name,
                                stage=stage,
                                subtask=subtask
                            ))
        except Exception:
            logger.exception("Failed sending alert email")

        try:
            insert_log_db(
                order_number=order_number,
                employee_id=employee_id,
                model=model_name,
                stage=stage,
                subtask=subtask,
                current_quantity=current_quantity,
                required_quantity=required_quantity,
                status=status,
                image_path=uploaded_image_blob_url,
                predicted_image_path=predicted_image_blob_url
            )
        except Exception:
            logger.exception("Failed to insert fail log into DB")

        return render_template("retake.html")


# DB-backed retrieval of subtasks
def get_all_required_subtasks_db(stage, model_name):
    return get_all_required_subtasks(stage, model_name)

@orings_bp.route('/update_status/<subtask>/<order_number>/<employee_id>/<model>/<stage>/<status>', methods=['GET', 'POST'])
def update_status(subtask, order_number, employee_id, model, stage, status):
    subtask = safe_int(subtask)
    if subtask is None:
        return jsonify({"error": "Invalid subtask"}), 400

    try:
        from db import get_conn_cursor
        with get_conn_cursor() as (conn, cur):
            cur.execute("""
                SELECT id FROM ipv_ai_logs
                WHERE order_number = %s AND employee_id = %s AND model = %s AND stage = %s AND subtask = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (order_number, employee_id, model, stage, subtask))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "No matching row found"}), 404
            log_id = row['id']
            cur.execute("UPDATE ipv_ai_logs SET status = %s WHERE id = %s", (status, log_id))
        return jsonify({"message": "Status updated successfully"})
    except Exception as e:
        logger.exception("Failed to update status in DB: %s", e)
        return jsonify({"error": str(e)}), 500

from db import get_conn_cursor

@orings_bp.route('/logs-oring-sml')
def view_logs():
    """
    Server-side filtered view of ipv_ai_logs.
    Returns all rows matching filters (no dedupe) ordered by order_number and subtask ASC.
    Accepts GET params: order_number, model, employee_id, stage, subtask (subtask integer).
    """
    # read filters from query string
    order_number = request.args.get('order_number', type=str)
    model = request.args.get('model', type=str)
    employee_id = request.args.get('employee_id', type=str)
    stage = request.args.get('stage', type=str)
    subtask_raw = request.args.get('subtask')
    try:
        subtask = int(subtask_raw) if subtask_raw not in (None, '') else None
    except Exception:
        subtask = None

    # Build WHERE clauses & params safely
    where_clauses = []
    params = []

    if order_number:
        where_clauses.append("lower(order_number) LIKE lower(%s)")
        params.append(f"%{order_number.strip()}%")
    if model:
        where_clauses.append("lower(model) = lower(%s)")
        params.append(model.strip())
    if employee_id:
        where_clauses.append("employee_id = %s")
        params.append(employee_id.strip())
    if stage:
        where_clauses.append("lower(stage) = lower(%s)")
        params.append(stage.strip())
    if subtask is not None:
        where_clauses.append("subtask = %s")
        params.append(subtask)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # ✅ Sort by order_number first, then subtask numerically (ascending), then created_at (descending)
    sql = f"""
    SELECT *
    FROM ipv_ai_logs
    {where_sql}
    ORDER BY order_number ASC, subtask::int ASC NULLS LAST, created_at DESC
    LIMIT 5000
    """

    try:
        with get_conn_cursor(dict_cursor=True) as (conn, cur):
            cur.execute(sql, tuple(params))
            logs = cur.fetchall()

            # fetch distinct lists for dropdowns
            cur.execute("SELECT DISTINCT model FROM ipv_ai_logs WHERE model IS NOT NULL ORDER BY model")
            models = [r['model'] for r in cur.fetchall()]

            cur.execute("SELECT DISTINCT employee_id FROM ipv_ai_logs WHERE employee_id IS NOT NULL ORDER BY employee_id")
            employees = [r['employee_id'] for r in cur.fetchall()]

            cur.execute("SELECT DISTINCT stage FROM ipv_ai_logs WHERE stage IS NOT NULL ORDER BY stage")
            stages = [r['stage'] for r in cur.fetchall()]

            cur.execute("SELECT DISTINCT subtask FROM ipv_ai_logs WHERE subtask IS NOT NULL ORDER BY subtask::int")
            subtasks = [r['subtask'] for r in cur.fetchall()]

    except Exception as e:
        logger.exception("Failed to load logs: %s", e)
        logs, models, employees, stages, subtasks = [], [], [], [], []

    return render_template(
        'log-all-ipv-ai.html',
        logs=logs,
        models=models,
        employees=employees,
        stages=stages,
        subtasks=subtasks,
        filters=request.args
    )



@orings_bp.route('/update_status_by_approval/<subtask>/<order_number>/<employee_id>/<model>/<stage>/<status>', methods=['GET','POST'])
def update_status_by_approval(subtask, order_number, employee_id, model, stage, status):
    """Sends approval request email to approvers with a decision link and inline image if available.

    Ensures the image link included in the email is accessible by:
      - using the blob listing if possible, or
      - falling back to DB's predicted_image_path and appending the PRED_SAS token
        when the DB path/URL does not already include a SAS query string.
    """
    from urllib.parse import urlparse, urlunparse

    # build approve link (will call update_status when clicked)
    approval_link = url_for('orings.update_status', subtask=subtask, order_number=order_number,
                            employee_id=employee_id, model=model, stage=stage, status=status, _external=True)

    image_blob_url = None

    try:
        # container config (from env or app config)
        PRED_CONTAINER_URL = os.environ.get(
            'AZURE_BLOB_CONTAINER_URL_PREDICT_IMAGE_IPV_AI',
            current_app.config.get('IPV_AI_PRED_CONTAINER', 'https://shadowboardstorage.blob.core.windows.net/internal-parts-pred-dump-imgs')
        ).rstrip('/')

        PRED_SAS = os.environ.get(
            'AZURE_BLOB_SAS_PREDICT_IMAGE_IPV_AI',
            current_app.config.get('IPV_AI_PRED_SAS', '')
        )

        container_name = PRED_CONTAINER_URL.split('/')[-1]
        account_url = PRED_CONTAINER_URL[:PRED_CONTAINER_URL.rfind('/' + container_name)]

        # Try ContainerClient listing first (fast path)
        try:
            container_client = ContainerClient(account_url=account_url, container_name=container_name, credential=PRED_SAS)
        except Exception:
            container_client = None
            logger.warning("Couldn't create ContainerClient for approval email lookup; will fallback to DB logs.")

        # 1) try exact prefix search (if we have container_client)
        blob_name = None
        if container_client:
            file_prefix = f"{order_number}_{model}_{stage}_{subtask}_{employee_id}"
            for blob in container_client.list_blobs(name_starts_with=file_prefix):
                blob_name = blob.name
                break

            # 2) flexible token search
            if not blob_name:
                tokens = [str(order_number), str(model), str(stage), str(subtask), str(employee_id)]
                inspected = 0
                for blob in container_client.list_blobs(name_starts_with=str(order_number)):
                    inspected += 1
                    name = blob.name
                    if all(tok in name for tok in tokens):
                        blob_name = name
                        break
                    if inspected > 5000:
                        break

            # 3) looser fallback
            if not blob_name:
                for blob in container_client.list_blobs(name_starts_with=str(order_number)):
                    if (str(order_number) in blob.name) and (str(model) in blob.name):
                        blob_name = blob.name
                        break

        # If we found a blob via listing, build SAS URL (if we have PRED_SAS) or plain URL otherwise
        if blob_name:
            image_blob_url = f"{account_url}/{container_name}/{blob_name}"
            if PRED_SAS:
                image_blob_url = f"{image_blob_url}?{PRED_SAS}"
        else:
            # fallback to DB logs
            logger.debug("Blob-listing failed for approval email; trying DB logs fallback.")
            logs = read_logs()
            matched = None
            # Try exact match first
            for log in logs:
                if (str(log.get('order_number')) == str(order_number)
                        and str(log.get('model')) == str(model)
                        and str(log.get('stage')) == str(stage)
                        and str(log.get('subtask')) == str(subtask)
                        and str(log.get('employee_id')) == str(employee_id)
                        and log.get('predicted_image_path')):
                    matched = log
                    break
            # Looser fallback (order + model)
            if not matched:
                for log in logs:
                    if (str(log.get('order_number')) == str(order_number)
                            and str(log.get('model')) == str(model)
                            and log.get('predicted_image_path')):
                        matched = log
                        break

            if matched:
                predicted_value = (matched.get('predicted_image_path') or "").strip()
                logger.debug("Found predicted_image_path in DB: %s", predicted_value)

                # If DB value is full URL
                if predicted_value.lower().startswith(("http://", "https://")):
                    parsed = urlparse(predicted_value)
                    # If it already contains query params (SAS), use as-is
                    if parsed.query:
                        image_blob_url = predicted_value
                    else:
                        # If it's pointing to our container and we have PRED_SAS -> append SAS
                        if f"/{container_name}/" in predicted_value and PRED_SAS:
                            image_blob_url = f"{predicted_value.split('?',1)[0]}?{PRED_SAS}"
                        elif PRED_SAS and parsed.netloc.endswith("blob.core.windows.net"):
                            # It's some blob URL (maybe other container) without SAS -> append SAS
                            image_blob_url = f"{predicted_value.split('?',1)[0]}?{PRED_SAS}"
                        else:
                            # No SAS to add: use as-is (likely private and won't render)
                            image_blob_url = predicted_value
                            logger.warning("predicted_image_path is a blob URL but no SAS available; image may not render in email.")
                else:
                    # predicted_value is likely a blob path (container-relative or blob key); construct full URL + SAS if possible
                    # Examples:
                    #  - internal-parts-pred-dump-imgs/ORING-PRED-.../file.jpg
                    #  - ORING-PRED-.../file.jpg
                    blob_candidate = predicted_value.split('?', 1)[0].lstrip('/')
                    # Remove possible leading container name if present
                    if blob_candidate.startswith(f"{container_name}/"):
                        blob_candidate = blob_candidate[len(container_name)+1:]
                    # Build url with SAS if available
                    if blob_candidate:
                        image_blob_url = f"{account_url}/{container_name}/{blob_candidate}"
                        if PRED_SAS:
                            image_blob_url = f"{image_blob_url}?{PRED_SAS}"
                        else:
                            logger.warning("Predicted image path found in DB but PRED_SAS not set; email link may be private.")
                    else:
                        image_blob_url = None
            else:
                image_blob_url = None
    except Exception as e:
        logger.exception("Failed to lookup task image for approval email: %s", e)
        image_blob_url = None

    # Build email body with image preview (if available)
    email_subject = f"Approval Request (Internal Parts Verify AI): Status Change for Order {order_number}"
    email_body = f"""
        <div style="border: 1px solid #ccc; padding: 15px; font-family: Arial, sans-serif;">
            <p>Hello,</p>
            <p>An approval request has been made to update the status of the following task:</p>
            <ul>
                <li><strong>Order Number:</strong> {order_number}</li>
                <li><strong>Employee ID:</strong> {employee_id}</li>
                <li><strong>Model:</strong> {model}</li>
                <li><strong>Stage:</strong> {stage}</li>
                <li><strong>Subtask:</strong> {subtask}</li>
                <li><strong>New Status:</strong> {status}</li>
            </ul>
            <p style="margin-top:12px;">
                <a href="{approval_link}" style="background-color: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Approve Request</a>
            </p>
    """

    if image_blob_url:
        # inline preview + link for clients that don't render images
        email_body += f"""
            <hr style="border: none; border-top: 1px solid #eee; margin-top:18px;">
            <p><strong>Task Image Preview:</strong></p>
            <p>
                <a href="{image_blob_url}" target="_blank" rel="noopener noreferrer">
                    <img src="{image_blob_url}" alt="Task image" style="max-width:420px; width:100%; border:1px solid #ddd; border-radius:4px;" />
                </a>
            </p>
            <p style="font-size:12px; color:#666; word-break:break-all;">
                Image link: <a href="{image_blob_url}">{image_blob_url}</a>
            </p>
        """
    else:
        email_body += """
            <hr style="border: none; border-top: 1px solid #eee; margin-top:18px;">
            <p><em>No task image available</em></p>
        """

    email_body += """
            <hr>
            <p style="font-size: 12px; color: #888; text-align: center;">This is an automated email - please do not reply.</p>
        </div>
    """
    # send email and include image path in payload (so Function App also receives it)
    data = request.get_json(silent=True) or {}
    selected_email = data.get("approver_email", "").strip()
    # Default approvers list
    default_recipients = [
        "svc.z327496@zf.com",
        "vinothkumar.theenadhayalan@zf.com",
        "karthikeyank.k@zf.com",
        "karthick.ramasamy@zf.com",
        "ragunathr.r@zf.com"
    ]
    # Use selected email if provided, else default list
    recipients = [selected_email,"karthick.ramasamy@zf.com"] if selected_email else default_recipients
    print(recipients)
    email_status = send_email_via_functionapp(
        email_subject=email_subject,
        email_body=email_body,
        recipients=recipients,
        image_path=image_blob_url 
    )
    return jsonify({"message": "Approval request sent to approvers.", "status": email_status, "image": image_blob_url})

@orings_bp.route('/view_task_image/<order_number>/<model>/<stage>/<subtask>/<employee_id>', methods=['GET'])
def view_task_image(order_number, model, stage, subtask, employee_id):

    from db import get_conn_cursor
    from azure.storage.blob import BlobClient
    import os
    import tempfile

    # 1️⃣ Get predicted_image_path from DB
    with get_conn_cursor(dict_cursor=True) as (conn, cur):
        cur.execute("""
            SELECT predicted_image_path
            FROM ipv_ai_logs
            WHERE order_number=%s
              AND model=%s
              AND stage=%s
              AND subtask=%s
              AND employee_id=%s
            ORDER BY created_at DESC
            LIMIT 1
        """, (order_number, model, stage, subtask, employee_id))

        row = cur.fetchone()

    if not row or not row.get("predicted_image_path"):
        return "No image found", 404

    full_url = row["predicted_image_path"]

    # 2️⃣ Extract blob relative path after container
    container_url = os.environ.get('AZURE_BLOB_CONTAINER_URL_PREDICT_IMAGE_IPV_AI').rstrip('/')
    sas_token = os.environ.get('AZURE_BLOB_SAS_PREDICT_IMAGE_IPV_AI')

    if not container_url or not sas_token:
        return "Blob config missing", 500

    blob_name = full_url.replace(container_url + "/", "")

    # 3️⃣ Download blob using SAS
    blob_url_with_sas = f"{container_url}/{blob_name}?{sas_token}"
    blob_client = BlobClient.from_blob_url(blob_url_with_sas)

    try:
        blob_bytes = blob_client.download_blob().readall()
    except Exception:
        return "Failed to download blob", 500

    # 4️⃣ Save temp & return
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp.write(blob_bytes)
    tmp.close()

    return send_file(tmp.name, mimetype="image/jpeg")

# @orings_bp.route('/view_task_image/<order_number>/<model>/<stage>/<subtask>/<employee_id>', methods=['GET'])
# def view_task_image(order_number, model, stage, subtask, employee_id):
#     """
#     Robust image lookup:
#       1) Try name_starts_with with exact prefix (fast).
#       2) If not found, list blobs that start with order_number and find first blob
#          whose name contains all tokens (handles timestamps/folders).
#       3) Fallback: try looser matches (order+model).
#       4) If still not found, fallback to DB logs: read_logs() and use predicted_image_path.
#          - If predicted_image_path is http(s) URL -> download via requests.
#          - If predicted_image_path is blob path or container URL -> try to resolve with PRED_SAS/container.
#     Downloads the matched blob into temp-task-imgs and serves via /temp_images/<filename>.
#     """
#     try:
#         # container config (from env or app config)
#         PRED_CONTAINER_URL = os.environ.get(
#             'AZURE_BLOB_CONTAINER_URL_PREDICT_IMAGE_IPV_AI',
#             current_app.config.get('IPV_AI_PRED_CONTAINER', 'https://shadowboardstorage.blob.core.windows.net/internal-parts-pred-dump-imgs')
#         ).rstrip('/')

#         PRED_SAS = os.environ.get(
#             'AZURE_BLOB_SAS_PREDICT_IMAGE_IPV_AI',
#             current_app.config.get('IPV_AI_PRED_SAS', '')
#         )

#         container_name = PRED_CONTAINER_URL.split('/')[-1]
#         account_url = PRED_CONTAINER_URL[:PRED_CONTAINER_URL.rfind('/' + container_name)]
#         container_client = ContainerClient(account_url=account_url, container_name=container_name, credential=PRED_SAS)

#         # 1) Exact prefix search
#         file_prefix = f"{order_number}_{model}_{stage}_{subtask}_{employee_id}"
#         logger.debug("Attempting exact prefix search: %s", file_prefix)

#         blob_name = None
#         for blob in container_client.list_blobs(name_starts_with=file_prefix):
#             logger.debug("Found blob by exact prefix: %s", blob.name)
#             blob_name = blob.name
#             break

#         # 2) Flexible token search (order_number prefix -> tokens present)
#         if not blob_name:
#             logger.debug("Exact prefix search failed, trying flexible token search.")
#             tokens = [str(order_number), str(model), str(stage), str(subtask), str(employee_id)]
#             inspected = 0
#             for blob in container_client.list_blobs(name_starts_with=str(order_number)):
#                 inspected += 1
#                 name = blob.name
#                 if all(tok in name for tok in tokens):
#                     logger.debug("Found blob by token match after inspecting %d entries: %s", inspected, name)
#                     blob_name = name
#                     break
#                 if inspected > 5000:
#                     logger.warning("Inspected >5000 blobs while searching for image; aborting flexible search.")
#                     break

#         # 3) Looser fallback: order + model
#         if not blob_name:
#             logger.debug("Trying looser order+model search.")
#             for blob in container_client.list_blobs(name_starts_with=str(order_number)):
#                 name = blob.name
#                 if (str(order_number) in name) and (str(model) in name):
#                     logger.debug("Found blob by order+model: %s", name)
#                     blob_name = name
#                     break

#         temp_dir = "temp-task-imgs"
#         os.makedirs(temp_dir, exist_ok=True)

#         # If we found blob_name in container_client listing -> download via blob client
#         if blob_name:
#             local_filename = os.path.basename(blob_name)
#             temp_file_path = os.path.join(temp_dir, local_filename)
#             blob_client = container_client.get_blob_client(blob=blob_name)
#             with open(temp_file_path, "wb") as f:
#                 f.write(blob_client.download_blob().readall())
#             logger.info("Downloaded image to: %s (blob: %s)", temp_file_path, blob_name)
#             return render_template("view_image.html", image_url=f"/temp_images/{local_filename}")

#         # 4) FALLBACK -> Use DB logs (predicted_image_path)
#         logger.debug("Blob-listing searches failed; falling back to DB logs for predicted_image_path.")
#         try:
#             logs = read_logs()  # already imported helper that returns recent logs
#             # find a log matching all fields (prefer most recent)
#             matched = None
#             for log in logs:
#                 if (str(log.get('order_number')) == str(order_number)
#                         and str(log.get('model')) == str(model)
#                         and str(log.get('stage')) == str(stage)
#                         and str(log.get('subtask')) == str(subtask)
#                         and str(log.get('employee_id')) == str(employee_id)
#                         and log.get('predicted_image_path')):
#                     matched = log
#                     break
#             # if no exact match, try looser matching (order+model)
#             if not matched:
#                 for log in logs:
#                     if (str(log.get('order_number')) == str(order_number)
#                             and str(log.get('model')) == str(model)
#                             and log.get('predicted_image_path')):
#                         matched = log
#                         break

#             if not matched:
#                 logger.info("No matching blob or DB log entry with predicted_image_path found for order=%s model=%s", order_number, model)
#                 return "No matching image found", 404

#             predicted_value = matched.get('predicted_image_path')
#             logger.debug("Found predicted_image_path in DB: %s", predicted_value)

#             # If predicted_value is a full URL -> fetch using requests
#             if isinstance(predicted_value, str) and predicted_value.lower().startswith(("http://", "https://")):
#                 try:
#                     resp = requests.get(predicted_value, stream=True, timeout=15)
#                     if resp.status_code == 200 and resp.content:
#                         filename = os.path.basename(predicted_value.split('?')[0])
#                         tmp_path = os.path.join(temp_dir, filename)
#                         with open(tmp_path, "wb") as f:
#                             f.write(resp.content)
#                         logger.info("Downloaded image from HTTP URL to %s", tmp_path)
#                         return render_template("view_image.html", image_url=f"/temp_images/{filename}")
#                     else:
#                         logger.warning("HTTP fetch of predicted_image_path failed: %s (status=%s)", predicted_value, resp.status_code)
#                 except Exception as e:
#                     logger.exception("HTTP fetch of predicted_image_path raised: %s", e)

#             # If predicted_value is not an http URL, try to interpret it as a blob path
#             # Predicted path examples:
#             #  - full: https://account.blob.core.windows.net/container/path/to/file.jpg
#             #  - container-rooted: internal-parts-pred-dump-imgs/ORING-PRED-.../file.jpg
#             #  - simple blob name: ORING-PRED-.../file.jpg
#             blob_candidate = None
#             # if contains the container part, extract blob path
#             if isinstance(predicted_value, str) and f"/{container_name}/" in predicted_value:
#                 try:
#                     blob_candidate = predicted_value.split(f"/{container_name}/", 1)[1].split('?', 1)[0]
#                 except Exception:
#                     blob_candidate = None
#             else:
#                 # maybe the value already looks like blob path (no container)
#                 # strip leading slashes
#                 if isinstance(predicted_value, str):
#                     blob_candidate = predicted_value.split('?', 1)[0].lstrip('/')

#             if blob_candidate:
#                 try:
#                     # Attempt to get blob by exact path from container
#                     blob_client = container_client.get_blob_client(blob=blob_candidate)
#                     data = blob_client.download_blob().readall()
#                     filename = os.path.basename(blob_candidate)
#                     tmp_path = os.path.join(temp_dir, filename)
#                     with open(tmp_path, "wb") as f:
#                         f.write(data)
#                     logger.info("Downloaded image from blob_candidate to %s", tmp_path)
#                     return render_template("view_image.html", image_url=f"/temp_images/{filename}")
#                 except Exception as e:
#                     logger.exception("Failed to download blob_candidate %s: %s", blob_candidate, e)

#             # if still not attached, give up with not found
#             logger.info("Could not fetch predicted image from DB path or blob. predicted_value=%s", predicted_value)
#             return "No matching image found", 404

#         except Exception as e:
#             logger.exception("DB fallback lookup failed: %s", e)
#             return "Error fetching image", 500

#     except Exception as e:
#         logger.exception("Failed to fetch predicted image: %s", e)
#         return "Error fetching image", 500


@orings_bp.route('/temp_images/<filename>')
def serve_temp_image(filename):
    temp_dir = "temp-task-imgs"
    os.makedirs(temp_dir, exist_ok=True)
    return send_from_directory(temp_dir, filename)

@orings_bp.route('/generate_overall_pdf', methods=['POST'])
def generate_overall_pdf():
    logger = logging.getLogger(__name__)

    order_number = request.form['order_number']
    model_name = request.form['model_name']
    stage = request.form['stage']

    # pull logs and filter
    logs = read_logs()
    filtered_logs = [log for log in logs if log['order_number'] == order_number and log['model'] == model_name and log['stage'] == stage]

    # normalize/parse datetimes (if string)
    for log in filtered_logs:
        dt = log.get('date_time')
        if isinstance(dt, str):
            try:
                log['date_time'] = parse(dt)
            except Exception:
                pass  # leave as-is if cannot parse

    filtered_logs.sort(key=lambda x: x['date_time'], reverse=True)

    # keep latest passed per subtask
    latest_passed_subtasks = {}
    for log in filtered_logs:
        if str(log.get('status', '')).lower() == "pass" and log.get('subtask') not in latest_passed_subtasks:
            latest_passed_subtasks[log['subtask']] = log

    passed_subtasks = list(latest_passed_subtasks.values())
    all_required_subtasks = get_all_required_subtasks(stage, model_name)
    completed_subtasks = [int(log['subtask']) for log in passed_subtasks]
    remaining_subtasks = [subtask for subtask in all_required_subtasks if subtask not in completed_subtasks]
    overall_status = "OK" if len(remaining_subtasks) == 0 else "NOK"

    pdf = FPDF()
    pdf.add_page()

    try:
        pdf.image(os.path.join('static', 'images', 'zf.png'), x=10, y=8, w=30)
    except Exception:
        pass

    pdf.set_font('Arial', 'B', 16)
    pdf.cell(200, 10, txt=f"Internal Parts AI Verification Report", ln=True, align='C')
    pdf.cell(200, 10, txt=f"Order: {order_number}", ln=True, align='C')
    pdf.cell(200, 10, txt=f"Model: {model_name}, Stage: {stage}", ln=True, align='C')
    pdf.ln(10)

    # container config (from env or app config)
    PRED_CONTAINER_URL = os.environ.get(
        'AZURE_BLOB_CONTAINER_URL_PREDICT_IMAGE_IPV_AI',
        current_app.config.get('IPV_AI_PRED_CONTAINER', 'https://shadowboardstorage.blob.core.windows.net/internal-parts-pred-dump-imgs')
    ).rstrip('/')
    PRED_SAS = os.environ.get(
        'AZURE_BLOB_SAS_PREDICT_IMAGE_IPV_AI',
        current_app.config.get('IPV_AI_PRED_SAS', '')
    )

    # create ContainerClient pieces (account_url, container_name)
    try:
        container_name = PRED_CONTAINER_URL.split('/')[-1]
        account_url = PRED_CONTAINER_URL[:PRED_CONTAINER_URL.rfind('/' + container_name)]
        container_client = ContainerClient(account_url=account_url, container_name=container_name, credential=PRED_SAS)
    except Exception as e:
        logger.exception("Failed to create ContainerClient: %s", e)
        container_client = None

    pdf.set_font('Arial', '', 12)
    for log in passed_subtasks:
        pdf.cell(200, 10, txt=f"Sub-task ID: {log['subtask']}, Requirement Qty: {log['current_quantity']} / {log['required_quantity']}", ln=True)
        # date/time line (safe)
        dt = log.get('date_time')
        try:
            pdf.cell(200, 10, txt=f"Time: {dt.strftime('%m/%d/%Y %H:%M')}" if hasattr(dt, "strftime") else f"Time: {dt}", ln=True)
        except Exception:
            pdf.cell(200, 10, txt="Time: N/A", ln=True)
        pdf.cell(200, 10, txt=f"Operator: {log.get('employee_id', '')}", ln=True)
        pdf.cell(200, 2, txt=f"{log.get('predicted_image_path', '')}", ln=True)

        # Try to attach image (no helper functions — all inline)
        try:
            pdf.ln(5)
            predicted_value = log.get('predicted_image_path') or ''
            filename = os.path.basename(predicted_value) if predicted_value else None
            temp_local = None
            attached = False

            # 1) If we have a filename and ContainerClient, list blobs and find match ending with filename
            if filename and container_client is not None:
                try:
                    # iterate blobs until we find one whose name endswith filename
                    # (you can limit by a counter if container is huge)
                    count = 0
                    for blob in container_client.list_blobs():
                        count += 1
                        if blob.name.endswith(filename):
                            blob_client = container_client.get_blob_client(blob=blob.name)
                            blob_bytes = blob_client.download_blob().readall()
                            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1] or ".jpg")
                            tmp.write(blob_bytes)
                            tmp.flush()
                            tmp.close()
                            temp_local = tmp.name
                            pdf.image(temp_local, x=10, w=100)
                            attached = True
                            # cleanup
                            try:
                                os.remove(temp_local)
                            except Exception:
                                pass
                            break
                        # optional safety break if you want to limit listing (uncomment if needed)
                        # if count > 5000: break
                except Exception as e:
                    logger.exception("Error while searching/downloading blob by filename: %s", e)

            # 2) fallback: if predicted_value looks like an http(s) URL, try requests.get()
            if not attached and predicted_value and predicted_value.lower().startswith(("http://", "https://")):
                try:
                    resp = requests.get(predicted_value, timeout=10)
                    if resp.status_code == 200 and resp.content:
                        ext = os.path.splitext(filename or "tmp.jpg")[1] or ".jpg"
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                        tmp.write(resp.content)
                        tmp.flush()
                        tmp.close()
                        temp_local = tmp.name
                        pdf.image(temp_local, x=10, w=100)
                        attached = True
                        try:
                            os.remove(temp_local)
                        except Exception:
                            pass
                except Exception:
                    logger.debug("requests.get fallback failed for: %s", predicted_value)

            # 3) if still not attached, show text
            if not attached:
                pdf.cell(200, 10, txt="Image not available", ln=True)

            pdf.ln(10)
        except Exception as e:
            logger.exception("Failed to attach image for log entry: %s", e)
            pdf.cell(200, 10, txt="Image not available", ln=True)

    # remaining subtasks section
    if remaining_subtasks:
        pdf.cell(200, 10, txt="Remaining Sub-tasks:", ln=True, align='L')
        for subtask_id in remaining_subtasks:
            pdf.cell(200, 10, txt=f"Sub-task ID: {subtask_id} - Status: Not Completed", ln=True)
        pdf.cell(200, 10, txt=f"Number of Sub-tasks Left: {len(remaining_subtasks)}", ln=True)
    else:
        pdf.cell(200, 10, txt="All Sub-tasks Completed Successfully!", ln=True)

    pdf.ln(10)
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(200, 10, txt=f"Overall Status: {overall_status}", ln=True, align='C')

    if overall_status == "NOK":
        pdf.ln(20)
        pdf.set_font('Arial', '', 12)
        pdf.cell(100, 10, txt="Moderator's Signature: ____________________", ln=False, align='L')
        pdf.cell(100, 10, txt="CB's Signature: ____________________", ln=True, align='R')

    pdf_output = f"Internal_PartsVerification_Report_Order_{order_number}.pdf"
    pdf.output(pdf_output)

    return send_file(pdf_output, as_attachment=True)


@orings_bp.route(
    '/hil_review/<order_number>/<employee_id>/<model>/<stage>/<subtask>',
    methods=['GET']
)
def hil_review(order_number, employee_id, model, stage, subtask):

    # Find predicted image (reuse your existing logic)
    image_blob_url = None
    logs = read_logs()

    approver_emails = [
        "vinothkumar.theenadhayalan@zf.com",
        "karthikeyank.k@zf.com",
        "karthick.ramasamy@zf.com",
        "ragunathr.r@zf.com",
        "svc.z327496@zf.com"
    ]

    for log in logs:
        if (log['order_number'] == order_number
            and log['model'] == model
            and log['stage'] == stage
            and str(log['subtask']) == str(subtask)
            and log.get('predicted_image_path')):
            image_blob_url = log['predicted_image_path']
            break

    approval_url = url_for(
        'orings.update_status_by_approval',
        subtask=subtask,
        order_number=order_number,
        employee_id=employee_id,
        model=model,
        stage=stage,
        status='pass'
    )

    retry_url = url_for(
        'orings.oring_sml_index'
    )

    return render_template(
        'approval_hil.html',
        approval_url=approval_url,
        retry_url=retry_url,
        image_blob_url=image_blob_url,approver_emails=approver_emails
    )


#--------------------------WEBHOOK---------------------------
@orings_bp.route('/api/webhooks/ipv/check-stage-complete', methods=['POST'])
def ipv_stage_completion_webhook():
    data = request.get_json(force=True)

    order_number = data.get('order_number')
    model = data.get('model')
    stage = data.get('stage')

    if not order_number or not model or not stage:
        return jsonify({"error": "order_number, model, stage required"}), 400
    order_number=order_number+"-"+stage

    completed, required, done = is_stage_fully_completed(
        order_number=order_number,
        model=model,
        stage=stage
    )

    if completed == False:
        send_email_via_functionapp("O-Ring Images not attached",
                                   f"{order_number} This order has not satisfied the required o-ring images for verification but user closed the stage on Andon.",
                                   ["karthick.ramasamy@zf.com"])

    return jsonify({
        "order_number": order_number,
        "model": model,
        "stage": stage,
        "completed": completed,
        "required_subtasks": required,
        "completed_subtasks": done,
        "pending_subtasks": sorted(set(required) - set(done))
    }), 200



#----------------- Live RT --------------------

@orings_bp.route("/ipv-detection-live/frame", methods=["POST"])
def ipv_detection_live_frame():
    """
    Receives a single frame
    Runs YOLO
    Returns JSON boxes + count
    """
    if "image" not in request.files:
        return jsonify({"boxes": [], "count": 0})

    model_name = request.form.get("model_yolo")
    class_name = request.form.get("class_name")
    conf_th = float(request.form.get("conf", 0.65))

    file = request.files["image"]
    pil_img = Image.open(file.stream).convert("RGB")
    img_arr = np.array(pil_img)

    model = get_live_model(model_name)
    results = model.predict(img_arr, conf=conf_th, save=False, verbose=False)[0]

    # find class index
    class_index = next(
        (int(ci) for ci, cn in results.names.items() if cn == class_name),
        None
    )

    boxes = []
    count = 0

    if results.boxes is not None and class_index is not None:
        for box, cls, conf in zip(
            results.boxes.xyxy,
            results.boxes.cls,
            results.boxes.conf
        ):
            if int(cls) == class_index:
                count += 1
                x1, y1, x2, y2 = map(int, box.tolist())
                boxes.append({
                    "x1": x1, "y1": y1,
                    "x2": x2, "y2": y2,
                    "conf": float(conf)
                })

    return jsonify({
        "boxes": boxes,
        "count": count
    })

@orings_bp.route("/ipv-detection-live")
def ipv_detection_live():
    return render_template("ipv_detection_live.html")
