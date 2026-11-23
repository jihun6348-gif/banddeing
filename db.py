"""SQLite 연결, 테이블 생성·마이그레이션, (데모 모드 전용) 데모 데이터."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import current_app, g

KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL UNIQUE,
    provider TEXT,
    school_email TEXT,
    school_verified INTEGER NOT NULL DEFAULT 0,
    neighborhood TEXT,
    lat REAL,
    lng REAL,
    radius INTEGER NOT NULL DEFAULT 500,
    bank TEXT,
    watch TEXT NOT NULL DEFAULT '',
    noshow INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    kind TEXT,
    hours TEXT,
    lat REAL NOT NULL,
    lng REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES users(id),
    shop_id INTEGER REFERENCES shops(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    buy_from TEXT,
    total_price INTEGER NOT NULL,
    total_qty REAL NOT NULL,
    unit TEXT NOT NULL,
    slot_qty REAL NOT NULL,
    slot_count INTEGER NOT NULL,
    host_slots INTEGER NOT NULL,
    slot_price INTEGER NOT NULL,
    ref_price INTEGER,
    photo TEXT,
    pickup_place TEXT NOT NULL,
    pickup_lat REAL NOT NULL,
    pickup_lng REAL NOT NULL,
    pickup_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recruiting',
    notice TEXT,
    receipt_photo TEXT,
    product_photo TEXT,
    expiry TEXT,
    full_at TEXT,
    arrived INTEGER NOT NULL DEFAULT 0,
    pickup_notified INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    slots INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    received INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (room_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    body TEXT NOT NULL,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    from_id INTEGER NOT NULL REFERENCES users(id),
    to_id INTEGER NOT NULL REFERENCES users(id),
    good TEXT NOT NULL DEFAULT '',
    bad TEXT NOT NULL DEFAULT '',
    comment TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (room_id, from_id, to_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""

# 기존 MVP DB에 없던 열 — 있으면 건너뛴다
MIGRATIONS = [
    ("users", "email", "TEXT"),
    ("users", "password_hash", "TEXT"),
    ("users", "provider_id", "TEXT"),
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "banned", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "deleted", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "agreed_at", "TEXT"),
    ("users", "last_login", "TEXT"),
    ("rooms", "cancel_reason", "TEXT"),
    ("reports", "resolved", "INTEGER NOT NULL DEFAULT 0"),
    # 운영자 이메일 인증을 마친 이메일. ADMIN_EMAILS 에 있고 이 값과 같아야 운영자가 된다
    ("users", "admin_verified_email", "TEXT"),
]

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_provider ON users(provider, provider_id);
CREATE INDEX IF NOT EXISTS ix_rooms_status ON rooms(status, deadline);
CREATE INDEX IF NOT EXISTS ix_rooms_host ON rooms(host_id);
CREATE INDEX IF NOT EXISTS ix_part_user ON participations(user_id);
CREATE INDEX IF NOT EXISTS ix_part_room ON participations(room_id);
CREATE INDEX IF NOT EXISTS ix_msg_room ON messages(room_id, id);
CREATE INDEX IF NOT EXISTS ix_noti_user ON notifications(user_id, is_read);
"""


def connect(path):
    # isolation_level=None: 문장마다 바로 저장, 여러 문장을 묶을 땐 transaction() 사용
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_e=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    cur = get_db().execute(sql, args)
    return cur.lastrowid


def update(sql, args=()):
    """UPDATE/DELETE 를 실행하고 실제로 바뀐 행 수(rowcount)를 돌려준다.
    'WHERE status=...' 조건을 붙인 상태 변경이 0이면, 그 사이에 다른 요청이 먼저 상태를 바꾼 것이다."""
    cur = get_db().execute(sql, args)
    return cur.rowcount


@contextmanager
def transaction():
    """BEGIN IMMEDIATE로 쓰기 잠금을 먼저 잡아, 동시에 참여해도 칸이 넘치지 않게 한다."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn):
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.executescript(INDEXES)


def init_app(app):
    app.teardown_appcontext(close_db)
    conn = connect(app.config["DATABASE"])
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    if app.config.get("DEMO_MODE") and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("BEGIN")
        seed(conn)
        conn.execute("COMMIT")
    conn.close()


def _t(d):
    return d.strftime("%Y-%m-%dT%H:%M")


def seed(conn):
    """데모 모드(DEMO_MODE=1)에서만 넣는 회기역 주변 데모 데이터. 실제 서비스에서는 넣지 않는다."""
    now = datetime.now(KST).replace(tzinfo=None, second=0, microsecond=0)
    c = conn.cursor()
    lat, lng = 37.5898, 127.0579  # 회기역

    users = [
        # nickname, provider, school_email, verified, neighborhood, lat, lng, radius, bank
        ("서연", "demo", "seoyeon@khu.ac.kr", 1, "회기동", lat + 0.0006, lng - 0.0012, 1000, "국민 123-45-678901"),
        ("민호", "demo", None, 0, "회기동", lat - 0.0004, lng + 0.0008, 1000, "신한 110-222-333444"),
        ("지원", "demo", "jiwon@khu.ac.kr", 1, "회기동", lat + 0.0012, lng + 0.0004, 1000, "우리 1002-555-666777"),
        ("회기정육점", "demo", None, 0, "회기동", lat + 0.0010, lng - 0.0016, 1000, "농협 301-1111-2222-33"),
        ("회기청과", "demo", None, 0, "회기동", lat - 0.0020, lng - 0.0010, 1000, "기업 010-1234-5678"),
    ]
    for u in users:
        c.execute(
            "INSERT INTO users (nickname, provider, school_email, school_verified, neighborhood, lat, lng, radius, bank,"
            " created_at, agreed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*u, _t(now - timedelta(days=60)), _t(now - timedelta(days=60))),
        )
    seoyeon, minho, jiwon, butcher, fruit = 1, 2, 3, 4, 5
    c.execute("UPDATE users SET watch='양파,두부' WHERE id=?", (minho,))

    c.execute("INSERT INTO shops (owner_id, name, kind, hours, lat, lng) VALUES (?,?,?,?,?,?)",
              (butcher, "회기정육점", "정육", "픽업 17:00~21:00", lat + 0.0010, lng - 0.0016))
    c.execute("INSERT INTO shops (owner_id, name, kind, hours, lat, lng) VALUES (?,?,?,?,?,?)",
              (fruit, "회기청과", "청과", "매주 화·금 공구", lat - 0.0020, lng - 0.0010))

    places = [
        ("GS25 회기역점 앞", lat - 0.0003, lng - 0.0004),
        ("회기역 1번 출구", lat, lng),
        ("경희대 정문 버스정류장", lat + 0.0030, lng - 0.0060),
        ("CU 회기파출소점 앞", lat + 0.0008, lng - 0.0020),
        ("이문동 주민센터 앞", lat + 0.0040, lng + 0.0030),
    ]
    c.executemany("INSERT INTO places (name, lat, lng) VALUES (?,?,?)", places)

    def room(host, title, cat, buy_from, total, qty, unit, sqty, scount, hslots, sprice, ref,
             place, plat, plng, pickup, deadline, status="recruiting", shop=None):
        c.execute(
            "INSERT INTO rooms (host_id, shop_id, title, category, buy_from, total_price, total_qty, unit, slot_qty,"
            " slot_count, host_slots, slot_price, ref_price, pickup_place, pickup_lat, pickup_lng, pickup_at, deadline,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (host, shop, title, cat, buy_from, total, qty, unit, sqty, scount, hslots, sprice, ref,
             place, plat, plng, _t(pickup), _t(deadline), status, _t(now - timedelta(hours=5))),
        )
        return c.lastrowid

    gs = places[0]
    egg = room(seoyeon, "계란 30구 (특란)", "egg", "이마트 청량리점", 9000, 30, "구", 10, 3, 1, 3000, 4000,
               gs[0], gs[1], gs[2], now + timedelta(days=1, hours=3), now + timedelta(hours=10))
    c.execute("INSERT INTO participations (room_id, user_id, slots, paid, paid_at, created_at) VALUES (?,?,?,?,?,?)",
              (egg, minho, 1, 1, _t(now - timedelta(hours=2)), _t(now - timedelta(hours=3))))
    c.execute("INSERT INTO messages (room_id, user_id, body, is_system, created_at) VALUES (?,?,?,?,?)",
              (egg, None, "민호님이 1칸 참여했어요", 1, _t(now - timedelta(hours=3))))
    c.execute("INSERT INTO messages (room_id, user_id, body, is_system, created_at) VALUES (?,?,?,?,?)",
              (egg, minho, "안녕하세요! 에코백 챙겨 갈게요", 0, _t(now - timedelta(hours=2))))

    p3 = places[2]
    room(jiwon, "양파 3kg 망", "veg", "경동시장", 6900, 3, "kg", 1, 3, 1, 2300, 3500,
         p3[0], p3[1], p3[2], now + timedelta(days=2), now + timedelta(days=1))
    p2 = places[1]
    room(minho, "키친타월 12롤", "daily", "쿠팡", 11600, 12, "롤", 3, 4, 1, 2900, 3900,
         p2[0], p2[1], p2[2], now + timedelta(days=3), now + timedelta(days=2))

    room(butcher, "한돈 앞다리 1kg", "meat", "회기정육점", 24500, 1000, "g", 200, 5, 0, 4900, 6500,
         "회기정육점 (가게에서 수령)", lat + 0.0010, lng - 0.0016, now + timedelta(days=1, hours=8),
         now + timedelta(hours=20), shop=1)
    room(fruit, "사과 5kg 한 박스", "veg", "회기청과", 26000, 5, "kg", 1, 5, 0, 5200, 7000,
         "회기청과 (가게에서 수령)", lat - 0.0020, lng - 0.0010, now + timedelta(days=2, hours=5),
         now + timedelta(days=1, hours=6), shop=2)

    milk = room(jiwon, "우유 2.3L × 2팩", "egg", "홈플러스 동대문점", 9600, 2, "팩", 1, 2, 1, 4800, 5900,
                gs[0], gs[1], gs[2], now - timedelta(days=3), now - timedelta(days=4), status="done")
    c.execute("INSERT INTO participations (room_id, user_id, slots, paid, paid_at, confirmed, received, created_at)"
              " VALUES (?,?,?,?,?,?,?,?)", (milk, seoyeon, 1, 1, _t(now - timedelta(days=4)), 1, 1,
                                          _t(now - timedelta(days=4))))
    c.execute("INSERT INTO reviews (room_id, from_id, to_id, good, bad, comment, created_at) VALUES (?,?,?,?,?,?,?)",
              (milk, jiwon, seoyeon, "시간 약속을 잘 지켜요,응답이 빨라요", "", "다음에도 같이 사요!",
               _t(now - timedelta(days=3))))
    c.execute("INSERT INTO reviews (room_id, from_id, to_id, good, bad, comment, created_at) VALUES (?,?,?,?,?,?,?)",
              (milk, seoyeon, jiwon, "상품 상태가 좋아요,소분이 정확해요", "", None, _t(now - timedelta(days=3))))
