"""ファイル実体（blob）の保存。内容アドレス files/ab/cd/<sha256>。docs/design/pipeline.md 1 章。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import filetype

from .errors import FileTooLarge, InsufficientStorage

CHUNK = 1024 * 1024
MIN_FREE_RATIO = 0.05  # 空きが 5% を切ったらアップロードを拒否（deployment.md 6 章）

# 拡張子からの補完（filetype は中身の先頭バイトで判定し、テキスト系は判定できない）
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/csv", ".csv")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/heic", ".heic")
_TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml", ".ini", ".toml"}


@dataclass
class StoredFile:
    sha256: str
    size: int
    mime_type: str
    rel_path: str      # data_dir/files からの相対パス
    tmp_path: Path     # まだ確定していない一時ファイル（commit / discard する）


def blob_rel_path(sha: str) -> str:
    return f"{sha[:2]}/{sha[2:4]}/{sha}"


def detect_mime(head: bytes, filename: str) -> str:
    kind = filetype.guess(head)
    if kind is not None:
        return kind.mime
    ext = Path(filename).suffix.lower()
    guessed, _ = mimetypes.guess_type(filename)
    if ext in _TEXT_EXTS or (guessed and guessed.startswith("text/")):
        return guessed or "text/plain"
    if _looks_like_text(head):
        return guessed or "text/plain"
    return guessed or "application/octet-stream"


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 4  # 末尾でマルチバイト文字が切れただけ


class BlobStore:
    def __init__(self, files_dir: Path, tmp_dir: Path):
        self.files_dir = files_dir
        self.tmp_dir = tmp_dir

    def path(self, rel_path: str) -> Path:
        return self.files_dir / rel_path

    def check_free_space(self) -> None:
        st = shutil.disk_usage(self.files_dir)
        if st.free / st.total < MIN_FREE_RATIO:
            raise InsufficientStorage("保存先の空き容量が 5% を下回っているため、アップロードできません")

    def receive(self, src: BinaryIO, filename: str, max_bytes: int) -> StoredFile:
        """ストリームを一時ファイルへ書き込みながら SHA-256 とサイズを求める。"""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"up-{secrets.token_hex(8)}"
        h = hashlib.sha256()
        size = 0
        head = b""
        try:
            with tmp.open("wb") as out:
                while True:
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    size += len(buf)
                    if size > max_bytes:
                        raise FileTooLarge(f"1 ファイルの上限（{max_bytes // 1024 // 1024} MB）を超えています")
                    if len(head) < 8192:
                        head += buf[: 8192 - len(head)]
                    h.update(buf)
                    out.write(buf)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        sha = h.hexdigest()
        return StoredFile(sha, size, detect_mime(head, filename), blob_rel_path(sha), tmp)

    def commit(self, f: StoredFile) -> None:
        """一時ファイルを本来の場所へ移す（同じファイルシステム内の rename なので途中で壊れない）。"""
        dest = self.path(f.rel_path)
        if dest.exists():
            f.tmp_path.unlink(missing_ok=True)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(f.tmp_path, dest)

    def discard(self, f: StoredFile) -> None:
        f.tmp_path.unlink(missing_ok=True)

    def delete(self, rel_path: str) -> None:
        self.path(rel_path).unlink(missing_ok=True)
