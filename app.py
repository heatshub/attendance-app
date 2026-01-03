import os
import secrets
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, g

load_dotenv()

# =========================
# Config
# =========================
DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です")

app = Flask(__name__)
app.secret_key = SECRET_KEY

TZ = ZoneInfo("Asia/Tokyo")

# =========================
# DB helpers
# =========================
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return g.db

def db_execute(sql, params=None, *, fetchone=False, fetchall=False):
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
    return None


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db:
        db.close()

# =========================
# Auth helper
# =========================
def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

# =========================
# Pages
# =========================
@app.route("/")
@login_required
def index():
    return render_template(
        "home.html",
        username=session["display_name"]
    )

@app.route("/timetable")
@login_required
def timetable_view():
    day_str = request.args.get("day") or date.today().isoformat()

    # ★ここに貼る（DB接続確認）
    row = db_execute("""
        SELECT current_database() AS db,
               current_user AS user,
               current_schema() AS schema
    """, fetchone=True)
    print("DB CHECK:", row)

    row2 = db_execute("""
        SELECT to_regclass('public.attendance') AS public_attendance,
               to_regclass('attendance') AS attendance_default
    """, fetchone=True)
    print("TABLE CHECK:", row2)
    # ★ここまで

    # 以下は既存の処理
    d = date.fromisoformat(day_str)
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()

    # …（以下省略）

    # 表示日
    day_str = request.args.get("day") or date.today().isoformat()
    d = date.fromisoformat(day_str)
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()

    # 時刻目盛り（0-24）
    ticks = []
    for m in range(0, 24 * 60 + 1, 30):
        ticks.append({
            "label": f"{m//60:02d}:{m%60:02d}",
            "pos_pct": m / (24 * 60) * 100
        })

    db = get_db()
    with db.cursor() as cur:
        # ユーザー（今は全ユーザー表示）
        cur.execute("SELECT id, display_name FROM users ORDER BY id")
        users = cur.fetchall()

        # 出席ブロック
        cur.execute("""
            SELECT user_id, start_at, end_at
            FROM attendance
            WHERE start_at::date = %s
            ORDER BY start_at ASC
        """, (day_str,))
        rows = cur.fetchall()

    blocks = []
    now = datetime.now(TZ)

    for r in rows:
        start = r["start_at"].astimezone(TZ)
        end = r["end_at"].astimezone(TZ) if r["end_at"] else now

        st_min = start.hour * 60 + start.minute
        et_min = end.hour * 60 + end.minute

        blocks.append({
            "user_id": r["user_id"],
            "top_pct": st_min / (24 * 60) * 100,
            "height_pct": (et_min - st_min) / (24 * 60) * 100,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if r["end_at"] else "now"
        })

    return render_template(
        "timetable.html",
        day=day_str,
        prev_day=prev_day,
        next_day=next_day,
        ticks=ticks,
        users=users,
        blocks=blocks,
        username=session["display_name"]
    )

# =========================
# Attendance actions
# =========================
@app.post("/attendance/start")
@login_required
def attendance_start():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO attendance (user_id, start_at)
            VALUES (%s, NOW())
        """, (session["user_id"],))
    db.commit()
    return redirect(url_for("timetable_view"))

@app.post("/attendance/end")
@login_required
def attendance_end():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            WITH target AS (
              SELECT id
              FROM attendance
              WHERE user_id = %s AND end_at IS NULL
              ORDER BY start_at DESC
              LIMIT 1
            )
            UPDATE attendance a
            SET end_at = NOW()
            FROM target
            WHERE a.id = target.id;
        """, (session["user_id"],))
    db.commit()
    return redirect(url_for("timetable_view"))


# =========================
# Dummy login（LINE連携済みなら削除OK）
# =========================
@app.route("/login")
def login():
    session["user_id"] = 1
    session["display_name"] = "テストユーザー"
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
