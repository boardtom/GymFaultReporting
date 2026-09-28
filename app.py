"""
Fault Flag — QR-based equipment fault reporting.

A member scans a QR code fixed to a machine, lands on a pre-filled report
form, submits a fault, and:
  1. an email is sent to the facilities mailbox
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
from datetime import datetime, timezone
from email.message import EmailMessage
import smtplib

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, abort, jsonify, send_file
)
from flask_sqlalchemy import SQLAlchemy
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# --- Database -----------------------------------------------------------
# Render's free web service has no persistent disk. For real use, set
# DATABASE_URL to a Render Postgres connection string (Render provides one
# automatically if you attach a Postgres add-on and reference it here).
# Falls back to a local SQLite file for development.
db_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'faultflag.db')}")
if db_url.startswith("postgres://"):  # SQLAlchemy wants postgresql://
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --- Config from environment --------------------------------------------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
ALERT_TO = os.environ.get("ALERT_TO_EMAIL", "facilities@wellnessinternational.co.uk")
MAIL_FROM = os.environ.get("MAIL_FROM_EMAIL", "faultflag@wellnessinternational.co.uk")
MAIL_SERVER = os.environ.get("MAIL_SERVER")            # e.g. smtp.office365.com
MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")  # used when generating QR links

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

    def to_dict(self):
        return {"id": self.id, "name": self.name, "location": self.location}


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
def send_fault_email(report: Report):
    """Send the fault alert. Logs to console if SMTP isn't configured,
    so the app still works end-to-end in development."""
    subject_prefix = "[URGENT] " if report.severity == "High" else ""
    subject = f"{subject_prefix}Equipment fault — {report.machine.name}"
    body = (
        f"Machine: {report.machine.name} ({report.machine.id})\n"
        f"Location: {report.machine.location}\n"
        f"Fault: {report.fault_type}\n"
        f"Urgency: {report.severity}\n"
        f"Reported: {report.created_at.strftime('%d %b %Y %H:%M')} UTC\n"
        + (f"Details: {report.details}\n" if report.details else "")
        + f"\nView in the fault log: {APP_BASE_URL}{url_for('log_view')}\n"
    )

    if not MAIL_SERVER:
        app.logger.warning("MAIL_SERVER not set — email not sent. Would have sent:\n%s\n%s", subject, body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ALERT_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=10) as smtp:
            if MAIL_USE_TLS:
                smtp.starttls()
            if MAIL_USERNAME and MAIL_PASSWORD:
                smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 — surfaced to the submitter as a flash message
        app.logger.error("Failed to send fault email: %s", exc)
        return False


# --- Routes: member-facing report flow ------------------------------------
@app.route("/")
def index():
    machines = Machine.query.filter_by(active=True).order_by(Machine.location, Machine.name).all()
    return render_template("index.html", machines=machines)


@app.route("/report/<machine_id>", methods=["GET", "POST"])
def report(machine_id):
    machine = Machine.query.get_or_404(machine_id)

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
    rpt = Report.query.get_or_404(report_id)
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
def _machine_report_url(machine_id: str) -> str:
    return f"{APP_BASE_URL}{url_for('report', machine_id=machine_id)}"


def _qr_png_bytes(url: str) -> bytes:
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
    machine = Machine.query.get_or_404(machine_id)
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
        elif Machine.query.get(machine_id):
            flash(f"Machine ID {machine_id} already exists.")
        else:
            db.session.add(Machine(id=machine_id, name=name, location=location))
            db.session.commit()
            flash(f"Added {name}.")
        return redirect(url_for("machines_admin"))

    machines = Machine.query.order_by(Machine.location, Machine.name).all()
    return render_template("machines.html", machines=machines)


@app.route("/machines/<machine_id>/deactivate", methods=["POST"])
@login_required
def deactivate_machine(machine_id):
    machine = Machine.query.get_or_404(machine_id)
    machine.active = False
    db.session.commit()
    return redirect(url_for("machines_admin"))


# --- CLI: seed some starter data ------------------------------------------
@app.cli.command("seed")
def seed():
    """Populate the database with a starter machine list.
    Usage: flask --app app seed
    """
    seed_path = os.path.join(BASE_DIR, "scripts", "machines_seed.json")
    with open(seed_path) as f:
        rows = json.load(f)
    for row in rows:
        if not Machine.query.get(row["id"]):
            db.session.add(Machine(**row))
    db.session.commit()
    print(f"Seeded {len(rows)} machines.")


import click  # noqa: E402


@app.cli.command("import-csv")
@click.argument("csv_path")
def import_csv(csv_path):
    """Bulk-load or update the full machine list from a CSV file.
    CSV columns: id,name,location
    Usage: flask --app app import-csv machines_full.csv
    """
    import csv as csv_mod
    added, updated = 0, 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv_mod.DictReader(f):
            machine_id = row["id"].strip().upper()
            existing = Machine.query.get(machine_id)
            if existing:
                existing.name = row["name"].strip()
                existing.location = row["location"].strip()
                existing.active = True
                updated += 1
            else:
                db.session.add(Machine(
                    id=machine_id,
                    name=row["name"].strip(),
                    location=row["location"].strip(),
                ))
                added += 1
    db.session.commit()
    print(f"Added {added}, updated {updated} machines from {csv_path}.")


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
