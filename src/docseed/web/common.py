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

from .. import __version__, i18n
from .. import folders as fo
from .. import users as um
from ..deps import (
    Auth,
)
from ..errors import AppError
from ..files import can_preview, image_src, media_kind
from ..i18n import N_, Labels, _
from ..urls import BASE_GLOBAL, href, is_local_path, to_browser
from . import helpers as h

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def code(text: str) -> Markup:
    """文の中に差し込む <code>（_("… {cmd} …", cmd=code("docker compose logs")) のように使う）。"""
    return Markup("<code>{}</code>").format(text)


templates.env.globals.update(
    _=_, code=code, lang=i18n.get_lang, lang_names=i18n.LANG_NAMES, js_catalog=i18n.js_catalog,
    version=__version__, kind_of=h.kind_of, kind_label=Labels(h.KIND_LABEL), status_label=Labels(h.STATUS_LABEL),
    can_preview=can_preview, image_src=image_src, media_kind=media_kind, icon=h.icon, kind_icon=h.KIND_ICON,
    status_icon=h.STATUS_ICON, base=BASE_GLOBAL,
)


templates.env.filters.update(filesize=h.filesize, localtime=h.localtime, shortdate=h.shortdate, taken_at=h.taken_at,
                             duration=h.duration)


router = APIRouter(include_in_schema=False)


DONE_MESSAGES = {
    "renamed": N_("名前を変更しました"),
    "moved": N_("移動しました"),
    "saved": N_("保存しました"),
    "deleted": N_("ゴミ箱に移動しました"),
    "password": N_("パスワードを変更しました。他の端末ではログアウトされます"),
    "user_created": N_("ユーザーを作成しました"),
    "user_updated": N_("ユーザーを更新しました"),
    "token_revoked": N_("トークンを無効にしました"),
    "reprocess": N_("再処理を受け付けました"),
    "reprocess_page": N_("このページの読み直しを受け付けました"),
    "tag_renamed": N_("タグの名前を変えました"),
    "tag_deleted": N_("タグを外しました"),
    "unrejected": N_("却下を取り消しました"),
    "unrejected_cleared": N_("却下の記録を消しました（今の状態では候補に出ません）"),
    "retried": N_("ジョブをやり直します"),
    "share_revoked": N_("共有リンクを無効にしました"),
    "refetch": N_("再取得を受け付けました"),
    "suggest_accept": N_("候補を採用しました"),
    "suggest_reject": N_("候補を却下しました"),
    "prefs": N_("整理の補助の設定を保存しました"),
    "summaries": N_("古い要約の作り直しを受け付けました"),
    "redo": N_("要約とタグの候補の作り直しを受け付けました"),
    "summary_saved": N_("要約を保存しました"),
    "summary_hidden": N_("この文書では要約を出さないようにしました"),
    "summary_restored": N_("自動で作った要約に戻しました"),
    "redo_suggest": N_("タグとフォルダの候補を作り直しました"),
    "redo_short": N_("タグとフォルダの候補を作り直しました（本文が短いため、要約は作りません）"),
    "summaries_none": N_("作り直す要約はありません"),
    "reauthed": N_("本人確認ができました"),
    "totp_disabled": N_("確認コードを無効にしました"),
    "passkey_added": N_("パスキーを登録しました"),
    "passkey_removed": N_("パスキーを削除しました"),
    "identity_linked": N_("外部アカウントを連携しました"),
    "identity_removed": N_("外部アカウントの連携を解除しました"),
    "password_login_on": N_("パスワードでのログインを有効にしました"),
    "password_login_off": N_("パスワードでのログインを止めました"),
    "sessions_revoked": N_("ほかの端末をすべてログアウトしました"),
    "auth_settings": N_("ログインの設定を保存しました"),
    "version_uploaded": N_("新しい版にしました（前の版は「過去の版」に残しています）"),
    "version_same": N_("今の版と同じ内容でした（版は増やしていません）"),
    "version_restored": N_("過去の版に戻しました（戻す前の版も「過去の版」に残しています）"),
    "version_deleted": N_("過去の版を削除しました"),
    "notify_sent": N_("テスト通知を送りました。届いたか確かめてください"),
    "recent_cleared": N_("最近開いたファイルの記録を消しました"),
    "theme": N_("画面の色を変えました"),
    "trash_retention": N_("ゴミ箱の保持日数を保存しました"),
    "restored": N_("ゴミ箱から元に戻しました"),
    "purged": N_("完全に削除しました"),
    "quota": N_("容量の上限を保存しました"),
    "search_saved": N_("検索を保存しました"),
    "search_deleted": N_("保存した検索を消しました"),
    "search_renamed": N_("保存した検索の名前を変えました"),
    "history_deleted": N_("検索の履歴から消しました"),
    "history_cleared": N_("検索の履歴をすべて消しました"),
    "notify_failed": N_("テスト通知を送れませんでした。下の「最近の通知」のエラーを確認してください"),
    "lang": N_("画面の言語を変えました"),
}


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def toast(message: str, kind: str = "ok") -> Markup:
    return Markup(f'<div id="toast" hx-swap-oob="true" class="toast toast-{kind}" role="status">'
                  f'{escape(_(message))}</div>')


def base_context(request: Request, conn: sqlite3.Connection, auth: Auth | None, **extra) -> dict:
    ctx = {"request": request, "user": auth.user if auth else None,
           "csrf_token": getattr(request.state, "csrf_token", ""), "done": None}
    if auth:
        ctx["root_id"] = um.root_folder_id(conn, auth.user.id)
        from ..personal import get_lang, get_theme
        ctx["theme"] = get_theme(conn, auth.user)
        # 設定で言語を決めていれば、Cookie・ブラウザの言語より優先する
        ctx["lang_pref"] = get_lang(conn, auth.user)
        if ctx["lang_pref"] != "auto":
            i18n.set_lang(ctx["lang_pref"])
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
        ctx["done"] = _(DONE_MESSAGES[done])
    ctx.update(extra)
    return ctx


def set_lang_cookie(resp: Response, settings, lang: str) -> None:
    """ログイン前の画面の言語（i18n.COOKIE）。auto なら消してブラウザの言語に任せる。"""
    if lang in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, lang, max_age=400 * 86400, secure=settings.cookie_secure, samesite="lax")
    else:
        resp.delete_cookie(i18n.COOKIE)


def render_error(request: Request, status: int, detail: str) -> Response:
    if is_htmx(request):
        # htmx は 4xx/5xx の応答を差し込まないため、200 でお知らせだけを差し込む
        return HTMLResponse(str(toast(detail, "error")), status_code=200,
                            headers={"HX-Reswap": "none"})
    return templates.TemplateResponse(request, "error.html", {"request": request, "status": status,
                                                              "detail": _(detail), "user": None},
                                      status_code=status)


def _safe_next(nxt: str | None) -> str:
    """ログイン後の戻り先。このアプリの中のパス（接頭辞を含む）だけを受け付ける。"""
    if is_local_path(nxt):
        return to_browser(nxt)
    return href("/")


def _check_origin(request: Request) -> None:
    """ログインフォームの CSRF 対策（セッションが無いので Origin で確認する）。"""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise AppError(_("別のサイトからのログインは受け付けていません"))


def display_location(conn: sqlite3.Connection, auth: Auth, folder_id: int, cache: dict[int, str]) -> str:
    """「マイファイル / 経理 / 2026」や「Hanako さんのファイル / 共有」のような場所の表示。"""
    if folder_id not in cache:
        crumbs = fo.path(conn, folder_id)
        owner_id = fo.get_folder_row(conn, folder_id)["owner_id"]
        if owner_id == auth.user.id:
            root = _("マイファイル")
        else:
            root = _("{name} さんのファイル", name=um.get_user(conn, owner_id).display_name)
        cache[folder_id] = " / ".join([root] + [c.name for c in crumbs if c.name])
    return cache[folder_id]
