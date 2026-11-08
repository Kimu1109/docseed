"""受け取り箱（inbox）からの取り込み。docs/design/pipeline.md 7 章。

/srv/docseed/inbox/<ユーザー名>/ に置かれたファイルを、そのユーザーの「Inbox」フォルダへ取り込む。
Samba などで LAN に公開して使う。サブフォルダの構造は保つ。

- 書き込み中のファイルを避けるため、サイズと更新時刻が STABLE_SECONDS 以上変わっていないものだけを取り込む
- 取り込んだ元ファイルは削除する（keep=True なら inbox/<ユーザー名>/.imported/ へ移す）
- 取り込めなかったファイルは inbox/<ユーザー名>/.failed/ へ移し、理由を <名前>.error.txt に書く
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import documents as dm
from . import folders as fo
from .errors import AppError
from .storage import BlobStore
from .users import User, root_folder_id

log = logging.getLogger("docseed.inbox")

STABLE_SECONDS = 10
INBOX_FOLDER = "Inbox"
SKIP_PREFIXES = (".", "~$")
SKIP_NAMES = {"Thumbs.db", "desktop.ini", ".DS_Store"}


class InboxScanner:
    def __init__(self, inbox_dir: Path, store: BlobStore, *, max_bytes: int, keep: bool = False):
        self.dir = inbox_dir
        self.store = store
        self.max_bytes = max_bytes
        self.keep = keep
        self._seen: dict[Path, tuple[int, float]] = {}

    def ensure_user_dirs(self, conn: sqlite3.Connection) -> None:
        """有効なユーザーごとに inbox/<ユーザー名>/ を用意する（LAN から見えるように）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        for r in conn.execute("SELECT username FROM users WHERE is_active = 1"):
            (self.dir / r[0]).mkdir(exist_ok=True)

    def _stable(self, p: Path) -> bool:
        st = p.stat()
        sig = (st.st_size, st.st_mtime)
        prev = self._seen.get(p)
        self._seen[p] = sig
        return prev == sig and time.time() - st.st_mtime >= STABLE_SECONDS

    def scan(self, conn: sqlite3.Connection) -> int:
        """1 回走査して取り込んだ件数を返す。"""
        self.ensure_user_dirs(conn)
        imported = 0
        alive: set[Path] = set()
        for user_dir in sorted(p for p in self.dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            row = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (user_dir.name,)).fetchone()
            if row is None:
                continue
            user = User.from_row(row)
            for path in sorted(user_dir.rglob("*")):
                rel = path.relative_to(user_dir)
                if any(part.startswith(SKIP_PREFIXES) or part in SKIP_NAMES for part in rel.parts):
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
                alive.add(path)
                if not self._stable(path):
                    continue
                if self._import(conn, user, user_dir, path):
                    imported += 1
            _remove_empty_dirs(user_dir)
        self._seen = {p: v for p, v in self._seen.items() if p in alive}
        return imported

    def _import(self, conn: sqlite3.Connection, user: User, user_dir: Path, path: Path) -> bool:
        rel = path.relative_to(user_dir)
        try:
            root = root_folder_id(conn, user.id)
            target = fo.ensure_path(conn, user, root, [INBOX_FOLDER, *rel.parts[:-1]])
            with path.open("rb") as f:
                r = dm.upload(conn, self.store, user, target, path.name, f, max_bytes=self.max_bytes,
                              on_conflict="rename")
        except (AppError, OSError) as e:
            msg = getattr(e, "detail", None) or str(e)
            log.warning("inbox: %s/%s を取り込めませんでした: %s", user.username, rel, msg)
            self._move(user_dir, path, ".failed", note=msg)
            return False
        log.info("inbox: %s/%s → %s/%s", user.username, rel, INBOX_FOLDER, r.filename)
        if self.keep:
            self._move(user_dir, path, ".imported")
        else:
            path.unlink(missing_ok=True)
        return True

    @staticmethod
    def _move(user_dir: Path, path: Path, sub: str, note: str | None = None) -> None:
        rel = path.relative_to(user_dir)
        dest = user_dir / sub / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
        shutil.move(str(path), dest)
        if note:
            dest.with_name(dest.name + ".error.txt").write_text(note + "\n", encoding="utf-8")


def _remove_empty_dirs(user_dir: Path) -> None:
    for d in sorted((p for p in user_dir.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        if d.name.startswith(".") or any(part.startswith(".") for part in d.relative_to(user_dir).parts):
            continue
        try:
            os.rmdir(d)  # 空のときだけ消える
        except OSError:
            pass
