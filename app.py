import os
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "church.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "see-of-christ-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present', 'absent', 'excused')),
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(member_id, attendance_date),
            FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
        );
        """
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
    extension = secure_filename(file.filename).rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4().hex}.{extension}"
    file.save(UPLOAD_DIR / filename)
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


app.jinja_env.globals["initials"] = initials


@app.context_processor
def inject_today_label():
    return {"today_label": datetime.now().strftime("%A, %B %d, %Y")}


@app.route("/")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    member_count = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    present_today = db.execute(
        "SELECT COUNT(*) FROM attendance WHERE attendance_date = ? AND status = 'present'",
        (today,),
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
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not first_name or not last_name:
            flash("First name and last name are required.", "error")
            return render_template("member_form.html", active_page="members", member=None)
        try:
            photo_filename = save_photo(request.files.get("photo"))
        except ValueError as error:
            flash(str(error), "error")
            return render_template("member_form.html", active_page="members", member=None)
        db = get_db()
        db.execute(
            """
            INSERT INTO members
            (first_name, last_name, phone, email, address, group_name, join_date, birth_date, photo_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        db.commit()
        flash(f"{first_name} {last_name} has been added to the church family.", "success")
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
    selected_date = request.values.get("attendance_date", date.today().isoformat())
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()
    if request.method == "POST":
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
        flash(f"Attendance for {selected_date} has been saved.", "success")
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