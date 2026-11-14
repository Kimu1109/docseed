"""JavaScript で描くページの描画の依頼（docs/design/extensions.md 6.10）。

fetcher は HTML を取得したあと、本文がほとんど無ければ描画サービス（browser.py。別コンテナ）に描画を頼み、
描画後の HTML を保存する。描画サービスが無い・失敗したときは、取得した HTML のまま保存する（失敗にしない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..pipeline.extract import WEBCLIP_SHORT
from .safefetch import FetchError, check_url

RENDER_TIMEOUT = 45.0      # 描画サービス側の上限（20 秒 + 待ち 5 秒）より長く取る
RENDER_PATH = "/render"


class RenderError(Exception):
    """描画できなかった。呼び出し側は取得した HTML のまま保存する。"""


@dataclass
class Rendered:
    html: str
    final_url: str


def looks_client_rendered(html: str, url: str) -> bool:
    """取得した HTML の本文が短く、JavaScript で描くページらしいか（extract と同じ基準で測る）。"""
    import trafilatura
    try:
        body = trafilatura.extract(html, url=url, output_format="txt", include_tables=True,
                                   include_comments=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001 - 取り出せなければ短いものとして扱う
        body = ""
    return len(re.sub(r"\s", "", body)) < WEBCLIP_SHORT


def render(render_url: str, url: str, *, max_bytes: int) -> Rendered:
    """描画サービスに url を描かせる。失敗は RenderError。"""
    try:
        check_url(url)
    except FetchError:
        raise RenderError("描画の依頼の URL が正しくありません") from None
    endpoint = render_url.rstrip("/") + RENDER_PATH
    try:
        # 描画サービスは内部のネットワークにあるので、プロキシの設定は使わない
        with httpx.Client(trust_env=False, timeout=RENDER_TIMEOUT) as client:
            r = client.post(endpoint, json={"url": url, "max_bytes": max_bytes})
    except httpx.HTTPError as e:
        raise RenderError(f"描画サービスに接続できませんでした: {e}") from None
    if r.status_code != 200:
        try:
            message = r.json().get("message") or f"HTTP {r.status_code}"
        except ValueError:
            message = f"HTTP {r.status_code}"
        raise RenderError(f"描画できませんでした: {message}")
    try:
        data = r.json()
        html, final = data["html"], data.get("final_url") or url
    except (ValueError, KeyError, TypeError):
        raise RenderError("描画サービスの応答が正しくありません") from None
    if not isinstance(html, str) or not html or not isinstance(final, str):
        raise RenderError("描画サービスの応答が正しくありません")
    try:
        check_url(final)
    except Exception:  # noqa: BLE001 - 最後の URL が不正なら、描画は使わない
        raise RenderError("描画後の URL が正しくありません") from None
    return Rendered(html, final)
