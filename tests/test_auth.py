from datetime import datetime, timedelta

from app import auth
from app.models import LoginAttempt


def test_code_format_and_normalize():
    code = auth.generate_code()
    assert len(code) == 8 and set(code) <= set(auth.CODE_ALPHABET)
    assert len(auth.generate_code(admin=True)) == 16
    assert not set("01ILO") & set(auth.CODE_ALPHABET)
    assert auth.format_code("K7MXP3QR") == "K7MX-P3QR"
    assert auth.normalize_code(" k7mx-p3qr ") == "K7MXP3QR"


def test_code_stored_only_as_hmac(make_user, db):
    user, code = make_user()
    assert code not in user.code_hash and len(user.code_hash) == 64
    assert user.code_hash == auth.hash_code(code.lower())


def test_login_form_accepts_lowercase_with_dash(client, make_user):
    user, code = make_user()
    page = client.get("/login")
    token = client.cookies[auth.LOGIN_CSRF_COOKIE]
    assert token in page.text
    formatted = auth.format_code(code).lower()
    r = client.post("/login", data={"code": formatted, "login_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and auth.SESSION_COOKIE in r.cookies
    assert "Anna" not in client.get("/login", follow_redirects=False).text  # already logged in


def test_login_requires_login_token(client, make_user):
    _, code = make_user()
    r = client.post("/login", data={"code": code, "login_token": "x"}, follow_redirects=False)
    assert r.status_code == 400


def test_wrong_code(client):
    client.get("/login")
    r = client.post("/login", data={"code": "AAAA-AAAA",
                                    "login_token": client.cookies[auth.LOGIN_CSRF_COOKIE]})
    assert r.status_code == 401 and "ungültig" in r.text


def test_qr_link_redirects_and_removes_code(client, make_user):
    _, code = make_user()
    r = client.get(f"/login?c={code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get("/login?c=WRONGCOD", follow_redirects=False)
    assert r.headers["location"] == "/login?error=code"


def test_rate_limit(client, make_user, db):
    _, code = make_user()
    for _ in range(auth.MAX_FAILURES):
        client.get("/login?c=WRONGCOD")
    r = client.get(f"/login?c={code}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=locked"
    client.get("/login")
    r = client.post("/login", data={"code": code,
                                    "login_token": client.cookies[auth.LOGIN_CSRF_COOKIE]})
    assert r.status_code == 429


def test_lockout_expires(db):
    now = datetime.now()
    for i in range(auth.MAX_FAILURES):
        db.add(LoginAttempt(ip="1.2.3.4", ts=now - timedelta(minutes=20, seconds=i), success=False))
    db.commit()
    assert auth.is_locked_out(db, "1.2.3.4", now - timedelta(minutes=10))
    assert not auth.is_locked_out(db, "1.2.3.4", now)
    assert not auth.is_locked_out(db, "5.6.7.8", now)


def test_failures_spread_out_do_not_lock(db):
    now = datetime.now()
    for i in range(auth.MAX_FAILURES):
        db.add(LoginAttempt(ip="1.2.3.4", ts=now - timedelta(minutes=2 * i), success=False))
    db.commit()
    assert not auth.is_locked_out(db, "1.2.3.4", now)


def test_disabled_user_cannot_login(client, make_user, db):
    user, code = make_user()
    user.disabled = True
    db.commit()
    r = client.get(f"/login?c={code}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=code"


def test_pages_require_login(client):
    for path in ("/", "/browse", "/export", "/notes/new", "/review/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers["HX-Redirect"] == "/login"


def test_post_requires_csrf(client, login):
    login()
    r = client.post("/decks", data={"name": "X"})
    assert r.status_code == 403
    r = client.post("/decks", data={"name": "X", "csrf_token": "wrong"})
    assert r.status_code == 403


def test_post_requires_login(client):
    r = client.post("/decks", data={"name": "X"}, follow_redirects=False)
    assert r.status_code == 303


def test_logout(client, login):
    _, csrf = login()
    r = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_reset_code_invalidates_old_code_and_sessions(client, login, db):
    user, _ = login()
    from app.models import User
    new = auth.reset_code(db, db.get(User, user.id))
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get(f"/login?c={new}", follow_redirects=False).headers["location"] == "/"


def test_require_admin():
    import pytest
    from app.models import User
    with pytest.raises(auth.Forbidden):
        auth.require_admin(User(role="learner"))
    assert auth.require_admin(User(role="admin")).role == "admin"


def test_access_log_redacts_code():
    import logging
    from app.main import RedactLoginCode
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s "%s %s"',
                            ("1.2.3.4", "GET", "/login?c=K7MXP3QR&x=1"), None)
    RedactLoginCode().filter(rec)
    assert "K7MXP3QR" not in rec.getMessage() and "c=***" in rec.getMessage()


def test_bootstrap_creates_admin_once(migrated, capsys):
    from app import bootstrap
    assert bootstrap.main() == 0
    out = capsys.readouterr().out
    assert "Admin code" in out
    assert bootstrap.ensure_initial_admin() is None


def test_cli_create_user(migrated, capsys):
    from app import cli
    assert cli.main(["create-user", "Anna", "Ben"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith("Anna\t")
