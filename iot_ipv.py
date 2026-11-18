import os
import json
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app, render_template, send_file, redirect, url_for, abort, flash
from PIL import Image
import requests
from ultralytics import YOLO
from db import get_conn_cursor  
from dotenv import load_dotenv
import re

load_dotenv()

iot_ipv_app = Blueprint('iot_ipv', __name__,template_folder='templates')

@iot_ipv_app.route('/iot-ipv-onboard', methods=['GET', 'POST'])
def device_onboard():
    """
    Show onboarding form (GET)
    Process onboarding form (POST)
    """
    if request.method == 'POST':
        # get form fields
        device_name = (request.form.get('device_name') or "").strip()
        nick_name = (request.form.get('nick_name') or "").strip()
        position = (request.form.get('position') or "").strip()
        ip = (request.form.get('ip') or "").strip()
        mac = (request.form.get('mac') or "").strip().upper()
        model_name = (request.form.get('model') or "").strip()
        stage = request.form.get('stage') or None
        subtask = request.form.get('subtask') or None
        user_id = (request.form.get('user_id') or "").strip()
        password = request.form.get('password') or None
        ssid = (request.form.get('ssid') or "").strip()
        ssid_key = (request.form.get('ssid_key') or "").strip()

        # basic server-side validation
        if not device_name:
            flash("Device name is required", "danger")
            return render_template('iot_ipv/onboard_device.html', form=request.form)

        # Optional: simple IP and MAC format checks (not strict)
        ip_pattern = r'^[0-9\.]{0,45}$'  # allow IPv4 / IPv6 minimal check
        mac_pattern = r'^[0-9A-F:-]{0,17}$'
        if ip and not re.match(ip_pattern, ip):
            flash("IP format looks invalid", "warning")

        if mac and not re.match(mac_pattern, mac):
            flash("MAC format looks suspicious — expected hex digits and colons", "warning")

        # Normalize stage/subtask to integers if provided (or set None)
        try:
            stage_val = int(stage) if stage not in (None, '', 'None') else None
        except ValueError:
            flash("Stage must be an integer", "danger")
            return render_template('iot_ipv/onboard_device.html', form=request.form)

        try:
            subtask_val = int(subtask) if subtask not in (None, '', 'None') else None
        except ValueError:
            flash("Subtask must be an integer", "danger")
            return render_template('iot_ipv/onboard_device.html', form=request.form)

        # Insert into DB with uniqueness checks
        try:
            # <-- NOTE: unpack (conn, cur) here because get_conn_cursor yields a tuple
            with get_conn_cursor() as (conn, cur):
                # check device_name uniqueness
                cur.execute("SELECT id FROM ipv_ai_devices WHERE device_name = %s", (device_name,))
                if cur.fetchone():
                    flash("Device name already exists", "danger")
                    return render_template('iot_ipv/onboard_device.html', form=request.form)

                # check mac uniqueness if provided
                if mac:
                    cur.execute("SELECT id FROM ipv_ai_devices WHERE mac = %s", (mac,))
                    if cur.fetchone():
                        flash("MAC address already exists", "danger")
                        return render_template('iot_ipv/onboard_device.html', form=request.form)

                # Insert new device
                cur.execute("""
                    INSERT INTO ipv_ai_devices
                    (device_name, nick_name, position, ip, mac, model_name, stage, subtask,
                     user_id, password_hash, ssid, ssid_key_encrypted, metadata, created_at, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, now(), TRUE)
                    RETURNING id
                """, (
                    device_name, nick_name, position, ip, mac, model_name, stage_val, subtask_val,
                    user_id, '12345678', ssid, ssid_key
                ))
                row = cur.fetchone()
                device_id = row['id']

            flash(f"Device onboarded successfully (id: {device_id})", "success")
            # Redirect to GET to clear form POST data
            return redirect(url_for('iot_ipv.device_onboard'))  # use blueprint name if registered as 'iot_ipv'
        except Exception as e:
            current_app.logger.exception("Error onboarding device")
            flash("Internal server error while onboarding device", "danger")
            return render_template('iot_ipv/onboard_device.html', form=request.form)

    # GET -> render form
    return render_template('iot_ipv/onboard_device.html', form={})


import subprocess
import shlex
import ipaddress

# GET available devices for model & stage
@iot_ipv_app.route('/iot-ipv-devices/<model>/<stage>', methods=['GET'])
def iot_ipv_devices(model, stage):
    try:
        # simple sanitization: allow alphanum + - _
        model_s = model.strip()
        stage_s = stage.strip()
        with get_conn_cursor() as (conn, cur):
            cur.execute("""
                SELECT id, device_name, nick_name, position, ip, mac, model_name, stage, subtask, is_active
                FROM ipv_ai_devices
                WHERE model_name = %s AND stage = %s AND is_active = TRUE
                ORDER BY id ASC
            """, (model_s, stage_s))
            rows = cur.fetchall() or []
            devices = []
            for r in rows:
                devices.append({
                    'id': r.get('id'),
                    'device_name': r.get('device_name'),
                    'nick_name': r.get('nick_name'),
                    'position': r.get('position'),
                    'ip': r.get('ip'),
                    'mac': r.get('mac'),
                    'model_name': r.get('model_name'),
                    'stage': r.get('stage'),
                    'subtask': r.get('subtask'),
                    'is_active': r.get('is_active'),
                })
        return jsonify({'ok': True, 'devices': devices})
    except Exception as e:
        current_app.logger.exception("Error fetching devices for model/stage")
        return jsonify({'ok': False, 'message': 'internal_error'}), 500

import platform
# POST ping IP
@iot_ipv_app.route('/iot-ipv-ping', methods=['POST'])
def iot_ipv_ping():
    """
    Cross-platform ping (Windows + Linux/macOS)
    No admin/sudo required.
    
    Body: { ip: "10.0.0.5" }
    Returns: { ok, rtt_ms, stdout, stderr }
    """
    js = request.get_json(force=True, silent=True) or {}
    ip = (js.get('ip') or '').strip()

    if not ip:
        return jsonify({'ok': False, 'message': 'ip required'}), 400

    # Validate IPv4/IPv6
    try:
        ip_obj = ipaddress.ip_address(ip)
    except Exception:
        return jsonify({'ok': False, 'message': 'invalid_ip'}), 400

    # Detect OS
    os_name = platform.system().lower()  # 'windows', 'linux', 'darwin'

    # Build OS-specific ping command (no admin needed)
    if 'windows' in os_name:
        # Windows ping syntax:
        #   ping -n 1 -w 1000 <IP>
        #   -n count
        #   -w timeout in ms
        cmd = ['ping', '-n', '1', '-w', '1000', str(ip)]
    else:
        # Linux/macOS:
        #   ping -c 1 -W 1 <IP>
        #   Linux:  -W timeout (seconds)
        #   macOS:  does NOT support -W; fallback to -t for TTL
        try:
            # Test if -W is supported (Linux)
            cmd = ['ping', '-c', '1', '-W', '1', str(ip)]
            test_proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2
            )
            # If the OS rejects -W, we use the macOS-friendly version below
            if "illegal option" in (test_proc.stderr or "").lower():
                raise Exception("Unsupported -W")
        except:
            # macOS fallback: use TTL instead of timeout
            cmd = ['ping', '-c', '1', '-t', '2', str(ip)]

    # Run ping
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        out = proc.stdout or ""
        err = proc.stderr or ""
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'message': 'timeout'}), 504
    except Exception as e:
        current_app.logger.exception("Ping failed")
        return jsonify({'ok': False, 'message': 'error'}), 500

    # Parse RTT (supports both Windows and Linux/macOS)
    rtt_ms = None

    # Windows line example: "Approximate round trip times in milli-seconds:"
    #                       "Minimum = 4ms, Maximum = 4ms, Average = 4ms"
    if 'windows' in os_name:
        for line in out.splitlines():
            if "Average" in line:
                # extract "Average = 4ms"
                parts = line.split("Average")
                if len(parts) > 1:
                    try:
                        rtt_str = parts[1].split("=")[1].strip().replace("ms","").strip()
                        rtt_ms = float(rtt_str)
                    except:
                        pass

    else:
        # Linux/macOS example:
        # rtt min/avg/max/mdev = 0.034/0.034/0.034/0.000 ms
        for line in out.splitlines():
            if "rtt min" in line or "round-trip" in line:
                try:
                    numbers = line.split("=")[1].split("/")
                    rtt_ms = float(numbers[1])
                except:
                    rtt_ms = None

    return jsonify({
        'ok': ok,
        'ip': ip,
        'rtt_ms': rtt_ms,
        'stdout': out,
        'stderr': err
    })

@iot_ipv_app.route('/iot-ipv-autoqueue', methods=['POST'])
def iot_ipv_autoqueue():
    """
    Body: { order_number, model, stage, devices: [device_id,...] }
    For each device:
      - verify device exists, matches model and stage, and is_active
      - read device.subtask
      - insert into device_pairs with status='PENDING' and subtask set
    """
    payload = request.get_json(force=True, silent=True) or {}
    order_number = (payload.get('order_number') or '').strip()
    model = (payload.get('model') or '').strip()
    stage_raw = (payload.get('stage') or '').strip()
    device_ids = payload.get('devices') or []

    if not order_number or not model or not stage_raw or not device_ids:
        return jsonify({'ok': False, 'message': 'order_number, model, stage and devices are required'}), 400

    added = 0
    skipped = []
    created_pairs = []

    try:
        with get_conn_cursor() as (conn, cur):
            for did in device_ids:
                # ensure integer id
                try:
                    did_int = int(did)
                except Exception:
                    skipped.append({'device_id': did, 'reason': 'invalid_id'})
                    continue

                # fetch device row and its subtask
                cur.execute("""
                    SELECT id, device_name, model_name, stage, subtask, is_active
                    FROM ipv_ai_devices
                    WHERE id = %s
                    LIMIT 1
                """, (did_int,))
                dev = cur.fetchone()
                if not dev:
                    skipped.append({'device_id': did_int, 'reason': 'not_found'})
                    continue
                if (dev.get('model_name') != model) or dev.get('stage') != stage_raw:
                    skipped.append({'device_id': did_int, 'reason': 'model_or_stage_mismatch'})
                    continue

                if not dev.get('is_active'):
                    skipped.append({'device_id': did_int, 'reason': 'inactive'})
                    continue

                # get subtask from device (could be NULL)
                device_subtask = dev.get('subtask')

                # check if a pair already exists for this device + order_number
                cur.execute("""
                    SELECT id, status FROM device_pairs
                    WHERE device_id=%s AND order_number=%s
                    LIMIT 1
                """, (did_int, order_number))
                existing = cur.fetchone()
                if existing:
                    # if already PENDING/ACTIVE, skip or update status to PENDING
                    if existing.get('status') in ('PENDING', 'ACTIVE'):
                        skipped.append({'device_id': did_int, 'reason': 'already_paired', 'pair_id': existing.get('id')})
                        continue
                    else:
                        # update status to PENDING and set subtask where needed
                        cur.execute("""
                            UPDATE device_pairs
                            SET status=%s, auto_capture=%s, paired_at=now(), last_capture_at=NULL, subtask=%s
                            WHERE id=%s
                        """, ('PENDING', False, device_subtask, existing.get('id')))
                        created_pairs.append(existing.get('id'))
                        added += 1
                        continue

                # insert new pair (PENDING)
                cur.execute("""
                    INSERT INTO device_pairs
                    (device_id, order_number, paired_by, auto_capture, status, last_capture_at, paired_at, model_name, stage, subtask)
                    VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s)
                    RETURNING id
                """, (did_int, order_number, None, False, 'PENDING', None, model, stage_raw, device_subtask))
                r = cur.fetchone()
                if r:
                    pid = r.get('id')
                    created_pairs.append(pid)
                    added += 1

        return jsonify({'ok': True, 'added': added, 'created_pairs': created_pairs, 'skipped': skipped})
    except Exception as e:
        current_app.logger.exception("Error enqueuing devices")
        return jsonify({'ok': False, 'message': 'internal_error'}), 500
    

@iot_ipv_app.route('/iot-tasks', methods=['GET'])
def iot_ipv_tasks():
    return 'Conf.'