from fastapi.testclient import TestClient

from docseed import security
from tests.conftest import PASSWORD


def test_login_wrong_password(app, users):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong-password-123"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "unauthenticated"


def test_unknown_user_same_error(app, users):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
    assert r.status_code == 401
    assert r.json()["detail"] == "ユーザー名またはパスワードが違います"


def test_rate_limit_after_failures(app, users):
    from docseed.api import routes
    routes.limiter._fail.clear()
    c = TestClient(app)
    for _ in range(5):
        c.post("/api/v1/auth/login", json={"username": "bob", "password": "nope-nope-nope"})
    r = c.post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    routes.limiter._fail.clear()


def test_requires_login(app, users):
    c = TestClient(app)
    assert c.get("/api/v1/me").status_code == 401
    r = c.get("/f/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_csrf_required_for_session_writes(login):
    alice = login("alice")
    r = alice.http.post("/api/v1/folders", json={"parent_id": alice.root, "name": "x"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "x"})
    assert r.status_code == 201


def test_logout_invalidates_session(login):
    alice = login("alice")
    assert alice.post("/api/v1/auth/logout").status_code == 204
    assert alice.get("/api/v1/me").status_code == 401


def test_api_token(login, app):
    alice = login("alice")
    r = alice.post("/api/v1/me/tokens", json={"name": "cli", "scopes": ["read"]})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith(security.TOKEN_PREFIX)
    c = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/v1/me", headers=h).status_code == 200
    # read だけのトークンでは変更できない（CSRF は不要）
    r = c.post("/api/v1/folders", json={"parent_id": alice.root, "name": "y"}, headers=h)
    assert r.status_code == 403
    # トークンでトークンは作れない
    assert c.get("/api/v1/me/tokens", headers=h).status_code == 403
    tid = alice.get("/api/v1/me/tokens").json()["tokens"][0]["id"]
    assert alice.delete(f"/api/v1/me/tokens/{tid}").status_code == 204
    assert c.get("/api/v1/me", headers=h).status_code == 401


def test_password_change_logs_out_other_sessions(login):
    a1 = login("alice")
    a2 = login("alice")
    r = a1.post("/api/v1/me/password", json={"current_password": PASSWORD, "new_password": "another-long-password"})
    assert r.status_code == 204
    assert a1.get("/api/v1/me").status_code == 200
    assert a2.get("/api/v1/me").status_code == 401
    r = a1.post("/api/v1/me/password", json={"current_password": "x", "new_password": "another-long-password2"})
    assert r.status_code == 422


def test_short_password_rejected(login):
    admin = login("admin")
    r = admin.post("/api/v1/admin/users", json={"username": "carol", "password": "short"})
    assert r.status_code == 422


def test_admin_only(login):
    alice = login("alice")
    assert alice.get("/api/v1/admin/users").status_code == 403
    admin = login("admin")
    assert admin.get("/api/v1/admin/users").status_code == 200


def test_cannot_remove_last_admin(login, users):
    admin = login("admin")
    r = admin.patch(f"/api/v1/admin/users/{users['admin'].id}", json={"role": "user"})
    assert r.status_code == 422


def test_disabled_user_cannot_login(login, users):
    admin = login("admin")
    alice = login("alice")
    admin.patch(f"/api/v1/admin/users/{users['alice'].id}", json={"is_active": False})
    assert alice.get("/api/v1/me").status_code == 401
    r = TestClient(alice.http.app).post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401


def test_unwritable_data_dir_gives_clear_message(tmp_path, monkeypatch):
    import pathlib

    import pytest

    from docseed.config import Settings

    def deny(self, *a, **kw):
        raise PermissionError(13, "Permission denied", str(self))
    monkeypatch.setattr(pathlib.Path, "mkdir", deny)
    with pytest.raises(SystemExit) as e:
        Settings(data_dir=tmp_path / "data", secret_key="x" * 40).ensure_dirs()
    assert "sudo chown -R" in str(e.value) and "データディレクトリに書き込めません" in str(e.value)
