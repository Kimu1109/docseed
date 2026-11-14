"""ヘルプ: docs/ の文書を画面で読む（docs/design/ui.md S12、helpdocs.py）。

ログインした人なら誰でも読める（文書はリポジトリで公開しているものと同じ。秘密は書かない）。
上のガイドの切り替えに、管理者・開発者向けのガイドは管理者にだけ出す（リンクから開くことは誰でもできる）。
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from ... import helpdocs
from ...deps import Auth, get_conn, require_auth
from ..common import base_context, render_error, router, templates


@router.get("/help", response_class=HTMLResponse)
@router.get("/help/{key:path}", response_class=HTMLResponse)
def help_page(request: Request, key: str = "", conn: sqlite3.Connection = Depends(get_conn),
              auth: Auth = Depends(require_auth)):
    page = helpdocs.get_page(key)
    if page is None:
        if helpdocs.docs_dir() is None:
            return render_error(request, 404, "このサーバーには文書が入っていません（イメージを作り直してください）")
        return render_error(request, 404, "その文書はありません")
    guides = [(g, label, helpdocs.url_of(g)) for g, label, admin_only in helpdocs.GUIDES
              if auth.user.is_admin or not admin_only]
    return templates.TemplateResponse(request, "help.html", base_context(
        request, conn, auth, nav="help", page=page, guides=guides, map_url=helpdocs.url_of("")))
