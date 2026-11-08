"""埋め込み（ruri-v3-70m）。ONNX Runtime + tokenizers で推論し、torch は使わない。

sentence-transformers の出力（Transformer → 平均プーリング → L2 正規化）と同じ結果になることを確認済み
（コサイン類似度 1.000）。docs/design/pipeline.md 6 章。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import numpy as np

DOC_PREFIX = "検索文書: "
QUERY_PREFIX = "検索クエリ: "
MAX_TOKENS = 1024
BATCH = 16


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def model_dir(models_dir: Path, model_name: str) -> Path:
    return models_dir / "embed" / model_name.replace("/", "__")


class OnnxEmbedder:
    def __init__(self, models_dir: Path, model_name: str, *, threads: int = 3):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = model_dir(models_dir, model_name)
        onnx_path = d / "onnx" / "model.onnx"
        if not onnx_path.exists():
            raise RuntimeError(f"埋め込みモデルがありません: {onnx_path}（`docseed fetch-models` を実行してください）")
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self._tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        pad_id = self._tok.token_to_id("<pad>")
        self._tok.enable_padding(pad_id=pad_id if pad_id is not None else 0)
        self._tok.enable_truncation(MAX_TOKENS)
        self.model_name = model_name
        pooling = json.loads((d / "1_Pooling" / "config.json").read_text())
        if pooling.get("pooling_mode", "mean") != "mean":
            raise RuntimeError(f"未対応のプーリング: {pooling}")
        self.dim = int(pooling["embedding_dimension"])

    def _run(self, texts: list[str]) -> np.ndarray:
        enc = self._tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        hidden = self._sess.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        m = mask[..., None].astype(np.float32)
        v = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        return (v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)).astype(np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """長さ順に並べてバッチ処理する（パディングの無駄を減らす）。"""
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for k in range(0, len(order), BATCH):
            idx = order[k:k + BATCH]
            out[idx] = self._run([DOC_PREFIX + texts[i] for i in idx])
        return out

    def embed_query(self, text: str) -> np.ndarray:
        return self._run([QUERY_PREFIX + text])[0]
