"""수업 자료(게시판 수정·삭제, 예외 처리, 로깅)를 반띵에 적용한 부분의 테스트."""
import os
from datetime import timedelta

from conftest import flashes, make_user, post
from test_app import create_room


def db_conn(mod):
    return mod.db.connect(mod.app.config["DATABASE"])


def log_text(mod):
    for h in mod.log.handlers:
        h.flush()
    with open(os.path.join(mod.LOG_DIR, "banddeing.log"), encoding="utf-8") as f:
        return f.read()


def done_room_with_review(mod):
    """거래 완료된 방 + 참여자가 방장에게 남긴 후기 하나."""
    host, guest = make_user(), make_user()
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    conn = db_conn(mod)
    conn.execute("UPDATE rooms SET status='done' WHERE id=?", (rid,))
    conn.execute("UPDATE participations SET confirmed=1, received=1 WHERE room_id=?", (rid,))
    host_id = conn.execute("SELECT host_id FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    r = post(guest, f"/rooms/{rid}/review/{host_id}", {"good": ["소분이 정확해요"], "comment": "고마워요"})
    assert r.status_code == 302
    review_id = conn.execute("SELECT id FROM reviews WHERE room_id=?", (rid,)).fetchone()[0]
    return host, guest, rid, review_id


# ---------------------------------------------------------------- U: 공구방 수정
def test_room_update(mod):
    host, guest = make_user(), make_user()
    rid = create_room(host)

    # GET: 기존 값이 폼에 채워져 있다 (instance=record)
    page = host.get(f"/rooms/{rid}/edit").get_data(as_text=True)
    assert 'value="계란 30구"' in page

    room = db_conn(mod).execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
    r = post(host, f"/rooms/{rid}/edit", {"title": "계란 30구 (왕란)", "category": "egg", "buy_from": "홈플러스",
                                          "pickup_at": room[18], "deadline": room[19], "remove_photo": "1"},
             content_type="multipart/form-data")
    assert r.status_code == 302 and r.location.endswith(f"/rooms/{rid}")
    row = db_conn(mod).execute("SELECT title, buy_from, photo FROM rooms WHERE id=?", (rid,)).fetchone()
    assert row[0] == "계란 30구 (왕란)" and row[1] == "홈플러스" and row[2] is None
    # 사진 파일도 지워졌다
    assert not os.path.exists(os.path.join(mod.app.config["UPLOAD_PUBLIC"], room["photo"]))
    assert "공구방 수정: room=" in log_text(mod)


def test_room_update_locks_title_when_joined(mod):
    host, guest = make_user(), make_user()
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    page = host.get(f"/rooms/{rid}/edit").get_data(as_text=True)
    assert "상품명·카테고리</b>는 바꿀 수 없어요" in page and 'name="title"' not in page
    room = db_conn(mod).execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
    # 화면을 우회해 상품명·카테고리를 보내도 거절
    r = post(host, f"/rooms/{rid}/edit", {"title": "가짜 상품", "category": "meat",
                                          "pickup_at": room["pickup_at"], "deadline": room["deadline"]},
             content_type="multipart/form-data")
    assert r.status_code == 200 and "바꿀 수 없어요" in "".join(flashes(r))
    row = db_conn(mod).execute("SELECT title, category FROM rooms WHERE id=?", (rid,)).fetchone()
    assert tuple(row) == ("계란 30구", "egg")
    # 시간은 바꿀 수 있고, 참여자에게 알림이 간다
    new_pickup = mod.iso(mod.parse(room["pickup_at"]) + timedelta(hours=1))
    r = post(host, f"/rooms/{rid}/edit", {"buy_from": "홈플러스", "pickup_at": new_pickup,
                                          "deadline": room["deadline"]}, content_type="multipart/form-data")
    assert r.status_code == 302
    conn = db_conn(mod)
    assert conn.execute("SELECT title, pickup_at FROM rooms WHERE id=?", (rid,)).fetchone()[:] == ("계란 30구", new_pickup)
    uid = conn.execute("SELECT id FROM users WHERE email=?", (guest.email,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND body LIKE '%픽업 시간%'",
                        (uid,)).fetchone()[0] == 1


def test_log_retention_is_daily_90(mod):
    import logging.handlers
    fh = [h for h in mod.log.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(fh) == 1 and fh[0].when == "MIDNIGHT" and fh[0].backupCount == 90


def test_room_update_permission_and_validation(mod):
    host, other = make_user(), make_user()
    rid = create_room(host)
    assert other.get(f"/rooms/{rid}/edit").status_code == 403
    r = post(host, f"/rooms/{rid}/edit", {"title": "", "pickup_at": "", "deadline": ""},
             content_type="multipart/form-data")
    assert r.status_code == 200 and "상품명" in "".join(flashes(r))
    # 모집이 끝난 방은 수정 불가
    db_conn(mod).execute("UPDATE rooms SET status='buying' WHERE id=?", (rid,))
    r = host.get(f"/rooms/{rid}/edit")
    assert r.status_code == 302


# ---------------------------------------------------------------- D: 공구방 삭제
def test_room_delete(mod):
    host, guest, other = make_user(), make_user(), make_user()
    rid = create_room(host)
    photo = db_conn(mod).execute("SELECT photo FROM rooms WHERE id=?", (rid,)).fetchone()[0]

    # 삭제는 POST 로만
    assert host.get(f"/rooms/{rid}/delete").status_code == 405
    # 남의 방 → 예외 처리(404 안내 화면) + 오류 로그
    r = post(other, f"/rooms/{rid}/delete")
    assert r.status_code == 404 and "오류 코드 404" in r.get_data(as_text=True)
    assert "ERROR [banddeing:" in log_text(mod) and "room_delete" in log_text(mod)

    # 참여자가 있으면 지울 수 없다
    post(guest, f"/rooms/{rid}/join", {"slots": "1"})
    r = post(host, f"/rooms/{rid}/delete")
    assert r.status_code == 302
    assert db_conn(mod).execute("SELECT COUNT(*) FROM rooms WHERE id=?", (rid,)).fetchone()[0] == 1

    # 참여자가 나가면 지울 수 있다 (채팅까지 같이 삭제)
    post(guest, f"/rooms/{rid}/leave")
    r = post(host, f"/rooms/{rid}/delete")
    assert r.status_code == 302
    conn = db_conn(mod)
    assert conn.execute("SELECT COUNT(*) FROM rooms WHERE id=?", (rid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE room_id=?", (rid,)).fetchone()[0] == 0
    assert not os.path.exists(os.path.join(mod.app.config["UPLOAD_PUBLIC"], photo))
    assert host.get(f"/rooms/{rid}").status_code == 404


# ---------------------------------------------------------------- 후기 수정 · 삭제
def test_review_update_and_delete(mod):
    host, guest, rid, review_id = done_room_with_review(mod)

    page = guest.get(f"/rooms/{rid}/review").get_data(as_text=True)
    assert f"/reviews/{review_id}/edit" in page and f"/reviews/{review_id}/delete" in page

    # 수정 화면에 기존 값이 채워져 있다
    page = guest.get(f"/reviews/{review_id}/edit").get_data(as_text=True)
    assert "고마워요" in page and 'value="소분이 정확해요" checked' in page

    r = post(guest, f"/reviews/{review_id}/edit", {"good": ["응답이 빨라요"], "comment": "또 봬요"})
    assert r.status_code == 302
    row = db_conn(mod).execute("SELECT good, comment FROM reviews WHERE id=?", (review_id,)).fetchone()
    assert row[0] == "응답이 빨라요" and row[1] == "또 봬요"

    # 남의 후기는 수정·삭제할 수 없다 → 404 + 로그
    assert post(host, f"/reviews/{review_id}/edit", {"comment": "조작"}).status_code == 404
    assert post(host, f"/reviews/{review_id}/delete").status_code == 404
    assert "No Review matches the given query." in log_text(mod)

    r = post(guest, f"/reviews/{review_id}/delete")
    assert r.status_code == 302
    assert db_conn(mod).execute("SELECT COUNT(*) FROM reviews WHERE id=?", (review_id,)).fetchone()[0] == 0
    # 없는 후기를 다시 지우려 하면 예외 처리
    assert post(guest, f"/reviews/{review_id}/delete").status_code == 404


def test_review_edit_window(mod):
    _host, guest, rid, review_id = done_room_with_review(mod)
    db_conn(mod).execute("UPDATE reviews SET created_at=? WHERE id=?",
                         (mod.iso(mod.now() - timedelta(days=mod.REVIEW_EDIT_DAYS + 1)), review_id))
    r = post(guest, f"/reviews/{review_id}/delete")
    assert r.status_code == 302 and f"/rooms/{rid}/review" in r.location
    assert db_conn(mod).execute("SELECT COUNT(*) FROM reviews WHERE id=?", (review_id,)).fetchone()[0] == 1


# ---------------------------------------------------------------- 로깅
def test_logging_format(mod):
    import re
    make_user()
    text = log_text(mod)
    # [2026/11/22 16:05:00] INFO [banddeing:123] 회원 가입: user=1
    assert re.search(r"^\[\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\] INFO \[banddeing:\d+\] 회원 가입: user=\d+$", text, re.M)
    assert not re.search(r"user\d+@test\.kr", text)  # 회원 이메일 같은 개인정보는 로그에 남기지 않는다


def test_unhandled_error_logged(mod, monkeypatch):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setitem(mod.app.view_functions, "terms", _boom)
    c = make_user()
    r = c.get("/terms")
    assert r.status_code == 500 and "오류 코드 500" in r.get_data(as_text=True)
    text = log_text(mod)
    assert "CRITICAL [banddeing:" in text and "Traceback" in text and "RuntimeError: boom" in text


# ---------------------------------------------------------------- 코드 리뷰 보안 수정
def test_admin_requires_email_verification(mod, monkeypatch):
    c = make_user()  # 일반 사용자
    assert c.get("/admin").status_code == 404
    # 운영자 이메일로 남이 먼저 가입해도 운영자 화면이 열리지 않는다
    mod.ADMIN_EMAILS.add("boss@test.kr")
    try:
        squatter = make_user(email="boss@test.kr")
        assert squatter.get("/admin").status_code == 404
        assert "운영자 이메일 인증" in squatter.get("/me").get_data(as_text=True)
        # 메일 인증 흐름: 인증번호 받기 → 틀린 번호 → 맞는 번호
        monkeypatch.setattr(mod, "MAIL_ENABLED", True)
        sent = {}
        monkeypatch.setattr(mod, "send_mail", lambda to, sub, body: sent.update(to=to, body=body) or True)
        post(squatter, "/verify/admin/send")
        assert sent["to"] == "boss@test.kr"
        code = __import__("re").search(r"인증번호: (\d{6})", sent["body"]).group(1)
        post(squatter, "/verify/admin/confirm", {"code": "000000" if code != "000000" else "111111"})
        assert squatter.get("/admin").status_code == 404
        post(squatter, "/verify/admin/confirm", {"code": code})
        assert squatter.get("/admin").status_code == 200
        # ADMIN_EMAILS 에서 빼면 바로 권한이 사라진다
        mod.ADMIN_EMAILS.discard("boss@test.kr")
        assert squatter.get("/admin").status_code == 404
    finally:
        mod.ADMIN_EMAILS.discard("boss@test.kr")
    # 없는 이메일은 서버 명령도 거절
    res = mod.app.test_cli_runner().invoke(args=["verify-admin", "nobody@test.kr"])
    assert res.exit_code != 0


def test_pickup_place_xss_and_csp(mod):
    host, guest = make_user(), make_user()
    evil = '<img src=x onerror=alert(1)>'
    rid = create_room(host, place_name=evil)
    db_conn(mod).execute("UPDATE rooms SET status='pickup' WHERE id=?", (rid,))
    post(guest, f"/rooms/{rid}/join", {"slots": "1"})  # 모집 끝난 방이라 참여 불가 → 방장 화면으로 확인
    r = host.get(f"/rooms/{rid}/pickup")
    html = r.get_data(as_text=True)
    assert evil not in html  # HTML 로 그대로 들어가지 않는다
    assert "bindTooltip(tip" in html
    csp = r.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    nonce = __import__("re").search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in html
    assert __import__("re").search(r'\son(click|submit|change)="', html) is None


def test_password_change_logs_out_other_devices(mod):
    from conftest import new_client
    a = make_user()
    b = new_client()  # 같은 계정으로 다른 기기 로그인
    r = post(b, "/login", {"email": a.email, "password": "abcd1234"})
    assert r.status_code == 302
    with b.session_transaction() as s:
        b.csrf = s["_csrf"]
    assert b.get("/me").status_code == 200
    post(a, "/me", {"action": "password", "current": "abcd1234", "password": "newpass99", "password2": "newpass99"})
    assert a.get("/me").status_code == 200          # 바꾼 기기는 그대로
    r = b.get("/me")
    assert r.status_code == 302 and "/login" in r.location  # 다른 기기는 로그아웃


def test_reset_link_uses_public_url_only(mod, monkeypatch):
    from conftest import new_client
    u = make_user()
    sent = {}
    monkeypatch.setattr(mod, "MAIL_ENABLED", True)
    monkeypatch.setattr(mod, "send_mail", lambda to, sub, body: sent.update(body=body) or True)
    monkeypatch.setitem(mod.SITE, "url", "https://banddeing.example")
    # 공격자가 Host 헤더를 evil.example 로 바꿔 보낸 요청
    c = mod.app.test_client()
    c.get("/login", base_url="http://evil.example")
    with c.session_transaction(base_url="http://evil.example") as sess:
        c.csrf = sess["_csrf"]
    post(c, "/password/forgot", {"email": u.email}, base_url="http://evil.example")
    assert "https://banddeing.example/password/reset/" in sent["body"] and "evil.example" not in sent["body"]
    # 배포 모드에서 PUBLIC_URL 이 없으면 메일을 보내지 않는다
    sent.clear()
    monkeypatch.setitem(mod.SITE, "url", "")
    monkeypatch.setattr(mod, "PRODUCTION", True)
    post(c, "/password/forgot", {"email": u.email}, base_url="http://evil.example")
    assert not sent


def test_local_rooms_not_crowded_out(mod):
    host = make_user()
    rid = create_room(host)
    conn = db_conn(mod)
    # 부산에 마감이 더 이른 방 300개
    row = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
    cols = [k for k in row.keys() if k != "id"]
    vals = [row[k] for k in cols]
    i_lat, i_lng, i_dl = cols.index("pickup_lat"), cols.index("pickup_lng"), cols.index("deadline")
    far = []
    for _ in range(310):
        v = list(vals); v[i_lat], v[i_lng] = 35.1, 129.0
        v[i_dl] = mod.iso(mod.now() + timedelta(hours=1)); far.append(v)
    conn.executemany(f"INSERT INTO rooms ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", far)
    guest = make_user()
    assert f'href="/rooms/{rid}"' in guest.get("/home").get_data(as_text=True)
    conn.execute("DELETE FROM rooms WHERE pickup_lat=35.1")


def test_double_submit_is_not_500(mod):
    host, guest = make_user(), make_user()
    rid = create_room(host)
    post(guest, f"/rooms/{rid}/join", {"slots": "1"})
    # 같은 요청이 한 번 더 와도(이미 참여) 500 이 아니다 — join 화면 검사를 건너뛰고 DB 제약에 걸리게 만든다
    conn = db_conn(mod)
    conn.execute("DELETE FROM participations WHERE room_id=?", (rid,))
    import app as appmod
    orig = appmod.enrich
    def fake_enrich(r):
        d = orig(r); d["mine"] = None; return d
    try:
        appmod.enrich = fake_enrich
        post(guest, f"/rooms/{rid}/join", {"slots": "1"})
        r = post(guest, f"/rooms/{rid}/join", {"slots": "1"})
    finally:
        appmod.enrich = orig
    assert r.status_code == 302 and "이미 참여" in "".join(flashes(guest.get(r.location)))
    # 가입 두 번
    from conftest import new_client
    c = new_client()
    data = {"email": "twice@test.kr", "password": "abcd1234", "password2": "abcd1234", "nickname": "두번",
            "agree_terms": 1, "agree_privacy": 1, "agree_location": 1, "agree_age": 1}
    post(c, "/signup", data)
    c2 = new_client()
    orig_q = appmod.db.query
    appmod.db.query = lambda sql, args=(), one=False: None if "FROM users WHERE email=?" in sql or \
        "FROM users WHERE nickname=?" in sql else orig_q(sql, args, one)
    try:
        r = post(c2, "/signup", data)
    finally:
        appmod.db.query = orig_q
    assert r.status_code == 200 and "이미 가입된" in "".join(flashes(r))


def test_deposit_expiry_keeps_paid(mod):
    host, a, b = make_user(), make_user(), make_user()
    rid = create_room(host)
    post(a, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    post(b, f"/rooms/{rid}/join", {"slots": "1"})
    conn = db_conn(mod)
    conn.execute("UPDATE rooms SET full_at=? WHERE id=?", (mod.iso(mod.now() - timedelta(hours=25)), rid))
    with mod.app.test_request_context():
        mod.housekeeping(force=True)
    left = conn.execute("SELECT paid FROM participations WHERE room_id=?", (rid,)).fetchall()
    assert [r[0] for r in left] == [1]
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "recruiting"


def test_rate_limit_memory_is_swept(mod):
    import time as _t
    mod._hits.clear()
    old = _t.monotonic() - 7200
    for i in range(1000):
        mod._hits[("x", f"10.0.{i // 250}.{i % 250}")].append(old)
    mod._hits_swept[0] = 0
    mod.app.config["RATE_LIMIT_IN_TESTS"] = True
    try:
        with mod.app.test_request_context(environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            mod.rate_limit("y", 100, 60)
    finally:
        mod.app.config["RATE_LIMIT_IN_TESTS"] = False
        assert len(mod._hits) == 1
    mod._hits.clear()


# ---------------------------------------------------------------- 거래 멈춤 자동 처리 규칙
def full_paid_room(mod):
    host, a, b = make_user(), make_user(), make_user()
    rid = create_room(host)
    post(a, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    post(b, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    return host, a, b, rid


def run_housekeeping(mod):
    with mod.app.test_request_context():
        mod.housekeeping(force=True)


def test_auto_cancel_when_host_does_not_purchase(mod):
    host, a, _b, rid = full_paid_room(mod)
    conn = db_conn(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "deposit"
    assert "자동 취소돼요" in host.get(f"/rooms/{rid}/deposits").get_data(as_text=True)
    # 24시간이 안 지났으면 그대로
    run_housekeeping(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "deposit"
    old = mod.iso(mod.now() - timedelta(hours=25))
    conn.execute("UPDATE participations SET paid_at=? WHERE room_id=?", (old, rid))
    conn.execute("UPDATE rooms SET full_at=? WHERE id=?", (old, rid))
    run_housekeeping(mod)
    status, reason = conn.execute("SELECT status, cancel_reason FROM rooms WHERE id=?", (rid,)).fetchone()
    assert status == "failed" and "자동 취소" in reason
    uid_a = conn.execute("SELECT id FROM users WHERE email=?", (a.email,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND body LIKE '%환불%'",
                        (uid_a,)).fetchone()[0] == 1
    assert "자동 취소(구매 미확정)" in log_text(mod)


def test_no_auto_cancel_while_someone_unpaid(mod):
    host, a, b = make_user(), make_user(), make_user()
    rid = create_room(host)
    post(a, f"/rooms/{rid}/join", {"slots": "1", "action": "paid"})
    post(b, f"/rooms/{rid}/join", {"slots": "1"})  # 미입금
    conn = db_conn(mod)
    conn.execute("UPDATE participations SET paid_at=? WHERE room_id=? AND paid=1",
                 (mod.iso(mod.now() - timedelta(hours=30)), rid))
    run_housekeeping(mod)  # full_at 은 방금이라 입금 기한도 안 지남
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "deposit"


def pickup_room(mod):
    host, a, b, rid = full_paid_room(mod)
    conn = db_conn(mod)
    conn.execute("UPDATE participations SET confirmed=1 WHERE room_id=?", (rid,))
    conn.execute("UPDATE rooms SET status='pickup', pickup_notified=1 WHERE id=?", (rid,))
    return host, a, b, rid


def test_auto_receive_after_48h(mod):
    host, a, b, rid = pickup_room(mod)
    post(a, f"/rooms/{rid}/pickup", {"action": "receive"})
    conn = db_conn(mod)
    assert "자동으로 수령 처리돼요" in b.get(f"/rooms/{rid}/pickup").get_data(as_text=True)
    conn.execute("UPDATE rooms SET pickup_at=? WHERE id=?", (mod.iso(mod.now() - timedelta(hours=47)), rid))
    run_housekeeping(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "pickup"
    conn.execute("UPDATE rooms SET pickup_at=? WHERE id=?", (mod.iso(mod.now() - timedelta(hours=49)), rid))
    run_housekeeping(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT MIN(received) FROM participations WHERE room_id=?", (rid,)).fetchone()[0] == 1
    # 자동 완료 뒤 후기 작성 가능
    host_id = conn.execute("SELECT host_id FROM rooms WHERE id=?", (rid,)).fetchone()[0]
    assert post(b, f"/rooms/{rid}/review/{host_id}", {"good": ["응답이 빨라요"]}).status_code == 302


def test_no_auto_receive_when_reported(mod):
    host, a, b, rid = pickup_room(mod)
    post(b, f"/rooms/{rid}/report", {"body": "약속 장소에 아무도 안 왔어요"})
    conn = db_conn(mod)
    conn.execute("UPDATE rooms SET pickup_at=? WHERE id=?", (mod.iso(mod.now() - timedelta(hours=60)), rid))
    run_housekeeping(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "pickup"
    conn.execute("UPDATE reports SET resolved=1 WHERE room_id=?", (rid,))  # 운영팀이 처리하면
    run_housekeeping(mod)
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "done"


def test_auto_cancel_race_no_false_notice(mod, monkeypatch):
    """자동 취소가 방을 읽은 직후 방장이 구매 확정을 누른 경우 → 취소도 '자동 취소' 알림도 없어야 한다."""
    host, a, _b, rid = full_paid_room(mod)
    conn = db_conn(mod)
    old = mod.iso(mod.now() - timedelta(hours=25))
    conn.execute("UPDATE participations SET paid_at=?, confirmed=1 WHERE room_id=?", (old, rid))
    conn.execute("UPDATE rooms SET full_at=? WHERE id=?", (old, rid))
    stale = conn.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()  # 자동 처리가 읽어 둔 'deposit' 상태
    r = post(host, f"/rooms/{rid}/deposits", {"action": "purchase"})  # 방장이 먼저 구매 확정
    assert r.status_code == 302 and "/proof" in r.location
    real_query = mod.db.query
    monkeypatch.setattr(mod.db, "query", lambda sql, args=(), one=False:
                        [stale] if sql == "SELECT * FROM rooms WHERE status='deposit'" else real_query(sql, args, one))
    with mod.app.test_request_context():
        mod.auto_cancel_unpurchased(mod.now())
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "buying"
    uid_a = conn.execute("SELECT id FROM users WHERE email=?", (a.email,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND body LIKE '%취소%'",
                        (uid_a,)).fetchone()[0] == 0


def test_purchase_after_auto_cancel_is_refused(mod):
    """반대 순서: 자동 취소가 먼저 되면 방장의 구매 확정은 '구매 확정' 알림 없이 거절된다."""
    host, a, _b, rid = full_paid_room(mod)
    conn = db_conn(mod)
    old = mod.iso(mod.now() - timedelta(hours=25))
    conn.execute("UPDATE participations SET paid_at=?, confirmed=1 WHERE room_id=?", (old, rid))
    conn.execute("UPDATE rooms SET full_at=? WHERE id=?", (old, rid))
    # 방장 화면이 아직 'deposit' 으로 보던 순간 자동 취소가 끼어든 상황
    real_get_room = mod.get_room
    def stale_get_room(room_id):
        d = real_get_room(room_id)
        if d["status"] == "deposit":
            mod.auto_cancel_unpurchased(mod.now())
        return d
    import app as appmod
    appmod.get_room = stale_get_room
    try:
        r = post(host, f"/rooms/{rid}/deposits", {"action": "purchase"})
    finally:
        appmod.get_room = real_get_room
    assert conn.execute("SELECT status FROM rooms WHERE id=?", (rid,)).fetchone()[0] == "failed"
    uid_a = conn.execute("SELECT id FROM users WHERE email=?", (a.email,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='구매 확정'",
                        (uid_a,)).fetchone()[0] == 0
    assert "자동 취소" in "".join(flashes(host.get(r.location)))
