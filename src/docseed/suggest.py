"""整理の補助: フォルダ・タグの候補。docs/design/extensions.md 7.2・7.3・7.6。

計算済みの埋め込み（chunk_vectors）だけを使うので、追加の負荷はほぼない。候補は提案するだけで、
利用者が画面で「移動」「付ける」を押して決める（設定で、確かさの高いものを自動で適用することもできる）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from . import folders as fo
from .audit import audit
from .db import Tx, now
from .errors import NotFound, ValidationFailed
from .users import User, get_user

DOC_CHUNKS = 20            # 文書のベクトルに使う先頭のチャンク数
NEAR_K = 200               # 近いチャンクを取る数
TOP_DOCS = 10              # 似ている文書の上位
SIM_MIN = 0.80             # フォルダの合計に数える類似度の下限（仮。scripts/eval_suggest.py で決める）
RECOMMEND_CONF = 0.6       # 「おすすめ」にする確かさの下限
RECOMMEND_DOCS = 3         # 「おすすめ」には、1 位のフォルダの文書が上位にこの件数以上必要
SMALL_FOLDER = 3           # 文書がこれより少ないフォルダは、パスの埋め込みとも比べる
PATH_SIM_MIN = 0.85        # パスとの類似度の下限
MAX_FOLDERS = 3
TAG_MIN = 1.5              # 既存のタグの候補にする重みの合計（似た文書 2 件ほど）
MAX_TAGS = 5
AUTO_MOVE_CONF = 0.8       # 自動で移動する確かさの下限（7.6）
AUTO_TAG_MIN = 2.5         # 自動でタグを付ける重みの合計の下限
TAG_MERGE_SIM = 0.90       # LLM のタグを既存のタグに置き換える類似度
INBOX_FOLDER = "Inbox"
RECOMMEND_BONUS = 100.0    # 保存するとき、おすすめのフォルダの score に足す（並べ替えと見分けのため）

PREFS = {                  # 利用者の設定（user_prefs）と既定値
    "suggest_folders": True,
    "auto_move": False,
    "suggest_tags": True,
    "auto_tag": False,
    "show_summary": True,
}


# ---------------------------------------------------------------------------
# 利用者の設定
# ---------------------------------------------------------------------------

def get_prefs(conn: sqlite3.Connection, user_id: int) -> dict[str, bool]:
    out = dict(PREFS)
    for k, v in conn.execute("SELECT key, value FROM user_prefs WHERE user_id = ?", (user_id,)):
        if k in out:
            out[k] = v == "1"
    return out


def set_prefs(conn: sqlite3.Connection, user_id: int, values: dict) -> dict[str, bool]:
    unknown = set(values) - set(PREFS)
    if unknown:
        raise ValidationFailed(f"不明な設定です: {', '.join(sorted(unknown))}")
    with Tx(conn):
        for k, v in values.items():
            conn.execute("INSERT INTO user_prefs(user_id, key, value) VALUES (?,?,?)"
                         " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                         (user_id, k, "1" if v else "0"))
    return get_prefs(conn, user_id)


# ---------------------------------------------------------------------------
# ベクトル
# ---------------------------------------------------------------------------

def _vec(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.float32)


def doc_vector(conn: sqlite3.Connection, blob_id: int) -> np.ndarray | None:
    """先頭 20 チャンクのベクトルの平均を L2 正規化したもの。ベクトルが無ければ None。"""
    rows = conn.execute(
        "SELECT v.embedding FROM chunks c JOIN chunk_vectors v ON v.rowid = c.id WHERE c.blob_id = ?"
        " ORDER BY c.seq LIMIT ?", (blob_id, DOC_CHUNKS)).fetchall()
    if not rows:
        return None
    m = np.mean([_vec(r[0]) for r in rows], axis=0)
    n = float(np.linalg.norm(m))
    return (m / n).astype(np.float32) if n > 0 else None


def inbox_folder_ids(conn: sqlite3.Connection, owner_id: int) -> set[int]:
    """ルート直下の Inbox とその配下。"""
    root = conn.execute("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS NULL", (owner_id,)).fetchone()
    if root is None:
        return set()
    ib = conn.execute("SELECT id FROM folders WHERE parent_id = ? AND name = ? COLLATE NOCASE AND deleted_at IS NULL",
                      (root[0], INBOX_FOLDER)).fetchone()
    return set(fo.subtree_ids(conn, ib[0])) if ib else set()


def candidate_folders(conn: sqlite3.Connection, owner_id: int) -> dict[int, str]:
    """候補にできるフォルダ {id: パス表記}。同じ所有者のフォルダだけ（所有者の違うフォルダへは移動できない）。

    Inbox（とその配下）、共有リンクの受け取り用のフォルダ、ゴミ箱の中は除く。
    """
    exclude = inbox_folder_ids(conn, owner_id)
    exclude |= {r[0] for r in conn.execute(
        "SELECT target_id FROM shares WHERE target_type = 'folder' AND permission = 'upload' AND revoked_at IS NULL")}
    return {fid: label for fid, label in fo.folders_of_owner(conn, owner_id) if fid not in exclude}


def _ensure_folder_vectors(conn: sqlite3.Connection, embedder, folders: dict[int, str]) -> dict[int, np.ndarray]:
    """フォルダのパスのベクトル（検索クエリとして埋め込む）。パスが変わっていれば作り直す。"""
    if not folders:
        return {}
    marks = ",".join("?" * len(folders))
    have = {r[0]: (r[1], _vec(r[2])) for r in conn.execute(
        f"SELECT rowid, path, embedding FROM folder_vectors WHERE rowid IN ({marks})", list(folders))}
    out: dict[int, np.ndarray] = {}
    todo = []
    for fid, path in folders.items():
        cur = have.get(fid)
        if cur and cur[0] == path:
            out[fid] = cur[1]
        else:
            todo.append((fid, path))
    if todo and embedder is not None:
        vecs = [np.asarray(embedder.embed_query(path.replace("/", " / ")), dtype=np.float32) for _, path in todo]
        with Tx(conn):
            for (fid, path), v in zip(todo, vecs, strict=True):
                conn.execute("DELETE FROM folder_vectors WHERE rowid = ?", (fid,))
                conn.execute("INSERT INTO folder_vectors(rowid, embedding, path) VALUES (?, ?, ?)",
                             (fid, v.tobytes(), path))
                out[fid] = v
    return out


# ---------------------------------------------------------------------------
# 候補の計算
# ---------------------------------------------------------------------------

@dataclass
class FolderCandidate:
    folder_id: int
    path: str
    score: float
    recommended: bool = False
    by_path: bool = False


@dataclass
class Result:
    folders: list[FolderCandidate] = field(default_factory=list)
    tags: list[tuple[str, float]] = field(default_factory=list)
    confidence: float = 0.0
    similar: list[tuple[int, float]] = field(default_factory=list)   # (文書 ID, 類似度)


def similar_documents(conn: sqlite3.Connection, vec: np.ndarray, doc_id: int,
                      allowed: dict[int, str]) -> list[tuple[int, int, float]]:
    """似ている文書の上位 10 件 (文書 ID, フォルダ ID, 類似度)。候補のフォルダの文書だけ。"""
    rows = conn.execute(
        "SELECT rowid, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = ?",
        (vec.astype(np.float32).tobytes(), NEAR_K)).fetchall()
    if not rows:
        return []
    sim = {r[0]: 1.0 - float(r[1]) for r in rows}
    best: dict[int, tuple[int, float]] = {}
    ids = list(sim)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(
                f"SELECT c.id, d.id AS doc_id, d.folder_id FROM chunks c JOIN documents d ON d.blob_id = c.blob_id"
                f" WHERE c.id IN ({','.join('?' * len(part))}) AND d.deleted_at IS NULL AND d.id != ?",
                (*part, doc_id)):
            if r["folder_id"] not in allowed:
                continue
            s = sim[r["id"]]
            if r["doc_id"] not in best or s > best[r["doc_id"]][1]:
                best[r["doc_id"]] = (r["folder_id"], s)
    top = sorted(best.items(), key=lambda kv: -kv[1][1])[:TOP_DOCS]
    return [(d, f, s) for d, (f, s) in top]


def compute(conn: sqlite3.Connection, doc: sqlite3.Row, embedder=None, *, sim_min: float = SIM_MIN,
            conf_min: float = RECOMMEND_CONF, skip_current: bool = True) -> Result:
    """文書の候補を計算する（保存はしない）。embedder はフォルダのパスの埋め込みに使う（無ければ使わない）。

    sim_min・conf_min・skip_current は評価（evaluate）で変えるためのもの。
    """
    res = Result()
    vec = doc_vector(conn, doc["blob_id"])
    if vec is None:
        return res
    owner = conn.execute("SELECT owner_id FROM folders WHERE id = ?", (doc["folder_id"],)).fetchone()[0]
    allowed = candidate_folders(conn, owner)
    top = similar_documents(conn, vec, doc["id"], allowed)
    res.similar = [(d, s) for d, _f, s in top]

    # フォルダ: 類似度 0.80 以上の文書の類似度をフォルダごとに足す
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    for _d, f, s in top:
        if s >= sim_min:
            sums[f] = sums.get(f, 0.0) + s
            counts[f] = counts.get(f, 0) + 1
    total = sum(sums.values())
    ranked = sorted(sums.items(), key=lambda kv: -kv[1])
    if ranked:
        res.confidence = ranked[0][1] / total if total else 0.0
    for i, (f, s) in enumerate(ranked):
        if skip_current and f == doc["folder_id"]:
            continue
        rec = i == 0 and counts[f] >= RECOMMEND_DOCS and res.confidence >= conf_min
        res.folders.append(FolderCandidate(f, allowed[f], round(s, 4), rec))

    # 文書の少ないフォルダは、パスの埋め込みとも比べる
    sizes = dict(conn.execute(
        "SELECT folder_id, count(*) FROM documents WHERE deleted_at IS NULL GROUP BY folder_id").fetchall())
    small = {f: p for f, p in allowed.items() if sizes.get(f, 0) < SMALL_FOLDER and p != "/"
             and not (skip_current and f == doc["folder_id"])}
    have = {c.folder_id for c in res.folders}
    for f, v in _ensure_folder_vectors(conn, embedder, small).items():
        s = float(np.dot(vec, v))
        if s >= PATH_SIM_MIN and f not in have:
            res.folders.append(FolderCandidate(f, allowed[f], round(s, 4), by_path=True))
    res.folders.sort(key=lambda c: (not c.recommended, -c.score))
    res.folders = res.folders[:MAX_FOLDERS]

    # タグ: 似た文書に付いているタグを類似度の重みで数える
    weights: dict[str, float] = {}
    for d, _f, s in top:
        for (name,) in conn.execute("SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id"
                                    " WHERE dt.document_id = ?", (d,)):
            weights[name] = weights.get(name, 0.0) + s
    mine = {r[0].lower() for r in conn.execute(
        "SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.document_id = ?", (doc["id"],))}
    res.tags = sorted(((n, round(w, 4)) for n, w in weights.items() if w >= TAG_MIN and n.lower() not in mine),
                      key=lambda x: -x[1])[:MAX_TAGS]
    return res


# ---------------------------------------------------------------------------
# 保存・採用・却下
# ---------------------------------------------------------------------------

def _rejected(conn: sqlite3.Connection, doc_id: int) -> tuple[set[int], set[str]]:
    folders, tags = set(), set()
    for r in conn.execute("SELECT kind, folder_id, tag_name FROM document_suggestions"
                          " WHERE document_id = ? AND status = 'rejected'", (doc_id,)):
        if r["kind"] == "folder":
            folders.add(r["folder_id"])
        else:
            tags.add((r["tag_name"] or "").lower())
    return folders, tags


def store(conn: sqlite3.Connection, doc_id: int, res: Result, *, folders: bool = True, tags: bool = True,
          llm_tags: list[str] | None = None) -> None:
    """open の候補を作り直す（却下されたものは二度と出さない）。llm_tags が None なら LLM のタグ候補は残す。"""
    rej_f, rej_t = _rejected(conn, doc_id)
    t = now()
    with Tx(conn):
        if llm_tags is None:
            conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open'"
                         " AND source = 'embedding'", (doc_id,))
        else:
            conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open'"
                         " AND source = 'llm'", (doc_id,))
        if folders:
            for c in res.folders:
                if c.folder_id not in rej_f:
                    conn.execute("INSERT INTO document_suggestions(document_id, kind, folder_id, source, score,"
                                 " created_at) VALUES (?, 'folder', ?, 'embedding', ?, ?)",
                                 (doc_id, c.folder_id, c.score + (RECOMMEND_BONUS if c.recommended else 0.0), t))
        existing = {r[0].lower() for r in conn.execute(
            "SELECT tag_name FROM document_suggestions WHERE document_id = ? AND kind = 'tag' AND status = 'open'",
            (doc_id,))}
        existing |= {r[0].lower() for r in conn.execute(      # もう付いているタグは出さない
            "SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.document_id = ?", (doc_id,))}
        if tags:
            items = [(n, s, "embedding") for n, s in res.tags] if llm_tags is None else \
                [(n, 1.0, "llm") for n in llm_tags]
            for n, s, src in items:
                if n.lower() in rej_t or n.lower() in existing:
                    continue
                existing.add(n.lower())
                conn.execute("INSERT INTO document_suggestions(document_id, kind, tag_name, source, score, created_at)"
                             " VALUES (?, 'tag', ?, ?, ?, ?)", (doc_id, n, src, s, t))


def is_recommended(score: float) -> bool:
    return score >= RECOMMEND_BONUS


def open_suggestions(conn: sqlite3.Connection, doc_id: int) -> dict:
    rows = conn.execute(
        "SELECT * FROM document_suggestions WHERE document_id = ? AND status = 'open' ORDER BY score DESC, id",
        (doc_id,)).fetchall()
    labels = {}
    out = {"folders": [], "tags": []}
    for r in rows:
        if r["kind"] == "folder":
            if r["folder_id"] not in labels:
                labels[r["folder_id"]] = folder_label(conn, r["folder_id"])
            rec = is_recommended(r["score"])
            out["folders"].append({"id": r["id"], "folder_id": r["folder_id"], "path": labels[r["folder_id"]],
                                   "score": round(r["score"] - (RECOMMEND_BONUS if rec else 0), 3),
                                   "recommended": rec, "source": r["source"]})
        else:
            out["tags"].append({"id": r["id"], "name": r["tag_name"], "score": round(r["score"], 3),
                                "source": r["source"]})
    return out


def folder_label(conn: sqlite3.Connection, folder_id: int) -> str:
    names = [c.name for c in fo.path(conn, folder_id)][1:]
    return " / ".join(names) if names else "マイファイル"


def clear_open(conn: sqlite3.Connection, doc_id: int, kind: str | None = None) -> None:
    """文書を移動したり、タグを手で変えたりしたら、open の候補を消す（古い候補を残さない）。"""
    if kind:
        conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open' AND kind = ?",
                     (doc_id, kind))
    else:
        conn.execute("DELETE FROM document_suggestions WHERE document_id = ? AND status = 'open'", (doc_id,))


def _get(conn: sqlite3.Connection, doc_id: int, sid: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM document_suggestions WHERE id = ? AND document_id = ?", (sid, doc_id)).fetchone()
    if r is None or r["status"] != "open":
        raise NotFound("候補が見つかりません（既に決めたか、文書が変わりました）")
    return r


def accept(conn: sqlite3.Connection, user: User, doc_id: int, sid: int, *, status: str = "accepted") -> None:
    """候補を採用する（移動 / タグを付ける）。権限の確認は update_document が行う。"""
    from . import documents as dm
    r = _get(conn, doc_id, sid)
    dm.get_document(conn, user, doc_id, "editor")
    with Tx(conn):
        if r["kind"] == "folder":
            dm.update_document(conn, user, doc_id, folder_id=r["folder_id"], clear_suggestions=False)
            clear_open(conn, doc_id, "folder")
        else:
            names = dm.document_tags(conn, doc_id)
            if r["tag_name"].lower() not in {n.lower() for n in names}:
                dm.update_document(conn, user, doc_id, tags=[*names, r["tag_name"]], clear_suggestions=False)
        conn.execute("UPDATE document_suggestions SET status = ?, decided_at = ? WHERE id = ?", (status, now(), sid))


def reject(conn: sqlite3.Connection, user: User, doc_id: int, sid: int) -> None:
    from . import documents as dm
    dm.get_document(conn, user, doc_id, "editor")
    _get(conn, doc_id, sid)
    with Tx(conn):
        conn.execute("UPDATE document_suggestions SET status = 'rejected', decided_at = ? WHERE id = ?", (now(), sid))


# ---------------------------------------------------------------------------
# worker: suggest_document ジョブ
# ---------------------------------------------------------------------------

def needs_job(conn: sqlite3.Connection, doc: sqlite3.Row) -> bool:
    """受け取り箱・共有リンクからの受け取り・ルート直下の文書（利用者がフォルダを選んでいないもの）。"""
    if doc["uploaded_via_share_id"] is not None:
        return True
    f = conn.execute("SELECT parent_id, owner_id FROM folders WHERE id = ?", (doc["folder_id"],)).fetchone()
    if f is None:
        return False
    return f["parent_id"] is None or doc["folder_id"] in inbox_folder_ids(conn, f["owner_id"])


def enqueue_for_blob(conn: sqlite3.Connection, blob_id: int) -> int:
    n = 0
    with Tx(conn):
        docs = conn.execute("SELECT * FROM documents WHERE blob_id = ? AND deleted_at IS NULL", (blob_id,)).fetchall()
        for d in docs:
            if needs_job(conn, d):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO jobs(type, blob_id, payload, priority, status, run_after, created_at)"
                    " VALUES ('suggest_document', NULL, ?, 15, 'pending', ?, ?)",
                    (json.dumps({"document_id": d["id"]}), now(), now()))
                n += cur.rowcount
    return n


def run_job(conn: sqlite3.Connection, doc_id: int, embedder) -> str:
    """候補を計算して保存し、設定に従って自動で適用する。"""
    d = conn.execute("SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (doc_id,)).fetchone()
    if d is None:
        return "gone"
    owner_id = conn.execute("SELECT owner_id FROM folders WHERE id = ?", (d["folder_id"],)).fetchone()[0]
    prefs = get_prefs(conn, owner_id)
    if not prefs["suggest_folders"] and not prefs["suggest_tags"]:
        return "disabled"
    res = compute(conn, d, embedder)
    store(conn, doc_id, res, folders=prefs["suggest_folders"], tags=prefs["suggest_tags"])
    owner = get_user(conn, owner_id)
    applied = []
    # 自動での移動は Inbox と共有リンクからの受け取りの文書だけ（ルート直下は利用者が置いたもの）
    auto_ok = d["uploaded_via_share_id"] is not None or d["folder_id"] in inbox_folder_ids(conn, owner_id)
    sugg = open_suggestions(conn, doc_id)
    if prefs["auto_move"] and auto_ok and res.confidence >= AUTO_MOVE_CONF:
        rec = next((f for f in sugg["folders"] if f["recommended"]), None)
        if rec is not None:
            src = d["folder_id"]
            accept(conn, owner, doc_id, rec["id"], status="auto_applied")
            with Tx(conn):
                audit(conn, "auto_move", user_id=owner_id, target_type="document", target_id=doc_id,
                      from_folder=src, to_folder=rec["folder_id"], confidence=round(res.confidence, 3))
            applied.append("move")
    if prefs["auto_tag"]:
        for t in sugg["tags"]:
            if t["source"] == "embedding" and t["score"] >= AUTO_TAG_MIN:
                accept(conn, owner, doc_id, t["id"], status="auto_applied")
                applied.append("tag")
    return "suggested" + (f" (auto: {', '.join(applied)})" if applied else "")


def auto_moved(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    """自動で移動した文書（お知らせと「元に戻す」用）。"""
    return conn.execute(
        "SELECT a.id, a.at, a.target_id AS document_id, json_extract(a.detail, '$.from_folder') AS from_folder,"
        " json_extract(a.detail, '$.to_folder') AS to_folder, d.filename, d.folder_id"
        " FROM audit_log a JOIN documents d ON d.id = a.target_id"
        " WHERE a.action = 'auto_move' AND a.user_id = ? AND d.deleted_at IS NULL ORDER BY a.id DESC LIMIT ?",
        (user_id, limit)).fetchall()


# ---------------------------------------------------------------------------
# 画面・API から（文書詳細を開いたときは、その場で計算する）
# ---------------------------------------------------------------------------

def for_document(conn: sqlite3.Connection, doc: sqlite3.Row, *, embedder=None, enabled: bool = True,
                 llm_enabled: bool = False) -> dict:
    """文書の候補と要約。まだ一度も計算していなければ、その場で計算して保存する（100ms 程度）。"""
    from .llm import get_summary
    owner_id = conn.execute("SELECT owner_id FROM folders WHERE id = ?", (doc["folder_id"],)).fetchone()[0]
    prefs = get_prefs(conn, owner_id)
    out = {"folders": [], "tags": [], "summary": None, "prefs": prefs}
    if enabled and (prefs["suggest_folders"] or prefs["suggest_tags"]):
        done = conn.execute("SELECT 1 FROM document_suggestions WHERE document_id = ? AND source = 'embedding'"
                            " LIMIT 1", (doc["id"],)).fetchone()
        ready = conn.execute("SELECT status FROM blobs WHERE id = ?", (doc["blob_id"],)).fetchone()[0] == "ready"
        if done is None and ready:
            store(conn, doc["id"], compute(conn, doc, embedder), folders=prefs["suggest_folders"],
                  tags=prefs["suggest_tags"])
        s = open_suggestions(conn, doc["id"])
        out["folders"] = s["folders"] if prefs["suggest_folders"] else []
        out["tags"] = s["tags"] if prefs["suggest_tags"] else []
    if llm_enabled and prefs["show_summary"]:
        out["summary"] = get_summary(conn, doc["blob_id"])
    return out


def recommended_for_folder(conn: sqlite3.Connection, folder_id: int) -> dict[int, dict]:
    """一覧の各文書のおすすめのフォルダ（1 つ）。{文書 ID: 候補}。"""
    out: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT s.id, s.document_id, s.folder_id, s.score FROM document_suggestions s"
        " JOIN documents d ON d.id = s.document_id WHERE d.folder_id = ? AND d.deleted_at IS NULL"
        " AND s.kind = 'folder' AND s.status = 'open' AND s.score >= ? ORDER BY s.score DESC",
        (folder_id, RECOMMEND_BONUS)).fetchall()
    for r in rows:
        if r["document_id"] not in out:
            out[r["document_id"]] = {"id": r["id"], "folder_id": r["folder_id"],
                                     "path": folder_label(conn, r["folder_id"])}
    return out


def undo_auto_move(conn: sqlite3.Connection, user: User, audit_id: int) -> int:
    """自動での移動を元に戻す。戻した文書の ID を返す。"""
    from . import documents as dm
    a = conn.execute("SELECT * FROM audit_log WHERE id = ? AND action = 'auto_move'", (audit_id,)).fetchone()
    if a is None:
        raise NotFound("見つかりません")
    detail = json.loads(a["detail"] or "{}")
    dm.update_document(conn, user, a["target_id"], folder_id=int(detail["from_folder"]))
    with Tx(conn):
        conn.execute("UPDATE audit_log SET action = 'auto_move_undone' WHERE id = ?", (audit_id,))
        conn.execute("UPDATE document_suggestions SET status = 'rejected', decided_at = ? WHERE document_id = ?"
                     " AND kind = 'folder' AND status = 'auto_applied' AND folder_id = ?",
                     (now(), a["target_id"], detail["to_folder"]))
    return int(a["target_id"])


# ---------------------------------------------------------------------------
# しきい値の評価（docseed eval-suggest）
# ---------------------------------------------------------------------------

def evaluate(conn: sqlite3.Connection, owner_id: int, *, sim_mins: list[float], conf_mins: list[float],
             limit: int = 300) -> list[dict]:
    """利用者が既に分類した文書を 1 つずつ「未分類」とみなして候補を出し、正しいフォルダが
    1 位に来る割合・上位 3 に入る割合、「おすすめ」の数と正しさを、しきい値の組ごとに数える。"""
    allowed = candidate_folders(conn, owner_id)
    root = conn.execute("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS NULL", (owner_id,)).fetchone()[0]
    docs = [d for d in conn.execute(
        "SELECT d.* FROM documents d JOIN blobs b ON b.id = d.blob_id WHERE d.deleted_at IS NULL"
        " AND b.status = 'ready' ORDER BY d.id DESC").fetchall()
        if d["folder_id"] in allowed and d["folder_id"] != root][:limit]
    out = []
    for sm in sim_mins:
        for cm in conf_mins:
            n = top1 = top3 = rec = rec_ok = 0
            for d in docs:
                r = compute(conn, d, None, sim_min=sm, conf_min=cm, skip_current=False)
                if not r.similar:
                    continue
                n += 1
                ids = [c.folder_id for c in r.folders]
                top1 += bool(ids) and ids[0] == d["folder_id"]
                top3 += d["folder_id"] in ids
                if r.folders and r.folders[0].recommended:
                    rec += 1
                    rec_ok += ids[0] == d["folder_id"]
            out.append({"sim_min": sm, "conf_min": cm, "documents": n,
                        "top1": round(top1 / n, 3) if n else None, "top3": round(top3 / n, 3) if n else None,
                        "recommended": rec, "recommended_precision": round(rec_ok / rec, 3) if rec else None})
    return out
