"""
Fault Flag: QR-based equipment fault reporting.

A member scans a QR code fixed to a machine, lands on a pre-filled report
form, submits a fault, and:
  1. an email alert is sent (via Resend's web API, or SMTP as a fallback)
  2. the report is logged in the database with a status (open/resolved)

Run locally:
    pip install -r requirements.txt
    flask --app app run --debug

Deploy: see README.md for Render setup.
"""
import os
import io
import csv
import hmac
import time
import json
import hashlib
import base64
import functools
import smtplib
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from email.message import EmailMessage

import click
import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, abort, jsonify, send_file, Response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import false, inspect, text
from sqlalchemy.exc import IntegrityError

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:  # tz database missing: fall back to UTC
    UK_TZ = timezone.utc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# --- Database -----------------------------------------------------------
# Render's free web service has no persistent disk. For real use, set
# DATABASE_URL to the Render Postgres Internal Database URL.
# Falls back to a local SQLite file for development.
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
db_url = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'faultflag.db')}",
)
if db_url.startswith("postgres://"):  # SQLAlchemy wants postgresql://
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Serverless Postgres (e.g. Neon) closes idle connections. Test each connection
# before use and recycle old ones so the first request after a quiet spell works.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 240}

db = SQLAlchemy(app)

# --- Config from environment --------------------------------------------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
ALERT_TO = os.environ.get("ALERT_TO_EMAIL", "facilities@wellnessinternational.co.uk")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
IS_PRODUCTION = APP_BASE_URL.startswith("https://")

# --- Security settings ---------------------------------------------------
# Render sits behind a proxy: trust one hop so the real client address and
# the https scheme are seen (needed for the sign-in attempt limit).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Without a SECRET_KEY the app would sign sessions with a default that is in the
# source code. Derive a private one from values that are not public instead.
if not os.environ.get("SECRET_KEY"):
    if ADMIN_PASSWORD != "changeme":
        app.config["SECRET_KEY"] = hashlib.sha256(
            f"faultflag|{ADMIN_PASSWORD}|{db_url}".encode("utf-8")).hexdigest()
    if IS_PRODUCTION:
        app.logger.warning("SECRET_KEY is not set. Set it in the Render environment.")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",          # also stops cross-site form posts
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if session.get("is_admin"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

# Email option 1 (recommended on Render's free tier): Resend web API.
# Free Render services block SMTP ports, but HTTPS calls are fine.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# Until a domain is verified in Resend, the sender must be onboarding@resend.dev
# and Resend only delivers to the address the Resend account was created with.
RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "Fault Flag <onboarding@resend.dev>")

# Email option 2: plain SMTP (works on paid Render plans or other hosts).
MAIL_FROM = os.environ.get("MAIL_FROM_EMAIL", "faultflag@wellnessinternational.co.uk")
MAIL_SERVER = os.environ.get("MAIL_SERVER")
MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"

FAULT_TYPES = [
    "Won't power on",
    "Unusual noise",
    "Visible damage",
    "Display / console error",
    "Missing part",
    "Other",
]
# Severity is set by Wellness International at triage, not by the member.
SEVERITIES = ["Low", "Medium", "High"]
UNASSESSED = "Unassessed"
# What the member is asked instead: an observation, not a rating.
SAFE_ANSWERS = ["Yes", "No", "Not sure"]
YES_NO = ["Yes", "No"]


# --- Models ---------------------------------------------------------------
class Machine(db.Model):
    id = db.Column(db.String(20), primary_key=True)   # e.g. "RW-03"
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True)
    # Service provider's asset number for the machine (e.g. Gym Gear "23521").
    asset_no = db.Column(db.String(40), nullable=True)
    # Out of service: members see a "do not use" warning when they scan the code.
    out_of_service = db.Column(db.Boolean, nullable=False, default=False)
    out_of_service_note = db.Column(db.String(200), nullable=True)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(20), db.ForeignKey("machine.id"), nullable=False)
    fault_type = db.Column(db.String(80), nullable=False)
    # Set by WI in the fault log. "Unassessed" until someone has triaged it.
    severity = db.Column(db.String(10), nullable=False, default=UNASSESSED)
    # Legacy: older reports hold the member's answer to "Is the equipment safe to use?". No longer asked.
    safe_to_use = db.Column(db.String(10), nullable=True)
    # "Have you placed a red out of order hang tag on the machine?" (Yes | No), answered by the member.
    hang_tag = db.Column(db.String(3), nullable=True)
    details = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(10), nullable=False, default="open")  # open | resolved
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    machine = db.relationship("Machine")


class ServiceRecord(db.Model):
    """One line of a service provider's report for one machine."""
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(20), db.ForeignKey("machine.id"), nullable=False)
    service_date = db.Column(db.Date, nullable=False)
    provider = db.Column(db.String(80), nullable=True)
    job_ref = db.Column(db.String(40), nullable=True)
    engineer = db.Column(db.String(80), nullable=True)
    result = db.Column(db.String(10), nullable=False)       # Pass | Fail
    severity = db.Column(db.String(20), nullable=True)      # Urgent | Recommended
    notes = db.Column(db.Text, nullable=True)

    machine = db.relationship("Machine")


class AppliedUpdate(db.Model):
    """Marks a one-off data update as done, so it is never applied twice."""
    key = db.Column(db.String(80), primary_key=True)
    applied_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class OutOfServiceLog(db.Model):
    """One period when a machine was out of service, and how it ended."""
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(20), db.ForeignKey("machine.id"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False)      # UTC
    reason = db.Column(db.Text, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)         # UTC; empty while still out
    work_done = db.Column(db.Text, nullable=True)
    carried_out_by = db.Column(db.String(80), nullable=True)
    job_ref = db.Column(db.String(60), nullable=True)

    machine = db.relationship("Machine")


def latest_service_by_machine():
    """Return {machine_id: most recent ServiceRecord}."""
    latest = {}
    for rec in ServiceRecord.query.order_by(ServiceRecord.service_date, ServiceRecord.id).all():
        latest[rec.machine_id] = rec
    return latest


# --- Time helpers: stored in UTC, shown and entered in UK time ---------------
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_uk(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UK_TZ)


def parse_uk_local(value):
    """Turn a datetime-local form value (UK time) into naive UTC, or None."""
    try:
        local = datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return local.replace(tzinfo=UK_TZ).astimezone(timezone.utc).replace(tzinfo=None)


@app.template_filter("uk")
def uk_filter(dt, fmt="%d %b %Y %H:%M"):
    local = to_uk(dt)
    return local.strftime(fmt) if local else ""


def duration_text(start, end=None):
    if not start:
        return ""
    secs = max(int(((end or utcnow()) - start).total_seconds()), 0)
    if secs < 3600:
        n = max(secs // 60, 1)
        return f"{n} min{'s' if n != 1 else ''}"
    if secs < 48 * 3600:
        n = secs // 3600
        return f"{n} hr{'s' if n != 1 else ''}"
    return f"{secs // 86400} days"


app.jinja_env.globals["duration_text"] = duration_text


# --- Auth (single shared admin password, no user accounts) ---------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def new_reports_query():
    """Reports nobody at WI has reviewed yet: still open, severity not set."""
    return Report.query.filter(Report.status == "open", Report.severity.notin_(SEVERITIES))


@app.context_processor
def inject_admin_counts():
    if session.get("is_admin"):
        return {
            "new_report_count": new_reports_query().count(),
            "oos_count": Machine.query.filter_by(out_of_service=True).count(),
        }
    return {"new_report_count": 0, "oos_count": 0}


# Limit wrong password guesses: 5 failures from one address locks it for 10 minutes.
_failed_logins = defaultdict(list)
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SECONDS = 600


def _login_blocked(ip):
    now = time.time()
    recent = [t for t in _failed_logins[ip] if now - t < LOGIN_WINDOW_SECONDS]
    _failed_logins[ip] = recent
    if len(_failed_logins) > 500:       # forget old addresses
        for key in [k for k, v in _failed_logins.items() if not v]:
            del _failed_logins[key]
    return len(recent) >= LOGIN_MAX_FAILS


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_blocked(ip):
            flash("Too many incorrect attempts. Please wait 10 minutes and try again.")
            return render_template("login.html"), 429
        supplied = (request.form.get("password") or "").encode("utf-8")
        if hmac.compare_digest(supplied, ADMIN_PASSWORD.encode("utf-8")):
            _failed_logins.pop(ip, None)
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            # Take WI straight to anything that still needs reviewing.
            if new_reports_query().count():
                session["new_prompt"] = True
                return redirect(url_for("log_view", status="new"))
            return redirect(request.args.get("next") or url_for("log_view"))
        _failed_logins[ip].append(time.time())
        flash("Incorrect password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Email ------------------------------------------------------------
def _local_time(dt):
    """Report times are stored in UTC; show them in UK time in emails."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UK_TZ).strftime("%d %b %Y %H:%M")


def build_fault_email(report):
    """Return (subject, plain-text body) for a fault alert."""
    subject = f"Equipment fault: {report.machine.name} ({report.machine.id})"
    body = (
        f"Machine: {report.machine.name} ({report.machine.id})\n"
        f"Location: {report.machine.location}\n"
        f"Fault: {report.fault_type}\n"
        f"Red out of order hang tag placed by member: {report.hang_tag or 'Not asked'}\n"
        f"Reported: {_local_time(report.created_at)}\n"
        + (f"Details: {report.details}\n" if report.details else "")
        + "\nPlease review this report in the fault log and mark the machine out of\n"
          "service if required.\n"
        + f"\nView in the fault log: {APP_BASE_URL}{url_for('log_view')}\n"
    )
    return subject, body


def _send_via_resend(subject, body):
    payload = json.dumps({
        "from": RESEND_FROM,
        "to": [ALERT_TO],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "fault-flag/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return 200 <= resp.status < 300


def _send_via_smtp(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ALERT_TO
    msg.set_content(body)
    with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=10) as smtp:
        if MAIL_USE_TLS:
            smtp.starttls()
        if MAIL_USERNAME and MAIL_PASSWORD:
            smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        smtp.send_message(msg)
    return True


def send_fault_email(report):
    """Send the fault alert. Returns True if sent. If no email option is
    configured, the email is written to the log instead, so the app still
    works end to end in development."""
    subject, body = build_fault_email(report)

    try:
        if RESEND_API_KEY:
            return _send_via_resend(subject, body)
        if MAIL_SERVER:
            return _send_via_smtp(subject, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        app.logger.error("Resend rejected the email (%s): %s", exc.code, detail)
        return False
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Failed to send fault email: %s", exc)
        return False

    app.logger.warning("No email provider configured. Would have sent:\n%s\n%s", subject, body)
    return False


# --- Routes: member-facing report flow ------------------------------------
@app.route("/")
def index():
    machines = Machine.query.filter_by(active=True).order_by(Machine.location, Machine.name).all()
    return render_template("index.html", machines=machines)


@app.route("/report/<machine_id>", methods=["GET", "POST"])
def report(machine_id):
    machine = db.get_or_404(Machine, machine_id)

    if request.method == "POST":
        fault_type = request.form.get("fault_type", "Other")
        hang_tag = request.form.get("hang_tag", "")
        details = request.form.get("details", "").strip()

        if fault_type not in FAULT_TYPES or hang_tag not in YES_NO:
            abort(400)

        rpt = Report(
            machine_id=machine.id,
            fault_type=fault_type,
            severity=UNASSESSED,
            hang_tag=hang_tag,
            details=details or None,
        )
        db.session.add(rpt)
        db.session.commit()

        email_sent = send_fault_email(rpt)
        return render_template("confirm.html", machine=machine, email_sent=email_sent)

    return render_template(
        "report.html", machine=machine, fault_types=FAULT_TYPES, yes_no=YES_NO
    )


# --- Routes: admin log ---------------------------------------------------
REPEAT_DAYS = 90


def repeat_counts():
    """Machines with two or more reports in the last 90 days: {machine_id: count}."""
    cutoff = utcnow() - timedelta(days=REPEAT_DAYS)
    rows = (db.session.query(Report.machine_id, db.func.count(Report.id))
            .filter(Report.created_at >= cutoff).group_by(Report.machine_id).all())
    return {machine_id: n for machine_id, n in rows if n >= 2}


def reports_for_filter(status_filter):
    query = Report.query
    if status_filter in ("open", "resolved"):
        query = query.filter_by(status=status_filter)
    elif status_filter == "new":
        query = new_reports_query()
    elif status_filter == "repeat":
        ids = list(repeat_counts())
        cutoff = utcnow() - timedelta(days=REPEAT_DAYS)
        query = query.filter(Report.machine_id.in_(ids) if ids else false(),
                             Report.created_at >= cutoff)
    reports = query.order_by(Report.created_at.desc()).all()
    if status_filter == "new":      # machines the member has already tagged first
        reports.sort(key=lambda r: r.hang_tag != "Yes")
    if status_filter == "repeat":   # group by machine
        reports.sort(key=lambda r: r.machine_id)
    return reports


@app.route("/log")
@login_required
def log_view():
    status_filter = request.args.get("status", "all")
    reports = reports_for_filter(status_filter)

    total = Report.query.count()
    open_count = Report.query.filter_by(status="open").count()
    repeats = repeat_counts()

    return render_template(
        "log.html",
        reports=reports,
        status_filter=status_filter,
        total=total,
        open_count=open_count,
        resolved_count=total - open_count,
        severities=SEVERITIES,
        unassessed=UNASSESSED,
        repeats=repeats,
        repeat_days=REPEAT_DAYS,
        show_new_prompt=session.pop("new_prompt", False),
    )


def csv_safe(value):
    """Stop spreadsheet programs treating member-typed text as a formula."""
    if value is None:
        return ""
    text_value = str(value)
    return "'" + text_value if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return Response(
        "\ufeff" + buf.getvalue(),         # BOM so Excel reads accents correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/log/export.csv")
@login_required
def export_log():
    status_filter = request.args.get("status", "all")
    rows = []
    for r in reports_for_filter(status_filter):
        rows.append([
            to_uk(r.created_at).strftime("%Y-%m-%d %H:%M"), r.machine.id, r.machine.name,
            r.machine.location, r.fault_type, r.details, r.safe_to_use or "", r.hang_tag or "",
            r.severity, r.status,
            to_uk(r.resolved_at).strftime("%Y-%m-%d %H:%M") if r.resolved_at else "",
        ])
    return csv_response(
        f"fault-log-{to_uk(utcnow()).strftime('%Y-%m-%d')}.csv",
        ["Reported (UK time)", "Machine ID", "Machine", "Location", "Fault", "Details",
         "Safe to use (older reports only)", "Hang tag placed", "Severity", "Status", "Resolved (UK time)"],
        rows)


@app.route("/log/<int:report_id>/toggle", methods=["POST"])
@login_required
def toggle_report(report_id):
    rpt = db.get_or_404(Report, report_id)
    if rpt.status == "open":
        rpt.status = "resolved"
        rpt.resolved_at = datetime.now(timezone.utc)
    else:
        rpt.status = "open"
        rpt.resolved_at = None
    db.session.commit()
    if request.headers.get("Accept") == "application/json":
        return jsonify({"id": rpt.id, "status": rpt.status})
    return redirect(url_for("log_view", status=request.args.get("status", "all")))


@app.route("/log/<int:report_id>/severity", methods=["POST"])
@login_required
def set_report_severity(report_id):
    """WI sets (or clears) the severity of a report at triage."""
    rpt = db.get_or_404(Report, report_id)
    severity = request.form.get("severity", "")
    if severity not in SEVERITIES + [UNASSESSED]:
        abort(400)
    rpt.severity = severity
    db.session.commit()
    return redirect(url_for("log_view", status=request.args.get("status", "all")))


@app.route("/log/<int:report_id>/delete", methods=["POST"])
@login_required
def delete_report(report_id):
    """Remove one fault report for good (for test entries and mistakes).
    Real faults should be marked resolved instead, so the history is kept."""
    rpt = db.get_or_404(Report, report_id)
    name = rpt.machine.name
    db.session.delete(rpt)
    db.session.commit()
    flash(f"Deleted the fault report for {name}.")
    return redirect(url_for("log_view", status=request.args.get("status", "all")))


# --- Routes: QR codes ----------------------------------------------------
def _machine_report_url(machine_id):
    return f"{APP_BASE_URL}{url_for('report', machine_id=machine_id)}"


def _qr_png_bytes(url):
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.route("/qr")
@login_required
def qr_index():
    machines = Machine.query.filter_by(active=True).order_by(Machine.location, Machine.name).all()
    qr_data = {}
    for m in machines:
        url = _machine_report_url(m.id)
        png = _qr_png_bytes(url)
        qr_data[m.id] = {
            "url": url,
            "data_uri": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        }
    return render_template("qr_index.html", machines=machines, qr_data=qr_data)


@app.route("/qr/<machine_id>.png")
@login_required
def qr_png(machine_id):
    machine = db.get_or_404(Machine, machine_id)
    png = _qr_png_bytes(_machine_report_url(machine.id))
    return send_file(io.BytesIO(png), mimetype="image/png",
                     download_name=f"{machine.id}-qr.png")


# --- Admin: manage machine list -------------------------------------------
@app.route("/machines", methods=["GET", "POST"])
@login_required
def machines_admin():
    if request.method == "POST":
        machine_id = request.form.get("id", "").strip().upper()
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        if not (machine_id and name and location):
            flash("All fields are required.")
        elif db.session.get(Machine, machine_id):
            flash(f"Machine ID {machine_id} already exists.")
        else:
            db.session.add(Machine(id=machine_id, name=name, location=location))
            db.session.commit()
            flash(f"Added {name}.")
        return redirect(url_for("machines_admin"))

    machines = Machine.query.order_by(Machine.location, Machine.name).all()
    report_counts = dict(
        db.session.query(Report.machine_id, db.func.count(Report.id))
        .group_by(Report.machine_id).all()
    )
    return render_template("machines.html", machines=machines, report_counts=report_counts,
                           service=latest_service_by_machine())


def import_machine_rows(rows):
    """Add new machines and update existing ones (matched by ID).
    Each row needs id, name and location. Returns (added, updated, skipped)."""
    added = updated = skipped = 0
    for row in rows:
        machine_id = (row.get("id") or "").strip().upper()
        name = (row.get("name") or "").strip()
        location = (row.get("location") or "").strip()
        if not (machine_id and name and location) or len(machine_id) > 20:
            skipped += 1
            continue
        existing = db.session.get(Machine, machine_id)
        if existing:
            existing.name = name
            existing.location = location
            existing.active = True
            updated += 1
        else:
            db.session.add(Machine(id=machine_id, name=name, location=location))
            added += 1
    db.session.commit()
    return added, updated, skipped


@app.route("/machines/import", methods=["POST"])
@login_required
def import_machines():
    import csv as csv_mod
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash("Choose a CSV file first.")
        return redirect(url_for("machines_admin"))
    try:
        text = upload.read().decode("utf-8-sig")
        reader = csv_mod.DictReader(io.StringIO(text))
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        if not {"id", "name", "location"} <= headers:
            flash("The CSV needs three columns named id, name and location.")
            return redirect(url_for("machines_admin"))
        rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in reader]
    except Exception:  # noqa: BLE001
        flash("That file couldn't be read as a CSV.")
        return redirect(url_for("machines_admin"))

    added, updated, skipped = import_machine_rows(rows)
    msg = f"Import finished: {added} added, {updated} updated"
    if skipped:
        msg += f", {skipped} skipped (missing a value or ID over 20 characters)"
    flash(msg + ".")
    return redirect(url_for("machines_admin"))


@app.route("/machines/<machine_id>/deactivate", methods=["POST"])
@login_required
def deactivate_machine(machine_id):
    machine = db.get_or_404(Machine, machine_id)
    machine.active = False
    db.session.commit()
    return redirect(url_for("machines_admin"))


@app.route("/machines/<machine_id>/activate", methods=["POST"])
@login_required
def activate_machine(machine_id):
    machine = db.get_or_404(Machine, machine_id)
    machine.active = True
    db.session.commit()
    return redirect(url_for("machines_admin"))


@app.route("/machines/<machine_id>/delete", methods=["POST"])
@login_required
def delete_machine(machine_id):
    """Remove a machine entirely, along with any fault reports logged
    against it. Use Deactivate instead to keep the history."""
    machine = db.get_or_404(Machine, machine_id)
    removed = Report.query.filter_by(machine_id=machine.id).delete()
    ServiceRecord.query.filter_by(machine_id=machine.id).delete()
    OutOfServiceLog.query.filter_by(machine_id=machine.id).delete()
    db.session.delete(machine)
    db.session.commit()
    msg = f"Deleted {machine.name}"
    if removed:
        msg += f" and {removed} fault report{'s' if removed != 1 else ''}"
    flash(msg + ".")
    return redirect(url_for("machines_admin"))


def _back_to_log_or_machines():
    if request.form.get("from") == "log":
        return url_for("log_view", status=request.form.get("status", "all"))
    return url_for("machines_admin")


def _safe_next(default):
    """A redirect target from ?next=, only ever a path on this site."""
    target = request.values.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


def _open_oos_entry(machine_id):
    return (OutOfServiceLog.query.filter_by(machine_id=machine_id, ended_at=None)
            .order_by(OutOfServiceLog.started_at.desc()).first())


@app.route("/machines/<machine_id>/out-of-service", methods=["POST"])
@login_required
def set_out_of_service(machine_id):
    """Show members a 'do not use' warning on this machine's report page."""
    machine = db.get_or_404(Machine, machine_id)
    note = (request.form.get("note") or "").strip()[:200] or None
    reason = note
    report_id = request.form.get("report_id", type=int)
    if not reason and report_id:
        rpt = db.session.get(Report, report_id)
        if rpt and rpt.machine_id == machine.id:
            reason = rpt.fault_type + (f": {rpt.details}" if rpt.details else "")
    if not _open_oos_entry(machine.id):
        where = "fault log" if request.form.get("from") == "log" else "Machines page"
        db.session.add(OutOfServiceLog(
            machine_id=machine.id, started_at=utcnow(),
            reason=reason or f"Taken out of service from the {where}."))
    machine.out_of_service = True
    machine.out_of_service_note = note
    db.session.commit()
    flash(f"{machine.name} is now marked out of service.")
    return redirect(_back_to_log_or_machines())


@app.route("/machines/<machine_id>/back-in-service", methods=["GET", "POST"])
@login_required
def set_back_in_service(machine_id):
    """Record what was done, by whom and when, then return the machine to use."""
    machine = db.get_or_404(Machine, machine_id)
    if not machine.out_of_service:
        flash(f"{machine.name} is already in service.")
        return redirect(_safe_next(url_for("out_of_service_view")))

    entry = _open_oos_entry(machine.id)
    open_faults = Report.query.filter_by(machine_id=machine.id, status="open").count()
    form = {
        "ended_at": to_uk(utcnow()).strftime("%Y-%m-%dT%H:%M"),
        "work_done": "", "carried_out_by": "", "job_ref": "", "resolve_faults": "1",
    }
    error = None

    if request.method == "POST":
        for key in ("ended_at", "work_done", "carried_out_by", "job_ref"):
            form[key] = (request.form.get(key) or "").strip()
        form["resolve_faults"] = "1" if request.form.get("resolve_faults") else ""
        ended = parse_uk_local(form["ended_at"])
        if ended is None:
            error = "Enter the date and time the machine went back into use."
        elif not form["work_done"]:
            error = "Say what was done, for example the repair carried out."
        elif not form["carried_out_by"]:
            error = "Say who carried out the work."
        elif entry and ended < entry.started_at:
            error = ("That is before the machine went out of service ("
                     + uk_filter(entry.started_at) + ").")
        elif ended > utcnow() + timedelta(minutes=5):
            error = "That date and time is in the future."

        if not error:
            if not entry:           # should not happen, but keep the record complete
                entry = OutOfServiceLog(machine_id=machine.id, started_at=ended,
                                        reason="Not recorded")
                db.session.add(entry)
            entry.ended_at = ended
            entry.work_done = form["work_done"][:2000]
            entry.carried_out_by = form["carried_out_by"][:80]
            entry.job_ref = form["job_ref"][:60] or None
            machine.out_of_service = False
            machine.out_of_service_note = None
            resolved = 0
            if form["resolve_faults"]:
                for rpt in Report.query.filter_by(machine_id=machine.id, status="open"):
                    rpt.status = "resolved"
                    rpt.resolved_at = utcnow()
                    resolved += 1
            db.session.commit()
            msg = f"{machine.name} is back in service."
            if resolved:
                msg += f" {resolved} open fault{'s' if resolved != 1 else ''} marked resolved."
            flash(msg)
            return redirect(_safe_next(url_for("out_of_service_view")))

    return render_template(
        "return_to_service.html", machine=machine, entry=entry, form=form,
        error=error, open_faults=open_faults, next_url=_safe_next(""),
    ), (400 if error else 200)


@app.route("/out-of-service")
@login_required
def out_of_service_view():
    current = [e for e in OutOfServiceLog.query.filter_by(ended_at=None)
               .order_by(OutOfServiceLog.started_at).all() if e.machine.out_of_service]
    history = (OutOfServiceLog.query.filter(OutOfServiceLog.ended_at.isnot(None))
               .order_by(OutOfServiceLog.ended_at.desc()).all())
    return render_template("out_of_service.html", current=current, history=history)


@app.route("/out-of-service/export.csv")
@login_required
def export_out_of_service():
    rows = []
    for e in OutOfServiceLog.query.order_by(OutOfServiceLog.started_at.desc()).all():
        rows.append([
            e.machine.id, e.machine.name, e.machine.location,
            to_uk(e.started_at).strftime("%Y-%m-%d %H:%M"), e.reason,
            to_uk(e.ended_at).strftime("%Y-%m-%d %H:%M") if e.ended_at else "Still out of service",
            duration_text(e.started_at, e.ended_at), e.work_done, e.carried_out_by, e.job_ref,
        ])
    return csv_response(
        f"out-of-service-{to_uk(utcnow()).strftime('%Y-%m-%d')}.csv",
        ["Machine ID", "Machine", "Location", "Out of service (UK time)", "Reason",
         "Back in service (UK time)", "Time out of service", "Work done", "Carried out by",
         "Job reference"],
        rows)


# --- Admin: edit a machine -------------------------------------------------
@app.route("/machines/<machine_id>/edit", methods=["GET", "POST"])
@login_required
def edit_machine(machine_id):
    machine = db.get_or_404(Machine, machine_id)
    locations = sorted({m.location for m in Machine.query.all()})
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        location = (request.form.get("location") or "").strip()
        asset_no = (request.form.get("asset_no") or "").strip()
        if not (name and location):
            flash("Name and location are both required.")
        elif len(name) > 120 or len(location) > 120 or len(asset_no) > 40:
            flash("One of those entries is too long.")
        else:
            machine.name, machine.location = name, location
            machine.asset_no = asset_no or None
            db.session.commit()
            flash(f"Saved changes to {machine.name}.")
            return redirect(url_for("machines_admin"))
    return render_template("machine_edit.html", machine=machine, locations=locations)


# --- Admin: service records ---------------------------------------------------
@app.route("/service")
@login_required
def service_view():
    result_filter = request.args.get("result", "all")
    latest = latest_service_by_machine()
    machines = Machine.query.filter_by(active=True).order_by(Machine.location, Machine.name).all()
    rows = [(m, latest.get(m.id)) for m in machines]
    counts = {
        "pass": sum(1 for _, r in rows if r and r.result == "Pass"),
        "fail": sum(1 for _, r in rows if r and r.result == "Fail"),
        "none": sum(1 for _, r in rows if r is None),
    }
    if result_filter == "fail":
        rows = [x for x in rows if x[1] and x[1].result == "Fail"]
    elif result_filter == "pass":
        rows = [x for x in rows if x[1] and x[1].result == "Pass"]
    elif result_filter == "none":
        rows = [x for x in rows if x[1] is None]
    return render_template("service.html", rows=rows, counts=counts, result_filter=result_filter)


# --- One-off data updates -------------------------------------------------------
def apply_service_report(path):
    """Load a service provider's report (JSON in scripts/) once.

    For each machine listed it can update the name and asset number, add a
    service record, log a failed item as an open fault and mark a machine out
    of service. Nothing is emailed. A marker row stops it running twice, so
    later edits made in the admin pages are never overwritten.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    key = data["key"]
    if db.session.get(AppliedUpdate, key):
        return False
    if Machine.query.count() == 0:      # empty database: nothing to update yet
        return False

    svc_date = date.fromisoformat(data["service_date"])
    found_at = datetime(svc_date.year, svc_date.month, svc_date.day, 12, 0)
    label = f"{data['provider']} {data['job_type'].lower()} {svc_date.strftime('%d/%m/%Y')} (job {data['job_ref']})"

    db.session.add(AppliedUpdate(key=key))
    for row in data["machines"]:
        machine = db.session.get(Machine, row["id"])
        if not machine:
            continue
        if row.get("name"):
            machine.name = row["name"]
        if row.get("asset_no"):
            machine.asset_no = row["asset_no"]
        db.session.add(ServiceRecord(
            machine_id=machine.id, service_date=svc_date, provider=data["provider"],
            job_ref=data["job_ref"], engineer=data.get("engineer"),
            result=row["result"], severity=row.get("severity"), notes=row.get("notes"),
        ))
        if row["result"] == "Fail":
            db.session.add(Report(
                machine_id=machine.id, fault_type="Service finding",
                severity="High" if row.get("severity") == "Urgent" else "Medium",
                details=f"{label}: {row.get('notes') or 'Failed'}",
                created_at=found_at,
            ))
        if row.get("out_of_service"):
            machine.out_of_service = True
            machine.out_of_service_note = row.get("out_of_service_note")
            db.session.add(OutOfServiceLog(
                machine_id=machine.id, started_at=found_at,
                reason=f"{data['provider']} service finding: {row.get('notes') or 'Failed'}"))
    try:
        db.session.commit()
    except IntegrityError:      # another worker applied it at the same moment
        db.session.rollback()
        return False
    return True


def upgrade_database():
    """Create new tables and add columns introduced since the first release."""
    db.create_all()
    existing = {c["name"] for c in inspect(db.engine).get_columns("machine")}
    additions = {
        "asset_no": "VARCHAR(40)",
        "out_of_service": "BOOLEAN NOT NULL DEFAULT FALSE",
        "out_of_service_note": "VARCHAR(200)",
    }
    for column, ddl in additions.items():
        if column not in existing:
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE machine ADD COLUMN {column} {ddl}"))
            except Exception as exc:  # noqa: BLE001  (e.g. another worker added it first)
                app.logger.warning("Could not add machine.%s: %s", column, exc)

    report_cols = {c["name"] for c in inspect(db.engine).get_columns("report")}
    if "safe_to_use" not in report_cols:
        try:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE report ADD COLUMN safe_to_use VARCHAR(10)"))
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("Could not add report.safe_to_use: %s", exc)

    if "hang_tag" not in report_cols:
        try:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE report ADD COLUMN hang_tag VARCHAR(3)"))
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("Could not add report.hang_tag: %s", exc)

    # Machines already out of service before the stoppage history existed.
    try:
        for machine in Machine.query.filter_by(out_of_service=True):
            if not _open_oos_entry(machine.id):
                rec = (ServiceRecord.query.filter_by(machine_id=machine.id, result="Fail")
                       .order_by(ServiceRecord.service_date.desc()).first())
                if rec:
                    started = datetime(rec.service_date.year, rec.service_date.month,
                                       rec.service_date.day, 12, 0)
                    reason = f"{rec.provider} service finding: {rec.notes or 'Failed'}"
                else:
                    started, reason = utcnow(), machine.out_of_service_note or "Taken out of service"
                db.session.add(OutOfServiceLog(machine_id=machine.id, started_at=started, reason=reason))
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        app.logger.error("Could not backfill out of service history: %s", exc)

    updates_dir = os.path.join(BASE_DIR, "scripts")
    for filename in sorted(os.listdir(updates_dir)):
        if filename.startswith("service_") and filename.endswith(".json"):
            try:
                if apply_service_report(os.path.join(updates_dir, filename)):
                    app.logger.warning("Applied service report %s", filename)
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                app.logger.error("Could not apply %s: %s", filename, exc)

    # One-off rename of the "Fitbox" area to "Functional Area" (12 Nov 2026). Machine IDs
    # (FBX-...) are printed on the QR labels, so they are deliberately left unchanged.
    rename_key = "rename-fitbox-to-functional-area-2026-11-12"
    try:
        if not db.session.get(AppliedUpdate, rename_key) and Machine.query.count() > 0:
            db.session.add(AppliedUpdate(key=rename_key))
            for m in Machine.query.filter(Machine.location == "Fitbox"):
                m.location = "Functional Area"
            for model, col in ((Report, "details"), (ServiceRecord, "notes"),
                               (OutOfServiceLog, "reason"), (OutOfServiceLog, "work_done"),
                               (Machine, "out_of_service_note")):
                attr = getattr(model, col)
                for row in model.query.filter(attr.like("%Fitbox%")):
                    setattr(row, col, getattr(row, col).replace("Fitbox", "Functional Area"))
            db.session.commit()
            app.logger.warning("Renamed Fitbox to Functional Area")
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        app.logger.error("Could not rename Fitbox: %s", exc)


# --- CLI ------------------------------------------------------------------
@app.cli.command("seed")
def seed():
    """Load the starter machine list. Usage: flask --app app seed"""
    seed_path = os.path.join(BASE_DIR, "scripts", "machines_seed.json")
    with open(seed_path) as f:
        rows = json.load(f)
    for row in rows:
        if not db.session.get(Machine, row["id"]):
            db.session.add(Machine(**row))
    db.session.commit()
    print(f"Seeded {len(rows)} machines.")


@app.cli.command("import-csv")
@click.argument("csv_path")
def import_csv(csv_path):
    """Bulk-load or update machines from a CSV with columns id,name,location."""
    import csv as csv_mod
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        added, updated, skipped = import_machine_rows(csv_mod.DictReader(f))
    print(f"Added {added}, updated {updated}, skipped {skipped} from {csv_path}.")


with app.app_context():
    upgrade_database()

if __name__ == "__main__":
    app.run(debug=True)
