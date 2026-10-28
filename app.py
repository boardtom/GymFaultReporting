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
import json
import base64
import functools
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.message import EmailMessage

import click
import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, abort, jsonify, send_file
)
from flask_sqlalchemy import SQLAlchemy

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

db = SQLAlchemy(app)

# --- Config from environment --------------------------------------------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
ALERT_TO = os.environ.get("ALERT_TO_EMAIL", "facilities@wellnessinternational.co.uk")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")

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
SEVERITIES = ["Low", "Medium", "High"]


# --- Models ---------------------------------------------------------------
class Machine(db.Model):
    id = db.Column(db.String(20), primary_key=True)   # e.g. "RW-03"
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(20), db.ForeignKey("machine.id"), nullable=False)
    fault_type = db.Column(db.String(80), nullable=False)
    severity = db.Column(db.String(10), nullable=False, default="Medium")
    details = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(10), nullable=False, default="open")  # open | resolved
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    machine = db.relationship("Machine")


# --- Auth (single shared admin password, no user accounts) ---------------
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("log_view"))
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
    prefix = "[URGENT] " if report.severity == "High" else ""
    subject = f"{prefix}Equipment fault: {report.machine.name} ({report.machine.id})"
    body = (
        f"Machine: {report.machine.name} ({report.machine.id})\n"
        f"Location: {report.machine.location}\n"
        f"Fault: {report.fault_type}\n"
        f"Urgency: {report.severity}\n"
        f"Reported: {_local_time(report.created_at)}\n"
        + (f"Details: {report.details}\n" if report.details else "")
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
        severity = request.form.get("severity", "Medium")
        details = request.form.get("details", "").strip()

        if fault_type not in FAULT_TYPES or severity not in SEVERITIES:
            abort(400)

        rpt = Report(
            machine_id=machine.id,
            fault_type=fault_type,
            severity=severity,
            details=details or None,
        )
        db.session.add(rpt)
        db.session.commit()

        email_sent = send_fault_email(rpt)
        return render_template("confirm.html", machine=machine, email_sent=email_sent)

    return render_template(
        "report.html", machine=machine, fault_types=FAULT_TYPES, severities=SEVERITIES
    )


# --- Routes: admin log ---------------------------------------------------
@app.route("/log")
@login_required
def log_view():
    status_filter = request.args.get("status", "all")
    query = Report.query
    if status_filter in ("open", "resolved"):
        query = query.filter_by(status=status_filter)
    reports = query.order_by(Report.created_at.desc()).all()

    total = Report.query.count()
    open_count = Report.query.filter_by(status="open").count()

    return render_template(
        "log.html",
        reports=reports,
        status_filter=status_filter,
        total=total,
        open_count=open_count,
        resolved_count=total - open_count,
    )


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
    return render_template("machines.html", machines=machines)


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
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
