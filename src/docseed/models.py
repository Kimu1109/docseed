"""モデルの取得（docseed fetch-models）。docs/design/deployment.md 4 章。

- OCR: RapidOCR が配布する PP-OCRv6（ONNX）を models/rapidocr に取得する
- 埋め込み: Hugging Face から ruri-v3 を取得し、ONNX に変換して models/embed/<名前> に置く
  （公式の配布物に ONNX が無いため自分で変換する。変換にだけ torch が必要なので、
   この処理は tools イメージ（pip install ".[tools]"）で 1 度だけ実行する）
- 取得したファイルの SHA-256 を models/manifest.json に記録する
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

from .config import Settings
from .pipeline.embed import model_dir


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def fetch_ocr(settings: Settings, model_type: str = "small") -> list[Path]:
    from .pipeline.ocr import RapidOcrEngine
    eng = RapidOcrEngine(settings.models_dir, model_type=model_type, allow_download=True)
    eng.load_all()
    files = sorted((settings.models_dir / "rapidocr").rglob("*.onnx"))
    names = " ".join(p.name for p in files)
    for part in ("det", "rec", "cls"):
        if f"_{part}" not in names:
            raise SystemExit(f"OCR モデル（{part}）を取得できませんでした: {settings.models_dir / 'rapidocr'}")
    return files


def fetch_embedding(settings: Settings, *, force: bool = False) -> list[Path]:
    dest = model_dir(settings.models_dir, settings.embed_model)
    onnx_path = dest / "onnx" / "model.onnx"
    if onnx_path.exists() and not force:
        print(f"  埋め込みモデルは取得済みです: {dest}")
        return sorted(dest.rglob("*"))
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise SystemExit(
            "埋め込みモデルの ONNX 変換には torch と sentence-transformers が必要です。\n"
            "  Docker: docker compose --profile tools run --rm tools\n"
            "  手元:   pip install -e \".[tools]\" してから docseed fetch-models") from e
    cache = settings.models_dir / ".hf-cache"
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  {settings.embed_model} を取得して ONNX に変換しています（数分かかります）")
    m = SentenceTransformer(settings.embed_model, device="cpu", backend="onnx", cache_folder=str(cache))
    m.save_pretrained(str(tmp))
    if not (tmp / "onnx" / "model.onnx").exists() or not (tmp / "tokenizer.json").exists():
        raise SystemExit(f"変換結果が不完全です: {tmp}")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    shutil.rmtree(cache, ignore_errors=True)
    return sorted(dest.rglob("*"))


def write_manifest(settings: Settings) -> Path:
    root = settings.models_dir
    files = {str(p.relative_to(root)): {"sha256": sha256_file(p), "bytes": p.stat().st_size}
             for p in sorted(root.rglob("*")) if p.is_file() and p.name != "manifest.json"
             and ".hf-cache" not in p.parts}
    out = root / "manifest.json"
    out.write_text(json.dumps({"created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                               "embed_model": settings.embed_model, "files": files},
                              ensure_ascii=False, indent=2))
    return out


def fetch_all(settings: Settings, *, force: bool = False) -> None:
    settings.ensure_dirs()
    print("OCR モデル（PP-OCRv6）を取得しています")
    model_type = settings.ocr_engine.removeprefix("ppocrv6_")
    for p in fetch_ocr(settings, model_type):
        print(f"  {p.relative_to(settings.models_dir)}")
    print("埋め込みモデルを準備しています")
    fetch_embedding(settings, force=force)
    m = write_manifest(settings)
    print(f"完了しました → {m}")
