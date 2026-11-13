"""関連する文書（文書の画面の「関連する文書」、GET /api/v1/documents/{id}/related）。

整理の補助（suggest.py）と同じく、計算済みの埋め込みだけを使う: 文書のベクトル（先頭のチャンクの平均）に近い
チャンクを探し、それを含む文書を類似度の高い順に並べる。見る人が開ける文書だけを出す。
"""

from __future__ import annotations

import sqlite3

from .search import accessible_folders
from .suggest import doc_vector, similar_documents
from .users import User

SIM_MIN = 0.80        # これより低いものは出さない（ruri-v3-70m。整理の補助の SIM_MIN と同じ）
LIMIT = 5


def related_documents(conn: sqlite3.Connection, user: User, doc: sqlite3.Row, *, limit: int = LIMIT,
                      sim_min: float | None = None) -> list[dict]:
    """[{id, filename, kind, mime_type, folder_id, updated_at, similarity}]。ベクトルが無ければ []。

    同じ中身（同じ blob）の文書（ただのコピー）とゴミ箱の中は出さない。
    """
    vec = doc_vector(conn, doc["blob_id"])
    if vec is None:
        return []
    allowed = accessible_folders(conn, user)
    floor = SIM_MIN if sim_min is None else sim_min
    top = [(d, s) for d, _f, s in similar_documents(conn, vec, doc["id"], allowed, top=limit,
                                                     exclude_blob=doc["blob_id"]) if s >= floor]
    if not top:
        return []
    ids = [d for d, _ in top]
    rows = {r["id"]: r for r in conn.execute(
        "SELECT d.id, d.filename, d.kind, d.folder_id, d.updated_at, b.mime_type, b.thumb_path"
        f" FROM documents d JOIN blobs b ON b.id = d.blob_id WHERE d.id IN ({','.join('?' * len(ids))})", ids)}
    return [dict(rows[d]) | {"similarity": round(s, 4)} for d, s in top if d in rows]
