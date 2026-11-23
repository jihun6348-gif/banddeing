"""반띵 — 1인가구 동네 소분 공동구매

로컬 실행:  uv run app.py          →  http://127.0.0.1:5000
배포 실행:  gunicorn -c gunicorn.conf.py app:app   (README '배포' 참고)
"""
import hashlib
import hmac
import io
import json
import logging
import logging.config
import logging.handlers
import math
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import click

import db

# ---------------------------------------------------------------- 설정 (환경 변수)
BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ
PRODUCTION = ENV.get("APP_ENV", "development") == "production"
DATA_DIR = ENV.get("DATA_DIR", BASE)
os.makedirs(DATA_DIR, exist_ok=True)


def _secret_key():
    key = ENV.get("SECRET_KEY")
    if key:
        return key
    if PRODUCTION:
        raise RuntimeError("SECRET_KEY 환경 변수를 설정해야 해요 (배포 환경).")
    path = os.path.join(DATA_DIR, ".secret_key")  # 로컬 개발용: 한 번 만들고 재사용
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    DATABASE=ENV.get("DATABASE_PATH", os.path.join(DATA_DIR, "banddeing.db")),
    UPLOAD_PUBLIC=os.path.join(DATA_DIR, "uploads", "public"),
    UPLOAD_PRIVATE=os.path.join(DATA_DIR, "uploads", "private"),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    DEMO_MODE=ENV.get("DEMO_MODE", "0") == "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    PREFERRED_URL_SCHEME="https" if PRODUCTION else "http",
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7) if PRODUCTION else None,
)
if PRODUCTION:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
os.makedirs(app.config["UPLOAD_PUBLIC"], exist_ok=True)
os.makedirs(app.config["UPLOAD_PRIVATE"], exist_ok=True)
db.init_app(app)

# ---------------------------------------------------------------- 로깅 (Logger → Handler ← Formatter)
# 수업 자료의 Django settings.py LOGGING 과 같은 구조를 logging.config.dictConfig 로 그대로 쓴다.
#   Formatter : 로그 한 줄의 모양을 정한다 ('detail')
#   Handler   : 받은 로그를 파일(logs/banddeing.log)과 콘솔에 남긴다
#   Logger    : 'banddeing' 이름으로 로그를 만들어 핸들러에게 넘긴다
# 로그 레벨: DEBUG(10) < INFO(20) < WARNING(30) < ERROR(40) < CRITICAL(50)
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_LEVEL = ENV.get("LOG_LEVEL", "INFO").upper()
LOG_RETENTION_DAYS = 90
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "detail": {
            "format": "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
            "datefmt": "%Y/%m/%d %H:%M:%S",
        },
    },
    "handlers": {
        "file": {
            "level": "INFO",
            # FileHandler 와 같지만 매일 자정에 새 파일(banddeing.log.2026-11-22 …)로 넘기고 90개(약 3개월)만 남긴다.
            # 로그에는 접속 IP 가 들어가므로 개인정보처리방침의 '접속 기록 3개월 보관'에 맞춘다.
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": os.path.join(LOG_DIR, "banddeing.log"),
            "when": "midnight",
            "backupCount": LOG_RETENTION_DAYS,
            "encoding": "utf-8",
            "formatter": "detail",
        },
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "detail",
        },
    },
    "loggers": {
        "banddeing": {
            "handlers": ["file", "console"],
            "propagate": False,
            "level": LOG_LEVEL,
        },
    },
})
log = logging.getLogger("banddeing")

SITE = {
    "name": "반띵",
    # PUBLIC_URL 이 없으면 Render 가 자동으로 넣어 주는 RENDER_EXTERNAL_URL 을 쓴다
    "url": (ENV.get("PUBLIC_URL") or ENV.get("RENDER_EXTERNAL_URL", "")).rstrip("/"),
    "operator": ENV.get("OPERATOR_NAME", "반띵 운영팀"),
    "contact": ENV.get("CONTACT_EMAIL", ""),
    "privacy_officer": ENV.get("PRIVACY_OFFICER", ENV.get("OPERATOR_NAME", "반띵 운영팀")),
    "hosting": ENV.get("HOSTING_PROVIDER", "Render Services, Inc. (미국)"),
    "effective": ENV.get("POLICY_EFFECTIVE_DATE", "2026-11-01"),
}
KAKAO_KEY = ENV.get("KAKAO_REST_API_KEY", "")
KAKAO_SECRET = ENV.get("KAKAO_CLIENT_SECRET", "")
SMTP = {k: ENV.get(f"SMTP_{k.upper()}", "") for k in ("host", "port", "user", "password")}
MAIL_FROM = ENV.get("MAIL_FROM", SMTP["user"])
MAIL_ENABLED = bool(SMTP["host"] and MAIL_FROM)
ADMIN_EMAILS = {e.strip().lower() for e in ENV.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin_user(u):
    """운영자 = ADMIN_EMAILS 에 있는 이메일 + 그 이메일로 인증번호 확인(또는 서버 명령)을 마친 계정.
    가입만 해서는 운영자가 되지 않는다 (남이 운영자 이메일로 먼저 가입해도 권한을 못 얻음)."""
    email = (u["email"] or "").lower()
    return bool(email and email in ADMIN_EMAILS and (u["admin_verified_email"] or "").lower() == email)


def sync_admin_flags():
    """서버를 켤 때 is_admin 열을 지금 ADMIN_EMAILS 기준으로 맞춘다 (목록에서 빠진 사람은 바로 해제)."""
    conn = db.connect(app.config["DATABASE"])
    try:
        rows = conn.execute("SELECT id, email, admin_verified_email, is_admin FROM users").fetchall()
        for u in rows:
            want = 1 if is_admin_user(u) else 0
            if u["is_admin"] != want:
                conn.execute("UPDATE users SET is_admin=? WHERE id=?", (want, u["id"]))
    finally:
        conn.close()


sync_admin_flags()

if PRODUCTION and not SITE["url"]:
    log.error("PUBLIC_URL 이 비어 있어요. 비밀번호 재설정 메일을 보내지 않아요. 실제 주소를 넣어 주세요.")


def public_base():
    """메일·공유 링크에 넣을 사이트 주소. 요청의 Host 헤더는 위조할 수 있으므로 배포에서는 PUBLIC_URL 만 쓴다."""
    if SITE["url"]:
        return SITE["url"]
    if PRODUCTION:
        return None
    return request.host_url.rstrip("/")  # 로컬 개발용


DEMO_LAT, DEMO_LNG = 37.5898, 127.0579  # 회기역 (데모 모드 전용)
CATEGORIES = [("veg", "채소·과일", "🥬"), ("egg", "계란·유제품", "🥚"), ("meat", "육류", "🥩"),
              ("daily", "생필품", "🧻"), ("etc", "기타", "📦")]
CAT = {k: {"name": n, "icon": i} for k, n, i in CATEGORIES}
STATUS = {
    "recruiting": ("모집 중", "orange"),
    "deposit": ("입금 대기", "blue"),
    "buying": ("구매 진행", "blue"),
    "pickup": ("픽업 대기", "green"),
    "done": ("거래 완료", "gray"),
    "failed": ("모집 실패", "gray"),
    "canceled": ("운영 정책으로 취소", "gray"),
}
CLOSED = ("done", "failed", "canceled")
GOOD_TAGS = ["시간 약속을 잘 지켜요", "상품 상태가 좋아요", "소분이 정확해요", "응답이 빨라요"]
BAD_TAGS = ["늦게 왔어요", "수량이 달랐어요", "연락이 안 됐어요"]
DEPOSIT_HOURS = 24        # 칸이 다 찬 뒤 이 시간 안에 입금하지 않으면 자리가 풀린다
PURCHASE_HOURS = 24       # 모두 입금한 뒤 이 시간 안에 방장이 구매를 확정하지 않으면 공구 자동 취소
AUTO_RECEIVE_HOURS = 48   # 픽업 시간이 이만큼 지나면 수령 확인을 안 누른 참여자도 자동으로 수령 처리
MAX_PICKUP_KM = 5          # 내 동네에서 이 거리 안에서만 픽업 장소를 정할 수 있다
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")
NICK_RE = re.compile(r"^[0-9A-Za-z가-힣_]{2,12}$")
Image.MAX_IMAGE_PIXELS = 40_000_000


# ---------------------------------------------------------------- 공통 함수
def now():
    """한국 시간 기준 현재 시각. 서버가 해외(UTC)에 있어도 마감·픽업 시간이 어긋나지 않는다."""
    return datetime.now(db.KST).replace(tzinfo=None, second=0, microsecond=0)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M")


def parse(s):
    try:
        return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M") if s else None
    except ValueError:
        return None


def haversine(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def to_float(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi and math.isfinite(f) else None


def clean(s, n):
    return (s or "").strip()[:n]


def safe_next(nxt, default=None):
    """같은 사이트 안의 경로로만 돌려보낸다 (//evil.com 같은 외부 주소 차단)."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and "\n" not in nxt:
        return nxt
    return default


def client_ip():
    return request.remote_addr or "?"


def notify(uid, kind, body, link=None):
    db.execute("INSERT INTO notifications (user_id, kind, body, link, created_at) VALUES (?,?,?,?,?)",
               (uid, kind, body, link, iso(now())))


def sysmsg(room_id, body):
    db.execute("INSERT INTO messages (room_id, user_id, body, is_system, created_at) VALUES (?,?,?,1,?)",
               (room_id, None, body, iso(now())))


# ---------------------------------------------------------------- 보안: CSRF · 요청 제한 · 헤더
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_field():
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


def csp_nonce():
    """요청마다 새로 만드는 1회용 값. 이 값이 붙은 <script> 만 브라우저가 실행한다 (CSP)."""
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


app.jinja_env.globals.update(csrf_token=csrf_token, csrf_field=csrf_field, csp_nonce=csp_nonce)

_hits = defaultdict(deque)
_hits_lock = threading.Lock()
_hits_swept = [0.0]
RATE_WINDOW_MAX = 3600  # 가장 긴 제한 시간(초). 이보다 오래된 기록은 필요 없다


def rate_limit(bucket, limit, per_seconds, key=None):
    """같은 IP(또는 사용자)가 짧은 시간에 너무 많이 요청하면 429로 막는다."""
    if app.config.get("TESTING") and not app.config.get("RATE_LIMIT_IN_TESTS"):
        return
    k = (bucket, key or client_ip())
    t = time.monotonic()
    with _hits_lock:
        if t - _hits_swept[0] > 60:
            # 1분마다 오래된 IP 기록을 지워서 접속자가 늘어도 메모리가 계속 커지지 않게 한다
            _hits_swept[0] = t
            for old in [key for key, dq in _hits.items() if not dq or dq[-1] < t - RATE_WINDOW_MAX]:
                del _hits[old]
        q = _hits[k]
        while q and q[0] < t - per_seconds:
            q.popleft()
        if len(q) >= limit:
            abort(429)
        q.append(t)


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, description="csrf")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https://*.tile.openstreetmap.org; "
        # script-src 에 'unsafe-inline' 이 없으므로, 누가 HTML 에 <script> 나 onclick= 을 끼워 넣어도
        # 이번 요청의 nonce 가 없으면 브라우저가 실행하지 않는다 (XSS 2차 방어)
        f"style-src 'self' 'unsafe-inline'; script-src 'self' 'nonce-{csp_nonce()}'; script-src-attr 'none'; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self' https://kauth.kakao.com; base-uri 'self'")
    if PRODUCTION:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if g.get("user") and resp.mimetype == "text/html":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ---------------------------------------------------------------- 사진 저장 (위치정보 제거·재인코딩)
def save_upload(file, private=False):
    """이미지인지 실제로 열어 확인하고, EXIF(촬영 위치 등)를 지운 JPEG로 다시 저장한다."""
    if not file or not file.filename:
        return None
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = Image.open(file.stream)
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError):
        raise ValueError("사진 파일만 올릴 수 있어요. (jpg, png, webp, gif)")
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img.thumbnail((1600, 1600))
    name = f"{uuid.uuid4().hex}.jpg"
    folder = app.config["UPLOAD_PRIVATE" if private else "UPLOAD_PUBLIC"]
    img.convert("RGB").save(os.path.join(folder, name), "JPEG", quality=85, optimize=True)
    return name


# ---------------------------------------------------------------- 메일 (설정된 경우에만)
def send_mail(to, subject, body):
    if not MAIL_ENABLED:
        log.warning("메일 설정이 없어 보내지 않았어요: to=%s subject=%s", to, subject)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.set_content(body)
    port = int(SMTP["port"] or 587)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(SMTP["host"], port, timeout=15)
        else:
            server = smtplib.SMTP(SMTP["host"], port, timeout=15)
            server.starttls()
        with server:
            if SMTP["user"]:
                server.login(SMTP["user"], SMTP["password"])
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError):
        log.exception("메일 전송 실패: %s", to)
        return False


def code_hash(code):
    return hashlib.sha256((app.config["SECRET_KEY"] + code).encode()).hexdigest()


# ---------------------------------------------------------------- 방 · 사용자 정보
def members(room_id):
    return db.query(
        "SELECT p.*, u.nickname, u.school_verified FROM participations p JOIN users u ON u.id = p.user_id"
        " WHERE p.room_id=? ORDER BY p.id", (room_id,))


def taken_slots(room):
    s = db.query("SELECT COALESCE(SUM(slots),0) AS s FROM participations WHERE room_id=?", (room["id"],), one=True)["s"]
    return room["host_slots"] + s


def enrich(r):
    """방 한 개에 화면에서 쓰는 계산 값을 붙인다."""
    d = dict(r)
    d["taken"] = taken_slots(r)
    d["remaining"] = r["slot_count"] - d["taken"]
    d["host"] = db.query("SELECT * FROM users WHERE id=?", (r["host_id"],), one=True)
    d["shop"] = db.query("SELECT * FROM shops WHERE id=?", (r["shop_id"],), one=True) if r["shop_id"] else None
    d["cat"] = CAT.get(r["category"], CAT["etc"])
    d["status_label"], d["status_color"] = STATUS.get(r["status"], ("알 수 없음", "gray"))
    u = g.get("user")
    d["dist"] = haversine(u["lat"], u["lng"], r["pickup_lat"], r["pickup_lng"]) if u and u["lat"] is not None else None
    d["is_host"] = bool(u and u["id"] == r["host_id"])
    d["mine"] = db.query("SELECT * FROM participations WHERE room_id=? AND user_id=?",
                         (r["id"], u["id"]), one=True) if u else None
    return d


def get_or_404(sql, args=(), what="데이터"):
    """Django 의 get_object_or_404 와 같은 역할. 찾는 행이 없으면 NotFound(404) 예외를 던진다."""
    row = db.query(sql, args, one=True)
    if row is None:
        raise NotFound(f"No {what} matches the given query.")
    return row


def not_found_page(e, what):
    """수업 자료의 except 블록: 오류를 로그로 남기고 안내 화면(404)을 보여 준다."""
    log.error(f"An error occurred : {e} ({what}, user={g.user['id'] if g.get('user') else '-'}, "
              f"path={request.path})", exc_info=False)
    return render_template("error.html", msg=ERROR_MSG[404], err_msg="404"), 404


def get_room(room_id):
    return enrich(get_or_404("SELECT * FROM rooms WHERE id=?", (room_id,), "Room"))


def require_member(room):
    if not (room["is_host"] or room["mine"] or (g.user and g.user["is_admin"])):
        abort(403)


def user_stats(uid):
    """신뢰도·약속 이행·절약 금액."""
    reviews = db.query("SELECT good, bad FROM reviews WHERE to_id=?", (uid,))
    good = sum(len([t for t in r["good"].split(",") if t]) for r in reviews)
    bad = sum(len([t for t in r["bad"].split(",") if t]) for r in reviews)
    kept = db.query(
        "SELECT (SELECT COUNT(*) FROM participations p WHERE p.user_id=? AND p.received=1)"
        " + (SELECT COUNT(*) FROM rooms WHERE host_id=? AND status='done') AS n", (uid, uid), one=True)["n"]
    u = db.query("SELECT noshow FROM users WHERE id=?", (uid,), one=True)
    noshow = u["noshow"] if u else 0
    score = max(0, min(100, 70 + 2 * good - 4 * bad + 2 * kept - 10 * noshow))
    month = now().strftime("%Y-%m")
    rows = db.query(
        "SELECT r.slot_price, r.ref_price, p.slots FROM participations p"
        " JOIN rooms r ON r.id=p.room_id WHERE p.user_id=? AND p.received=1 AND substr(r.pickup_at,1,7)=?",
        (uid, month))
    hosted = db.query(
        "SELECT slot_price, ref_price, host_slots AS slots FROM rooms"
        " WHERE host_id=? AND status='done' AND substr(pickup_at,1,7)=?", (uid, month))
    saved = sum(max(0, (x["ref_price"] or x["slot_price"]) - x["slot_price"]) * x["slots"] for x in [*rows, *hosted])
    shared = sum(x["slots"] for x in [*rows, *hosted])
    return {"score": score, "good": good, "bad": bad, "kept": kept, "noshow": noshow,
            "saved": saved, "shared": shared, "deals": kept}


# ---------------------------------------------------------------- 자동 처리 (마감·입금 기한·픽업 알림)
_hk_lock = threading.Lock()
_hk_last = [0.0]


def housekeeping(force=False):
    """요청이 들어올 때 최대 30초에 한 번, 마감·입금 기한·픽업 알림을 처리한다."""
    if not force and time.monotonic() - _hk_last[0] < 30:
        return
    if not _hk_lock.acquire(blocking=False):
        return
    try:
        _hk_last[0] = time.monotonic()
        _housekeeping()
    finally:
        _hk_lock.release()


_purge_day = [""]


def purge_old_data(t):
    """하루 한 번: 개인정보처리방침의 보관 기간이 지난 기록을 지운다."""
    today = t.strftime("%Y-%m-%d")
    if _purge_day[0] == today:
        return
    _purge_day[0] = today
    closed = ",".join(f"'{s}'" for s in CLOSED)
    old = db.query(f"SELECT id, photo, receipt_photo, product_photo FROM rooms WHERE status IN ({closed})"
                   " AND pickup_at < ?", (iso(t - timedelta(days=365)),))
    for r in old:
        for name, private in ((r["photo"], False), (r["receipt_photo"], True), (r["product_photo"], True)):
            if name:
                path = os.path.join(app.config["UPLOAD_PRIVATE" if private else "UPLOAD_PUBLIC"], name)
                if os.path.exists(path):
                    os.remove(path)
        db.execute("DELETE FROM rooms WHERE id=?", (r["id"],))
    db.execute("DELETE FROM notifications WHERE created_at < ?", (iso(t - timedelta(days=90)),))
    db.execute("DELETE FROM email_codes WHERE expires_at < ?", (iso(t - timedelta(days=1)),))
    if old:
        log.info("보관 기간이 지난 공구방 %d개를 지웠어요", len(old))


def all_paid_at(room_id):
    """참여자가 모두 입금 표시를 했으면 마지막 입금 시각, 아니면 None."""
    row = db.query("SELECT COUNT(*) AS n, SUM(paid=0) AS unpaid, MAX(paid_at) AS last FROM participations"
                   " WHERE room_id=?", (room_id,), one=True)
    if not row["n"] or row["unpaid"]:
        return None
    return row["last"]


def purchase_deadline(room):
    """모두 입금한 방의 구매 확정 기한 (마지막 입금과 모집 완료 중 늦은 시각 + PURCHASE_HOURS)."""
    last = all_paid_at(room["id"])
    if not last:
        return None
    base = max(last, room["full_at"] or last)
    return parse(base) + timedelta(hours=PURCHASE_HOURS)


def auto_cancel_unpurchased(t):
    """규칙 1: 모두 입금했는데 방장이 24시간 안에 구매를 확정하지 않으면 공구를 취소한다."""
    for r in db.query("SELECT * FROM rooms WHERE status='deposit'"):
        due = purchase_deadline(r)
        if not due or due > t:
            continue
        reason = f"방장이 모두 입금한 뒤 {PURCHASE_HOURS}시간 안에 구매를 확정하지 않아 자동 취소"
        if db.update("UPDATE rooms SET status='failed', cancel_reason=? WHERE id=? AND status='deposit'",
                     (reason, r["id"])) == 0:
            continue  # 그 사이 방장이 구매를 확정했다 → 취소도 알림도 하지 않는다
        sysmsg(r["id"], f"방장이 {PURCHASE_HOURS}시간 안에 구매를 확정하지 않아 공구가 자동 취소됐어요. "
                        "입금한 금액은 방장에게 환불을 요청해 주세요.")
        notify(r["host_id"], "공구", f"‘{r['title']}’ 구매 확정 기한이 지나 공구가 자동 취소됐어요. "
                                    "참여자에게 입금액을 돌려주세요.", f"/rooms/{r['id']}/chat")
        for p in db.query("SELECT * FROM participations WHERE room_id=?", (r["id"],)):
            notify(p["user_id"], "공구", f"‘{r['title']}’ 방장이 구매를 확정하지 않아 공구가 취소됐어요. "
                                        "방장에게 환불을 요청하세요. 문제가 있으면 신고해 주세요.", f"/rooms/{r['id']}/chat")
        log.warning(f"자동 취소(구매 미확정): room={r['id']} host={r['host_id']}")


def auto_receive(t):
    """규칙 2: 픽업 시간 48시간 뒤에도 수령 확인을 안 누른 참여자는 자동으로 수령 처리하고 거래를 끝낸다.
    단, 처리되지 않은 신고가 있는 방은 운영팀이 볼 때까지 그대로 둔다."""
    limit = iso(t - timedelta(hours=AUTO_RECEIVE_HOURS))
    for r in db.query("SELECT * FROM rooms WHERE status='pickup' AND pickup_at < ?", (limit,)):
        if db.query("SELECT 1 FROM reports WHERE room_id=? AND resolved=0", (r["id"],), one=True):
            continue
        with db.transaction():
            if db.update("UPDATE rooms SET status='done' WHERE id=? AND status='pickup'", (r["id"],)) == 0:
                continue  # 그 사이 마지막 참여자가 직접 수령 확인을 눌러 이미 완료됐다
            pending = db.query("SELECT * FROM participations WHERE room_id=? AND received=0", (r["id"],))
            db.execute("UPDATE participations SET received=1 WHERE room_id=? AND received=0", (r["id"],))
        sysmsg(r["id"], f"픽업 시간이 {AUTO_RECEIVE_HOURS}시간 지나 {len(pending)}명을 자동으로 수령 처리했어요. "
                        "거래 완료! 후기를 남겨 주세요.")
        for uid in [r["host_id"]] + [p["user_id"] for p in db.query(
                "SELECT user_id FROM participations WHERE room_id=?", (r["id"],))]:
            notify(uid, "거래 완료", f"‘{r['title']}’ 거래가 자동으로 완료됐어요. 후기를 남겨 주세요.",
                   f"/rooms/{r['id']}/review")
        log.info(f"자동 수령 처리: room={r['id']} 자동 처리 {len(pending)}명 → done")


def _housekeeping():
    t = now()
    for r in db.query("SELECT * FROM rooms WHERE status='recruiting' AND deadline < ?", (iso(t),)):
        if db.update("UPDATE rooms SET status='failed' WHERE id=? AND status='recruiting'", (r["id"],)) == 0:
            continue
        sysmsg(r["id"], "모집 마감 시간까지 칸이 다 차지 않아 공구가 취소됐어요.")
        notify(r["host_id"], "공구", f"‘{r['title']}’ 모집이 마감 전에 다 차지 않아 취소됐어요.", f"/rooms/{r['id']}")
        for p in db.query("SELECT * FROM participations WHERE room_id=?", (r["id"],)):
            notify(p["user_id"], "공구", f"‘{r['title']}’ 공구가 취소됐어요. 입금했다면 방장에게 환불을 요청하세요.",
                   f"/rooms/{r['id']}/chat")
    limit = iso(t - timedelta(hours=DEPOSIT_HOURS))
    for r in db.query("SELECT * FROM rooms WHERE status='deposit' AND full_at < ?", (limit,)):
        # 쓰기 잠금(BEGIN IMMEDIATE) 안에서 다시 확인하고 지운다.
        # 전에는 '미입금자 목록'을 읽은 뒤 지우기 전 사이에 입금 표시를 한 사람도 지워질 수 있었다.
        with db.transaction():
            unpaid = db.query("SELECT * FROM participations WHERE room_id=? AND paid=0 AND confirmed=0",
                              (r["id"],))
            still = db.query("SELECT 1 FROM rooms WHERE id=? AND status='deposit' AND full_at < ?",
                             (r["id"], limit), one=True)
            if not unpaid or not still:
                continue
            for p in unpaid:
                db.execute("DELETE FROM participations WHERE id=? AND paid=0 AND confirmed=0", (p["id"],))
                db.execute("UPDATE users SET noshow = noshow + 1 WHERE id=?", (p["user_id"],))
            status = "recruiting" if r["deadline"] > iso(t) else "failed"
            db.execute("UPDATE rooms SET status=?, full_at=NULL WHERE id=?", (status, r["id"]))
        for p in unpaid:
            notify(p["user_id"], "공구", f"‘{r['title']}’ 입금 기한이 지나 자리가 해제됐어요.", f"/rooms/{r['id']}")
        log.info(f"입금 기한 만료: room={r['id']} 해제 {len(unpaid)}자리 → {status}")
        sysmsg(r["id"], f"입금 기한(24시간)이 지나 미입금 자리 {len(unpaid)}개가 풀렸어요. 다시 모집합니다.")
        notify(r["host_id"], "공구", f"‘{r['title']}’ 미입금 자리가 풀려 다시 모집 중이에요."
               if status == "recruiting" else f"‘{r['title']}’ 미입금 자리가 풀렸고 마감이 지나 공구가 취소됐어요.",
               f"/rooms/{r['id']}")
    auto_cancel_unpurchased(t)
    auto_receive(t)
    purge_old_data(t)
    soon = iso(t + timedelta(minutes=30))
    for r in db.query("SELECT * FROM rooms WHERE status='pickup' AND pickup_notified=0 AND pickup_at <= ?", (soon,)):
        db.execute("UPDATE rooms SET pickup_notified=1 WHERE id=?", (r["id"],))
        targets = [r["host_id"]] + [p["user_id"] for p in db.query(
            "SELECT user_id FROM participations WHERE room_id=?", (r["id"],))]
        for uid in targets:
            notify(uid, "픽업", f"30분 뒤 픽업이에요 · {r['title']} · {r['pickup_place']}", f"/rooms/{r['id']}/pickup")


# ---------------------------------------------------------------- 요청 전처리 · 로그인
def password_signature(u):
    """비밀번호가 바뀌면 달라지는 짧은 값. 세션과 재설정 링크에 넣어 예전 것을 무효로 만든다."""
    return hmac.new(app.config["SECRET_KEY"].encode(), (u["password_hash"] or "-").encode(),
                    hashlib.sha256).hexdigest()[:24]


@app.before_request
def load_user():
    uid = session.get("uid")
    row = db.query("SELECT * FROM users WHERE id=? AND deleted=0", (uid,), one=True) if uid else None
    g.user = dict(row) if row else None
    if g.user and not hmac.compare_digest(session.get("pw", ""), password_signature(g.user)):
        # 비밀번호가 바뀌면(다른 기기에서 변경·재설정) 예전 로그인은 끊는다
        session.pop("uid", None)
        g.user = None
    if g.user:
        g.user["is_admin"] = 1 if is_admin_user(g.user) else 0
        if g.user["is_admin"] != row["is_admin"]:
            db.execute("UPDATE users SET is_admin=? WHERE id=?", (g.user["is_admin"], g.user["id"]))
    if g.user and g.user["banned"]:
        session.pop("uid", None)
        g.user = None
        flash("운영 정책 위반으로 이용이 제한된 계정이에요. 문의: " + (SITE["contact"] or "운영팀"))
    if request.endpoint not in ("static", "public_upload", "healthz"):
        housekeeping()


def login_user(u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["pw"] = password_signature(u)
    csrf_token()
    db.execute("UPDATE users SET last_login=?, is_admin=? WHERE id=?",
               (iso(now()), 1 if is_admin_user(u) else 0, u["id"]))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if g.user["lat"] is None and request.endpoint not in ("neighborhood", "school_send", "school_confirm"):
            return redirect(url_for("neighborhood", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user["is_admin"]:
            abort(404)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject():
    unread = 0
    if g.get("user"):
        unread = db.query("SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND is_read=0",
                          (g.user["id"],), one=True)["n"]
    return {"me": g.get("user"), "unread": unread, "CATEGORIES": CATEGORIES, "STATUS": STATUS, "SITE": SITE,
            "DEMO_MODE": app.config["DEMO_MODE"], "KAKAO_ENABLED": bool(KAKAO_KEY), "MAIL_ENABLED": MAIL_ENABLED}


@app.template_filter("won")
def won(v):
    return f"{int(v or 0):,}원"


@app.template_filter("qty")
def qty(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


@app.template_filter("dt")
def dt(s):
    d = parse(s)
    if not d:
        return ""
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]}) {d:%H:%M}"


@app.template_filter("left")
def left(s):
    d = parse(s)
    if not d:
        return ""
    diff = d - now()
    if diff.total_seconds() <= 0:
        return "마감"
    h = int(diff.total_seconds() // 3600)
    if h >= 24:
        return f"{h // 24}일 남음"
    if h >= 1:
        return f"{h}시간 남음"
    return f"{int(diff.total_seconds() // 60)}분 남음"


@app.template_filter("ago")
def ago(s):
    d = parse(s)
    if not d:
        return ""
    secs = (now() - d).total_seconds()
    if secs < 60:
        return "방금"
    if secs < 3600:
        return f"{int(secs // 60)}분 전"
    if secs < 86400:
        return f"{int(secs // 3600)}시간 전"
    return f"{int(secs // 86400)}일 전"


@app.template_filter("meters")
def meters(m):
    if m is None:
        return ""
    return f"{m / 1000:.1f}km" if m >= 1000 else f"{int(round(m, -1))}m"


# ---------------------------------------------------------------- 온보딩 · 가입 · 로그인
@app.route("/")
def index():
    if g.user:
        return redirect(url_for("home"))
    return render_template("onboarding.html")


def shared_room(nxt):
    m = re.match(r"^/rooms/(\d+)", nxt or "")
    return db.query("SELECT title FROM rooms WHERE id=?", (int(m.group(1)),), one=True) if m else None


def agreements_ok(form):
    return all(form.get(k) for k in ("agree_terms", "agree_privacy", "agree_location", "agree_age"))


def unique_nickname(base):
    base = re.sub(r"[^0-9A-Za-z가-힣_]", "", base or "")[:10] or "이웃"
    if len(base) < 2:
        base += "님"
    name, i = base, 1
    while db.query("SELECT 1 FROM users WHERE nickname=?", (name,), one=True):
        i += 1
        name = f"{base[:9]}{i}"
    return name


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("home"))
    nxt = safe_next(request.values.get("next"), "")
    f = request.form
    if request.method == "POST":
        rate_limit("signup", 10, 3600)
        email = clean(f.get("email"), 254).lower()
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        nickname = clean(f.get("nickname"), 12)
        errors = []
        if not EMAIL_RE.match(email):
            errors.append("이메일 주소를 확인해 주세요.")
        elif db.query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            errors.append("이미 가입된 이메일이에요. 로그인해 주세요.")
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
            errors.append("비밀번호는 영문과 숫자를 섞어 8자 이상으로 정해 주세요.")
        elif pw != pw2:
            errors.append("비밀번호 확인이 일치하지 않아요.")
        if not NICK_RE.match(nickname):
            errors.append("닉네임은 한글·영문·숫자 2~12자로 정해 주세요.")
        elif db.query("SELECT 1 FROM users WHERE nickname=?", (nickname,), one=True):
            errors.append("이미 쓰고 있는 닉네임이에요.")
        if not agreements_ok(f):
            errors.append("필수 약관에 모두 동의해 주세요. (만 14세 이상만 가입할 수 있어요)")
        if not errors:
            try:
                uid = db.execute(
                    "INSERT INTO users (nickname, provider, email, password_hash, created_at, agreed_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (nickname, "email", email, generate_password_hash(pw), iso(now()), iso(now())))
            except db.sqlite3.IntegrityError:
                # 가입 버튼을 두 번 눌렀거나 같은 순간 다른 사람이 같은 이메일·닉네임으로 가입한 경우
                flash("이미 가입된 이메일이거나 쓰고 있는 닉네임이에요. 로그인해 보세요.")
                return render_template("signup.html", nxt=nxt, f=f, shared=shared_room(nxt))
            login_user(db.query("SELECT * FROM users WHERE id=?", (uid,), one=True))
            log.info(f"회원 가입: user={uid}")
            return redirect(url_for("neighborhood", next=nxt))
        for e in errors:
            flash(e)
    return render_template("signup.html", nxt=nxt, f=f, shared=shared_room(nxt))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("home"))
    nxt = safe_next(request.values.get("next"), "")
    if request.method == "POST":
        rate_limit("login", 10, 300)
        email = clean(request.form.get("email"), 254).lower()
        u = db.query("SELECT * FROM users WHERE email=? AND deleted=0", (email,), one=True)
        if not u or not u["password_hash"] or not check_password_hash(u["password_hash"], request.form.get("password", "")):
            log.warning(f"로그인 실패: ip={client_ip()}")  # 이메일·비밀번호는 로그에 남기지 않는다
            flash("이메일 또는 비밀번호가 맞지 않아요.")
        elif u["banned"]:
            log.warning(f"정지 계정 로그인 시도: user={u['id']}")
            flash("운영 정책 위반으로 이용이 제한된 계정이에요.")
        else:
            login_user(u)
            log.info(f"로그인: user={u['id']}")
            if u["lat"] is None:
                return redirect(url_for("neighborhood", next=nxt))
            return redirect(nxt or url_for("home"))
    demo = db.query("SELECT id, nickname FROM users WHERE provider='demo' ORDER BY id LIMIT 3") \
        if app.config["DEMO_MODE"] else []
    return render_template("login.html", nxt=nxt, shared=shared_room(nxt), demo=demo)


@app.route("/login/demo/<int:uid>", methods=["POST"])
def login_demo(uid):
    if not app.config["DEMO_MODE"]:
        abort(404)
    u = db.query("SELECT * FROM users WHERE id=? AND provider='demo'", (uid,), one=True)
    if not u:
        abort(404)
    login_user(u)
    return redirect(safe_next(request.form.get("next"), url_for("home")))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---- 카카오 로그인 (KAKAO_REST_API_KEY 가 있을 때만)
def kakao_redirect_uri():
    return (SITE["url"] + "/auth/kakao/callback") if SITE["url"] else url_for("kakao_callback", _external=True)


def http_json(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@app.route("/auth/kakao")
def kakao_start():
    if not KAKAO_KEY:
        abort(404)
    session["kakao_state"] = secrets.token_urlsafe(16)
    session["kakao_next"] = safe_next(request.args.get("next"), "")
    q = urllib.parse.urlencode({"client_id": KAKAO_KEY, "redirect_uri": kakao_redirect_uri(),
                                "response_type": "code", "state": session["kakao_state"]})
    return redirect("https://kauth.kakao.com/oauth/authorize?" + q)


@app.route("/auth/kakao/callback")
def kakao_callback():
    if not KAKAO_KEY:
        abort(404)
    state, code = request.args.get("state", ""), request.args.get("code", "")
    if not code or not hmac.compare_digest(state, session.pop("kakao_state", "") or "-"):
        flash("카카오 로그인이 취소됐거나 만료됐어요. 다시 시도해 주세요.")
        return redirect(url_for("login"))
    try:
        data = {"grant_type": "authorization_code", "client_id": KAKAO_KEY,
                "redirect_uri": kakao_redirect_uri(), "code": code}
        if KAKAO_SECRET:
            data["client_secret"] = KAKAO_SECRET
        token = http_json("https://kauth.kakao.com/oauth/token", data)
        info = http_json("https://kapi.kakao.com/v2/user/me",
                         headers={"Authorization": "Bearer " + token["access_token"]})
    except Exception:  # noqa: BLE001 — 외부 서비스 오류는 사용자에게 안내만
        log.exception("카카오 로그인 실패")
        flash("카카오 로그인 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.")
        return redirect(url_for("login"))
    kid = str(info.get("id", ""))
    if not kid:
        abort(400)
    nxt = session.pop("kakao_next", "")
    u = db.query("SELECT * FROM users WHERE provider='kakao' AND provider_id=?", (kid,), one=True)
    if u and (u["banned"] or u["deleted"]):
        flash("이용할 수 없는 계정이에요. 문의: " + (SITE["contact"] or "운영팀"))
        return redirect(url_for("login"))
    if u:
        login_user(u)
        return redirect(url_for("neighborhood", next=nxt) if u["lat"] is None else (nxt or url_for("home")))
    profile = (info.get("kakao_account") or {}).get("profile") or {}
    session["pending_kakao"] = {"id": kid, "nickname": unique_nickname(profile.get("nickname")), "next": nxt}
    return redirect(url_for("kakao_consent"))


@app.route("/auth/kakao/consent", methods=["GET", "POST"])
def kakao_consent():
    pend = session.get("pending_kakao")
    if not pend:
        return redirect(url_for("login"))
    f = request.form
    if request.method == "POST":
        nickname = clean(f.get("nickname"), 12)
        if not NICK_RE.match(nickname):
            flash("닉네임은 한글·영문·숫자 2~12자로 정해 주세요.")
        elif db.query("SELECT 1 FROM users WHERE nickname=?", (nickname,), one=True):
            flash("이미 쓰고 있는 닉네임이에요.")
        elif not agreements_ok(f):
            flash("필수 약관에 모두 동의해 주세요. (만 14세 이상만 가입할 수 있어요)")
        else:
            try:
                uid = db.execute("INSERT INTO users (nickname, provider, provider_id, created_at, agreed_at)"
                                 " VALUES (?,?,?,?,?)", (nickname, "kakao", pend["id"], iso(now()), iso(now())))
            except db.sqlite3.IntegrityError:
                existing = db.query("SELECT * FROM users WHERE provider='kakao' AND provider_id=? AND deleted=0"
                                    " AND banned=0", (pend["id"],), one=True)
                session.pop("pending_kakao", None)
                if existing:  # 버튼을 두 번 눌러 이미 가입된 경우 → 그 계정으로 로그인
                    login_user(existing)
                    return redirect(url_for("neighborhood", next=pend.get("next") or "")
                                    if existing["lat"] is None else url_for("home"))
                flash("이미 쓰고 있는 닉네임이에요. 다시 시도해 주세요.")
                return redirect(url_for("login"))
            session.pop("pending_kakao", None)
            login_user(db.query("SELECT * FROM users WHERE id=?", (uid,), one=True))
            return redirect(url_for("neighborhood", next=pend.get("next") or ""))
    return render_template("consent.html", nickname=f.get("nickname") or pend["nickname"], f=f)


# ---- 비밀번호 재설정 (메일 설정이 있을 때)
def reset_signature(u):
    return password_signature(u)


@app.route("/password/forgot", methods=["GET", "POST"])
def password_forgot():
    if request.method == "POST":
        rate_limit("forgot", 5, 3600)
        email = clean(request.form.get("email"), 254).lower()
        u = db.query("SELECT * FROM users WHERE email=? AND deleted=0", (email,), one=True)
        base = public_base()
        if u and MAIL_ENABLED and not base:
            log.error("PUBLIC_URL 이 없어 비밀번호 재설정 메일을 보내지 않았어요.")
        elif u and MAIL_ENABLED:
            from itsdangerous import URLSafeTimedSerializer
            s = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pw-reset")
            token = s.dumps({"uid": u["id"], "sig": reset_signature(u)})
            link = base + url_for("password_reset", token=token)
            send_mail(email, "[반띵] 비밀번호 재설정 안내",
                      f"아래 링크에서 30분 안에 새 비밀번호를 정해 주세요.\n\n{link}\n\n"
                      "본인이 요청하지 않았다면 이 메일을 무시해 주세요.")
        flash("가입된 이메일이라면 재설정 링크를 보냈어요. 메일함을 확인해 주세요.")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/password/reset/<token>", methods=["GET", "POST"])
def password_reset(token):
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    s = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pw-reset")
    try:
        data = s.loads(token, max_age=1800)
    except (BadSignature, SignatureExpired):
        flash("링크가 만료됐거나 올바르지 않아요. 다시 요청해 주세요.")
        return redirect(url_for("password_forgot"))
    u = db.query("SELECT * FROM users WHERE id=? AND deleted=0", (data.get("uid"),), one=True)
    if not u or reset_signature(u) != data.get("sig"):
        flash("이미 사용한 링크예요. 다시 요청해 주세요.")
        return redirect(url_for("password_forgot"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
            flash("비밀번호는 영문과 숫자를 섞어 8자 이상으로 정해 주세요.")
        elif pw != pw2:
            flash("비밀번호 확인이 일치하지 않아요.")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))
            flash("비밀번호를 바꿨어요. 새 비밀번호로 로그인해 주세요.")
            return redirect(url_for("login"))
    return render_template("reset.html")


# ---------------------------------------------------------------- 동네 인증 · 학교 인증
@app.route("/neighborhood", methods=["GET", "POST"])
def neighborhood():
    if not g.user:
        return redirect(url_for("login"))
    nxt = safe_next(request.values.get("next"), "")
    if request.method == "POST":
        lat, lng = to_float(request.form.get("lat"), -90, 90), to_float(request.form.get("lng"), -180, 180)
        if lat is None or lng is None:
            flash("위치를 정해 주세요. ‘현재 위치 가져오기’를 누르거나 지도를 눌러 주세요.")
            return redirect(url_for("neighborhood", next=nxt))
        name = clean(request.form.get("name"), 20) or "우리 동네"
        radius = 1000 if request.form.get("radius") == "1000" else 500
        db.execute("UPDATE users SET neighborhood=?, lat=?, lng=?, radius=? WHERE id=?",
                   (name, lat, lng, radius, g.user["id"]))
        return redirect(nxt or url_for("home"))
    return render_template("neighborhood.html", nxt=nxt, demo=(DEMO_LAT, DEMO_LNG))


@app.route("/verify/school/send", methods=["POST"])
def school_send():
    if not g.user:
        abort(401)
    if not MAIL_ENABLED:
        abort(404)
    rate_limit("school", 5, 3600, key=str(g.user["id"]))
    email = clean(request.form.get("school_email"), 254).lower()
    if not EMAIL_RE.match(email) or not email.endswith(".ac.kr"):
        flash("학교 이메일(.ac.kr) 주소를 입력해 주세요.")
        return redirect(url_for("neighborhood"))
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.execute("UPDATE email_codes SET used=1 WHERE user_id=? AND purpose='school'", (g.user["id"],))
    db.execute("INSERT INTO email_codes (user_id, purpose, email, code_hash, expires_at) VALUES (?,?,?,?,?)",
               (g.user["id"], "school", email, code_hash(code), iso(now() + timedelta(minutes=10))))
    if send_mail(email, "[반띵] 학교 인증번호", f"인증번호: {code}\n10분 안에 반띵 앱에 입력해 주세요."):
        flash(f"{email}로 인증번호를 보냈어요. 10분 안에 입력해 주세요.")
    else:
        flash("메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.")
    return redirect(url_for("neighborhood", code_sent=1))


@app.route("/verify/school/confirm", methods=["POST"])
def school_confirm():
    if not g.user:
        abort(401)
    row = db.query("SELECT * FROM email_codes WHERE user_id=? AND purpose='school' AND used=0 ORDER BY id DESC LIMIT 1",
                   (g.user["id"],), one=True)
    code = clean(request.form.get("code"), 6)
    if not row or row["expires_at"] < iso(now()) or row["tries"] >= 5:
        flash("인증번호가 만료됐어요. 다시 받아 주세요.")
    elif not hmac.compare_digest(row["code_hash"], code_hash(code)):
        db.execute("UPDATE email_codes SET tries=tries+1 WHERE id=?", (row["id"],))
        flash("인증번호가 맞지 않아요.")
    else:
        db.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
        db.execute("UPDATE users SET school_email=?, school_verified=1 WHERE id=?", (row["email"], g.user["id"]))
        flash("학교 인증을 마쳤어요. 프로필에 인증 배지가 붙어요.")
        return redirect(url_for("me") if g.user["lat"] is not None else url_for("neighborhood"))
    return redirect(url_for("neighborhood", code_sent=1))


# ---------------------------------------------------------------- 운영자 이메일 인증
def admin_candidate():
    """ADMIN_EMAILS 에 있는 이메일로 가입했지만 아직 인증하지 않은 계정인가."""
    u = g.get("user")
    return bool(u and u["email"] and u["email"].lower() in ADMIN_EMAILS and not u["is_admin"])


app.jinja_env.globals.update(admin_candidate=admin_candidate)


@app.route("/verify/admin/send", methods=["POST"])
@login_required
def admin_verify_send():
    if not admin_candidate() or not MAIL_ENABLED:
        abort(404)
    rate_limit("admin_verify", 5, 3600, key=str(g.user["id"]))
    code = f"{secrets.randbelow(1_000_000):06d}"
    email = g.user["email"].lower()
    db.execute("UPDATE email_codes SET used=1 WHERE user_id=? AND purpose='admin'", (g.user["id"],))
    db.execute("INSERT INTO email_codes (user_id, purpose, email, code_hash, expires_at) VALUES (?,?,?,?,?)",
               (g.user["id"], "admin", email, code_hash(code), iso(now() + timedelta(minutes=10))))
    if send_mail(email, "[반띵] 운영자 이메일 인증번호",
                 f"운영자 인증번호: {code}\n10분 안에 반띵 › 마이 › 운영자 이메일 인증에 입력해 주세요.\n"
                 "본인이 요청하지 않았다면 누군가 이 이메일로 가입한 것이니 운영팀에 알려 주세요."):
        flash("운영자 이메일로 인증번호를 보냈어요. 10분 안에 입력해 주세요.")
    else:
        flash("메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.")
    return redirect(url_for("me", admin_code=1))


@app.route("/verify/admin/confirm", methods=["POST"])
@login_required
def admin_verify_confirm():
    if not admin_candidate():
        abort(404)
    row = db.query("SELECT * FROM email_codes WHERE user_id=? AND purpose='admin' AND used=0 ORDER BY id DESC LIMIT 1",
                   (g.user["id"],), one=True)
    code = clean(request.form.get("code"), 6)
    if not row or row["expires_at"] < iso(now()) or row["tries"] >= 5:
        flash("인증번호가 만료됐어요. 다시 받아 주세요.")
    elif not hmac.compare_digest(row["code_hash"], code_hash(code)) or row["email"] != g.user["email"].lower():
        db.execute("UPDATE email_codes SET tries=tries+1 WHERE id=?", (row["id"],))
        log.warning(f"운영자 인증번호 불일치: user={g.user['id']}")
        flash("인증번호가 맞지 않아요.")
    else:
        db.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
        db.execute("UPDATE users SET admin_verified_email=?, is_admin=1 WHERE id=?", (row["email"], g.user["id"]))
        log.warning(f"[운영] 운영자 이메일 인증 완료: user={g.user['id']}")
        flash("운영자 인증을 마쳤어요. ‘운영자 화면’이 열렸어요.")
        return redirect(url_for("me"))
    return redirect(url_for("me", admin_code=1))


@app.cli.command("verify-admin")
@click.argument("email")
def verify_admin_command(email):
    """메일 설정이 없을 때 서버 Shell 에서 운영자를 인증한다:  flask --app app verify-admin 운영자이메일"""
    email = email.strip().lower()
    if email not in ADMIN_EMAILS:
        raise click.ClickException(f"{email} 이(가) ADMIN_EMAILS 환경 변수에 없어요. 먼저 넣고 서버를 다시 켜 주세요.")
    conn = db.connect(app.config["DATABASE"])
    try:
        u = conn.execute("SELECT id FROM users WHERE email=? AND deleted=0", (email,)).fetchone()
        if not u:
            raise click.ClickException(f"{email} 로 가입한 계정이 없어요. 먼저 그 이메일로 가입해 주세요.")
        conn.execute("UPDATE users SET admin_verified_email=?, is_admin=1 WHERE id=?", (email, u["id"]))
    finally:
        conn.close()
    log.warning(f"[운영] 서버 명령으로 운영자 인증: user={u['id']}")
    click.echo(f"운영자로 인증했어요: {email} (회원 번호 {u['id']})")


# ---------------------------------------------------------------- 홈 · 지도 · 상점
def nearby_rooms(include_all=False):
    # 전국 방을 300개만 가져온 뒤 거리를 거르면, 다른 동네 방이 많을 때 내 동네 방이 빠진다.
    # 그래서 먼저 내 위치 주변 네모 범위(위도·경도)로 DB에서 거르고, 그다음 정확한 거리로 다시 거른다.
    km = 20 if include_all else max(g.user["radius"], 500) / 1000
    lat, lng = g.user["lat"], g.user["lng"]
    dlat = km / 111.0
    dlng = km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    rows = db.query("SELECT * FROM rooms WHERE status='recruiting' AND pickup_lat BETWEEN ? AND ?"
                    " AND pickup_lng BETWEEN ? AND ? ORDER BY deadline LIMIT 300",
                    (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
    rooms = [enrich(r) for r in rows]
    rooms = [r for r in rooms if r["remaining"] > 0]
    if not include_all:
        rooms = [r for r in rooms if r["dist"] is None or r["dist"] <= g.user["radius"]]
    else:
        rooms = [r for r in rooms if r["dist"] is None or r["dist"] <= 20000]
    return rooms


@app.route("/home")
@login_required
def home():
    q = clean(request.args.get("q"), 30)
    cat = request.args.get("cat", "")
    sort = request.args.get("sort", "deadline")
    show_all = request.args.get("all") == "1"
    rooms = nearby_rooms(include_all=show_all)
    if q:
        rooms = [r for r in rooms if q in r["title"]]
    if cat in CAT:
        rooms = [r for r in rooms if r["category"] == cat]
    if sort == "distance":
        rooms.sort(key=lambda r: r["dist"] or 0)
    has_shops = bool(db.query("SELECT 1 FROM shops LIMIT 1", one=True))
    return render_template("home.html", rooms=rooms, q=q, cat=cat, sort=sort, nav="home", show_all=show_all,
                           has_shops=has_shops)


@app.route("/map")
@login_required
def map_view():
    rooms = nearby_rooms(include_all=True)
    pins = [{"id": r["id"], "title": r["title"], "price": won(r["slot_price"]), "lat": r["pickup_lat"],
             "lng": r["pickup_lng"], "place": r["pickup_place"], "official": bool(r["shop_id"]),
             "left": left(r["deadline"]), "taken": r["taken"], "count": r["slot_count"],
             "dist": meters(r["dist"]), "icon": r["cat"]["icon"]} for r in rooms]
    return render_template("map.html", pins=pins, nav="map")


@app.route("/shops")
@login_required
def shops():
    out = []
    for s in db.query("SELECT * FROM shops"):
        d = dict(s)
        d["dist"] = haversine(g.user["lat"], g.user["lng"], s["lat"], s["lng"])
        if d["dist"] > 20000:
            continue
        d["rooms"] = [enrich(r) for r in db.query(
            "SELECT * FROM rooms WHERE shop_id=? AND status='recruiting' ORDER BY deadline", (s["id"],))]
        out.append(d)
    out.sort(key=lambda s: s["dist"])
    return render_template("shops.html", shops=out, nav="home")


# ---------------------------------------------------------------- 공구방 상세 · 참여
@app.route("/rooms/<int:room_id>")
@login_required
def room_detail(room_id):
    room = get_room(room_id)
    if room["status"] == "canceled" and not (room["is_host"] or room["mine"] or g.user["is_admin"]):
        abort(404)
    if not room["is_host"]:
        db.execute("UPDATE rooms SET views = views + 1 WHERE id=?", (room_id,))
    return render_template("room.html", room=room, stats=user_stats(room["host_id"]))


class RoomFull(Exception):
    pass


@app.route("/rooms/<int:room_id>/join", methods=["GET", "POST"])
@login_required
def join(room_id):
    room = get_room(room_id)
    if room["is_host"] or room["mine"]:
        return redirect(url_for("room_detail", room_id=room_id))
    if room["status"] != "recruiting" or room["remaining"] <= 0:
        flash("이미 모집이 끝난 공구방이에요.")
        return redirect(url_for("room_detail", room_id=room_id))
    if request.method == "POST":
        slots = request.form.get("slots", 1, type=int) or 1
        paid = 1 if request.form.get("action") == "paid" else 0
        try:
            with db.transaction():
                r = db.query("SELECT * FROM rooms WHERE id=?", (room_id,), one=True)
                remaining = r["slot_count"] - taken_slots(r)
                if r["status"] != "recruiting" or r["deadline"] <= iso(now()):
                    raise RoomFull("이미 모집이 끝난 공구방이에요.")
                if not 1 <= slots <= remaining:
                    raise RoomFull(f"참여할 수 있는 칸은 1~{remaining}칸이에요." if remaining > 0
                                   else "방금 다른 이웃이 마지막 칸을 가져갔어요.")
                db.execute("INSERT INTO participations (room_id, user_id, slots, paid, paid_at, created_at)"
                           " VALUES (?,?,?,?,?,?)", (room_id, g.user["id"], slots, paid,
                                                     iso(now()) if paid else None, iso(now())))
                full = remaining - slots == 0
                if full:
                    db.execute("UPDATE rooms SET status='deposit', full_at=? WHERE id=?", (iso(now()), room_id))
        except RoomFull as e:
            flash(str(e))
            return redirect(url_for("room_detail", room_id=room_id))
        except db.sqlite3.IntegrityError:
            # 참여 버튼을 두 번 눌러 같은 요청이 두 번 온 경우 (participations 의 UNIQUE(room_id, user_id))
            flash("이미 참여한 공구방이에요.")
            return redirect(url_for("room_detail", room_id=room_id))
        sysmsg(room_id, f"{g.user['nickname']}님이 {slots}칸 참여했어요")
        notify(room["host_id"], "참여", f"{g.user['nickname']}님이 ‘{room['title']}’에 {slots}칸 참여했어요"
               + (" (입금 완료 표시)" if paid else ""), f"/rooms/{room_id}/deposits")
        if full:
            sysmsg(room_id, "모든 칸이 찼어요! 24시간 안에 입금을 마쳐 주세요.")
            notify(room["host_id"], "모집 완료", f"‘{room['title']}’ 모집이 끝났어요. 입금을 확인해 주세요.",
                   f"/rooms/{room_id}/deposits")
            for p in db.query("SELECT * FROM participations WHERE room_id=?", (room_id,)):
                msg = "모집이 끝났어요." + ("" if p["paid"] else " 24시간 안에 입금해 주세요.")
                notify(p["user_id"], "모집 완료", f"‘{room['title']}’ {msg}", f"/rooms/{room_id}")
        flash("참여 완료! 진행 상황은 ‘내 공구’에서 볼 수 있어요.")
        return redirect(url_for("my", tab="join"))
    return render_template("join.html", room=room)


@app.route("/rooms/<int:room_id>/paid", methods=["POST"])
@login_required
def mark_paid(room_id):
    room = get_room(room_id)
    if not room["mine"]:
        abort(403)
    if room["status"] not in ("recruiting", "deposit") or room["mine"]["paid"]:
        flash("지금은 입금 표시를 바꿀 수 없어요.")
        return redirect(url_for("room_detail", room_id=room_id))
    db.execute("UPDATE participations SET paid=1, paid_at=? WHERE id=?", (iso(now()), room["mine"]["id"]))
    notify(room["host_id"], "입금", f"{g.user['nickname']}님이 ‘{room['title']}’ 입금 완료를 표시했어요.",
           f"/rooms/{room_id}/deposits")
    flash("입금 완료로 표시했어요. 방장이 확인하면 구매가 진행돼요.")
    return redirect(safe_next(request.form.get("next"), url_for("my", tab="join")))


@app.route("/rooms/<int:room_id>/leave", methods=["POST"])
@login_required
def leave(room_id):
    room = get_room(room_id)
    mine = room["mine"]
    if not mine or room["status"] not in ("recruiting", "deposit") or mine["paid"]:
        flash("입금 전, 구매 확정 전에만 참여를 취소할 수 있어요.")
        return redirect(url_for("room_detail", room_id=room_id))
    with db.transaction():
        db.execute("DELETE FROM participations WHERE id=?", (mine["id"],))
        if room["status"] == "deposit":
            db.execute("UPDATE rooms SET status='recruiting', full_at=NULL WHERE id=?", (room_id,))
    sysmsg(room_id, f"{g.user['nickname']}님이 참여를 취소했어요")
    notify(room["host_id"], "참여", f"{g.user['nickname']}님이 ‘{room['title']}’ 참여를 취소했어요.",
           f"/rooms/{room_id}")
    flash("참여를 취소했어요.")
    return redirect(url_for("my", tab="join"))


@app.route("/rooms/<int:room_id>/cancel", methods=["POST"])
@login_required
def host_cancel(room_id):
    """방장이 모집 중(아직 아무도 입금 확인 전)인 방을 닫는다."""
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    ps = members(room_id)
    if room["status"] != "recruiting" or any(p["confirmed"] for p in ps):
        flash("모집 중이고 입금 확인 전인 방만 닫을 수 있어요. 진행 중인 거래는 채팅으로 조율해 주세요.")
        return redirect(url_for("room_detail", room_id=room_id))
    db.execute("UPDATE rooms SET status='failed', cancel_reason='방장 취소' WHERE id=?", (room_id,))
    sysmsg(room_id, "방장이 공구방을 닫았어요.")
    for p in ps:
        notify(p["user_id"], "공구", f"방장이 ‘{room['title']}’ 공구방을 닫았어요."
               + (" 입금했다면 방장에게 환불을 요청하세요." if p["paid"] else ""), f"/rooms/{room_id}/chat")
    log.info(f"공구방 닫기: room={room_id} host={g.user['id']}")
    flash("공구방을 닫았어요.")
    return redirect(url_for("my", tab="done"))


# ---------------------------------------------------------------- 공구방 수정 · 삭제 (CRUD 의 U · D)
def check_times(pickup_at, deadline, errors):
    """방 만들기와 수정에서 같이 쓰는 모집 마감 · 픽업 시간 검사."""
    if not pickup_at or not deadline:
        errors.append("픽업 일시와 모집 마감 시간을 정해 주세요.")
    elif deadline <= now() + timedelta(minutes=10):
        errors.append("모집 마감은 지금부터 10분 이후로 정해 주세요.")
    elif deadline > now() + timedelta(days=14):
        errors.append("모집 기간은 최대 14일이에요.")
    elif deadline >= pickup_at:
        errors.append("모집 마감은 픽업 시간보다 앞이어야 해요. (장볼 시간 확보)")
    elif pickup_at > deadline + timedelta(days=7):
        errors.append("픽업은 모집 마감 후 7일 안으로 정해 주세요.")


def remove_upload(name, private=False):
    if not name:
        return
    folder = app.config["UPLOAD_PRIVATE" if private else "UPLOAD_PUBLIC"]
    try:
        os.remove(os.path.join(folder, os.path.basename(name)))
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"사진 파일 삭제 실패: {name} ({e})")


@app.route("/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@login_required
def room_update(room_id):
    """방장이 모집 중인 공구방 정보를 고친다.
    수업 자료의 rhema_update 처럼 기존 레코드(record)를 불러와 폼에 채우고, POST 로 받은 값으로 덮어쓴다.
    가격 · 수량 · 칸 수 · 계좌는 이미 참여한 이웃의 돈과 연결돼 있어서 고칠 수 없다 (닫고 새로 열기)."""
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    if room["status"] != "recruiting":
        flash("모집 중인 공구방만 수정할 수 있어요.")
        return redirect(url_for("room_detail", room_id=room_id))
    locked = bool(members(room_id))  # 참여자가 있으면 상품명·카테고리는 참여 당시 약속 그대로
    if request.method == "POST":
        form = request.form
        errors = []
        if locked:
            title, category = room["title"], room["category"]
            if (form.get("title") not in (None, room["title"])) or (form.get("category") not in (None, room["category"])):
                errors.append("참여한 이웃이 있어서 상품명·카테고리는 바꿀 수 없어요. 바꾸려면 공구방을 닫고 새로 열어 주세요.")
        else:
            title = clean(form.get("title"), 40)
            if not title:
                errors.append("상품명을 입력해 주세요.")
            category = form.get("category") if form.get("category") in CAT else room["category"]
        ref_price = form.get("ref_price", "").replace(",", "").strip()
        ref_price = int(ref_price) if ref_price.isdigit() and int(ref_price) <= 5_000_000 else None
        pickup_at, deadline = parse(form.get("pickup_at")), parse(form.get("deadline"))
        check_times(pickup_at, deadline, errors)
        photo, new_photo = room["photo"], None
        if not errors:
            try:
                new_photo = save_upload(request.files.get("photo"))
            except ValueError as e:
                errors.append(str(e))
            else:
                if new_photo or form.get("remove_photo"):
                    photo = new_photo  # 예전 사진 파일은 DB 저장이 끝난 뒤에 지운다
        if errors:
            for e in errors:
                flash(e)
            return render_template("room_edit.html", room=room, f=form, categories=CATEGORIES, locked=locked)
        with db.transaction():
            # 화면을 연 뒤 저장 사이에 누가 참여했을 수 있으니 잠금 안에서 다시 확인한다
            if not locked and (title != room["title"] or category != room["category"]) and members(room_id):
                remove_upload(new_photo)
                flash("방금 이웃이 참여해서 상품명·카테고리는 바꿀 수 없게 됐어요. 다시 확인해 주세요.")
                return redirect(url_for("room_update", room_id=room_id))
            if db.update("UPDATE rooms SET title=?, category=?, buy_from=?, ref_price=?, photo=?, pickup_at=?,"
                         " deadline=? WHERE id=? AND status='recruiting'",
                         (title, category, clean(form.get("buy_from"), 40), ref_price, photo,
                          iso(pickup_at), iso(deadline), room_id)) == 0:
                remove_upload(new_photo)
                flash("모집이 끝나서 수정하지 못했어요.")
                return redirect(url_for("room_detail", room_id=room_id))
        if photo != room["photo"]:
            remove_upload(room["photo"])
        changed = [n for n, old, new in (("상품명", room["title"], title),
                                         ("픽업 시간", room["pickup_at"], iso(pickup_at)),
                                         ("모집 마감", room["deadline"], iso(deadline))) if old != new]
        if changed:
            sysmsg(room_id, f"방장이 공구방 정보를 수정했어요: {', '.join(changed)}")
            for p in members(room_id):
                notify(p["user_id"], "공구", f"‘{title}’ 공구방의 {', '.join(changed)}이(가) 바뀌었어요.",
                       f"/rooms/{room_id}")
        log.info(f"공구방 수정: room={room_id} host={g.user['id']} changed={changed}")
        flash("공구방 정보를 수정했어요.")
        return redirect(url_for("room_detail", room_id=room_id))
    # GET: 기존 값을 폼에 채운다 (Django 의 WriteForm(instance=record) 와 같은 역할)
    return render_template("room_edit.html", room=room, f=room, categories=CATEGORIES, locked=locked)


@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
@login_required
def room_delete(room_id):
    """참여자가 아무도 없는 모집 중 공구방을 완전히 지운다. 수업 자료처럼 삭제는 POST 로만 받는다.
    참여자가 있으면 기록을 남겨야 하므로 삭제 대신 '공구방 닫기'를 쓴다."""
    try:
        record = get_or_404("SELECT * FROM rooms WHERE id=? AND host_id=?", (room_id, g.user["id"]), "Room")
    except NotFound as e:
        return not_found_page(e, "room_delete")
    else:
        if record["status"] != "recruiting" or members(room_id):
            flash("참여한 이웃이 있는 방은 지울 수 없어요. ‘공구방 닫기’를 써 주세요.")
            return redirect(url_for("room_detail", room_id=room_id))
        with db.transaction():
            db.execute("DELETE FROM notifications WHERE link = ? OR link LIKE ?",
                       (f"/rooms/{room_id}", f"/rooms/{room_id}/%"))
            db.execute("DELETE FROM rooms WHERE id=?", (room_id,))  # 채팅·신고·후기는 ON DELETE CASCADE
        remove_upload(record["photo"])
        log.info(f"공구방 삭제: room={room_id} host={g.user['id']} title={record['title']!r}")
        flash("공구방을 삭제했어요.")
        return redirect(url_for("my", tab="host"))


# ---------------------------------------------------------------- 방 만들기
def pickup_suggestions():
    """내 위치 근처의 추천 장소 + 최근 이웃들이 쓴 공개 픽업 장소."""
    me_ = g.user
    seen, out = set(), []
    rows = [dict(p) for p in db.query("SELECT name, lat, lng FROM places")]
    rows += [{"name": r["pickup_place"], "lat": r["pickup_lat"], "lng": r["pickup_lng"]} for r in db.query(
        "SELECT pickup_place, pickup_lat, pickup_lng FROM rooms WHERE shop_id IS NULL AND created_at >= ?"
        " ORDER BY id DESC LIMIT 200", (iso(now() - timedelta(days=90)),))]
    for p in rows:
        p["dist"] = haversine(me_["lat"], me_["lng"], p["lat"], p["lng"])
        if p["dist"] <= 1500 and p["name"] not in seen:
            seen.add(p["name"])
            out.append(p)
    return sorted(out, key=lambda p: p["dist"])[:5]


@app.route("/rooms/new", methods=["GET", "POST"])
@login_required
def new_room():
    shop = db.query("SELECT * FROM shops WHERE owner_id=?", (g.user["id"],), one=True)
    places = pickup_suggestions()
    form = request.form
    if request.method == "POST":
        rate_limit("new_room", 10, 3600, key=str(g.user["id"]))
        errors = []
        title = clean(form.get("title"), 40)
        if not title:
            errors.append("상품명을 입력해 주세요.")
        try:
            total_price = int(form.get("total_price", "0").replace(",", "").strip() or 0)
            total_qty = float(form.get("total_qty", "0") or 0)
            slot_qty = float(form.get("slot_qty", "0") or 0)
            host_slots = int(form.get("host_slots", "1") or 0)
        except ValueError:
            errors.append("가격과 수량은 숫자로 입력해 주세요.")
            total_price = total_qty = slot_qty = host_slots = 0
        if not all(math.isfinite(x) for x in (total_qty, slot_qty)):
            total_qty = slot_qty = 0
        slot_count = int(round(total_qty / slot_qty)) if slot_qty > 0 else 0
        if not 100 <= total_price <= 5_000_000:
            errors.append("총 가격은 100원 ~ 500만 원 사이로 입력해 주세요.")
        if total_qty <= 0 or slot_qty <= 0 or total_qty > 100000:
            errors.append("총 수량과 한 칸의 양을 확인해 주세요.")
        elif abs(total_qty - slot_count * slot_qty) > 1e-6:
            errors.append("총 수량이 한 칸의 양으로 딱 나눠떨어지게 입력해 주세요.")
        elif not 2 <= slot_count <= 30:
            errors.append("2칸 ~ 30칸으로 나눠 주세요.")
        is_shop = bool(shop and form.get("official"))
        min_host = 0 if is_shop else 1
        if not min_host <= host_slots < max(slot_count, 1):
            errors.append("방장 몫은 최소 1칸이고, 모집할 칸이 1칸 이상 남아야 해요.")
        ref_price = form.get("ref_price", "").replace(",", "").strip()
        ref_price = int(ref_price) if ref_price.isdigit() and int(ref_price) <= 5_000_000 else None
        category = form.get("category") if form.get("category") in CAT else "etc"
        pickup_at, deadline = parse(form.get("pickup_at")), parse(form.get("deadline"))
        check_times(pickup_at, deadline, errors)
        if is_shop and form.get("place") == "shop":
            pname, plat, plng = f"{shop['name']} (가게에서 수령)", shop["lat"], shop["lng"]
        else:
            pname = clean(form.get("place_name"), 40)
            plat, plng = to_float(form.get("place_lat"), -90, 90), to_float(form.get("place_lng"), -180, 180)
            if not pname:
                errors.append("픽업 장소 이름을 적어 주세요. (예: GS25 OO점 앞)")
            if plat is None or plng is None:
                errors.append("지도에서 픽업 장소를 찍어 주세요.")
                plat = plng = 0
            elif haversine(g.user["lat"], g.user["lng"], plat, plng) > MAX_PICKUP_KM * 1000:
                errors.append(f"픽업 장소는 내 동네에서 {MAX_PICKUP_KM}km 안으로 정해 주세요.")
        bank = clean(form.get("bank"), 60)
        if not bank or not re.search(r"\d{2,}", bank):
            errors.append("참여자가 송금할 계좌(은행명 + 계좌번호)를 입력해 주세요.")
        if not form.get("agree_rules"):
            errors.append("거래 원칙(공개 장소 수령 · 영수증 인증 · 식품 위생)에 동의해 주세요.")
        photo = None
        if not errors:
            try:
                photo = save_upload(request.files.get("photo"))
            except ValueError as e:
                errors.append(str(e))
        if errors:
            for e in errors:
                flash(e)
            return render_template("new_room.html", places=places, shop=shop, f=form)
        slot_price = math.ceil(total_price / slot_count / 10) * 10
        db.execute("UPDATE users SET bank=? WHERE id=?", (bank, g.user["id"]))
        room_id = db.execute(
            "INSERT INTO rooms (host_id, shop_id, title, category, buy_from, total_price, total_qty, unit, slot_qty,"
            " slot_count, host_slots, slot_price, ref_price, photo, pickup_place, pickup_lat, pickup_lng, pickup_at,"
            " deadline, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.user["id"], shop["id"] if is_shop else None, title, category,
             clean(form.get("buy_from"), 40), total_price, total_qty, clean(form.get("unit"), 6) or "개",
             slot_qty, slot_count, host_slots, slot_price, ref_price, photo, pname, plat, plng,
             iso(pickup_at), iso(deadline), iso(now())))
        sysmsg(room_id, "공구방이 열렸어요. 칸이 다 차면 입금이 시작돼요.")
        for u in db.query("SELECT * FROM users WHERE watch != '' AND id != ? AND lat IS NOT NULL AND deleted=0",
                          (g.user["id"],)):
            words = [w.strip() for w in u["watch"].split(",") if w.strip()]
            if any(w in title for w in words) and haversine(u["lat"], u["lng"], plat, plng) <= u["radius"]:
                notify(u["id"], "관심", f"관심 품목 ‘{title}’ 공구방이 근처에 열렸어요 · {pname}", f"/rooms/{room_id}")
        log.info(f"공구방 생성: room={room_id} host={g.user['id']} slots={slot_count}")
        return redirect(url_for("created", room_id=room_id))
    # 기본값: 가장 가까운 밤 9시(3시간 이상 뒤)에 모집 마감, 다음 날 저녁 7시 픽업
    dl = now().replace(hour=21, minute=0)
    if dl < now() + timedelta(hours=3):
        dl += timedelta(days=1)
    pk = (dl + timedelta(days=1)).replace(hour=19, minute=0)
    defaults = {"pickup_at": iso(pk), "deadline": iso(dl),
                "bank": g.user["bank"] or "", "host_slots": "1" if not shop else "0"}
    return render_template("new_room.html", places=places, shop=shop, f=defaults)


@app.route("/rooms/<int:room_id>/created")
@login_required
def created(room_id):
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    base = public_base() or request.host_url.rstrip("/")
    return render_template("created.html", room=room, share_url=base + f"/rooms/{room_id}")


# ---------------------------------------------------------------- 내 공구 · 채팅
@app.route("/my")
@login_required
def my():
    tab = request.args.get("tab", "host")
    uid = g.user["id"]
    closed = ",".join(f"'{s}'" for s in CLOSED)
    hosting = [enrich(r) for r in db.query(
        f"SELECT * FROM rooms WHERE host_id=? AND status NOT IN ({closed}) ORDER BY pickup_at", (uid,))]
    joined = [enrich(r) for r in db.query(
        "SELECT r.* FROM rooms r JOIN participations p ON p.room_id=r.id WHERE p.user_id=?"
        f" AND r.status NOT IN ({closed}) ORDER BY r.pickup_at", (uid,))]
    finished = [enrich(r) for r in db.query(
        "SELECT DISTINCT r.* FROM rooms r LEFT JOIN participations p ON p.room_id=r.id"
        f" WHERE (r.host_id=? OR p.user_id=?) AND r.status IN ({closed}) ORDER BY r.pickup_at DESC LIMIT 50",
        (uid, uid))]
    for r in hosting:
        r["unpaid"] = db.query("SELECT COUNT(*) AS n FROM participations WHERE room_id=? AND paid=0",
                               (r["id"],), one=True)["n"]
    return render_template("my.html", tab=tab, hosting=hosting, joined=joined, finished=finished, nav="my")


@app.route("/chats")
@login_required
def chats():
    uid = g.user["id"]
    rooms = [enrich(r) for r in db.query(
        "SELECT DISTINCT r.* FROM rooms r LEFT JOIN participations p ON p.room_id=r.id"
        " WHERE r.host_id=? OR p.user_id=? ORDER BY r.id DESC LIMIT 50", (uid, uid))]
    for r in rooms:
        r["last"] = db.query("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 1", (r["id"],), one=True)
    rooms.sort(key=lambda r: r["last"]["id"] if r["last"] else 0, reverse=True)
    return render_template("chats.html", rooms=rooms, nav="chat")


@app.route("/rooms/<int:room_id>/chat")
@login_required
def chat(room_id):
    room = get_room(room_id)
    require_member(room)
    return render_template("chat.html", room=room, members=members(room_id))


@app.route("/rooms/<int:room_id>/messages", methods=["GET", "POST"])
@login_required
def messages(room_id):
    room = get_room(room_id)
    require_member(room)
    if request.method == "POST":
        rate_limit("chat", 30, 60, key=str(g.user["id"]))
        body = str((request.get_json(silent=True) or {}).get("body", "")).strip()[:500]
        if body and room["status"] not in ("canceled",):
            db.execute("INSERT INTO messages (room_id, user_id, body, created_at) VALUES (?,?,?,?)",
                       (room_id, g.user["id"], body, iso(now())))
    after = request.args.get("after", 0, type=int)
    rows = db.query("SELECT m.*, u.nickname FROM messages m LEFT JOIN users u ON u.id=m.user_id"
                    " WHERE m.room_id=? AND m.id>? ORDER BY m.id LIMIT 200", (room_id, after))
    room_now = db.query("SELECT notice FROM rooms WHERE id=?", (room_id,), one=True)
    return jsonify(notice=room_now["notice"], messages=[
        {"id": m["id"], "body": m["body"], "system": bool(m["is_system"]), "nickname": m["nickname"],
         "mine": m["user_id"] == g.user["id"], "time": m["created_at"][11:16]} for m in rows])


@app.route("/rooms/<int:room_id>/notice", methods=["POST"])
@login_required
def set_notice(room_id):
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    notice = clean(request.form.get("notice"), 100) or None
    db.execute("UPDATE rooms SET notice=? WHERE id=?", (notice, room_id))
    if notice:
        sysmsg(room_id, f"방장이 공지를 등록했어요: {notice}")
    return redirect(url_for("chat", room_id=room_id))


# ---------------------------------------------------------------- 입금 확인 · 구매 인증
@app.route("/rooms/<int:room_id>/deposits", methods=["GET", "POST"])
@login_required
def deposits(room_id):
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle" and room["status"] in ("recruiting", "deposit"):
            pid = request.form.get("pid", type=int)
            p = db.query("SELECT * FROM participations WHERE id=? AND room_id=?", (pid, room_id), one=True)
            if p:
                on = 0 if p["confirmed"] else 1
                db.execute("UPDATE participations SET confirmed=?, paid=MAX(paid, ?), paid_at=COALESCE(paid_at, ?)"
                           " WHERE id=?", (on, on, iso(now()) if on else None, pid))
                if on:
                    notify(p["user_id"], "입금", f"방장이 ‘{room['title']}’ 입금을 확인했어요.", f"/rooms/{room_id}")
        elif action == "remind":
            rate_limit("remind", 5, 3600, key=f"{g.user['id']}:{room_id}")
            unpaid = db.query("SELECT * FROM participations WHERE room_id=? AND paid=0", (room_id,))
            for p in unpaid:
                notify(p["user_id"], "입금 요청", f"‘{room['title']}’ 입금을 기다리고 있어요. 계좌: {room['host']['bank']}",
                       f"/rooms/{room_id}")
            flash(f"미입금자 {len(unpaid)}명에게 알림을 보냈어요." if unpaid else "미입금자가 없어요.")
        elif action == "purchase":
            ps = db.query("SELECT * FROM participations WHERE room_id=?", (room_id,))
            if room["status"] != "deposit" or not ps or any(not p["confirmed"] for p in ps):
                flash("모든 칸이 차고, 모든 참여자의 입금을 확인해야 구매를 확정할 수 있어요.")
            else:
                if db.update("UPDATE rooms SET status='buying' WHERE id=? AND status='deposit'", (room_id,)) == 0:
                    # 그 사이 구매 확정 기한이 지나 자동 취소됐다
                    flash("구매 확정 기한이 지나 공구가 자동 취소됐어요. 참여자에게 입금액을 돌려주세요.")
                    return redirect(url_for("deposits", room_id=room_id))
                sysmsg(room_id, "방장이 구매를 확정했어요. 장보기 후 영수증이 올라와요.")
                for p in ps:
                    notify(p["user_id"], "구매 확정", f"‘{room['title']}’ 구매가 확정됐어요.", f"/rooms/{room_id}/chat")
                return redirect(url_for("proof", room_id=room_id))
        return redirect(url_for("deposits", room_id=room_id))
    ps = members(room_id)
    deadline = parse(room["full_at"]) + timedelta(hours=DEPOSIT_HOURS) if room["full_at"] else None
    due = purchase_deadline(room) if room["status"] == "deposit" else None
    return render_template("deposits.html", room=room, ps=ps, purchase_due=iso(due) if due else None,
                           all_ok=room["status"] == "deposit" and ps and all(p["confirmed"] for p in ps),
                           pay_deadline=iso(deadline) if deadline else None)


@app.route("/rooms/<int:room_id>/proof", methods=["GET", "POST"])
@login_required
def proof(room_id):
    room = get_room(room_id)
    if not room["is_host"]:
        abort(403)
    if room["status"] != "buying":
        flash("구매 확정 후에 구매 인증을 할 수 있어요.")
        return redirect(url_for("my"))
    if request.method == "POST":
        try:
            receipt = save_upload(request.files.get("receipt"), private=True)
            product = save_upload(request.files.get("product"), private=True)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("proof", room_id=room_id))
        if not receipt:
            flash("영수증 사진을 올려 주세요.")
            return redirect(url_for("proof", room_id=room_id))
        expiry = request.form.get("expiry") or None
        if expiry and not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
            expiry = None
        db.execute("UPDATE rooms SET status='pickup', receipt_photo=?, product_photo=?, expiry=? WHERE id=?",
                   (receipt, product, expiry, room_id))
        sysmsg(room_id, f"구매 완료! 영수증이 올라왔어요. 픽업: {dt(room['pickup_at'])} · {room['pickup_place']}")
        for p in db.query("SELECT * FROM participations WHERE room_id=?", (room_id,)):
            notify(p["user_id"], "구매 완료", f"{room['host']['nickname']}님이 ‘{room['title']}’ 장보기를 마쳤어요.",
                   f"/rooms/{room_id}/pickup")
        return redirect(url_for("pickup", room_id=room_id))
    return render_template("proof.html", room=room)


# ---------------------------------------------------------------- 픽업 · 후기 · 신고
@app.route("/rooms/<int:room_id>/pickup", methods=["GET", "POST"])
@login_required
def pickup(room_id):
    room = get_room(room_id)
    require_member(room)
    if room["status"] not in ("pickup", "done"):
        flash("구매 인증이 끝나면 픽업 화면이 열려요.")
        return redirect(url_for("my", tab="host" if room["is_host"] else "join"))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "arrive" and room["status"] == "pickup":
            rate_limit("arrive", 3, 600, key=f"{g.user['id']}:{room_id}")
            db.execute("UPDATE rooms SET arrived=1 WHERE id=?", (room_id,))
            sysmsg(room_id, f"{g.user['nickname']}님이 픽업 장소에 도착했어요 📍")
            targets = [room["host_id"]] + [p["user_id"] for p in members(room_id)]
            for uid in set(targets) - {g.user["id"]}:
                notify(uid, "픽업", f"{g.user['nickname']}님이 {room['pickup_place']}에 도착했어요.",
                       f"/rooms/{room_id}/pickup")
        elif action == "receive" and room["mine"] and room["status"] == "pickup" and not room["mine"]["received"]:
            with db.transaction():
                db.execute("UPDATE participations SET received=1 WHERE id=?", (room["mine"]["id"],))
                left_n = db.query("SELECT COUNT(*) AS n FROM participations WHERE room_id=? AND received=0",
                                  (room_id,), one=True)["n"]
                if left_n == 0:
                    db.execute("UPDATE rooms SET status='done' WHERE id=?", (room_id,))
            sysmsg(room_id, f"{g.user['nickname']}님이 수령을 확인했어요 ✓")
            if left_n == 0:
                sysmsg(room_id, "모든 참여자가 수령했어요. 거래 완료! 후기를 남겨 주세요.")
                for uid in [room["host_id"]] + [p["user_id"] for p in members(room_id)]:
                    notify(uid, "거래 완료", f"‘{room['title']}’ 거래가 끝났어요. 후기를 남겨 주세요.",
                           f"/rooms/{room_id}/review")
            return redirect(url_for("review_pick", room_id=room_id))
        return redirect(url_for("pickup", room_id=room_id))
    auto_at = parse(room["pickup_at"]) + timedelta(hours=AUTO_RECEIVE_HOURS)
    return render_template("pickup.html", room=room, ps=members(room_id), auto_receive_at=iso(auto_at),
                           auto_hours=AUTO_RECEIVE_HOURS)


@app.route("/rooms/<int:room_id>/report", methods=["POST"])
@login_required
def report(room_id):
    room = get_room(room_id)
    rate_limit("report", 10, 3600, key=str(g.user["id"]))
    body = clean(request.form.get("body"), 1000)
    if body:
        db.execute("INSERT INTO reports (room_id, user_id, body, created_at) VALUES (?,?,?,?)",
                   (room_id, g.user["id"], body, iso(now())))
        if room["is_host"] or room["mine"]:
            sysmsg(room_id, f"{g.user['nickname']}님이 문제를 신고했어요. 운영팀이 확인할게요.")
        for a in [u for u in db.query("SELECT * FROM users WHERE is_admin=1 AND deleted=0") if is_admin_user(u)]:
            notify(a["id"], "신고", f"신고 접수 · {room['title']}", "/admin")
        if SITE["contact"]:
            send_mail(SITE["contact"], f"[반띵] 신고 접수 #{room_id}", f"{room['title']}\n\n{body}")
        log.warning(f"신고 접수: room={room_id} reporter={g.user['id']}")
        flash("신고가 접수됐어요. 운영팀이 확인 후 연락드릴게요.")
    return redirect(safe_next(request.form.get("next"), url_for("room_detail", room_id=room_id)))


@app.route("/private/<name>")
@login_required
def private_upload(name):
    r = db.query("SELECT id FROM rooms WHERE receipt_photo=? OR product_photo=?", (name, name), one=True)
    if not r:
        abort(404)
    require_member(get_room(r["id"]))
    resp = send_from_directory(app.config["UPLOAD_PRIVATE"], name)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.route("/uploads/<name>")
def public_upload(name):
    return send_from_directory(app.config["UPLOAD_PUBLIC"], name)


def review_targets(room):
    uid = g.user["id"]
    if room["is_host"]:
        ids = [(p["user_id"], p["nickname"]) for p in members(room["id"])]
    else:
        ids = [(room["host_id"], room["host"]["nickname"])]
    done = {r["to_id"]: r["id"] for r in db.query("SELECT id, to_id FROM reviews WHERE room_id=? AND from_id=?",
                                                  (room["id"], uid))}
    return [{"id": i, "nickname": n, "done": i in done, "review_id": done.get(i)} for i, n in ids]


@app.route("/rooms/<int:room_id>/review")
@login_required
def review_pick(room_id):
    room = get_room(room_id)
    require_member(room)
    targets = review_targets(room)
    pending = [t for t in targets if not t["done"]]
    if room["status"] == "done" and len(pending) == 1:
        return redirect(url_for("review", room_id=room_id, to_id=pending[0]["id"]))
    return render_template("review_pick.html", room=room, targets=targets)


@app.route("/rooms/<int:room_id>/review/<int:to_id>", methods=["GET", "POST"])
@login_required
def review(room_id, to_id):
    room = get_room(room_id)
    require_member(room)
    target = next((t for t in review_targets(room) if t["id"] == to_id), None)
    if not target:
        abort(404)
    if room["status"] != "done" and not (room["mine"] and room["mine"]["received"]):
        flash("수령을 확인한 뒤 후기를 남길 수 있어요.")
        return redirect(url_for("pickup", room_id=room_id))
    if target["done"]:
        flash("이미 후기를 남겼어요.")
        return redirect(url_for("review_pick", room_id=room_id))
    if request.method == "POST":
        good = [t for t in request.form.getlist("good") if t in GOOD_TAGS]
        bad = [t for t in request.form.getlist("bad") if t in BAD_TAGS]
        comment = clean(request.form.get("comment"), 100) or None
        try:
            db.execute("INSERT INTO reviews (room_id, from_id, to_id, good, bad, comment, created_at)"
                       " VALUES (?,?,?,?,?,?,?)", (room_id, g.user["id"], to_id, ",".join(good), ",".join(bad),
                                                   comment, iso(now())))
        except db.sqlite3.IntegrityError:
            flash("이미 후기를 남겼어요.")
            return redirect(url_for("review_pick", room_id=room_id))
        notify(to_id, "후기", f"{g.user['nickname']}님이 후기를 남겼어요"
               + (f" · “{good[0]}”" if good else ""), "/me")
        log.info(f"후기 작성: room={room_id} from={g.user['id']} to={to_id}")
        flash("후기를 남겼어요. 고마워요!")
        return redirect(url_for("review_pick", room_id=room_id))
    return render_template("review.html", room=room, target=target, good_tags=GOOD_TAGS, bad_tags=BAD_TAGS,
                           record=None)


REVIEW_EDIT_DAYS = 7  # 후기는 남긴 뒤 7일 안에만 고치거나 지울 수 있다 (신뢰도 조작 방지)


def my_review(review_id):
    """내가 쓴 후기만 찾는다. 남의 후기 번호를 넣으면 NotFound(404)."""
    return get_or_404("SELECT rv.*, u.nickname AS to_nickname FROM reviews rv JOIN users u ON u.id = rv.to_id"
                      " WHERE rv.id=? AND rv.from_id=?", (review_id, g.user["id"]), "Review")


def review_editable(record):
    created_at = parse(record["created_at"])
    return bool(created_at and now() <= created_at + timedelta(days=REVIEW_EDIT_DAYS))


@app.route("/reviews/<int:review_id>/edit", methods=["GET", "POST"])
@login_required
def review_update(review_id):
    """수업 자료의 rhema_update: 기존 후기(record)를 불러와 같은 화면(review.html)을 수정 모드로 쓴다."""
    try:
        record = my_review(review_id)
    except NotFound as e:
        return not_found_page(e, "review_update")
    room = get_room(record["room_id"])
    if not review_editable(record):
        flash(f"후기는 남긴 뒤 {REVIEW_EDIT_DAYS}일 안에만 고칠 수 있어요.")
        return redirect(url_for("review_pick", room_id=room["id"]))
    if request.method == "POST":
        good = [t for t in request.form.getlist("good") if t in GOOD_TAGS]
        bad = [t for t in request.form.getlist("bad") if t in BAD_TAGS]
        comment = clean(request.form.get("comment"), 100) or None
        db.execute("UPDATE reviews SET good=?, bad=?, comment=? WHERE id=? AND from_id=?",
                   (",".join(good), ",".join(bad), comment, review_id, g.user["id"]))
        log.info(f"후기 수정: review={review_id} room={room['id']} from={g.user['id']}")
        flash("후기를 수정했어요.")
        return redirect(url_for("review_pick", room_id=room["id"]))
    target = {"id": record["to_id"], "nickname": record["to_nickname"]}
    return render_template("review.html", room=room, target=target, good_tags=GOOD_TAGS, bad_tags=BAD_TAGS,
                           record=record, good=record["good"].split(","), bad=record["bad"].split(","))


@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@login_required
def review_delete(review_id):
    """수업 자료의 rhema_delete 를 그대로 옮긴 구조: try → except(로그 + 오류 화면) → else(삭제)."""
    try:
        record = my_review(review_id)
    except NotFound as e:
        return not_found_page(e, "review_delete")
    else:
        if not review_editable(record):
            flash(f"후기는 남긴 뒤 {REVIEW_EDIT_DAYS}일 안에만 지울 수 있어요.")
            return redirect(url_for("review_pick", room_id=record["room_id"]))
        db.execute("DELETE FROM reviews WHERE id=? AND from_id=?", (review_id, g.user["id"]))
        log.info(f"후기 삭제: review={review_id} room={record['room_id']} from={g.user['id']}")
        flash("후기를 삭제했어요.")
        return redirect(url_for("review_pick", room_id=record["room_id"]))


# ---------------------------------------------------------------- 알림 · 마이 · 탈퇴
@app.route("/notifications")
@login_required
def notifications():
    rows = [dict(r) for r in db.query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
                                      (g.user["id"],))]
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (g.user["id"],))
    return render_template("notifications.html", rows=rows)


@app.route("/me", methods=["GET", "POST"])
@login_required
def me():
    if request.method == "POST":
        action = request.form.get("action", "watch")
        if action == "watch":
            watch = ",".join(clean(w, 15) for w in request.form.get("watch", "").split(",") if w.strip())[:100]
            db.execute("UPDATE users SET watch=? WHERE id=?", (watch, g.user["id"]))
            flash("관심 품목을 저장했어요. 근처에 새 방이 열리면 알려 드릴게요.")
        elif action == "bank":
            bank = clean(request.form.get("bank"), 60)
            db.execute("UPDATE users SET bank=? WHERE id=?", (bank or None, g.user["id"]))
            flash("송금 계좌를 저장했어요.")
        elif action == "password" and g.user["password_hash"]:
            cur, pw, pw2 = (request.form.get(k, "") for k in ("current", "password", "password2"))
            if not check_password_hash(g.user["password_hash"], cur):
                flash("현재 비밀번호가 맞지 않아요.")
            elif len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
                flash("새 비밀번호는 영문과 숫자를 섞어 8자 이상으로 정해 주세요.")
            elif pw != pw2:
                flash("새 비밀번호 확인이 일치하지 않아요.")
            else:
                new_hash = generate_password_hash(pw)
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, g.user["id"]))
                session["pw"] = password_signature({"password_hash": new_hash})  # 이 기기는 그대로, 다른 기기는 로그아웃
                log.info(f"비밀번호 변경: user={g.user['id']}")
                flash("비밀번호를 바꿨어요. 다른 기기에서는 다시 로그인해야 해요.")
        return redirect(url_for("me"))
    reviews = db.query("SELECT r.*, u.nickname FROM reviews r JOIN users u ON u.id=r.from_id"
                       " WHERE r.to_id=? ORDER BY r.id DESC LIMIT 5", (g.user["id"],))
    return render_template("me.html", stats=user_stats(g.user["id"]), reviews=reviews, nav="me")


@app.route("/account/delete", methods=["GET", "POST"])
@login_required
def account_delete():
    uid = g.user["id"]
    closed = ",".join(f"'{s}'" for s in CLOSED)
    # 진행 중인 거래(입금·구매·픽업 단계, 이미 입금한 참여, 참여자가 있는 내 방)가 있으면 탈퇴를 미룬다
    active = db.query(
        f"SELECT r.id, r.title FROM rooms r WHERE r.status NOT IN ({closed}) AND ("
        "  (r.host_id=? AND (r.status != 'recruiting' OR EXISTS (SELECT 1 FROM participations p WHERE p.room_id=r.id)))"
        "  OR EXISTS (SELECT 1 FROM participations p WHERE p.room_id=r.id AND p.user_id=?"
        "             AND (r.status != 'recruiting' OR p.paid=1)))", (uid, uid))
    if request.method == "POST":
        ok = (check_password_hash(g.user["password_hash"], request.form.get("password", ""))
              if g.user["password_hash"] else request.form.get("confirm") == "탈퇴")
        if active:
            flash("진행 중인 거래가 끝난 뒤에 탈퇴할 수 있어요.")
        elif not ok:
            flash("비밀번호(또는 확인 문구)가 맞지 않아요.")
        else:
            with db.transaction():
                for r in db.query("SELECT id FROM rooms WHERE host_id=? AND status='recruiting'", (uid,)):
                    db.execute("UPDATE rooms SET status='failed', cancel_reason='방장 탈퇴' WHERE id=?", (r["id"],))
                db.execute("DELETE FROM participations WHERE user_id=? AND room_id IN"
                           " (SELECT id FROM rooms WHERE status='recruiting')", (uid,))
                db.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
                db.execute("DELETE FROM email_codes WHERE user_id=?", (uid,))
                db.execute("UPDATE users SET nickname=?, email=NULL, password_hash=NULL, provider_id=NULL,"
                           " school_email=NULL, school_verified=0, neighborhood=NULL, lat=NULL, lng=NULL, bank=NULL,"
                           " watch='', deleted=1 WHERE id=?", (f"탈퇴회원{uid}", uid))
            session.clear()
            log.info(f"회원 탈퇴: user={uid}")
            flash("탈퇴가 완료됐어요. 그동안 반띵을 이용해 주셔서 고마워요.")
            return redirect(url_for("index"))
    return render_template("account_delete.html", active=active)


# ---------------------------------------------------------------- 약관 · 개인정보 · 운영
@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/healthz")
def healthz():
    db.query("SELECT 1", one=True)
    return {"ok": True}


@app.route("/robots.txt")
def robots():
    return app.response_class("User-agent: *\nAllow: /$\nAllow: /terms\nAllow: /privacy\nDisallow: /\n",
                              mimetype="text/plain")


@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    if request.method == "POST":
        a = request.form.get("action")
        if a == "resolve":
            db.execute("UPDATE reports SET resolved=1 WHERE id=?", (request.form.get("id", type=int),))
        elif a == "cancel_room":
            rid = request.form.get("id", type=int)
            r = db.query("SELECT * FROM rooms WHERE id=?", (rid,), one=True)
            if r and r["status"] not in CLOSED:
                reason = clean(request.form.get("reason"), 100) or "운영 정책 위반"
                db.execute("UPDATE rooms SET status='canceled', cancel_reason=? WHERE id=?", (reason, rid))
                sysmsg(rid, f"운영팀이 공구방을 취소했어요. 사유: {reason}")
                log.warning(f"[운영] 공구방 취소: room={rid} admin={g.user['id']} reason={reason!r}")
                for uid in [r["host_id"]] + [p["user_id"] for p in members(rid)]:
                    notify(uid, "공구", f"‘{r['title']}’ 공구방이 운영 정책으로 취소됐어요. 입금했다면 방장에게 환불을 요청하세요.",
                           f"/rooms/{rid}/chat")
        elif a in ("ban", "unban"):
            uid = request.form.get("id", type=int)
            if uid != g.user["id"]:
                db.execute("UPDATE users SET banned=? WHERE id=?", (1 if a == "ban" else 0, uid))
                log.warning(f"[운영] 이용 {'정지' if a == 'ban' else '정지 해제'}: user={uid} admin={g.user['id']}")
        elif a == "add_shop":
            owner = db.query("SELECT id FROM users WHERE nickname=? AND deleted=0",
                             (clean(request.form.get("owner"), 12),), one=True)
            lat, lng = to_float(request.form.get("lat"), -90, 90), to_float(request.form.get("lng"), -180, 180)
            name = clean(request.form.get("name"), 30)
            if not owner or lat is None or lng is None or not name:
                flash("상점 등록: 사장님 닉네임, 상점 이름, 좌표를 확인해 주세요.")
            else:
                db.execute("INSERT INTO shops (owner_id, name, kind, hours, lat, lng) VALUES (?,?,?,?,?,?)",
                           (owner["id"], name, clean(request.form.get("kind"), 10),
                            clean(request.form.get("hours"), 40), lat, lng))
                flash(f"상점 ‘{name}’을 등록했어요.")
        elif a == "add_place":
            lat, lng = to_float(request.form.get("lat"), -90, 90), to_float(request.form.get("lng"), -180, 180)
            name = clean(request.form.get("name"), 40)
            if name and lat is not None and lng is not None:
                db.execute("INSERT INTO places (name, lat, lng) VALUES (?,?,?)", (name, lat, lng))
                flash(f"추천 픽업 장소 ‘{name}’을 추가했어요.")
        return redirect(url_for("admin", q=request.args.get("q", "")))
    q = clean(request.args.get("q"), 30)
    reports = db.query("SELECT rp.*, r.title, r.status, u.nickname FROM reports rp JOIN rooms r ON r.id=rp.room_id"
                       " JOIN users u ON u.id=rp.user_id ORDER BY rp.resolved, rp.id DESC LIMIT 50")
    rooms = db.query("SELECT r.*, u.nickname FROM rooms r JOIN users u ON u.id=r.host_id ORDER BY r.id DESC LIMIT 30")
    users = db.query("SELECT * FROM users WHERE deleted=0 AND (nickname LIKE ? OR email LIKE ?) ORDER BY id DESC LIMIT 30",
                     (f"%{q}%", f"%{q}%"))
    counts = db.query("SELECT (SELECT COUNT(*) FROM users WHERE deleted=0) AS users,"
                      " (SELECT COUNT(*) FROM rooms) AS rooms,"
                      " (SELECT COUNT(*) FROM rooms WHERE status='done') AS done,"
                      " (SELECT COUNT(*) FROM reports WHERE resolved=0) AS open_reports", one=True)
    return render_template("admin.html", reports=reports, rooms=rooms, users=users, q=q, counts=counts)


# ---------------------------------------------------------------- 오류 화면
ERROR_MSG = {
    400: "요청을 처리하지 못했어요. 페이지를 새로고침한 뒤 다시 시도해 주세요.",
    401: "로그인이 필요해요.",
    403: "이 공구방의 방장이나 참여자만 볼 수 있어요.",
    404: "페이지를 찾을 수 없어요.",
    413: "사진 용량이 너무 커요. 10MB 이하로 올려 주세요.",
    429: "요청이 너무 많아요. 잠시 후 다시 시도해 주세요.",
    500: "일시적인 오류가 생겼어요. 잠시 후 다시 시도해 주세요.",
}


@app.errorhandler(HTTPException)
def http_error(e):
    # 403(권한 없음) · 400(CSRF) · 429(요청 과다)는 공격 신호일 수 있어 WARNING, 나머지 4xx 는 INFO 로 남긴다
    level = logging.WARNING if e.code in (400, 403, 429) else logging.INFO
    log.log(level, f"HTTP {e.code}: {request.method} {request.path} "
                   f"(user={g.user['id'] if g.get('user') else '-'}, ip={client_ip()})")
    if request.path.endswith("/messages"):
        return jsonify(error=e.code), e.code
    return render_template("error.html", msg=ERROR_MSG.get(e.code, e.description), err_msg=str(e.code)), e.code


@app.errorhandler(Exception)
def server_error(e):
    # 예상하지 못한 오류: 스택 트레이스백까지(exc_info) 로그 파일에 남겨 디버깅에 쓴다
    log.critical(f"처리되지 않은 오류: {request.method} {request.path} → {e!r}", exc_info=True)
    return render_template("error.html", msg=ERROR_MSG[500], err_msg="500"), 500


if __name__ == "__main__":
    # 로컬 개발용. 배포에서는 gunicorn을 쓴다.
    app.run(debug=ENV.get("FLASK_DEBUG") == "1", host=ENV.get("HOST", "127.0.0.1"), port=int(ENV.get("PORT", 5000)))
