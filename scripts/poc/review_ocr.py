"""OCR の結果を目で確認するための HTML レポートを作る（Phase 0）。

ページ画像の上に認識した行の枠と読み順の番号を重ね、横に読み順どおりのテキストを並べる。
正解テキストとの数値比較（CER）は、段組みや表で正解の書き起こしと OCR の読み順が
違うと大きくぶれるため、文字の読み間違いと読み順の崩れをこの画面で分けて確認する。

使い方の例:
    python review_ocr.py samples/real
    python review_ocr.py samples/real/模試.jpg samples/real/冊子.pdf --max-pages 5
    python review_ocr.py samples/real --engine rapidocr-v5-mobile --out out/review-v5

出力: out/review/index.html（画像を埋め込んだ 1 ファイル。ブラウザで開く）
注意: 文書の内容がそのまま入るので、out/ は Git に入れない（.gitignore 済み）。

画面の見方:
    - 枠の色: 緑 = 信頼度 0.9 以上、黄 = 0.7〜0.9、赤 = 0.7 未満、灰 = 本文に採用しない（--min-score 未満）
    - 枠の番号: 本文の読み順。番号の流れが不自然なら読み順の問題、文字が違うなら認識の問題
    - テキストの行をクリック（またはマウスを重ねる）と、画像の該当する枠が強調される
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_ocr import (  # noqa: E402
    ENGINES, collect_inputs, load_page, make_engine, order_groups, read_gt,
)

PREVIEW_SIDE = 1600  # HTML に埋め込む画像の長辺（px）


def color(score: float, min_score: float) -> str:
    if score < min_score:
        return "#9aa0a6"
    if score >= 0.9:
        return "#1e8e3e"
    if score >= 0.7:
        return "#f9ab00"
    return "#d93025"


def page_html(idx: int, title: str, img: Image.Image, lines, groups, vertical: bool,
              secs: float, min_score: float, gt: str | None) -> str:
    # 画像を縮小して埋め込む（枠の座標も同じ比率で縮める）
    scale = min(1.0, PREVIEW_SIDE / max(img.size))
    prev = img.resize((round(img.size[0] * scale), round(img.size[1] * scale))) if scale < 1 else img
    buf = io.BytesIO()
    prev.save(buf, "JPEG", quality=80)
    data = base64.b64encode(buf.getvalue()).decode()
    w, h = prev.size

    pos = {id(l): k for k, l in enumerate(lines)}  # 行 → lines 内の位置（numpy を含むので id で引く）
    order: dict[int, int] = {}
    n = 0
    for g in groups:
        for l in g:
            n += 1
            order[id(l)] = n

    shapes, rows = [], []
    for k, l in enumerate(lines):
        pts = " ".join(f"{x * scale:.1f},{y * scale:.1f}" for x, y in l.box)
        c = color(l.score, min_score)
        num = order.get(id(l))
        x0, y0 = l.box[:, 0].min() * scale, l.box[:, 1].min() * scale
        label = (f'<text x="{x0:.1f}" y="{max(10, y0 - 2):.1f}" fill="{c}">{num}</text>' if num else "")
        shapes.append(f'<g class="b" data-k="p{idx}-{k}"><polygon points="{pts}" stroke="{c}"/>{label}</g>')

    for g in groups:
        for l in g:
            k = pos[id(l)]
            rows.append(
                f'<li data-k="p{idx}-{k}"><span class="n">{order[id(l)]}</span>'
                f'<span class="s" style="color:{color(l.score, min_score)}">{l.score:.2f}</span>'
                f'<span class="t">{html.escape(l.text)}</span></li>')
        rows.append('<li class="sep"></li>')
    dropped = [l for l in lines if l.score < min_score]
    if dropped:
        rows.append(f'<li class="sep">本文に採用しなかった行（信頼度 {min_score} 未満）</li>')
        for l in dropped:
            k = pos[id(l)]
            rows.append(f'<li data-k="p{idx}-{k}" class="drop"><span class="n">-</span>'
                        f'<span class="s">{l.score:.2f}</span><span class="t">{html.escape(l.text)}</span></li>')

    gt_block = (f'<details><summary>正解テキスト</summary><pre>{html.escape(gt)}</pre></details>'
                if gt else "")
    meta = (f"{img.size[0]}×{img.size[1]}px ・ {len(lines)} 行 ・ {secs:.1f} 秒 ・ "
            f"{'縦書き' if vertical else '横書き'}")
    return f'''
<section id="p{idx}">
  <h2>{html.escape(title)}</h2>
  <p class="meta">{meta}</p>
  <div class="cols">
    <div class="img"><svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">
      <image href="data:image/jpeg;base64,{data}" width="{w}" height="{h}"/>
      {"".join(shapes)}
    </svg></div>
    <div class="txt"><ol>{"".join(rows)}</ol>{gt_block}</div>
  </div>
</section>'''


CSS = """
:root { --bg:#fff; --fg:#202124; --muted:#5f6368; --line:#dadce0; --hl:#1a73e8; }
@media (prefers-color-scheme: dark) { :root { --bg:#1f1f1f; --fg:#e8eaed; --muted:#9aa0a6; --line:#3c4043; --hl:#8ab4f8; } }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font-family: system-ui, "Noto Sans CJK JP", "Hiragino Sans", sans-serif; }
h1 { font-size:20px; } h2 { font-size:16px; margin:32px 0 4px; border-top:1px solid var(--line); padding-top:16px; }
.meta, nav { color:var(--muted); font-size:13px; }
nav a { color:var(--hl); margin-right:12px; }
.cols { display:flex; gap:16px; align-items:flex-start; }
.img { flex:0 0 58%; position:sticky; top:8px; }
.img svg { width:100%; height:auto; border:1px solid var(--line); }
.txt { flex:1; min-width:0; font-size:14px; }
polygon { fill:transparent; stroke-width:2; }
.b text { font-size:14px; font-weight:bold; paint-order:stroke; stroke:#fff; stroke-width:3px; }
.b.on polygon { fill:rgba(26,115,232,.25); stroke:var(--hl); stroke-width:4; }
ol { list-style:none; padding:0; margin:0; }
li { display:flex; gap:8px; padding:2px 4px; border-radius:4px; cursor:pointer; }
li.on { background:rgba(26,115,232,.18); }
li.sep { height:6px; cursor:default; color:var(--muted); font-size:12px; height:auto; }
li.drop .t { color:var(--muted); text-decoration:line-through; }
.n { color:var(--muted); min-width:2.5em; text-align:right; font-variant-numeric:tabular-nums; }
.s { min-width:2.6em; font-variant-numeric:tabular-nums; }
.t { white-space:pre-wrap; word-break:break-all; }
pre { white-space:pre-wrap; background:rgba(127,127,127,.1); padding:8px; }
@media (max-width: 900px) { .cols { flex-direction:column; } .img { position:static; flex:none; width:100%; } }
"""

JS = """
document.querySelectorAll('[data-k]').forEach(el => {
  const on = v => document.querySelectorAll('[data-k="' + el.dataset.k + '"]').forEach(x => x.classList.toggle('on', v));
  el.addEventListener('mouseenter', () => on(true));
  el.addEventListener('mouseleave', () => on(false));
  el.addEventListener('click', () => {
    const t = document.querySelector('.b[data-k="' + el.dataset.k + '"]');
    if (t && el.tagName === 'LI') t.scrollIntoView({block: 'center', behavior: 'smooth'});
  });
});
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="画像・PDF・ディレクトリ")
    ap.add_argument("--engine", default="rapidocr-v6-small", help=f"OCR エンジン（{', '.join(ENGINES)}）")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--det-side", type=int, default=1600)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--max-pages", type=int, help="処理するページ数の上限")
    ap.add_argument("--models-dir", type=Path, help="モデルの保存先")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "out" / "review")
    args = ap.parse_args()

    pages = collect_inputs(args.inputs, args.max_pages)
    ocr = make_engine(args.engine, args.det_side, args.threads, args.models_dir, "jpn")
    sections, nav = [], []
    for i, p in enumerate(pages):
        img = load_page(p, args.dpi)
        t = time.perf_counter()
        lines = ocr(img)
        secs = time.perf_counter() - t
        groups, vertical = order_groups(lines, args.min_score)
        title = p.file.name + (f" p{p.page}" if p.is_pdf else "")
        gt = read_gt(p.gt_path) if p.gt_path.exists() else None
        sections.append(page_html(i, title, img, lines, groups, vertical, secs, args.min_score, gt))
        nav.append(f'<a href="#p{i}">{html.escape(title)}</a>')
        print(f"  {title}: {len(lines)} 行, {secs:.1f}s", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "index.html"
    out.write_text(f'''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCR 確認</title><style>{CSS}</style></head><body>
<h1>OCR 確認（{html.escape(args.engine)}, {args.dpi} dpi, 検出 {args.det_side}px）</h1>
<p class="meta">枠: 緑 ≥0.9 / 黄 0.7〜0.9 / 赤 &lt;0.7 / 灰 = 本文に不採用。番号 = 本文の読み順。
テキストの行にマウスを重ねると画像の枠が強調され、クリックでその位置へ移動します。</p>
<nav>{"".join(nav)}</nav>
{"".join(sections)}
<script>{JS}</script></body></html>''', encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
