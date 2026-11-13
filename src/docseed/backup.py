"""DB のスナップショット・復元と、原本ファイルの照合。docs/design/deployment.md 5 章。

原本（files/）と DB スナップショットを別の機器へ送るのはホストの restic（scripts/ops/backup.sh）が行う。
ここでは、restic に渡す前の「壊れていない DB の写し」を作ることと、復元後の確認を受け持つ。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_GLOB = "docseed-*.sqlite"
RESTIC_STATUS = "restic-status.json"  # ホストの backup.sh が書く（最後の restic の結果）
DRILL_STATUS = "restore-drill-status.json"  # ホストの restore-drill.sh が書く（最後の復旧の練習の結果）


class BackupError(Exception):
    pass


def _open_ro(path: Path) -> sqlite3.Connection:
    """読み取り専用で開く（ベクトル表の確認のため sqlite-vec も読み込む）。"""
    import sqlite_vec
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return c


def _quick_check(path: Path) -> None:
    c = _open_ro(path)
    try:
        res = c.execute("PRAGMA quick_check").fetchone()[0]
        c.execute("SELECT count(*) FROM schema_migrations").fetchone()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"{path.name} は docseed のデータベースとして読めません: {e}") from e
    finally:
        c.close()
    if res != "ok":
        raise BackupError(f"{path.name} の整合性の確認に失敗しました: {res}")


def snapshot_db(conn: sqlite3.Connection, out_dir: Path, *, keep: int = 7, when: dt.datetime | None = None) -> Path:
    """SQLite のオンラインバックアップ API で写しを作り、確認してから置き換える（途中の写しを restic に渡さない）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    when = when or dt.datetime.now()
    dest = out_dir / f"docseed-{when.strftime('%Y%m%d')}.sqlite"
    tmp = out_dir / f".{dest.name}.tmp"
    tmp.unlink(missing_ok=True)
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # 写しは 1 ファイルで完結させる（-wal を作らない）
    finally:
        target.close()
    try:
        _quick_check(tmp)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
    if keep:
        for old in sorted(out_dir.glob(SNAPSHOT_GLOB))[:-keep]:
            old.unlink()
    return dest


def latest_snapshot(out_dir: Path) -> Path | None:
    snaps = sorted(out_dir.glob(SNAPSHOT_GLOB))
    return snaps[-1] if snaps else None


def restore_db(src: Path, db_path: Path, *, force: bool = False) -> Path | None:
    """スナップショットを DB の場所に戻す。api と worker を止めてから実行する。

    既存の DB は `<名前>.before-restore-<日時>` に退避し、その名前を返す（無ければ None）。
    """
    if not src.is_file():
        raise BackupError(f"{src} がありません")
    _quick_check(src)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    moved: Path | None = None
    if db_path.exists():
        if not force:
            hb = _heartbeat_age(db_path)
            if hb is not None and hb < 300:
                raise BackupError("worker が 5 分以内に動いた記録があります。docker compose stop api worker で"
                                  "止めてから実行してください（止めてある場合は --force）")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        moved = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
        os.replace(db_path, moved)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            if moved is not None:
                os.replace(side, moved.with_name(moved.name + suffix))
            else:
                side.unlink()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(db_path)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    return moved


def _heartbeat_age(db_path: Path) -> float | None:
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT value FROM settings WHERE key = 'worker.heartbeat'").fetchone()
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    try:
        t = dt.datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return (dt.datetime.now(dt.UTC) - t).total_seconds()


# ---------------------------------------------------------------------------
# 原本ファイルの照合
# ---------------------------------------------------------------------------

@dataclass
class VerifyReport:
    checked: int = 0
    missing: list[str] = field(default_factory=list)      # DB にあるがファイルが無い（sha256）
    mismatched: list[str] = field(default_factory=list)   # 大きさか SHA-256 が合わない（sha256）
    orphans: list[str] = field(default_factory=list)      # ファイルはあるが DB に無い（files/ からの相対パス）

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "missing": self.missing, "mismatched": self.mismatched,
                "orphans": self.orphans}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while buf := fh.read(1024 * 1024):
            h.update(buf)
    return h.hexdigest()


def verify_files(conn: sqlite3.Connection, files_dir: Path, *, full: bool = True,
                 progress: Callable[[int, int], None] | None = None) -> VerifyReport:
    """DB の blobs と files/ の原本が対応しているかを確かめる。full=False なら大きさだけを比べる（速い）。"""
    rep = VerifyReport()
    rows = conn.execute("SELECT sha256, size_bytes, storage_path FROM blobs ORDER BY id").fetchall()
    known: set[str] = set()
    for i, r in enumerate(rows, 1):
        known.add(r["storage_path"])
        p = files_dir / r["storage_path"]
        rep.checked += 1
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            rep.missing.append(r["sha256"])
            continue
        if size != r["size_bytes"] or (full and _sha256(p) != r["sha256"]):
            rep.mismatched.append(r["sha256"])
        if progress:
            progress(i, len(rows))
    if files_dir.is_dir():
        for p in files_dir.rglob("*"):
            rel = p.relative_to(files_dir)
            if p.is_file() and rel.parts[0] != ".tmp" and rel.as_posix() not in known:
                rep.orphans.append(rel.as_posix())
    rep.orphans.sort()
    return rep


# ---------------------------------------------------------------------------
# 状態（管理画面・監視用）
# ---------------------------------------------------------------------------

def restic_status(backup_dir: Path) -> dict | None:
    """ホストの backup.sh が最後に書いた restic の結果（{"time", "ok", "snapshot", "message"}）。"""
    try:
        data = json.loads((backup_dir / RESTIC_STATUS).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def restore_drill_status(backup_dir: Path) -> dict | None:
    """ホストの restore-drill.sh が最後に書いた復旧の練習の結果。

    {"time", "ok", "result": ok / failed / skipped, "last_success", "snapshot", "snapshot_time", "restore_size",
     "duration_s", "auto", "message", "host"}
    """
    try:
        data = json.loads((backup_dir / DRILL_STATUS).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def free_ratio(path: Path) -> float:
    st = shutil.disk_usage(path)
    return st.free / st.total if st.total else 1.0
