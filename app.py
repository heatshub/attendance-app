import os
import secrets
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, g

# ローカル開発用（Render本番では環境変数をRender側で設定する想定）
load_dotenv()

# =========================
# Config
# =========================
DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_REDIRECT_URI = os.environ.get("LINE_REDIRECT_URI")  # 例: https://xxx.onrender.com/login/line/callback

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です")

# LINEログインを本番同様に試すなら、Render側で必ず設定してください
if not all([LINE_CHANNEL_ID, LINE_CHANNEL_SECRET, LINE_REDIRECT_URI]):
    raise RuntimeError("LINE環境変数（LINE_CHANNEL_ID / LINE_CHANNEL_SECRET / LINE_REDIRECT_URI）が未設定です")

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
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

# =========================
# LINE Login
# =========================
@app.route("/login")
def login():
    """
    未ログイン時にここへ来たら、LINEログインへ飛ばす（本番同様の動作）
    """
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": LINE_CHANNEL_ID,
        "redirect_uri": LINE_REDIRECT_URI,
        "state": state,
        "scope": "profile openid",  # profile で displayName/userId を取る
        "prompt": "consent",
    }
    return redirect(f"{LINE_AUTH_URL}?{requests.compat.urlencode(params)}")

@app.route("/login/line/callback")
def login_line_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if (not code) or (state != session.get("oauth_state")):
        return "認証エラー（code/state）", 400

    # 1) トークン取得
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
        return f"トークン取得失敗: {token}", 400

    # 2) プロフィール取得
    prof_resp = requests.get(
        LINE_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    profile = prof_resp.json()

    line_user_id = profile.get("userId")
    display_name = profile.get("displayName") or "LINE user"

    if not line_user_id:
        return f"プロフィール取得失敗: {profile}", 400

    # 3) public.users に upsert → id を session に入れる（外部キー対策の本命）
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO public.users (line_user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (line_user_id)
            DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING id, display_name;
        """, (line_user_id, display_name))
        u = cur.fetchone()
    db.commit()

    # 4) セッション確定
    session.clear()
    session["user_id"] = u["id"]
    session["display_name"] = u["display_name"]

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
    # 期間を決める（例：今週＝月曜〜今日）
    today = datetime.now(TZ).date()
    week_start = today - timedelta(days=today.weekday())  # Monday start
    month_start = today.replace(day=1)

    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 今日ランキング
        cur.execute("""
            SELECT
              u.id AS user_id,
              u.display_name,
              ROUND(SUM(EXTRACT(EPOCH FROM (COALESCE(a.end_at, NOW()) - a.start_at))) / 3600.0, 2) AS hours
            FROM public.users u
            JOIN public.attendance a ON a.user_id = u.id
            WHERE a.start_at::date = %s
            GROUP BY u.id, u.display_name
            ORDER BY hours DESC, u.id ASC;
        """, (today.isoformat(),))
        rank_today = cur.fetchall()

        # 今週ランキング（月曜〜）
        cur.execute("""
            SELECT
              u.id AS user_id,
              u.display_name,
              ROUND(SUM(EXTRACT(EPOCH FROM (COALESCE(a.end_at, NOW()) - a.start_at))) / 3600.0, 2) AS hours
            FROM public.users u
            JOIN public.attendance a ON a.user_id = u.id
            WHERE a.start_at >= %s::date
              AND a.start_at < (%s::date + INTERVAL '1 day')
            GROUP BY u.id, u.display_name
            ORDER BY hours DESC, u.id ASC;
        """, (week_start.isoformat(), today.isoformat()))
        rank_week = cur.fetchall()

        # 今月ランキング（1日〜）
        cur.execute("""
            SELECT
              u.id AS user_id,
              u.display_name,
              ROUND(SUM(EXTRACT(EPOCH FROM (COALESCE(a.end_at, NOW()) - a.start_at))) / 3600.0, 2) AS hours
            FROM public.users u
            JOIN public.attendance a ON a.user_id = u.id
            WHERE a.start_at >= %s::date
              AND a.start_at < (%s::date + INTERVAL '1 day')
            GROUP BY u.id, u.display_name
            ORDER BY hours DESC, u.id ASC;
        """, (month_start.isoformat(), today.isoformat()))
        rank_month = cur.fetchall()

    return render_template(
        "home.html",
        username=session["display_name"],
        rank_today=rank_today,
        rank_week=rank_week,
        rank_month=rank_month,
        today=today.isoformat(),
        week_start=week_start.isoformat(),
        month_start=month_start.isoformat(),
    )

@app.route("/timetable")
@login_required
def timetable_view():
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
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # ユーザー（今は全ユーザー表示）
        cur.execute("SELECT id, display_name FROM public.users ORDER BY id")
        users = cur.fetchall()

        # 出席ブロック（当日開始分）
        cur.execute("""
            SELECT user_id, start_at, end_at
            FROM public.attendance
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
        height = max(0, et_min - st_min)

        blocks.append({
            "user_id": r["user_id"],
            "top_pct": st_min / (24 * 60) * 100,
            "height_pct": height / (24 * 60) * 100,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if r["end_at"] else "now",
            "is_running": (r["end_at"] is None),
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
            INSERT INTO public.attendance (user_id, start_at)
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
    app.run(debug=True)
