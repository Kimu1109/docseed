"""画面（Jinja2 + htmx）。docs/design/ui.md。

データの処理は API と同じサービス関数（documents / folders / users）を使う。
htmx からの部分更新は HTML の断片を返し、結果のお知らせは #toast への OOB swap で表示する。

ルートは views/ のモジュールに分けている（読み込むと common.router に登録される）。ここでは読み込む順を決め、
ほかのモジュールが使う名前を今までどおり web.routes から import できるようにする。
"""

from __future__ import annotations

from .common import _check_origin, _safe_next, base_context, render_error, router, templates  # noqa: F401
from .views import (  # noqa: F401 - 読み込むとルートが登録される
    admin,
    documents,
    files,
    login,
    organize,
    search,
    settings,
    share,
    trash,
    webclips,
)
from .views.admin import admin_context  # noqa: F401

__all__ = ["_check_origin", "_safe_next", "admin_context", "base_context", "render_error", "router", "templates"]
