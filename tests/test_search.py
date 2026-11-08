import hashlib

import numpy as np
import pytest

from docseed.db import Tx, now
from docseed.jobs.process import Context
from docseed.jobs.worker import Worker
from docseed.search import make_snippet, parse_query


class TrigramEmbedder:
    """文字 3-gram の出現を 384 次元に畳み込んだベクトル（同じ語を含むほど近くなる）。"""
    model_name = "fake/trigram"
    dim = 384

    def _vec(self, t: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        t = t.replace("検索文書: ", "").replace("検索クエリ: ", "")
        for i in range(max(1, len(t) - 2)):
            g = t[i:i + 3]
            v[int(hashlib.md5(g.encode()).hexdigest()[:6], 16) % self.dim] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


class StubQueryEmbedder:
    def __init__(self, emb):
        self.emb = emb

    def get(self):
        return self.emb, None


DOCS = {
    "請求書_A社.txt": "請求書\n\nお手数ですが下記の振込先まで、請求金額をお振り込みください。"
                     "振込先は三井銀行 本店 普通 1234567 です。",
    "会議メモ.txt": "会議メモ\n\n次回の会議では共有フォルダの整理方法と、"
                   "バックアップの手順について話し合います。下書きです。",
    "契約書.txt": "業務委託契約書\n\n本契約の有効期間は締結日から一年間とし、"
                 "期間満了の一か月前までに申し出がなければ自動更新する。",
    "税金の手引き.txt": "確定申告の手引き\n\n所得税と住民税の計算方法、"
                     "医療費控除の申請について説明します。税の申告期限に注意。",
}


@pytest.fixture()
def indexed(app, login, settings):
    # 偽の埋め込みは類似度の水準が低いので、意味検索の下限（実モデル用 0.80）を下げる
    object.__setattr__(app.state.settings, "search_sem_min", 0.2)
    emb = TrigramEmbedder()
    app.state.query_embedder = StubQueryEmbedder(emb)
    alice = login("alice")
    ids = {}
    for name, body in DOCS.items():
        d = alice.upload(alice.root, {name: body.encode()}).json()["results"][0]["document"]
        ids[name] = d["id"]
    ctx = Context(settings.files_dir, ocr_factory=lambda: None, embedder_factory=lambda: emb)
    w = Worker(settings, ctx)
    while w.run_once():
        pass
    return alice, ids


def names(r):
    return [x["document"]["filename"] for x in r.json()["results"]]


def test_parse_query():
    pq = parse_query('請求書 "株式会社サンプル" -下書き type:pdf after:2026-01-01')
    assert pq.terms == ["請求書"] and pq.phrases == ["株式会社サンプル"] and pq.excludes == ["下書き"]
    assert pq.filters == {"type": "pdf", "after": "2026-01-01"}
    assert pq.semantic_text == "請求書 株式会社サンプル"
    assert parse_query("ＡＢＣ").terms == ["ABC"]  # NFKC


def test_snippet_escapes_and_marks():
    s = make_snippet("<script>alert(1)</script> 振込先はこちら", ["振込先"])
    assert "<script>" not in s and "&lt;script&gt;" in s
    assert "<mark>振込先</mark>" in s


def test_keyword_search(indexed):
    alice, ids = indexed
    r = alice.get("/api/v1/search", params={"q": "振込先", "mode": "keyword"})
    assert r.status_code == 200
    assert names(r) == ["請求書_A社.txt"]
    snip = r.json()["results"][0]["snippets"][0]
    assert "<mark>振込先</mark>" in snip["text"] and snip["page_no"] == 1 and "keyword" in snip["match"]


def test_and_exclude_and_phrase(indexed):
    alice, _ = indexed
    assert names(alice.get("/api/v1/search", params={"q": "会議 フォルダ", "mode": "keyword"})) == ["会議メモ.txt"]
    assert names(alice.get("/api/v1/search", params={"q": "共有フォルダ -下書き", "mode": "keyword"})) == []
    assert names(alice.get("/api/v1/search", params={"q": '"有効期間"'})) == ["契約書.txt"]


def test_hybrid_finds_semantic_and_keyword(indexed):
    alice, _ = indexed
    # 語としては一致しない（空白なしの長い語）が、意味検索で見つかる
    r = alice.get("/api/v1/search", params={"q": "バックアップの手順を知りたい"})
    assert names(r)[0] == "会議メモ.txt"
    assert r.json()["results"][0]["snippets"][0]["match"] == ["semantic"]
    assert r.json()["took_ms"] >= 0


def test_short_terms(indexed):
    alice, _ = indexed
    # 2 文字以下の語: 意味検索の候補の中で照合（お知らせつき）
    r = alice.get("/api/v1/search", params={"q": "税"})
    assert "税金の手引き.txt" in names(r)
    assert any("2 文字以下" in n for n in r.json()["notices"])
    # キーワードモードでは本文を走査して見つける
    assert names(alice.get("/api/v1/search", params={"q": "税", "mode": "keyword"})) == ["税金の手引き.txt"]


def test_filters(indexed, conn):
    alice, ids = indexed
    assert names(alice.get("/api/v1/search", params={"q": "振込先", "type": "pdf"})) == []
    assert names(alice.get("/api/v1/search", params={"q": "振込先", "type": "text"})) == ["請求書_A社.txt"]
    alice.patch(f"/api/v1/documents/{ids['契約書.txt']}", json={"tags": ["法務"]})
    assert names(alice.get("/api/v1/search", params={"q": "契約 tag:法務", "mode": "keyword"})) == ["契約書.txt"]
    assert names(alice.get("/api/v1/search", params={"q": "振込先 after:2099-01-01"})) == []
    assert names(alice.get("/api/v1/search", params={"q": "振込先 before:2099-01-01"})) == ["請求書_A社.txt"]


def test_folder_filter(indexed):
    alice, ids = indexed
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "経理"}).json()
    alice.patch(f"/api/v1/documents/{ids['請求書_A社.txt']}", json={"folder_id": f["id"]})
    assert names(alice.get("/api/v1/search", params={"q": "振込先", "folder": f["id"]})) == ["請求書_A社.txt"]
    assert names(alice.get("/api/v1/search", params={"q": "有効期間", "folder": f["id"]})) == []


def test_access_control(indexed, login, conn, users):
    alice, ids = indexed
    bob = login("bob")
    assert names(bob.get("/api/v1/search", params={"q": "振込先"})) == []
    # フォルダを共有されると見つかる
    with Tx(conn):
        conn.execute("INSERT INTO folder_members VALUES (?, ?, 'viewer', ?, ?)",
                     (alice.root, users["bob"].id, users["alice"].id, now()))
    assert names(bob.get("/api/v1/search", params={"q": "振込先"})) == ["請求書_A社.txt"]


def test_trash_hidden(indexed):
    alice, ids = indexed
    alice.delete(f"/api/v1/documents/{ids['請求書_A社.txt']}")
    assert names(alice.get("/api/v1/search", params={"q": "振込先"})) == []


def test_same_content_two_documents(indexed):
    alice, ids = indexed
    alice.post(f"/api/v1/documents/{ids['請求書_A社.txt']}/copy",
               json={"folder_id": alice.root, "filename": "写し.txt"})
    assert sorted(names(alice.get("/api/v1/search", params={"q": "振込先", "mode": "keyword"}))) == \
        ["写し.txt", "請求書_A社.txt"]


def test_without_embedding_model(indexed, app):
    alice, _ = indexed

    class Missing:
        def get(self):
            return None, "埋め込みモデルが読み込めません"
    app.state.query_embedder = Missing()
    r = alice.get("/api/v1/search", params={"q": "振込先"})
    assert names(r) == ["請求書_A社.txt"]
    assert any("キーワードだけ" in n for n in r.json()["notices"])


def test_pagination(indexed):
    alice, _ = indexed
    r1 = alice.get("/api/v1/search", params={"q": "の", "mode": "keyword", "limit": 2}).json()
    r2 = alice.get("/api/v1/search", params={"q": "の", "mode": "keyword", "limit": 2, "offset": 2}).json()
    assert r1["total"] == 4 and len(r1["results"]) == 2 and len(r2["results"]) == 2
    assert {x["document"]["id"] for x in r1["results"]}.isdisjoint({x["document"]["id"] for x in r2["results"]})


def test_search_page_renders(indexed):
    alice, _ = indexed
    r = alice.get("/search", params={"q": "振込先"})
    assert r.status_code == 200 and "請求書_A社.txt" in r.text and "<mark>振込先</mark>" in r.text
    r = alice.get("/search", params={"q": "存在しない語句です"})
    assert "見つかりませんでした" in r.text
