"""質問に答える API（POST /api/v1/ask）。docs/design/search.md 10 章、api.md 3.5。"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import rag
from ..deps import Auth, get_conn, require_auth
from ..errors import AppError, ValidationFailed
from ..llm import LlmClient, LlmError, LlmTimeout, LlmUnavailable
from .routes import folder_label, run_search

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

FILTER_KEYS = ("type", "tag", "folder", "after", "before", "taken")


class AskDisabled(AppError):
    status, code, title = 409, "ask_disabled", "質問に答える機能は使えません（DOCSEED_LLM_ENABLED が false です）"


class AskBusy(AppError):
    status, code, title = 429, "ask_busy", "ほかの質問に答えています。少し待ってからもう一度お試しください"


class AskFailed(AppError):
    status, code, title = 503, "ask_failed", "答えを作れませんでした"


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    filters: dict[str, str] = Field(default_factory=dict)
    stream: bool = False


def llm_client(request: Request) -> LlmClient:
    """テストでは app.state.ask_llm で差し替える。"""
    s = request.app.state.settings
    return getattr(request.app.state, "ask_llm", None) or LlmClient(
        s.llm_url, s.llm_model, timeout_s=s.llm_timeout_s, max_input_chars=s.llm_max_input_chars)


def prepare(request: Request, conn: sqlite3.Connection, auth: Auth, question: str,
            filters: dict[str, str]) -> rag.Prepared:
    q = rag.clean_question(question)
    if not q:
        raise ValidationFailed("質問を入れてください")
    f = {k: str(v) for k, v in filters.items() if k in FILTER_KEYS and v}
    result = run_search(request, conn, auth, q, "hybrid", rag.SEARCH_DOCS, 0, f)
    sources = rag.collect_sources(conn, result, request.app.state.settings.llm_max_input_chars)
    return rag.Prepared(q, sources, list(result.notices))


def sources_json(conn: sqlite3.Connection, sources: list[rag.Source]) -> list[dict[str, Any]]:
    labels: dict[int, str] = {}
    return [{"n": s.n, "document_id": s.document_id, "filename": s.filename, "page_no": s.page_no,
             "path": folder_label(conn, s.folder_id, labels), "text": s.text} for s in sources]


def _observe(result: str, t0: float) -> None:
    from ..metrics import observe_ask
    observe_ask(result, time.perf_counter() - t0)


@router.post("/ask")
def api_ask(body: AskIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
            auth: Auth = Depends(require_auth)):
    """質問に、見られる文書だけを根拠に答える（検索 + LLM）。

    stream=false: {question, answer, sources, cited, model, took_ms, notices}
    stream=true: 1 行 1 件の JSON（application/x-ndjson）。{"type": "sources", ...} → {"type": "text", "text"}…
    → {"type": "done", "cited", "took_ms"}。途中で失敗したら {"type": "error", "code", "message"}。
    資料が見つからなければ LLM を呼ばず、answer は空、sources も空。
    """
    settings = request.app.state.settings
    if not settings.llm_enabled:
        raise AskDisabled()
    t0 = time.perf_counter()
    prepared = prepare(request, conn, auth, body.question, body.filters)
    srcs = sources_json(conn, prepared.sources)
    client = llm_client(request)

    if not body.stream:
        if prepared.empty:
            _observe("no_sources", t0)
            return {"question": prepared.question, "answer": "", "sources": [], "cited": [],
                    "model": client.model, "took_ms": int((time.perf_counter() - t0) * 1000),
                    "notices": prepared.notices + ["質問に関係しそうな文書が見つかりませんでした"]}
        try:
            answer = "".join(rag.answer_stream(client, prepared)).strip()
        except rag.Busy as e:
            _observe("busy", t0)
            raise AskBusy(str(e)) from None
        except (LlmError, LlmTimeout, LlmUnavailable) as e:
            _observe("error", t0)
            raise AskFailed(str(e)) from None
        _observe("ok", t0)
        return {"question": prepared.question, "answer": answer, "sources": srcs,
                "cited": rag.cited(answer, prepared.sources), "model": client.model,
                "took_ms": int((time.perf_counter() - t0) * 1000), "notices": prepared.notices}

    def events():
        # 検索（DB）は応答の前に済ませてある。ここでは LLM の出力を流すだけ
        def line(obj: dict) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        yield line({"type": "sources", "question": prepared.question, "sources": srcs, "notices": prepared.notices,
                    "model": client.model})
        if prepared.empty:
            _observe("no_sources", t0)
            yield line({"type": "done", "cited": [], "took_ms": int((time.perf_counter() - t0) * 1000),
                        "no_sources": True})
            return
        text = []
        try:
            for piece in rag.answer_stream(client, prepared):
                text.append(piece)
                yield line({"type": "text", "text": piece})
        except rag.Busy as e:
            _observe("busy", t0)
            yield line({"type": "error", "code": "ask_busy", "message": str(e)})
            return
        except (LlmError, LlmTimeout, LlmUnavailable) as e:
            log.warning("質問に答えられませんでした: %s", e)
            _observe("error", t0)
            yield line({"type": "error", "code": "ask_failed", "message": str(e)})
            return
        _observe("ok", t0)
        yield line({"type": "done", "cited": rag.cited("".join(text), prepared.sources),
                    "took_ms": int((time.perf_counter() - t0) * 1000)})

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
