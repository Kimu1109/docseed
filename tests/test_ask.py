"""質問に答える（17。検索 + LLM、RAG）。llama-server は httpx のモックで置き換える。"""

import dataclasses
import json

import httpx
import pytest

from docseed import rag
from docseed.llm import LlmClient

from .test_search import indexed  # noqa: F401 - フィクスチャ


class FakeLlama:
    """llama-server の /apply-template と /completion（stream）の代わり。受け取ったプロンプトを残す。"""

    def __init__(self, answer: str = "振込先は三井銀行 本店です [1]。", status: int = 200):
        self.answer = answer
        self.status = status
        self.prompts: list[str] = []
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "\n".join(m["content"] for m in body["messages"])})
        self.calls += 1
        self.prompts.append(body["prompt"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "壊れています"}})
        assert body["stream"] is True
        pieces = [self.answer[i:i + 5] for i in range(0, len(self.answer), 5)]
        sse = "".join(f"data: {json.dumps({'content': p, 'stop': False}, ensure_ascii=False)}\n\n" for p in pieces)
        sse += 'data: {"content": "", "stop": true}\n\n'
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    def client(self) -> LlmClient:
        return LlmClient("http://llm:8080", "fake.gguf", timeout_s=30, transport=httpx.MockTransport(self.handle))


@pytest.fixture()
def llama(app):
    app.state.settings = dataclasses.replace(app.state.settings, llm_enabled=True)
    fake = FakeLlama()
    app.state.ask_llm = fake.client()
    return fake


def _ask(client, question, **kw):
    return client.post("/api/v1/ask", json={"question": question, **kw})


def test_disabled(indexed):  # noqa: F811
    alice, _ = indexed
    r = _ask(alice, "振込先は？")
    assert r.status_code == 409 and r.json()["code"] == "ask_disabled"


def test_answer_with_sources(indexed, llama):  # noqa: F811
    alice, ids = indexed
    r = _ask(alice, "振込先")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["answer"] == "振込先は三井銀行 本店です [1]。" and j["cited"] == [1]
    assert j["sources"][0]["document_id"] == ids["請求書_A社.txt"] and j["sources"][0]["path"] == "/"
    assert "三井銀行" in j["sources"][0]["text"] and j["model"] == "fake.gguf"
    # プロンプト: 決まりごと、番号つきの資料、質問
    p = llama.prompts[0]
    assert "資料に無いことを推測で書いてはいけません" in p and "[1] 請求書_A社.txt" in p and "質問: 振込先" in p


def test_stream(indexed, llama):  # noqa: F811
    alice, ids = indexed
    r = _ask(alice, "振込先", stream=True)
    assert r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(x) for x in r.text.splitlines()]
    assert events[0]["type"] == "sources" and events[0]["sources"][0]["document_id"] == ids["請求書_A社.txt"]
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert len(texts) > 1 and "".join(texts) == llama.answer
    assert events[-1]["type"] == "done" and events[-1]["cited"] == [1]


def test_no_sources_does_not_call_llm(indexed, llama):  # noqa: F811
    alice, _ = indexed
    j = _ask(alice, "量子コンピュータの冷却方式", filters={"type": "pdf"}).json()
    assert j["answer"] == "" and j["sources"] == [] and "見つかりませんでした" in j["notices"][-1]
    events = [json.loads(x) for x in _ask(alice, "振込先", filters={"type": "pdf"}, stream=True).text.splitlines()]
    assert events[-1] == {"type": "done", "cited": [], "took_ms": events[-1]["took_ms"], "no_sources": True}
    assert llama.calls == 0


def test_only_visible_documents_are_used(indexed, llama, login, users):  # noqa: F811
    bob = login("bob")
    j = _ask(bob, "振込先").json()
    assert j["sources"] == [] and llama.calls == 0                 # alice の文書は資料にしない


def test_filters_are_applied(indexed, llama):  # noqa: F811
    alice, ids = indexed
    f = alice.post("/api/v1/folders", json={"parent_id": alice.root, "name": "経理"}).json()
    alice.patch(f"/api/v1/documents/{ids['契約書.txt']}", json={"folder_id": f["id"]})
    j = _ask(alice, "振込先", filters={"folder": str(f["id"])}).json()
    assert all(s["document_id"] == ids["契約書.txt"] for s in j["sources"])


def test_llm_errors(indexed, app, llama):  # noqa: F811
    alice, _ = indexed
    llama.status = 500
    r = _ask(alice, "振込先")
    assert r.status_code == 503 and r.json()["code"] == "ask_failed"
    events = [json.loads(x) for x in _ask(alice, "振込先", stream=True).text.splitlines()]
    assert events[-1]["type"] == "error" and events[-1]["code"] == "ask_failed"
    # llm サービスに届かない
    app.state.ask_llm = LlmClient("http://llm:8080", "x", timeout_s=5,
                                  transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(
                                      httpx.ConnectError("refused"))))
    assert _ask(alice, "振込先").status_code == 503


def test_busy(indexed, llama, monkeypatch):  # noqa: F811
    alice, _ = indexed
    monkeypatch.setattr(rag, "BUSY_WAIT_S", 0.01)
    rag._slots.acquire()
    rag._slots.acquire()
    try:
        r = _ask(alice, "振込先")
        assert r.status_code == 429 and r.json()["code"] == "ask_busy"
    finally:
        rag._slots.release()
        rag._slots.release()


def test_question_validation(indexed, llama):  # noqa: F811
    alice, _ = indexed
    assert _ask(alice, "").status_code == 422
    assert _ask(alice, "   ").status_code == 422
    assert _ask(alice, "あ" * 1001).status_code == 422


def test_cited_and_prompt_helpers():
    srcs = [rag.Source(n, n, f"{n}.txt", 1, None, "x") for n in (1, 2, 3)]
    assert rag.cited("A [2]。B [1][2]。C [9]。", srcs) == [2, 1]
    assert rag.clean_question("  請求書の\n振込先は？  ") == "請求書の 振込先は？"
    assert "[3] 3.txt\nx" in rag.build_prompt("q", srcs)


def test_search_page_button(indexed, app, login):  # noqa: F811
    alice, _ = indexed
    assert "data-ask" not in alice.get("/search", params={"q": "振込先"}).text   # LLM を使わない設定
    app.state.settings = dataclasses.replace(app.state.settings, llm_enabled=True)
    page = alice.get("/search", params={"q": "振込先", "type": "text"}).text
    assert "data-ask" in page and 'data-question="振込先"' in page and "&#34;type&#34;: &#34;text&#34;" not in page
    assert 'data-filters=\'{"type": "text"}\'' in page
    assert "data-ask" not in alice.get("/search").text                          # 検索語が無いとき


def test_metrics_count_answers(indexed, llama, login):  # noqa: F811
    alice, _ = indexed
    _ask(alice, "振込先")
    m = login("admin").get("/api/v1/admin/metrics").text
    assert 'docseed_ask_duration_seconds_count{result="ok"}' in m
