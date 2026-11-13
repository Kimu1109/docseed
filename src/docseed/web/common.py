"""画面（Jinja2 + htmx）の共通部分: テンプレート・ルーター・共通の文脈・お知らせ。docs/design/ui.md。

画面のルートは views/ の各モジュールにあり、web/routes.py がまとめて読み込む。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import __version__
from .. import folders as fo
from .. import users as um
from ..deps import (
    Auth,
)
from ..errors import AppError
from ..files import can_preview, image_src
from . import helpers as h

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


templates.env.globals.update(
    version=__version__, kind_of=h.kind_of, kind_label=h.KIND_LABEL, status_label=h.STATUS_LABEL,
    can_preview=can_preview, image_src=image_src, icon=h.icon, kind_icon=h.KIND_ICON, status_icon=h.STATUS_ICON,
)


templates.env.filters.update(filesize=h.filesize, localtime=h.localtime, shortdate=h.shortdate, taken_at=h.taken_at,
                             duration=h.duration)


router = APIRouter(include_in_schema=False)


DONE_MESSAGES = {
    "renamed": "名前を変更しました",
    "moved": "移動しました",
    "saved": "保存しました",
    "deleted": "ゴミ箱に移動しました",
    "password": "パスワードを変更しました。他の端末ではログアウトされます",
    "user_created": "ユーザーを作成しました",
    "user_updated": "ユーザーを更新しました",
    "token_revoked": "トークンを無効にしました",
    "reprocess": "再処理を受け付けました",
    "reprocess_page": "このページの読み直しを受け付けました",
    "tag_renamed": "タグの名前を変えました",
    "tag_deleted": "タグを外しました",
    "unrejected": "却下を取り消しました",
    "unrejected_cleared": "却下の記録を消しました（今の状態では候補に出ません）",
    "retried": "ジョブをやり直します",
    "share_revoked": "共有リンクを無効にしました",
    "clip": "URL を受け付けました",
    "clip_dup": "URL を受け付けました（このページは既に保存されています）",
    "refetch": "再取得を受け付けました",
    "suggest_accept": "候補を採用しました",
    "suggest_reject": "候補を却下しました",
    "prefs": "整理の補助の設定を保存しました",
    "summaries": "古い要約の作り直しを受け付けました",
    "redo": "要約とタグの候補の作り直しを受け付けました",
    "summary_saved": "要約を保存しました",
    "summary_hidden": "この文書では要約を出さないようにしました",
    "summary_restored": "自動で作った要約に戻しました",
    "redo_suggest": "タグとフォルダの候補を作り直しました",
    "redo_short": "タグとフォルダの候補を作り直しました（本文が短いため、要約は作りません）",
    "summaries_none": "作り直す要約はありません",
    "reauthed": "本人確認ができました",
    "totp_disabled": "確認コードを無効にしました",
    "passkey_added": "パスキーを登録しました",
    "passkey_removed": "パスキーを削除しました",
    "identity_linked": "外部アカウントを連携しました",
    "identity_removed": "外部アカウントの連携を解除しました",
    "password_login_on": "パスワードでのログインを有効にしました",
    "password_login_off": "パスワードでのログインを止めました",
    "sessions_revoked": "ほかの端末をすべてログアウトしました",
    "auth_settings": "ログインの設定を保存しました",
    "version_uploaded": "新しい版にしました（前の版は「過去の版」に残しています）",
    "version_same": "今の版と同じ内容でした（版は増やしていません）",
    "version_restored": "過去の版に戻しました（戻す前の版も「過去の版」に残しています）",
    "version_deleted": "過去の版を削除しました",
    "notify_sent": "テスト通知を送りました。届いたか確かめてください",
    "recent_cleared": "最近開いたファイルの記録を消しました",
    "theme": "画面の色を変えました",
    "trash_retention": "ゴミ箱の保持日数を保存しました",
    "quota": "容量の上限を保存しました",
    "search_saved": "検索を保存しました",
    "search_deleted": "保存した検索を消しました",
    "search_renamed": "保存した検索の名前を変えました",
    "history_deleted": "検索の履歴から消しました",
    "history_cleared": "検索の履歴をすべて消しました",
    "notify_failed": "テスト通知を送れませんでした。下の「最近の通知」のエラーを確認してください",
}


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def toast(message: str, kind: str = "ok") -> Markup:
    return Markup(f'<div id="toast" hx-swap-oob="true" class="toast toast-{kind}" role="status">'
                  f'{escape(message)}</div>')


def base_context(request: Request, conn: sqlite3.Connection, auth: Auth | None, **extra) -> dict:
    ctx = {"request": request, "user": auth.user if auth else None,
           "csrf_token": getattr(request.state, "csrf_token", ""), "done": None}
    if auth:
        ctx["root_id"] = um.root_folder_id(conn, auth.user.id)
        from ..personal import get_theme
        ctx["theme"] = get_theme(conn, auth.user)
        from ..limits import status as quota_status
        ctx["quota"] = quota_status(conn, auth.user.id)
        ctx["usage"] = ctx["quota"]["total"]
        ctx["pending"] = conn.execute(
            "SELECT count(DISTINCT b.id) FROM documents d JOIN blobs b ON b.id = d.blob_id"
            " JOIN folders f ON f.id = d.folder_id"
            " WHERE f.owner_id = ? AND d.deleted_at IS NULL AND b.status IN ('pending', 'processing')",
            (auth.user.id,)).fetchone()[0]
        if auth.user.is_admin:
            from ..monitor import alert_count
            ctx["alert_count"] = alert_count(conn, request.app.state.settings)
    done = request.query_params.get("done")
    if done in DONE_MESSAGES:
        ctx["done"] = DONE_MESSAGES[done]
    ctx.update(extra)
    return ctx


def render_error(request: Request, status: int, detail: str) -> Response:
    if is_htmx(request):
        # htmx は 4xx/5xx の応答を差し込まないため、200 でお知らせだけを差し込む
        return HTMLResponse(str(toast(detail, "error")), status_code=200,
                            headers={"HX-Reswap": "none"})
    return templates.TemplateResponse(request, "error.html", {"request": request, "status": status,
                                                              "detail": detail, "user": None}, status_code=status)


def _safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


def _check_origin(request: Request) -> None:
    """ログインフォームの CSRF 対策（セッションが無いので Origin で確認する）。"""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise AppError("別のサイトからのログインは受け付けていません")


def display_location(conn: sqlite3.Connection, auth: Auth, folder_id: int, cache: dict[int, str]) -> str:
    """「マイファイル / 経理 / 2026」や「Hanako さんのファイル / 共有」のような場所の表示。"""
    if folder_id not in cache:
        crumbs = fo.path(conn, folder_id)
        owner_id = fo.get_folder_row(conn, folder_id)["owner_id"]
        if owner_id == auth.user.id:
            root = "マイファイル"
        else:
            root = um.get_user(conn, owner_id).display_name + " さんのファイル"
        cache[folder_id] = " / ".join([root] + [c.name for c in crumbs if c.name])
    return cache[folder_id]
