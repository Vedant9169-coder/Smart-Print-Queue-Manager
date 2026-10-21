import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "uploads"
INSTANCE_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
DATABASE = INSTANCE_DIR / "campus_print_manager.db"
ALLOWED_EXTENSIONS = {"pdf"}
MAX_UPLOAD_MB = 20

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("CPM_SECRET_KEY", "change-this-development-secret")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

# Shared in-process lock protects simulation state changes in this single-server demo.
simulation_lock = threading.RLock()


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('student','admin')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS printers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'AVAILABLE'
            CHECK(status IN ('AVAILABLE','BUSY','OFFLINE')),
        supports_color INTEGER NOT NULL DEFAULT 1,
        pages_per_minute INTEGER NOT NULL DEFAULT 20,
        current_job_id INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY(current_job_id) REFERENCES jobs(id)
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        original_filename TEXT NOT NULL,
        stored_filename TEXT,
        page_count INTEGER NOT NULL,
        copies INTEGER NOT NULL DEFAULT 1,
        color INTEGER NOT NULL DEFAULT 0,
        priority TEXT NOT NULL DEFAULT 'NORMAL'
            CHECK(priority IN ('NORMAL','HIGH','URGENT')),
        status TEXT NOT NULL DEFAULT 'WAITING'
            CHECK(status IN ('WAITING','ASSIGNED','PRINTING','COMPLETED','FAILED','CANCELLED')),
        printer_id INTEGER,
        progress_pages INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(printer_id) REFERENCES printers(id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        printer_id INTEGER,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES jobs(id),
        FOREIGN KEY(printer_id) REFERENCES printers(id)
    );
    """)
    # Demo accounts: change these passwords before any real deployment.
    users = [
        ("admin", "Campus Administrator", generate_password_hash("admin123"), "admin", now_text()),
        ("student", "Demo Student", generate_password_hash("student123"), "student", now_text()),
    ]
    for user in users:
        conn.execute("INSERT OR IGNORE INTO users(username,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?)", user)
    for name, color, ppm in [
        ("Printer P1", 1, 25), ("Printer P2", 0, 20), ("Printer P3", 1, 18),
        ("Printer P4", 1, 22), ("Printer P5", 0, 15)
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO printers(name,supports_color,pages_per_minute,created_at) VALUES(?,?,?,?)",
            (name, color, ppm, now_text())
        )
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('scheduler','PRIORITY_AGING')")
    conn.commit()
    conn.close()


def log_event(db, event_type, message, job_id=None, printer_id=None):
    db.execute(
        "INSERT INTO events(job_id,printer_id,event_type,message,created_at) VALUES(?,?,?,?,?)",
        (job_id, printer_id, event_type, message, now_text())
    )


def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute("SELECT id,username,display_name,role FROM users WHERE id=?", (session["user_id"],)).fetchone()


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "max_upload_mb": MAX_UPLOAD_MB}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_pdf_pages(file_path):
    if PdfReader is None:
        return None
    try:
        reader = PdfReader(str(file_path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return None
        count = len(reader.pages)
        return count if count > 0 else None
    except Exception:
        return None


def estimated_seconds(job, printer):
    pages = int(job["page_count"]) * int(job["copies"])
    ppm = max(1, int(printer["pages_per_minute"]))
    # Minimum one simulation second; real printing times vary by device and document.
    return max(1, int(round((pages / ppm) * 60)))


def scheduler_name(db):
    row = db.execute("SELECT value FROM settings WHERE key='scheduler'").fetchone()
    return row["value"] if row else "PRIORITY_AGING"


def choose_next_job(db, printer):
    query = """
        SELECT jobs.*, users.display_name, users.username
        FROM jobs JOIN users ON users.id=jobs.user_id
        WHERE jobs.status='WAITING'
          AND (?=1 OR jobs.color=0)
        """
    jobs = db.execute(query, (int(printer["supports_color"]),)).fetchall()
    if not jobs:
        return None
    policy = scheduler_name(db)
    current = time.time()
    # ISO timestamps are used for storage. Parse safely; missing/invalid dates get age zero.
    def age_seconds(job):
        try:
            created = datetime.fromisoformat(job["created_at"])
            return max(0, (datetime.now() - created).total_seconds())
        except Exception:
            return 0
    if policy == "SJF":
        return sorted(jobs, key=lambda j: (int(j["page_count"]) * int(j["copies"]), j["created_at"]))[0]
    if policy == "FCFS":
        return sorted(jobs, key=lambda j: j["created_at"])[0]
    if policy == "PRIORITY":
        ranks = {"URGENT": 0, "HIGH": 1, "NORMAL": 2}
        return sorted(jobs, key=lambda j: (ranks.get(j["priority"], 2), j["created_at"]))[0]
    # PRIORITY_AGING: waiting increases effective priority; oldest jobs cannot be ignored forever.
    ranks = {"URGENT": 0, "HIGH": 1, "NORMAL": 2}
    def score(j):
        base = ranks.get(j["priority"], 2)
        age_bonus = int(age_seconds(j) // 60)  # one priority level per minute
        return (max(-100, base - age_bonus), j["created_at"])
    return sorted(jobs, key=score)[0]


def assign_waiting_jobs(db):
    printers = db.execute("SELECT * FROM printers WHERE status='AVAILABLE' ORDER BY id").fetchall()
    assigned = []
    for printer in printers:
        job = choose_next_job(db, printer)
        if job is None:
            continue
        changed = db.execute(
            "UPDATE printers SET status='BUSY', current_job_id=? WHERE id=? AND status='AVAILABLE'",
            (job["id"], printer["id"])
        ).rowcount
        if changed != 1:
            continue
        db.execute(
            "UPDATE jobs SET status='PRINTING', printer_id=?, started_at=? WHERE id=? AND status='WAITING'",
            (printer["id"], now_text(), job["id"])
        )
        log_event(db, "JOB_STARTED", f"{job['public_id']} started on {printer['name']}", job["id"], printer["id"])
        assigned.append({"job_id": job["id"], "printer_id": printer["id"]})
    return assigned


def get_dashboard_data(db, user=None):
    is_admin = user and user["role"] == "admin"
    where = "" if is_admin else "WHERE jobs.user_id=?"
    args = () if is_admin else ((user["id"],) if user else ())
    job_rows = db.execute(f"""
        SELECT jobs.*, users.display_name, printers.name AS printer_name
        FROM jobs JOIN users ON users.id=jobs.user_id
        LEFT JOIN printers ON printers.id=jobs.printer_id
        {where}
        ORDER BY jobs.id DESC LIMIT 150
    """, args).fetchall()
    printers = db.execute("SELECT * FROM printers ORDER BY id").fetchall()
    counts = {}
    if is_admin:
        for status in ("WAITING","PRINTING","COMPLETED","FAILED","CANCELLED"):
            counts[status] = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE status=?", (status,)).fetchone()["n"]
    else:
        for status in ("WAITING","PRINTING","COMPLETED","FAILED","CANCELLED"):
            counts[status] = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND status=?", (user["id"], status)).fetchone()["n"]
    events = db.execute("""
        SELECT events.*, jobs.public_id, printers.name AS printer_name
        FROM events LEFT JOIN jobs ON jobs.id=events.job_id
        LEFT JOIN printers ON printers.id=events.printer_id
        ORDER BY events.id DESC LIMIT 12
    """).fetchall() if is_admin else []
    return job_rows, printers, counts, events


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['display_name']}!", "success")
            target = request.args.get("next")
            if user["role"] == "admin":
                return redirect(target or url_for("admin_dashboard"))
            return redirect(target or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    db = get_db()
    jobs, printers, counts, _ = get_dashboard_data(db, user)
    return render_template("student_dashboard.html", jobs=jobs, printers=printers, counts=counts)


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    jobs, printers, counts, events = get_dashboard_data(db, current_user())
    return render_template("admin_dashboard.html", jobs=jobs, printers=printers, counts=counts,
                           events=events, scheduler=scheduler_name(db))


@app.route("/submit", methods=["POST"])
@login_required
def submit_job():
    user = current_user()
    if user["role"] != "student":
        abort(403)
    upload = request.files.get("document")
    if not upload or not upload.filename:
        flash("Choose a PDF document to submit.", "error")
        return redirect(url_for("dashboard"))
    if not allowed_file(upload.filename):
        flash("Only PDF files are accepted.", "error")
        return redirect(url_for("dashboard"))
    try:
        copies = int(request.form.get("copies", "1"))
        if copies < 1 or copies > 20:
            raise ValueError()
    except ValueError:
        flash("Copies must be between 1 and 20.", "error")
        return redirect(url_for("dashboard"))
    color = 1 if request.form.get("color") == "on" else 0
    priority = request.form.get("priority", "NORMAL").upper()
    if priority not in {"NORMAL", "HIGH", "URGENT"}:
        priority = "NORMAL"

    original = secure_filename(upload.filename)[:180] or "document.pdf"
    stored = f"{uuid.uuid4().hex}.pdf"
    target = UPLOAD_DIR / stored
    upload.save(target)
    page_count = get_pdf_pages(target)
    if page_count is None:
        target.unlink(missing_ok=True)
        if PdfReader is None:
            flash("PDF page counting is unavailable. Install requirements.txt and restart the app.", "error")
        else:
            flash("The PDF could not be read. Upload a valid, non-password-protected PDF.", "error")
        return redirect(url_for("dashboard"))
    if page_count > 500:
        target.unlink(missing_ok=True)
        flash("This demo accepts PDFs with up to 500 pages per uploaded file.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    public_id = "CP-" + uuid.uuid4().hex[:8].upper()
    cur = db.execute("""
        INSERT INTO jobs(public_id,user_id,original_filename,stored_filename,page_count,copies,color,priority,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (public_id, user["id"], original, stored, page_count, copies, color, priority, "WAITING", now_text()))
    job_id = cur.lastrowid
    log_event(db, "JOB_SUBMITTED", f"{public_id} submitted by {user['username']}", job_id)
    with simulation_lock:
        assign_waiting_jobs(db)
    db.commit()
    flash(f"Print job {public_id} submitted ({page_count} pages, {copies} copy/copies).", "success")
    return redirect(url_for("dashboard"))


@app.route("/job/<int:job_id>/cancel", methods=["POST"])
@login_required
def cancel_job(job_id):
    user = current_user()
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        abort(404)
    if user["role"] != "admin" and job["user_id"] != user["id"]:
        abort(403)
    if job["status"] not in {"WAITING", "ASSIGNED"}:
        flash("Only waiting or assigned jobs can be cancelled in this simulation.", "error")
        return redirect(url_for("admin_dashboard" if user["role"] == "admin" else "dashboard"))
    db.execute("UPDATE jobs SET status='CANCELLED',completed_at=? WHERE id=?", (now_text(), job_id))
    log_event(db, "JOB_CANCELLED", f"{job['public_id']} cancelled", job_id, job["printer_id"])
    db.commit()
    flash(f"Job {job['public_id']} cancelled.", "success")
    return redirect(url_for("admin_dashboard" if user["role"] == "admin" else "dashboard"))


@app.route("/admin/scheduler", methods=["POST"])
@admin_required
def set_scheduler():
    policy = request.form.get("scheduler", "PRIORITY_AGING")
    if policy not in {"FCFS", "SJF", "PRIORITY", "PRIORITY_AGING"}:
        flash("Invalid scheduling policy.", "error")
    else:
        db = get_db()
        db.execute("INSERT INTO settings(key,value) VALUES('scheduler',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (policy,))
        log_event(db, "SCHEDULER_CHANGED", f"Scheduling policy changed to {policy}")
        with simulation_lock:
            assign_waiting_jobs(db)
        db.commit()
        flash(f"Scheduling policy set to {policy.replace('_', ' ')}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/printer/add", methods=["POST"])
@admin_required
def add_printer():
    name = request.form.get("name", "").strip()
    try:
        ppm = int(request.form.get("pages_per_minute", "20"))
        if not name or len(name) > 60 or ppm < 1 or ppm > 120:
            raise ValueError()
    except ValueError:
        flash("Enter a printer name and speed between 1 and 120 pages/minute.", "error")
        return redirect(url_for("admin_dashboard"))
    color = 1 if request.form.get("supports_color") == "on" else 0
    db = get_db()
    try:
        db.execute("INSERT INTO printers(name,supports_color,pages_per_minute,created_at) VALUES(?,?,?,?)",
                   (name, color, ppm, now_text()))
        log_event(db, "PRINTER_ADDED", f"{name} added")
        db.commit()
        flash(f"{name} added.", "success")
    except sqlite3.IntegrityError:
        flash("A printer with that name already exists.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/printer/<int:printer_id>/toggle", methods=["POST"])
@admin_required
def toggle_printer(printer_id):
    db = get_db()
    with simulation_lock:
        printer = db.execute("SELECT * FROM printers WHERE id=?", (printer_id,)).fetchone()
        if not printer:
            abort(404)
        if printer["status"] == "OFFLINE":
            db.execute("UPDATE printers SET status='AVAILABLE',current_job_id=NULL WHERE id=?", (printer_id,))
            log_event(db, "PRINTER_RESTORED", f"{printer['name']} restored", printer_id=printer_id)
        else:
            # If busy, fail the current job and return its progress to the waiting queue for a safe retry.
            if printer["current_job_id"]:
                job = db.execute("SELECT * FROM jobs WHERE id=?", (printer["current_job_id"],)).fetchone()
                if job and job["status"] == "PRINTING":
                    db.execute("UPDATE jobs SET status='WAITING',printer_id=NULL,progress_pages=0,started_at=NULL,error_message=? WHERE id=?",
                               ("Printer went offline; job returned to queue.", job["id"]))
                    log_event(db, "JOB_REQUEUED", f"{job['public_id']} requeued after printer failure", job["id"], printer_id)
            db.execute("UPDATE printers SET status='OFFLINE',current_job_id=NULL WHERE id=?", (printer_id,))
            log_event(db, "PRINTER_OFFLINE", f"{printer['name']} marked offline", printer_id=printer_id)
        assign_waiting_jobs(db)
        db.commit()
    flash("Printer status updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/job/<int:job_id>/retry", methods=["POST"])
@admin_required
def retry_job(job_id):
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        abort(404)
    if job["status"] != "FAILED":
        flash("Only failed jobs can be retried.", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute("UPDATE jobs SET status='WAITING',printer_id=NULL,progress_pages=0,started_at=NULL,completed_at=NULL,error_message=NULL WHERE id=?", (job_id,))
    log_event(db, "JOB_RETRY", f"{job['public_id']} returned to queue", job_id)
    with simulation_lock:
        assign_waiting_jobs(db)
    db.commit()
    flash("Job returned to the queue.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/api/tick", methods=["POST"])
@login_required
def tick_simulation():
    """Advance simulated printing by one second. The frontend calls this every second."""
    db = get_db()
    with simulation_lock:
        printers = db.execute("SELECT * FROM printers WHERE status='BUSY' AND current_job_id IS NOT NULL").fetchall()
        for printer in printers:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (printer["current_job_id"],)).fetchone()
            if not job or job["status"] != "PRINTING":
                db.execute("UPDATE printers SET status='AVAILABLE',current_job_id=NULL WHERE id=?", (printer["id"],))
                continue
            # Use fractional progress in the DB so even long jobs visibly advance in the simulation.
            progress = min(job["page_count"] * job["copies"], job["progress_pages"] + max(1, int(printer["pages_per_minute"] / 60)))
            total_pages = int(job["page_count"]) * int(job["copies"])
            if progress >= total_pages:
                db.execute("UPDATE jobs SET progress_pages=?,status='COMPLETED',completed_at=?,error_message=NULL WHERE id=?",
                           (total_pages, now_text(), job["id"]))
                db.execute("UPDATE printers SET status='AVAILABLE',current_job_id=NULL WHERE id=?", (printer["id"],))
                log_event(db, "JOB_COMPLETED", f"{job['public_id']} completed", job["id"], printer["id"])
            else:
                db.execute("UPDATE jobs SET progress_pages=? WHERE id=?", (progress, job["id"]))
        assign_waiting_jobs(db)
        db.commit()
    return jsonify({"ok": True, "updated_at": now_text()})


@app.route("/api/status")
@login_required
def api_status():
    user = current_user()
    db = get_db()
    jobs, printers, counts, _ = get_dashboard_data(db, user)
    if user["role"] != "admin":
        # queue position counts only eligible waiting jobs in front under the selected policy is intentionally omitted;
        # the table shows status and progress without pretending to calculate a universal queue position.
        pass
    return jsonify({
        "counts": counts,
        "printers": [{
            "id": p["id"], "name": p["name"], "status": p["status"],
            "supports_color": bool(p["supports_color"]), "pages_per_minute": p["pages_per_minute"]
        } for p in printers],
        "jobs": [{
            "id": j["id"], "public_id": j["public_id"], "status": j["status"],
            "progress_pages": j["progress_pages"], "page_count": j["page_count"],
            "copies": j["copies"], "printer_name": j["printer_name"]
        } for j in jobs]
    })


@app.route("/admin/analytics")
@admin_required
def analytics():
    db = get_db()
    rows = db.execute("""
        SELECT status, COUNT(*) AS count FROM jobs GROUP BY status
    """).fetchall()
    policy_rows = db.execute("SELECT value FROM settings WHERE key='scheduler'").fetchone()
    completed = db.execute("""
        SELECT created_at, started_at, completed_at FROM jobs
        WHERE status='COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL
    """).fetchall()
    wait_times = []
    turnaround_times = []
    for row in completed:
        try:
            created = datetime.fromisoformat(row["created_at"])
            started = datetime.fromisoformat(row["started_at"])
            ended = datetime.fromisoformat(row["completed_at"])
            wait_times.append(max(0, (started-created).total_seconds()))
            turnaround_times.append(max(0, (ended-created).total_seconds()))
        except Exception:
            continue
    printer_total = db.execute("SELECT COUNT(*) AS n FROM printers").fetchone()["n"]
    printer_busy = db.execute("SELECT COUNT(*) AS n FROM printers WHERE status='BUSY'").fetchone()["n"]
    return render_template("analytics.html", rows=rows, wait_times=wait_times, turnaround_times=turnaround_times,
                           scheduler=policy_rows["value"] if policy_rows else "PRIORITY_AGING",
                           printer_total=printer_total, printer_busy=printer_busy)


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have permission to access this page."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="The requested page was not found."), 404


@app.errorhandler(413)
def too_large(error):
    flash(f"Upload is too large. Maximum size is {MAX_UPLOAD_MB} MB.", "error")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, threaded=True)
