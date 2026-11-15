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
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/avif", ".avif")
# ZIP を使う形式（中身の判定で application/zip になった場合は拡張子で補う）
_OOXML_EXTS = {".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".potx"}
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
    ext = Path(filename).suffix.lower()
    guessed, _ = mimetypes.guess_type(filename)
    if kind is not None:
        if kind.mime == "application/zip" and ext in _OOXML_EXTS and guessed:
            return guessed.lower()
        if kind.mime == "image/heif":
            return "image/heic"   # HEIF と HEIC は同じに扱う（extensions.md 4.1）
        return kind.mime
    if ext in _TEXT_EXTS or (guessed and guessed.startswith("text/")):
        return guessed or "text/plain"
    if _looks_like_text(head):
        return guessed or "text/plain"
    return (guessed or "application/octet-stream").lower()


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

    def adopt(self, path: Path, filename: str) -> StoredFile:
        """tmp_dir に受け取り済みのファイル（分割アップロード）を、コピーせずにそのまま使う。

        SHA-256 と種類を求めるために 1 度読むだけで、commit では rename する。"""
        h = hashlib.sha256()
        size = 0
        head = b""
        with path.open("rb") as f:
            while buf := f.read(CHUNK):
                size += len(buf)
                if len(head) < 8192:
                    head += buf[: 8192 - len(head)]
                h.update(buf)
        sha = h.hexdigest()
        return StoredFile(sha, size, detect_mime(head, filename), blob_rel_path(sha), path)

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

    def derived_path(self, rel_path: str) -> Path:
        """派生ファイル（blobs.thumb_path / view_path。data_dir からの相対パス）の場所。"""
        return self.files_dir.parent / rel_path

    def delete_derived(self, *rel_paths: str | None) -> None:
        for rel in rel_paths:
            if rel:
                self.derived_path(rel).unlink(missing_ok=True)
