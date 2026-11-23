import os
from datetime import datetime, timedelta, timezone

from conftest import flashes, make_user, new_client, png_bytes, post


def room_form(**over):
    from app import iso, now
    f = {"title": "계란 30구", "category": "egg", "total_price": "9000", "total_qty": "30", "unit": "구",
         "slot_qty": "10", "host_slots": "1", "place": "map", "place_name": "GS25 회기역점 앞",
         "place_lat": "37.5895", "place_lng": "127.0575", "pickup_at": iso(now() + timedelta(days=1, hours=3)),
         "deadline": iso(now() + timedelta(hours=10)), "bank": "국민 123-45-678901 홍길동", "agree_rules": "1"}
    f.update(over)
    return f


def create_room(c, **over):
    data = room_form(**over)
    data["photo"] = (png_bytes(exif_gps=True), "egg.jpg")
    r = post(c, "/rooms/new", data, content_type="multipart/form-data")
    assert r.status_code == 302 and "/created" in r.location, flashes(r)
    return int(r.location.split("/")[-2])


# ---------------------------------------------------------------- 인증 · 보안
def test_csrf_required():
    c = new_client()
    r = c.post("/signup", data={"email": "x@test.kr"})
    assert r.status_code == 400


def test_signup_and_login(mod):
    c = make_user(email="login@test.kr")
    post(c, "/logout")
    c2 = new_client()
    r = post(c2, "/login", {"email": "login@test.kr", "password": "wrong-pass1"})
    assert "/home" not in (r.location or "") and "맞지 않아요" in "".join(flashes(r))
    r = post(c2, "/login", {"email": "login@test.kr", "password": "abcd1234"})
    assert r.status_code == 302 and r.location.endswith("/home")
    row = mod.db.connect(mod.app.config["DATABASE"]).execute(
        "SELECT password_hash FROM users WHERE email='login@test.kr'").fetchone()
    assert row[0] and "abcd1234" not in row[0]


def test_weak_password_and_agreements_rejected():
    c = new_client()
    r = post(c, "/signup", {"email": "weak@test.kr", "password": "1234", "password2": "1234", "nickname": "약한비번"})
    msgs = "".join(flashes(r))
    assert "8자 이상" in msgs and "약관" in msgs


def test_nickname_only_login_is_gone():
    make_user(nickname="서연")
    c = new_client()
    r = post(c, "/login", {"nickname": "서연", "agree": "1"})
    with c.session_transaction() as s:
        assert "uid" not in s
    assert r.status_code == 200


def test_demo_login_disabled():
    c = new_client()
    assert post(c, "/login/demo/1").status_code == 404


def test_open_redirect_blocked():
    make_user(email="redir@test.kr")
    c = new_client()
    r = post(c, "/login", {"email": "redir@test.kr", "password": "abcd1234", "next": "//evil.com/x"})
    assert r.location.endswith("/home")


def test_security_headers_and_health():
    c = new_client()
    r = c.get("/login")
    for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert h in r.headers
    assert c.get("/healthz").json == {"ok": True}
    assert c.get("/nope").status_code == 404
    assert c.get("/terms").status_code == 200 and c.get("/privacy").status_code == 200


def test_korea_time(mod):
    kst = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None, second=0, microsecond=0)
    assert abs((mod.now() - kst).total_seconds()) <= 60


def test_login_required_redirect():
    c = new_client()
    r = c.get("/home")
    assert r.status_code == 302 and "/login" in r.location


# ---------------------------------------------------------------- 거래 전체 흐름
def test_full_trade_flow(mod):
    host = make_user(nickname="방장A")
    guest = make_user(nickname="참여B")
    rid = create_room(host)

    # 사진 EXIF(위치) 제거 확인
    photo = mod.db.connect(mod.app.config["DATABASE"]).execute("SELECT photo FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    from PIL import Image
    im = Image.open(os.path.join(mod.app.config["UPLOAD_PUBLIC"], photo))
    assert not im.getexif()

    assert "계란 30구" in guest.get("/home").get_data(as_text=True)
    assert guest.get(f"/rooms/{rid}").status_code == 200
    r = post(guest, f"/rooms/{rid}/join", {"slots": "2", "action": "paid"})
    assert r.status_code == 302
    room = mod.db.connect(mod.app.config["DATABASE"]).execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()
    assert room[0] == "deposit"

    # 채팅 (JSON + CSRF 헤더)
    r = guest.post(f"/rooms/{rid}/messages", json={"body": "<b>안녕</b>"})
    assert r.status_code == 400
    r = guest.post(f"/rooms/{rid}/messages", json={"body": "<b>안녕</b>"}, headers={"X-CSRF-Token": guest.csrf})
    assert r.status_code == 200 and any(m["body"] == "<b>안녕</b>" for m in r.json["messages"])

    # 입금 확인 → 구매 확정 → 영수증
    pid = mod.db.connect(mod.app.config["DATABASE"]).execute(
        "SELECT id FROM participations WHERE room_id=?", (rid,)).fetchone()[0]
    post(host, f"/rooms/{rid}/deposits", {"action": "toggle", "pid": pid})
    r = post(host, f"/rooms/{rid}/deposits", {"action": "purchase"})
    assert r.status_code == 302 and "/proof" in r.location
    r = post(host, f"/rooms/{rid}/proof", {"receipt": (png_bytes(), "r.png"), "expiry": "2026-11-10"},
             content_type="multipart/form-data")
    assert r.status_code == 302 and "/pickup" in r.location

    # 영수증은 참여자만
    receipt = mod.db.connect(mod.app.config["DATABASE"]).execute(
        "SELECT receipt_photo FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    assert guest.get(f"/private/{receipt}").status_code == 200
    outsider = make_user(nickname="외부인C")
    assert outsider.get(f"/private/{receipt}").status_code == 403
    assert outsider.get(f"/rooms/{rid}/chat").status_code == 403

    # 수령 → 거래 완료 → 후기
    r = post(guest, f"/rooms/{rid}/pickup", {"action": "receive"})
    assert r.status_code == 302
    status = mod.db.connect(mod.app.config["DATABASE"]).execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    assert status == "done"
    host_id = mod.db.connect(mod.app.config["DATABASE"]).execute("SELECT host_id FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    r = post(guest, f"/rooms/{rid}/review/{host_id}", {"good": ["소분이 정확해요"], "comment": "고마워요"})
    assert r.status_code == 302
    assert "소분이 정확해요" in host.get("/me").get_data(as_text=True)


def test_oversubscription_blocked(mod):
    host = make_user()
    a, b = make_user(), make_user()
    rid = create_room(host)  # 3칸 중 방장 1칸 → 2칸 모집
    post(a, f"/rooms/{rid}/join", {"slots": "2"})
    r = post(b, f"/rooms/{rid}/join", {"slots": "1"})
    assert r.status_code == 302
    total = mod.db.connect(mod.app.config["DATABASE"]).execute(
        "SELECT SUM(slots) FROM participations WHERE room_id=?", (rid,)).fetchone()[0]
    assert total == 2


def test_room_validation():
    host = make_user()
    for over, msg in [({"place_lat": "35.1", "place_lng": "129.0"}, "km 안"),  # 부산: 너무 멂
                      ({"total_qty": "31"}, "나눠떨어지게"),
                      ({"agree_rules": ""}, "거래 원칙"),
                      ({"total_price": "0"}, "총 가격"),
                      ({"place_name": ""}, "장소 이름")]:
        r = post(host, "/rooms/new", room_form(**over), content_type="multipart/form-data")
        assert r.status_code == 200 and msg in "".join(flashes(r)), (over, flashes(r))


def test_non_image_upload_rejected():
    import io
    host = make_user()
    data = room_form()
    data["photo"] = (io.BytesIO(b"<script>alert(1)</script>"), "x.png")
    r = post(host, "/rooms/new", data, content_type="multipart/form-data")
    assert "사진 파일만" in "".join(flashes(r))


def test_deadline_housekeeping(mod):
    host = make_user()
    rid = create_room(host)
    conn = mod.db.connect(mod.app.config["DATABASE"])
    conn.execute("UPDATE rooms SET deadline=? WHERE id=?", (mod.iso(mod.now() - timedelta(minutes=5)), rid))
    with mod.app.test_request_context():
        mod.housekeeping(force=True)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "failed"


def test_host_cancel_and_leave(mod):
    host, guest = make_user(), make_user()
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/join", {"slots": "1"})
    post(guest, f"/rooms/{rid}/leave")
    rid2 = create_room(host)
    post(host, f"/rooms/{rid2}/cancel")
    conn = mod.db.connect(mod.app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM participations WHERE room_id=?", (rid,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid2,)).fetchone()[0] == "failed"


# ---------------------------------------------------------------- 탈퇴 · 운영자 · 카카오
def test_account_delete(mod):
    c = make_user(email="bye@test.kr")
    r = post(c, "/account/delete", {"password": "abcd1234"})
    assert r.status_code == 302
    row = mod.db.connect(mod.app.config["DATABASE"]).execute(
        "SELECT email, lat, deleted, nickname FROM users WHERE nickname LIKE '탈퇴회원%' ORDER BY id DESC").fetchone()
    assert row[0] is None and row[1] is None and row[2] == 1
    c2 = new_client()
    r = post(c2, "/login", {"email": "bye@test.kr", "password": "abcd1234"})
    assert "맞지 않아요" in "".join(flashes(r))


def test_admin_moderation(mod):
    admin = make_user(email="admin@test.kr", nickname="운영자")
    # 운영자 이메일로 가입만 해서는 운영자가 아니다 → 서버 명령으로 인증해야 한다
    assert admin.get("/admin").status_code == 404
    res = mod.app.test_cli_runner().invoke(args=["verify-admin", "admin@test.kr"])
    assert res.exit_code == 0, res.output
    host, guest = make_user(), make_user()
    assert host.get("/admin").status_code == 404
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/report", {"body": "판매 금지 품목이에요"})
    page = admin.get("/admin").get_data(as_text=True)
    assert "판매 금지 품목이에요" in page
    post(admin, "/admin", {"action": "cancel_room", "id": rid, "reason": "금지 품목"})
    conn = mod.db.connect(mod.app.config["DATABASE"])
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "canceled"
    uid = conn.execute("SELECT id FROM users WHERE email=?", (host.email,)).fetchone()[0]
    post(admin, "/admin", {"action": "ban", "id": uid})
    r = host.get("/home")
    assert r.status_code == 302 and "/login" in r.location


def test_kakao_login(mod, monkeypatch):
    calls = []

    def fake(url, data=None, headers=None):
        calls.append(url)
        if "token" in url:
            return {"access_token": "tok"}
        return {"id": 987654, "kakao_account": {"profile": {"nickname": "카카오친구"}}}

    monkeypatch.setattr(mod, "http_json", fake)
    c = new_client()
    r = c.get("/auth/kakao")
    assert r.status_code == 302 and "kauth.kakao.com" in r.location
    with c.session_transaction() as s:
        state = s["kakao_state"]
    r = c.get(f"/auth/kakao/callback?code=abc&state={state}")
    assert r.location.endswith("/auth/kakao/consent")
    with c.session_transaction() as s:
        c.csrf = s["_csrf"]
    r = post(c, "/auth/kakao/consent", {"nickname": "카카오친구", "agree_terms": 1, "agree_privacy": 1,
                                         "agree_location": 1, "agree_age": 1})
    assert r.status_code == 302 and "/neighborhood" in r.location
    # 두 번째 로그인은 바로 통과
    c2 = new_client()
    c2.get("/auth/kakao")
    with c2.session_transaction() as s:
        state = s["kakao_state"]
    r = c2.get(f"/auth/kakao/callback?code=abc&state={state}")
    assert "/neighborhood" in r.location or "/home" in r.location
    bad = new_client()
    r = bad.get("/auth/kakao/callback?code=abc&state=forged")
    assert r.location.endswith("/login")


def test_all_pages_render(mod):
    host, guest = make_user(), make_user()
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/join", {"slots": "1"})
    for c, urls in ((host, ["/home", "/map", "/my", "/my?tab=join", "/my?tab=done", "/chats", "/me", "/notifications",
                            "/rooms/new", f"/rooms/{rid}", f"/rooms/{rid}/chat", f"/rooms/{rid}/deposits",
                            f"/rooms/{rid}/created", "/neighborhood", "/shops", "/account/delete"]),
                    (guest, [f"/rooms/{rid}", f"/rooms/{rid}/chat", "/my?tab=join"])):
        for u in urls:
            r = c.get(u)
            assert r.status_code == 200, (u, r.status_code)
    anon = new_client()
    for u in ["/", "/login", "/signup", "/password/forgot", "/terms", "/privacy", "/robots.txt"]:
        assert anon.get(u).status_code == 200, u


def test_rate_limit(mod):
    mod.app.config["RATE_LIMIT_IN_TESTS"] = True
    try:
        c = new_client()
        codes = [post(c, "/login", {"email": "x@test.kr", "password": "nope"}).status_code for _ in range(12)]
        assert 429 in codes
    finally:
        mod.app.config["RATE_LIMIT_IN_TESTS"] = False
        mod._hits.clear()
