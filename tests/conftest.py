from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from docseed.app import create_app
from docseed.config import Settings
from docseed.db import connect
from docseed.users import create_user

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _reset_login_limits():
    """ログイン試行の制限はプロセス全体で共有するので、テストごとに空にする（失敗したテストの状態を持ち越さない）。"""
    from docseed import authn
    from docseed.web.public import pass_limiter
    authn.limiter._fail.clear()
    pass_limiter._fail.clear()
    yield
    authn.limiter._fail.clear()
    pass_limiter._fail.clear()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    # 受け取り用リンクの空き容量の確認は、テストを動かす機械のディスクに左右されないよう切る（test_sharing で確かめる）
    return Settings(data_dir=tmp_path / "data", secret_key="x" * 40, cookie_secure=False,
                    share_upload_min_free_pct=0.0)


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def conn(app, settings):
    c = connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture()
def users(conn):
    admin = create_user(conn, "admin", "管理者", PASSWORD, role="admin")
    alice = create_user(conn, "alice", "Alice", PASSWORD)
    bob = create_user(conn, "bob", "Bob", PASSWORD)
    return {"admin": admin, "alice": alice, "bob": bob}


class Client:
    """ログイン済みの API クライアント（CSRF トークンを自動で付ける）。"""

    def __init__(self, app, username: str):
        self.http = TestClient(app, base_url="http://testserver")
        r = self.http.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        self.user = r.json()["user"]
        self.root = self.http.get("/api/v1/me").json()["root_folder_id"]

    def _h(self, kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return kw

    def get(self, url, **kw):
        return self.http.get(url, **kw)

    def post(self, url, **kw):
        return self.http.post(url, **self._h(kw))

    def put(self, url, **kw):
        return self.http.put(url, **self._h(kw))

    def patch(self, url, **kw):
        return self.http.patch(url, **self._h(kw))

    def delete(self, url, **kw):
        return self.http.delete(url, **self._h(kw))

    def upload(self, folder_id: int, files: dict[str, bytes], **form):
        data = {"folder_id": str(folder_id), **form}
        multipart = [("files", (name, io.BytesIO(body), "application/octet-stream")) for name, body in files.items()]
        return self.post("/api/v1/documents", data=data, files=multipart)


@pytest.fixture()
def login(app, users):
    return lambda username: Client(app, username)
