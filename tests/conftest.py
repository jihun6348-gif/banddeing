import io
import os
import re
import sys
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="banddeing-test-")
os.environ.update(DATA_DIR=TMP, DEMO_MODE="0", ADMIN_EMAILS="admin@test.kr", KAKAO_REST_API_KEY="test-key",
                  CONTACT_EMAIL="help@test.kr")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402

appmod.app.config.update(TESTING=True)


@pytest.fixture
def mod():
    return appmod


def new_client():
    c = appmod.app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        c.csrf = s["_csrf"]
    return c


def post(c, url, data=None, **kw):
    data = dict(data or {})
    data.setdefault("_csrf", c.csrf)
    return c.post(url, data=data, **kw)


_n = [0]


def make_user(email=None, nickname=None, lat=37.5898, lng=127.0579):
    _n[0] += 1
    email = email or f"user{_n[0]}@test.kr"
    nickname = nickname or f"이웃{_n[0]}"
    c = new_client()
    r = post(c, "/signup", {"email": email, "password": "abcd1234", "password2": "abcd1234", "nickname": nickname,
                            "agree_terms": 1, "agree_privacy": 1, "agree_location": 1, "agree_age": 1})
    assert r.status_code == 302 and "/neighborhood" in r.location, r.get_data(as_text=True)[:500]
    with c.session_transaction() as s:
        c.csrf = s["_csrf"]
    r = post(c, "/neighborhood", {"lat": lat, "lng": lng, "name": "회기동", "radius": "1000"})
    assert r.status_code == 302
    c.email, c.nickname = email, nickname
    return c


def png_bytes(exif_gps=False):
    from PIL import Image
    img = Image.new("RGB", (40, 30), (200, 80, 50))
    b = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (37.0, 35.0, 23.0)}  # GPSInfo
        exif[0x010F] = "TestCam"
        img.save(b, "JPEG", exif=exif)
    else:
        img.save(b, "PNG")
    b.seek(0)
    return b


def flashes(r):
    return re.findall(r'<div class="flash" role="status">(.*?)</div>', r.get_data(as_text=True))
