import os
import sqlite3
import requests
import secrets
import urllib.parse
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, session, g

# --- 設定類は環境変数から読む ---
DATABASE = "attendance.db"  # とりあえずローカルSQLiteのまま
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")

LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
# デプロイ後の本番URLを使う（後でまた説明します）
LINE_REDIRECT_URI = os.environ.get("LINE_REDIRECT_URI", "http://127.0.0.1:5000/login/line/callback")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY



DATABASE = "attendance.db"
SECRET_KEY = "change-this-in-production"  # デモ用

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


# ---------- DBアクセスの共通処理 ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """初回用：テーブルがなければ作る"""
    db = get_db()
    # users テーブル
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password TEXT,
            line_user_id TEXT UNIQUE
        );
    """)
    # attendance テーブル（★ start_time / end_time 追加）
    db.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            UNIQUE(user_id, date)
        );
    """)
    db.commit()




# ---------- ログイン関連 ----------

@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        db = get_db()
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view_func):
    """ログイン必須のデコレータ"""
    from functools import wraps
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            error = "ユーザー名とパスワードを入力してください"
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, password)
                )
                db.commit()
                # 登録が成功したらログイン画面へ
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                # username が UNIQUE なので、かぶった時にここに来る
                error = "そのユーザー名はすでに使われています"

    return render_template("register.html", error=error)



@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user is None or user["password"] != password:
            error = "ユーザー名かパスワードが違います"
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("calendar_view"))

    return render_template("login.html", error=error)

@app.route("/login/line")
def login_line():
    # CSRF対策用の state をランダム生成してセッションに保存
    state = secrets.token_urlsafe(16)
    session["line_state"] = state

    # 認可エンドポイント（LINE公式）
    auth_endpoint = "https://access.line.me/oauth2/v2.1/authorize"

    params = {
        "response_type": "code",
        "client_id": LINE_CHANNEL_ID,
        "redirect_uri": LINE_REDIRECT_URI,
        "state": state,
        "scope": "profile openid",  # プロフィールとOpenID
    }

    url = auth_endpoint + "?" + urllib.parse.urlencode(params)
    return redirect(url)

@app.route("/login/line/callback")
def login_line_callback():
    # state の確認（CSRF対策）
    state = request.args.get("state")
    code = request.args.get("code")
    stored_state = session.get("line_state")

    if not state or not code or state != stored_state:
        return "不正なログインリクエストです（state不一致）", 400

    # 一度使ったら state は破棄
    session.pop("line_state", None)

    # 1) 認可コードからアクセストークンを取得
    token_endpoint = "https://api.line.me/oauth2/v2.1/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": LINE_REDIRECT_URI,
        "client_id": LINE_CHANNEL_ID,
        "client_secret": LINE_CHANNEL_SECRET,
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    token_res = requests.post(token_endpoint, data=data, headers=headers)
    if token_res.status_code != 200:
        return f"トークン取得に失敗しました: {token_res.text}", 400

    token_json = token_res.json()
    access_token = token_json.get("access_token")
    if not access_token:
        return "アクセストークンが取得できませんでした", 400

    # 2) アクセストークンでプロフィール取得
    profile_endpoint = "https://api.line.me/v2/profile"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    profile_res = requests.get(profile_endpoint, headers=headers)
    if profile_res.status_code != 200:
        return f"プロフィール取得に失敗しました: {profile_res.text}", 400

    profile = profile_res.json()
    line_user_id = profile.get("userId")
    display_name = profile.get("displayName", "NoName")

    if not line_user_id:
        return "LINEユーザーIDが取得できませんでした", 400

    # 3) DB上のユーザーと紐付け（初回なら作成）
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE line_user_id = ?",
        (line_user_id,)
    ).fetchone()

    if user is None:
        # 初回ログイン → 新規登録
        db.execute(
            "INSERT INTO users (username, password, line_user_id) VALUES (?, ?, ?)",
            (display_name, "", line_user_id)
        )
        db.commit()
        user = db.execute(
            "SELECT * FROM users WHERE line_user_id = ?",
            (line_user_id,)
        ).fetchone()

    # セッションにユーザーIDを保存してログイン状態に
    session.clear()
    session["user_id"] = user["id"]

    return redirect(url_for("calendar_view"))



@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- カレンダー & 出席ボタン ----------

@app.route("/")
def index():
    if g.user:
        return redirect(url_for("calendar_view"))
    else:
        return redirect(url_for("login"))


@app.route("/calendar")
@login_required
def calendar_view():
    db = get_db()

    today = date.today()
    year = today.year
    month = today.month

    # 今月のカレンダー（週ごとの日付）を取得
    cal = calendar.Calendar(firstweekday=6)  # 日曜始まり（0:月曜, 6:日曜）
    month_weeks = cal.monthdatescalendar(year, month)  # [[date, date, ...], [...], ...]

    # 今月の出席データを取得（YYYY-MM- でLIKE検索）
    prefix = f"{year:04d}-{month:02d}-"
    rows = db.execute("""
        SELECT a.date, u.username, a.start_time, a.end_time
        FROM attendance a
        JOIN users u ON a.user_id = u.id
        WHERE a.date LIKE ?
    """, (prefix + "%",)).fetchall()

    # date -> [username, ...] に変換
    attendance_map = {}
    for r in rows:
        attendance_map.setdefault(r["date"], []).append({
            "username": r["username"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
        })

    return render_template(
        "calendar.html",
        month_weeks=month_weeks,
        year=year,
        month=month,
        today=today,
        attendance_map=attendance_map
    )


@app.route("/attendance/start", methods=["POST"])
@login_required
def attendance_start():
    db = get_db()
    today_str = date.today().isoformat()
    user_id = g.user["id"]

    # 現在時刻（例: "14:35"）
    now_time = datetime.now().strftime("%H:%M")

    # すでに今日のレコードがあるか確認
    cur = db.execute(
        "SELECT id FROM attendance WHERE user_id = ? AND date = ?",
        (user_id, today_str)
    )
    row = cur.fetchone()

    if row is None:
        # まだ今日の出席記録がない → 新規作成
        db.execute(
            "INSERT INTO attendance (user_id, date, start_time, end_time) VALUES (?, ?, ?, ?)",
            (user_id, today_str, now_time, None)
        )
    else:
        # すでにある → start_time を上書き or 未設定ならセット
        db.execute(
            "UPDATE attendance SET start_time = ? WHERE user_id = ? AND date = ?",
            (now_time, user_id, today_str)
        )

    db.commit()
    return redirect(url_for("calendar_view"))

@app.route("/attendance/end", methods=["POST"])
@login_required
def attendance_end():
    db = get_db()
    today_str = date.today().isoformat()
    user_id = g.user["id"]

    now_time = datetime.now().strftime("%H:%M")

    # 今日のレコードを取得
    cur = db.execute(
        "SELECT id FROM attendance WHERE user_id = ? AND date = ?",
        (user_id, today_str)
    )
    row = cur.fetchone()

    if row is None:
        # まだ開始を押していない場合でも、とりあえずレコードを作る
        db.execute(
            "INSERT INTO attendance (user_id, date, start_time, end_time) VALUES (?, ?, ?, ?)",
            (user_id, today_str, None, now_time)
        )
    else:
        # すでにレコードがある場合は end_time を更新
        db.execute(
            "UPDATE attendance SET end_time = ? WHERE user_id = ? AND date = ?",
            (now_time, user_id, today_str)
        )

    db.commit()
    return redirect(url_for("calendar_view"))




if __name__ == "__main__":
    if not os.path.exists(DATABASE):
        open(DATABASE, "w").close()
    with app.app_context():
        init_db()
    app.run(debug=True, port=5000)  # ← これはローカルで動かすとき専用


