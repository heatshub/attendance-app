import os
import secrets
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, g
)

load_dotenv()

# =========================
# Config
# =========================

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_REDIRECT_URI = os.environ.get(
    "LINE_REDIRECT_URI",
    "http://127.0.0.1:5000/login/line/callback"
)
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


# =========================
# Utility
# =========================

def now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")

def time_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# =========================
# DB helpers
# =========================

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db:
        db.close()

def db_execute(sql, params=None, *, fetchone=False, fetchall=False):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(sql, params or ())
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()


# =========================
# Auth helpers
# =========================

@app.before_request
def load_logged_in_user():
    g.user = None
    user_id = session.get("user_id")
    if not user_id:
        return

    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, line_user_id, display_name FROM users WHERE id = %s",
                (user_id,)
            )
            g.user = cur.fetchone()
    except Exception:
        app.logger.exception("Failed to load logged-in user")
        session.clear()
        return redirect(url_for("login"))


def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# =========================
# LINE Login
# =========================

LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_PROFILE_URL = "https://api.line.me/v2/profile"

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/login/line")
def login_line():
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": LINE_CHANNEL_ID,
        "redirect_uri": LINE_REDIRECT_URI,
        "state": state,
        "scope": "profile openid",
        "prompt": "consent",
    }
    return redirect(f"{LINE_AUTH_URL}?{requests.compat.urlencode(params)}")

@app.route("/login/line/callback")
def login_line_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or state != session.get("oauth_state"):
        return "認証エラー", 400

    token = requests.post(
        LINE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LINE_REDIRECT_URI,
            "client_id": LINE_CHANNEL_ID,
            "client_secret": LINE_CHANNEL_SECRET,
        }
    ).json()

    access_token = token.get("access_token")
    profile = requests.get(
        LINE_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"}
    ).json()

    line_user_id = profile["userId"]
    display_name = profile.get("displayName", "LINE user")

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO users (line_user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (line_user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING id
        """, (line_user_id, display_name))
        user = cur.fetchone()
    db.commit()

    session.clear()
    session["user_id"] = user["id"]
    session["display_name"] = display_name

    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# Pages
# =========================

@app.route("/")
@login_required
def index():
    return render_template(
        "home.html",
        username=session.get("display_name"),
        today=date.today().isoformat()
    )


# =========================
# Attendance actions（★正）
# =========================

@app.post("/attendance/start")
@login_required
def attendance_start():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO attendance (user_id, date, start_time, end_time)
            VALUES (%s, %s, %s, NULL)
            ON CONFLICT (user_id, date)
            DO UPDATE SET
              start_time = CASE
                WHEN attendance.end_time IS NULL THEN attendance.start_time
                ELSE EXCLUDED.start_time
              END,
              end_time = NULL
        """, (
            session["user_id"],
            date.today(),
            datetime.now().time()
        ))
    db.commit()
    return redirect(url_for("timetable_view"))

@app.post("/attendance/end")
@login_required
def attendance_end():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            UPDATE attendance
            SET end_time = %s
            WHERE user_id = %s AND date = %s AND end_time IS NULL
        """, (
            datetime.now().time(),
            session["user_id"],
            date.today()
        ))
    db.commit()
    return redirect(url_for("timetable_view"))

# 互換（古いテンプレート用）
@app.post("/work/start")
@login_required
def work_start():
    return attendance_start()

@app.post("/work/end")
@login_required
def work_end():
    return attendance_end()



# =========================
# Timetable（時刻表）
# =========================

@app.route("/timetable")
@login_required
def timetable_view():
    day = date.today()

    row = db_execute("""
        SELECT start_time, end_time
        FROM attendance
        WHERE user_id = %s AND date = %s
        LIMIT 1
    """, (session["user_id"], day), fetchone=True)

    active_start = row["start_time"].strftime("%H:%M") if row and row["start_time"] else None
    active_end = (
        row["end_time"].strftime("%H:%M")
        if row and row["end_time"]
        else None
    )

    # 時刻目盛り
    start_min = 9 * 60
    end_min = 18 * 60
    total = end_min - start_min

    ticks = []
    for m in range(start_min, end_min + 1, 30):
        ticks.append({
            "label": f"{m//60:02d}:{m%60:02d}",
            "pos_pct": (m - start_min) / total * 100
        })

    return render_template(
        "timetable.html",
        day=day.isoformat(),
        ticks=ticks,
        users=[{
            "id": session["user_id"],
            "display_name": session["display_name"]
        }],
        blocks=[],
        username=session["display_name"],
        active_start=active_start,
        active_end=active_end,
        start_min=start_min,
        end_min=end_min
    )


# =========================
# Main
# =========================

if __name__ == "__main__":
    app.run(debug=True, port=5000)
