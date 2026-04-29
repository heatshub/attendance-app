import os
import secrets
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from functools import wraps

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, g

load_dotenv()

# =========================
# Config
# =========================

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_REDIRECT_URI = os.environ.get("LINE_REDIRECT_URI")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です")

app = Flask(__name__)
app.secret_key = SECRET_KEY

TZ = ZoneInfo("Asia/Tokyo")

LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_PROFILE_URL = "https://api.line.me/v2/profile"


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


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db:
        db.close()


# =========================
# Auth helper
# =========================

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# =========================
# Time helpers
# =========================

def jst_now():
    return datetime.now(TZ)


def jst_day_range(day_obj):
    start = datetime.combine(day_obj, time.min, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


# =========================
# LINE Login
# =========================

@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/login/line")
def login_line():
    if not LINE_CHANNEL_ID or not LINE_CHANNEL_SECRET or not LINE_REDIRECT_URI:
        return "LINEログイン用の環境変数が未設定です", 500

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

    token_resp = requests.post(
        LINE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LINE_REDIRECT_URI,
            "client_id": LINE_CHANNEL_ID,
            "client_secret": LINE_CHANNEL_SECRET,
        },
        timeout=10,
    )

    token = token_resp.json()
    access_token = token.get("access_token")

    if not access_token:
        return f"トークン取得に失敗しました: {token}", 400

    prof_resp = requests.get(
        LINE_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )

    profile = prof_resp.json()

    line_user_id = profile.get("userId")
    display_name = profile.get("displayName") or "LINE user"

    if not line_user_id:
        return f"プロフィール取得に失敗しました: {profile}", 400

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO public.users (line_user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (line_user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING id, display_name;
        """, (line_user_id, display_name))
        user = cur.fetchone()

    db.commit()

    session.clear()
    session["user_id"] = user["id"]
    session["display_name"] = user["display_name"]

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# Chart helper
# =========================

def get_monthly_chart_data(user_id, target_day):
    month_start = target_day.replace(day=1)

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1, day=1)

    labels = []
    hours_by_day = {}

    d = month_start
    while d < next_month:
        labels.append(f"{d.month}/{d.day}")
        hours_by_day[d.isoformat()] = 0.0
        d += timedelta(days=1)

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT start_at, end_at
            FROM public.attendance
            WHERE user_id = %s
              AND start_at >= %s
              AND start_at < %s
            ORDER BY start_at ASC;
        """, (
            user_id,
            datetime.combine(month_start, time.min, tzinfo=TZ),
            datetime.combine(next_month, time.min, tzinfo=TZ),
        ))
        rows = cur.fetchall()

    now = jst_now()

    for r in rows:
        start = r["start_at"].astimezone(TZ)
        end = r["end_at"].astimezone(TZ) if r["end_at"] else now

        key = start.date().isoformat()
        if key in hours_by_day:
            seconds = max(0, (end - start).total_seconds())
            hours_by_day[key] += seconds / 3600

    values = [round(v, 2) for v in hours_by_day.values()]
    max_value = max(values) if values else 0

    return labels, values, max_value


# =========================
# Pages
# =========================

@app.route("/")
@login_required
def index():
    today = jst_now().date()

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS count
            FROM public.attendance
            WHERE user_id = %s AND end_at IS NULL;
        """, (session["user_id"],))
        is_working = cur.fetchone()["count"] > 0

        cur.execute("""
            SELECT start_at
            FROM public.attendance
            WHERE user_id = %s AND end_at IS NULL
            ORDER BY start_at DESC
            LIMIT 1;
        """, (session["user_id"],))
        running = cur.fetchone()

    started_at = None
    if running:
        started_at = running["start_at"].astimezone(TZ).strftime("%H:%M")

    return render_template(
        "home.html",
        username=session["display_name"],
        today=today.isoformat(),
        is_working=is_working,
        started_at=started_at,
    )


@app.route("/timetable")
@login_required
def timetable_view():
    day_str = request.args.get("day") or jst_now().date().isoformat()
    day = date.fromisoformat(day_str)

    prev_day = (day - timedelta(days=1)).isoformat()
    next_day = (day + timedelta(days=1)).isoformat()

    day_start, day_end = jst_day_range(day)

    ticks = []
    for m in range(0, 24 * 60 + 1, 30):
        ticks.append({
            "label": f"{m // 60:02d}:{m % 60:02d}",
            "pos_pct": m / (24 * 60) * 100
        })

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT id, display_name
            FROM public.users
            ORDER BY id ASC;
        """)
        users = cur.fetchall()

        cur.execute("""
            SELECT user_id, start_at, end_at
            FROM public.attendance
            WHERE start_at >= %s AND start_at < %s
            ORDER BY start_at ASC;
        """, (day_start, day_end))
        rows = cur.fetchall()

    blocks = []
    now = jst_now()

    for r in rows:
        start = r["start_at"].astimezone(TZ)
        end = r["end_at"].astimezone(TZ) if r["end_at"] else now

        st_min = start.hour * 60 + start.minute
        et_min = end.hour * 60 + end.minute
        height = max(0, et_min - st_min)

        blocks.append({
            "user_id": r["user_id"],
            "top_pct": st_min / (24 * 60) * 100,
            "height_pct": height / (24 * 60) * 100,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if r["end_at"] else "now",
            "is_running": r["end_at"] is None,
        })

    chart_labels, chart_values, chart_max = get_monthly_chart_data(
        session["user_id"],
        day,
    )

    return render_template(
        "timetable.html",
        day=day_str,
        prev_day=prev_day,
        next_day=next_day,
        ticks=ticks,
        users=users,
        blocks=blocks,
        username=session["display_name"],
        chart_labels=chart_labels,
        chart_values=chart_values,
        chart_max=chart_max,
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
            SELECT id
            FROM public.attendance
            WHERE user_id = %s AND end_at IS NULL
            ORDER BY start_at DESC
            LIMIT 1;
        """, (session["user_id"],))
        running = cur.fetchone()

        if not running:
            cur.execute("""
                INSERT INTO public.attendance (user_id, start_at)
                VALUES (%s, NOW());
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
              FROM public.attendance
              WHERE user_id = %s AND end_at IS NULL
              ORDER BY start_at DESC
              LIMIT 1
            )
            UPDATE public.attendance a
            SET end_at = NOW()
            FROM target
            WHERE a.id = target.id;
        """, (session["user_id"],))

    db.commit()
    return redirect(url_for("timetable_view"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)