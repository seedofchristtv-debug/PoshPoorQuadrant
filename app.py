import base64
import binascii
import hmac
import os
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "church.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "change-this-development-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

CHURCH_NAME = "SEED OF CHRIST MINISTRY"
HEADQUARTERS = "( ATIMATIM HEAD QUATERS )"
PUBLIC_ENDPOINTS = {
    "login",
    "setup",
    "static",
    "favicon",
    "pwa_manifest",
    "service_worker",
}

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            address TEXT,
            group_name TEXT,
            join_date TEXT,
            birth_date TEXT,
            photo_filename TEXT,
            client_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present', 'absent', 'excused')),
            notes TEXT,
            sync_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(member_id, attendance_date),
            FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    member_columns = {
        row[1] for row in db.execute("PRAGMA table_info(members)").fetchall()
    }
    attendance_columns = {
        row[1] for row in db.execute("PRAGMA table_info(attendance)").fetchall()
    }
    if "client_id" not in member_columns:
        db.execute("ALTER TABLE members ADD COLUMN client_id TEXT")
    if "sync_id" not in attendance_columns:
        db.execute("ALTER TABLE attendance ADD COLUMN sync_id TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_client_id "
        "ON members(client_id) WHERE client_id IS NOT NULL"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_sync_id "
        "ON attendance(sync_id) WHERE sync_id IS NOT NULL"
    )
    db.commit()
    db.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_photo(file):
    if not file or not file.filename:
        return None
    if not allowed_file(file.filename):
        raise ValueError("Please upload a JPG, PNG, GIF, or WEBP image.")
    file.stream.seek(0, os.SEEK_END)
    if file.stream.tell() > 5 * 1024 * 1024:
        raise ValueError("Please choose an image under 5 MB.")
    file.stream.seek(0)
    extension = secure_filename(file.filename).rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{extension}"
    file.save(UPLOAD_DIR / filename)
    return filename


def save_photo_data(data_url):
    if not data_url:
        return None
    if not isinstance(data_url, str) or "," not in data_url:
        raise ValueError("The offline photo could not be read.")
    header, encoded = data_url.split(",", 1)
    mime = header.split(";", 1)[0].replace("data:", "").lower()
    extension_by_mime = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    extension = extension_by_mime.get(mime)
    if extension is None:
        raise ValueError("Offline photos must be JPG, PNG, GIF, or WEBP images.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("The offline photo could not be decoded.") from error
    if len(content) > 5 * 1024 * 1024:
        raise ValueError("Please choose an image under 5 MB.")
    filename = f"{uuid.uuid4().hex}.{extension}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return filename


def get_member(member_id):
    member = get_db().execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    if member is None:
        flash("That member could not be found.", "error")
        return None
    return member


def initials(member):
    return f"{member['first_name'][:1]}{member['last_name'][:1]}".upper()


def admin_exists():
    return get_db().execute("SELECT 1 FROM admins LIMIT 1").fetchone() is not None


def most_recent_sunday(reference=None):
    reference = reference or date.today()
    return reference - timedelta(days=(reference.weekday() + 1) % 7)


def valid_internal_next(path):
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


app.jinja_env.globals["initials"] = initials


def admin_form_token():
    token = session.get("admin_form_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["admin_form_token"] = token
    return token


def validate_admin_form_token():
    expected = session.get("admin_form_token", "")
    submitted = request.form.get("admin_form_token", "")
    if not expected or not hmac.compare_digest(expected, submitted):
        abort(400, description="This form expired. Reload the page and try again.")


app.jinja_env.globals["admin_form_token"] = admin_form_token


@app.context_processor
def inject_today_label():
    return {
        "today_label": datetime.now().strftime("%A, %B %d, %Y"),
        "church_name": CHURCH_NAME,
        "headquarters": HEADQUARTERS,
        "church_slogan": "Together We Can Build",
    }


@app.before_request
def protect_admin_pages():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    admin_id = session.get("admin_id")
    if admin_id and not get_db().execute(
        "SELECT 1 FROM admins WHERE id = ?", (admin_id,)
    ).fetchone():
        session.clear()
    # Keep the root URL healthy for publishing probes and render only the
    # public sign-in/setup screen when no administrator is authenticated.
    if request.endpoint == "dashboard" and "admin_id" not in session:
        return None
    if not admin_exists():
        return redirect(url_for("setup"))
    if "admin_id" not in session:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Sign in required to sync offline records."}), 401
        next_path = request.full_path.rstrip("?")
        return redirect(url_for("login", next=next_path))
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_exists():
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(username) < 3:
            flash("Choose an admin username with at least 3 characters.", "error")
        elif len(password) < 8:
            flash("Choose a password with at least 8 characters.", "error")
        elif password != confirm_password:
            flash("The passwords do not match.", "error")
        else:
            db = get_db()
            cursor = db.execute(
                "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            db.commit()
            session.clear()
            session.permanent = True
            session["admin_id"] = cursor.lastrowid
            session["admin_username"] = username
            flash("Admin account created. Welcome to your ministry workspace.", "success")
            return redirect(url_for("dashboard"))
    return render_template("setup.html", active_page="setup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_exists():
        return redirect(url_for("setup"))
    if session.get("admin_id"):
        return redirect(url_for("dashboard"))
    next_path = valid_internal_next(request.args.get("next", ""))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        admin = get_db().execute(
            "SELECT * FROM admins WHERE username = ?", (username,)
        ).fetchone()
        if admin is None or not check_password_hash(admin["password_hash"], password):
            flash("The username or password is not correct.", "error")
            return render_template("login.html", active_page="login", next_path=next_path)
        session.clear()
        session.permanent = True
        session["admin_id"] = admin["id"]
        session["admin_username"] = admin["username"]
        return redirect(valid_internal_next(request.form.get("next", next_path)))
    return render_template("login.html", active_page="login", next_path=next_path)


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/admins", methods=["GET", "POST"])
def admins():
    db = get_db()
    if request.method == "POST":
        validate_admin_form_token()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(username) < 3 or len(username) > 64:
            flash("Choose a username between 3 and 64 characters.", "error")
        elif len(password) < 8 or len(password) > 128:
            flash("Choose a password between 8 and 128 characters.", "error")
        elif password != confirm_password:
            flash("The passwords do not match.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)),
                )
                db.commit()
                flash(f"Admin account for {username} has been created.", "success")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("That username is already in use.", "error")
        return redirect(url_for("admins"))

    rows = db.execute(
        "SELECT id, username, created_at FROM admins ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return render_template(
        "admins.html",
        active_page="admins",
        admins=rows,
        current_admin_id=session["admin_id"],
    )


@app.post("/admins/<int:admin_id>/delete")
def delete_admin(admin_id):
    validate_admin_form_token()
    if admin_id == session.get("admin_id"):
        flash("You cannot remove the account you are currently using.", "error")
        return redirect(url_for("admins"))

    db = get_db()
    target = db.execute(
        "SELECT id, username FROM admins WHERE id = ?", (admin_id,)
    ).fetchone()
    if target is None:
        flash("That admin account could not be found.", "error")
        return redirect(url_for("admins"))

    admin_count = db.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    if admin_count <= 1:
        flash("The final admin account cannot be removed.", "error")
        return redirect(url_for("admins"))

    db.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
    db.commit()
    flash(f"Admin account for {target['username']} has been removed.", "success")
    return redirect(url_for("admins"))


@app.route("/")
def dashboard():
    if "admin_id" not in session:
        if not admin_exists():
            return render_template("setup.html", active_page="setup")
        return render_template(
            "login.html",
            active_page="login",
            next_path=url_for("dashboard"),
        )

    db = get_db()
    today = date.today().isoformat()
    service_date = most_recent_sunday()
    member_count = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    present_today = db.execute(
        "SELECT COUNT(*) FROM attendance WHERE attendance_date = ? AND status = 'present'",
        (service_date.isoformat(),),
    ).fetchone()[0]
    attendance_count = db.execute(
        "SELECT COUNT(*) FROM attendance WHERE status = 'present'"
    ).fetchone()[0]
    groups_count = db.execute(
        "SELECT COUNT(DISTINCT group_name) FROM members WHERE group_name IS NOT NULL AND group_name != ''"
    ).fetchone()[0]
    recent_members = db.execute(
        "SELECT * FROM members ORDER BY created_at DESC, id DESC LIMIT 5"
    ).fetchall()
    recent_attendance = db.execute(
        """
        SELECT a.*, m.first_name, m.last_name
        FROM attendance a
        JOIN members m ON m.id = a.member_id
        ORDER BY a.attendance_date DESC, a.id DESC
        LIMIT 6
        """
    ).fetchall()
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        today=today,
        service_date=service_date.isoformat(),
        member_count=member_count,
        present_today=present_today,
        attendance_count=attendance_count,
        groups_count=groups_count,
        recent_members=recent_members,
        recent_attendance=recent_attendance,
    )


@app.route("/members")
def members():
    db = get_db()
    query = request.args.get("q", "").strip()
    group = request.args.get("group", "").strip()
    sql = "SELECT * FROM members WHERE 1=1"
    params = []
    if query:
        sql += " AND (first_name LIKE ? OR last_name LIKE ? OR phone LIKE ? OR email LIKE ?)"
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern])
    if group:
        sql += " AND group_name = ?"
        params.append(group)
    sql += " ORDER BY first_name COLLATE NOCASE, last_name COLLATE NOCASE"
    rows = db.execute(sql, params).fetchall()
    groups = db.execute(
        "SELECT DISTINCT group_name FROM members WHERE group_name IS NOT NULL AND group_name != '' ORDER BY group_name"
    ).fetchall()
    return render_template(
        "members.html",
        active_page="members",
        members=rows,
        groups=groups,
        query=query,
        selected_group=group,
    )


@app.route("/members/add", methods=["GET", "POST"])
def add_member():
    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def form_error(message, status):
            if is_ajax:
                return jsonify({"ok": False, "error": message}), status
            flash(message, "error")
            return (
                render_template(
                    "member_form.html", active_page="members", member=None
                ),
                status,
            )

        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not first_name or not last_name:
            return form_error("First name and last name are required.", 400)
        try:
            photo_filename = save_photo(request.files.get("photo"))
        except ValueError as error:
            return form_error(str(error), 400)
        except OSError:
            app.logger.exception("Could not save the new member photo.")
            return form_error(
                "The photo could not be saved. Please try again without the photo or choose another image.",
                500,
            )

        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO members
                (first_name, last_name, phone, email, address, group_name, join_date, birth_date, photo_filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    first_name,
                    last_name,
                    request.form.get("phone", "").strip() or None,
                    request.form.get("email", "").strip() or None,
                    request.form.get("address", "").strip() or None,
                    request.form.get("group_name", "").strip() or None,
                    request.form.get("join_date", "").strip() or None,
                    request.form.get("birth_date", "").strip() or None,
                    photo_filename,
                ),
            )
            db.commit()
        except sqlite3.Error:
            db.rollback()
            if photo_filename:
                try:
                    (UPLOAD_DIR / photo_filename).unlink(missing_ok=True)
                except OSError:
                    app.logger.exception("Could not remove an unlinked member photo.")
            app.logger.exception("Could not add a member to the database.")
            return form_error(
                "The member could not be saved to the database. Your entries are still on this form; please try again.",
                500,
            )

        message = f"{first_name} {last_name} has been added to the church family."
        flash(message, "success")
        if is_ajax:
            return jsonify(
                {"ok": True, "message": message, "redirect_url": url_for("members")}
            )
        return redirect(url_for("members"))
    return render_template("member_form.html", active_page="members", member=None)


@app.route("/members/<int:member_id>")
def member_detail(member_id):
    member = get_member(member_id)
    if member is None:
        return redirect(url_for("members"))
    attendance = get_db().execute(
        """
        SELECT * FROM attendance
        WHERE member_id = ?
        ORDER BY attendance_date DESC
        LIMIT 12
        """,
        (member_id,),
    ).fetchall()
    return render_template(
        "member_detail.html",
        active_page="members",
        member=member,
        attendance=attendance,
    )


@app.route("/members/<int:member_id>/edit", methods=["GET", "POST"])
def edit_member(member_id):
    member = get_member(member_id)
    if member is None:
        return redirect(url_for("members"))
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not first_name or not last_name:
            flash("First name and last name are required.", "error")
            return render_template("member_form.html", active_page="members", member=member)
        photo_filename = member["photo_filename"]
        try:
            new_photo = save_photo(request.files.get("photo"))
            if new_photo:
                if photo_filename:
                    old_path = UPLOAD_DIR / photo_filename
                    if old_path.exists():
                        old_path.unlink()
                photo_filename = new_photo
        except ValueError as error:
            flash(str(error), "error")
            return render_template("member_form.html", active_page="members", member=member)
        db = get_db()
        db.execute(
            """
            UPDATE members SET first_name=?, last_name=?, phone=?, email=?, address=?,
            group_name=?, join_date=?, birth_date=?, photo_filename=? WHERE id=?
            """,
            (
                first_name,
                last_name,
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("address", "").strip(),
                request.form.get("group_name", "").strip(),
                request.form.get("join_date", "").strip(),
                request.form.get("birth_date", "").strip(),
                photo_filename,
                member_id,
            ),
        )
        db.commit()
        flash("Member details updated.", "success")
        return redirect(url_for("member_detail", member_id=member_id))
    return render_template("member_form.html", active_page="members", member=member)


@app.post("/members/<int:member_id>/delete")
def delete_member(member_id):
    member = get_member(member_id)
    if member is None:
        return redirect(url_for("members"))
    db = get_db()
    db.execute("DELETE FROM members WHERE id = ?", (member_id,))
    db.commit()
    if member["photo_filename"]:
        photo_path = UPLOAD_DIR / member["photo_filename"]
        if photo_path.exists():
            photo_path.unlink()
    flash(f"{member['first_name']} {member['last_name']} was removed.", "success")
    return redirect(url_for("members"))


@app.route("/attendance", methods=["GET", "POST"])
def attendance():
    db = get_db()
    selected_date = request.values.get(
        "attendance_date", most_recent_sunday().isoformat()
    )
    can_save = True
    try:
        parsed_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        parsed_date = most_recent_sunday()
        selected_date = parsed_date.isoformat()
    if parsed_date.weekday() != 6:
        flash("Attendance can only be recorded for a Sunday service.", "error")
        parsed_date = most_recent_sunday()
        selected_date = parsed_date.isoformat()
        can_save = False
    if request.method == "POST" and can_save and "save_attendance" in request.form:
        members = db.execute(
            "SELECT id FROM members ORDER BY first_name COLLATE NOCASE, last_name COLLATE NOCASE"
        ).fetchall()
        for member in members:
            status = request.form.get(f"status_{member['id']}", "absent")
            if status not in {"present", "absent", "excused"}:
                status = "absent"
            notes = request.form.get(f"notes_{member['id']}", "").strip()
            db.execute(
                """
                INSERT INTO attendance (member_id, attendance_date, status, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(member_id, attendance_date) DO UPDATE SET
                    status=excluded.status, notes=excluded.notes
                """,
                (member["id"], selected_date, status, notes),
            )
        db.commit()
        flash(f"Sunday service attendance for {selected_date} has been saved.", "success")
    rows = db.execute(
        """
        SELECT m.*, COALESCE(a.status, 'absent') AS attendance_status,
               COALESCE(a.notes, '') AS attendance_notes
        FROM members m
        LEFT JOIN attendance a
          ON a.member_id = m.id AND a.attendance_date = ?
        ORDER BY m.first_name COLLATE NOCASE, m.last_name COLLATE NOCASE
        """,
        (selected_date,),
    ).fetchall()
    counts = {
        "present": sum(row["attendance_status"] == "present" for row in rows),
        "absent": sum(row["attendance_status"] == "absent" for row in rows),
        "excused": sum(row["attendance_status"] == "excused" for row in rows),
    }
    return render_template(
        "attendance.html",
        active_page="attendance",
        attendance_date=selected_date,
        rows=rows,
        counts=counts,
    )


@app.get("/api/offline/bootstrap")
def offline_bootstrap():
    rows = get_db().execute(
        """
        SELECT id, client_id, first_name, last_name, group_name, photo_filename
        FROM members
        ORDER BY first_name COLLATE NOCASE, last_name COLLATE NOCASE
        """
    ).fetchall()
    return jsonify(
        {
            "members": [
                {
                    "server_id": row["id"],
                    "client_id": row["client_id"],
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "group_name": row["group_name"] or "",
                    "photo_filename": row["photo_filename"],
                }
                for row in rows
            ],
            "service_date": most_recent_sunday().isoformat(),
        }
    )


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return send_from_directory(
        app.static_folder,
        "site.webmanifest",
        mimetype="application/manifest+json",
        max_age=0,
    )


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(
        app.static_folder, "sw.js", mimetype="application/javascript"
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.post("/api/offline/sync")
def offline_sync():
    payload = request.get_json(silent=True) or {}
    operations = payload.get("operations")
    if not isinstance(operations, list) or len(operations) > 100:
        return jsonify({"ok": False, "error": "Invalid offline sync payload."}), 400

    db = get_db()
    member_map = {}
    try:
        db.execute("BEGIN")
        member_operations = [
            operation for operation in operations if operation.get("kind") == "member"
        ]
        attendance_operations = [
            operation
            for operation in operations
            if operation.get("kind") == "attendance"
        ]

        for operation in member_operations:
            client_id = str(operation.get("client_id", "")).strip()
            data = operation.get("data") or {}
            first_name = str(data.get("first_name", "")).strip()
            last_name = str(data.get("last_name", "")).strip()
            if not client_id or not first_name or not last_name:
                raise ValueError("An offline member is missing required information.")

            existing = db.execute(
                "SELECT id, photo_filename FROM members WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if existing:
                member_map[client_id] = existing["id"]
                continue

            photo_filename = save_photo_data(data.get("photo_data"))
            cursor = db.execute(
                """
                INSERT INTO members
                (first_name, last_name, phone, email, address, group_name,
                 join_date, birth_date, photo_filename, client_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    first_name,
                    last_name,
                    str(data.get("phone", "")).strip(),
                    str(data.get("email", "")).strip(),
                    str(data.get("address", "")).strip(),
                    str(data.get("group_name", "")).strip(),
                    str(data.get("join_date", "")).strip(),
                    str(data.get("birth_date", "")).strip(),
                    photo_filename,
                    client_id,
                ),
            )
            member_map[client_id] = cursor.lastrowid

        for operation in attendance_operations:
            sync_id = str(operation.get("sync_id", "")).strip()
            if not sync_id:
                raise ValueError("An offline attendance record is missing its sync ID.")
            already_synced = db.execute(
                "SELECT id FROM attendance WHERE sync_id = ?", (sync_id,)
            ).fetchone()
            if already_synced:
                continue

            member_id = operation.get("member_id")
            member_client_id = str(operation.get("member_client_id", "")).strip()
            if member_client_id:
                member_id = member_map.get(member_client_id)
                if member_id is None:
                    matched_member = db.execute(
                        "SELECT id FROM members WHERE client_id = ?",
                        (member_client_id,),
                    ).fetchone()
                    member_id = matched_member["id"] if matched_member else None
            try:
                member_id = int(member_id)
            except (TypeError, ValueError):
                member_id = None
            if member_id is None:
                raise ValueError("An offline attendance record references an unknown member.")

            attendance_date = str(operation.get("attendance_date", "")).strip()
            parsed_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
            if parsed_date.weekday() != 6:
                raise ValueError("Offline attendance can only be recorded for Sundays.")
            status = operation.get("status", "absent")
            if status not in {"present", "absent", "excused"}:
                status = "absent"
            notes = str(operation.get("notes", "")).strip()
            db.execute(
                """
                INSERT INTO attendance
                (member_id, attendance_date, status, notes, sync_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(member_id, attendance_date) DO UPDATE SET
                    status=excluded.status, notes=excluded.notes,
                    sync_id=COALESCE(attendance.sync_id, excluded.sync_id)
                """,
                (member_id, attendance_date, status, notes, sync_id),
            )

        db.commit()
        return jsonify({"ok": True, "member_map": member_map, "synced": len(operations)})
    except (ValueError, sqlite3.Error) as error:
        db.rollback()
        return jsonify({"ok": False, "error": str(error)}), 400


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.errorhandler(413)
def too_large(_error):
    flash("That image is too large. Please choose a photo under 5 MB.", "error")
    return redirect(request.referrer or url_for("members"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)