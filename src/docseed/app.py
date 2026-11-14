"""FastAPI アプリの組み立て。"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .api.auth import router as api_auth_router
from .api.routes import health
from .api.routes import router as api_router
from .api.suggestions import router as api_suggest_router
from .api.webclips import router as api_webclip_router
from .config import Settings
from .db import connect, migrate
from .errors import AppError, SecondFactorSetupRequired, Unauthenticated
from .search import QueryEmbedder
from .storage import BlobStore
from .urls import BasePathMiddleware, href
from .web.auth import router as auth_router
from .web.public import router as public_router
from .web.routes import render_error
from .web.routes import router as web_router

log = logging.getLogger("docseed")

APP_CSP = ("default-src 'self'; img-src 'self' data: blob:; frame-src 'self'; object-src 'none'; "
           "base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        # ファイル配信（files.file_response）は独自の CSP / X-Frame-Options を付ける
        if "content-security-policy" not in h and "x-frame-options" not in h:
            h["Content-Security-Policy"] = APP_CSP
            h["X-Frame-Options"] = "SAMEORIGIN"
        if request.url.path.startswith("/s/"):
            # 共有リンクのトークンを Referer で外部に漏らさない・検索エンジンに載せない・キャッシュさせない
            h["Referrer-Policy"] = "no-referrer"
            h["X-Robots-Tag"] = "noindex, nofollow"
            h["Cache-Control"] = "no-store"
        return resp


def _route_paths(routes, prefix: str = ""):
    """登録したルートのパス。FastAPI の include_router は、ルーターを入れ子のまま持つ版がある。"""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            ctx = getattr(r, "include_context", None)
            yield from _route_paths(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        elif isinstance(path := getattr(r, "path", None), str):
            yield prefix + path


def _check_base_path(app: FastAPI, base_path: str) -> None:
    """接頭辞の最初の部分がアプリのパス（/s/… や /api/… など）と同じだと、外側のプロキシが接頭辞を外して
    送ってきたときに区別できない。起動時に止める。"""
    if not base_path:
        return
    first = base_path.split("/")[1]
    used = {p.split("/")[1] for p in _route_paths(app.routes) if p.startswith("/")}
    if first in used - {""}:
        raise RuntimeError(f"DOCSEED_BASE_PATH={base_path} はアプリのパス /{first}/ と重なるため使えません"
                           "（例 /docseed）")


def _wants_json(request: Request) -> bool:
    # /auth/* のパスキー・2 段階目は、JSON で送られたときは JSON で返す（passkey.js が使う）
    return (request.url.path.startswith("/api/") or request.url.path == "/healthz"
            or request.headers.get("content-type", "").startswith("application/json"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.secret_key:
        raise RuntimeError("DOCSEED_SECRET_KEY が設定されていません（32 バイト以上の乱数を設定してください）")
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn)
        if applied:
            log.info("migrations applied: %s", applied)
    finally:
        conn.close()

    # API の説明ページは自前で出す（接頭辞があるとき、ページが読む openapi.json の URL にも接頭辞を付けるため）。
    # servers は「Try it out」の送り先
    app = FastAPI(title="docseed", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/api/v1/openapi.json",
                  servers=[{"url": settings.base_path}] if settings.base_path else None)

    @app.get("/api/docs", include_in_schema=False)
    def api_docs():
        return get_swagger_ui_html(openapi_url=href(app.openapi_url), title=f"{app.title} - Swagger UI")
    app.state.settings = settings
    app.state.store = BlobStore(settings.files_dir, settings.tmp_dir)
    app.state.query_embedder = QueryEmbedder(settings.models_dir, settings.embed_model,
                                             threads=settings.query_threads)
    # 最初の検索が遅くならないよう、モデルがあれば裏で読み込んでおく
    from .pipeline.embed import model_dir
    if (model_dir(settings.models_dir, settings.embed_model) / "onnx" / "model.onnx").exists():
        import threading
        threading.Thread(target=app.state.query_embedder.get, name="preload-embedder", daemon=True).start()

    from .tlscheck import start_background
    start_background(settings)
    from .notify import start_background as start_notify
    start_notify(settings)

    from .ratelimit import RateLimitMiddleware, setup_security_log
    setup_security_log(settings.data_dir / "logs")
    app.add_middleware(SecurityHeaders)
    # 流量の制限（docs/design/security.md 9 章）。SecurityHeaders より外側に置き、429 にもヘッダを付けない
    app.add_middleware(RateLimitMiddleware, enabled=settings.rate_limit_enabled)
    # URL の起点（DOCSEED_BASE_PATH）。一番外側に置き、リダイレクト・Cookie の Path にもまとめて接頭辞を付ける
    app.add_middleware(BasePathMiddleware, base_path=settings.base_path)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    app.include_router(health)
    app.include_router(api_router)
    app.include_router(api_auth_router)
    app.include_router(api_webclip_router)
    app.include_router(api_suggest_router)
    from .api.ask import router as api_ask_router
    app.include_router(api_ask_router)
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(web_router)
    _check_base_path(app, settings.base_path)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        if _wants_json(request):
            body = {"type": f"https://docseed.local/errors/{exc.code}", "title": exc.title, "status": exc.status,
                    "code": exc.code, "detail": exc.detail, "instance": request.url.path}
            if exc.errors:
                body["errors"] = exc.errors
            headers = {"Retry-After": str(exc.retry_after)} if hasattr(exc, "retry_after") else None
            return JSONResponse(body, status_code=exc.status, media_type="application/problem+json",
                                headers=headers)
        if isinstance(exc, Unauthenticated):
            if request.headers.get("hx-request"):
                return Response(status_code=204, headers={"HX-Redirect": "/login"})
            nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt, safe='/')}", status_code=303)
        if isinstance(exc, SecondFactorSetupRequired):
            if request.headers.get("hx-request"):
                return Response(status_code=204, headers={"HX-Redirect": "/settings/security?setup=1"})
            return RedirectResponse("/settings/security?setup=1", status_code=303)
        return render_error(request, exc.status, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        if _wants_json(request):
            return JSONResponse({"type": "https://docseed.local/errors/validation_error",
                                 "title": "入力内容を確認してください", "status": 422, "code": "validation_error",
                                 "detail": "入力内容を確認してください", "errors": errors,
                                 "instance": request.url.path},
                                status_code=422, media_type="application/problem+json")
        return render_error(request, 422, "入力内容を確認してください")

    return app


def app_factory() -> FastAPI:
    """uvicorn --factory 用。"""
    from .logs import setup
    setup(Settings.from_env().log_level)
    return create_app()
