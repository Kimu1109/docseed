"""OCR 計測用の合成サンプル画像と正解テキストを作る。

実データの代わりではなく、条件をそろえた比較（横書き・縦書き・小さい文字・
ノイズ・傾き）のための補助。実際の判断は手元の実文書（samples/real/）で行うこと。

出力: <out>/<名前>.png と <out>/<名前>.gt.txt（正解テキスト）

必要なフォント（Raspberry Pi OS）:
    sudo apt install fonts-noto-cjk
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]

# 業務文書らしい文（数字・記号・カタカナ・英字を混ぜる）
SENTENCES = [
    "請求書番号 INV-2026-0142 の金額は 128,500 円（税込）です。",
    "お支払期限は 2026 年 12 月 25 日までとなっております。",
    "振込先：みずほ銀行 渋谷支店 普通 1234567 カ）サンプルショウジ",
    "本契約の有効期間は締結日から一年間とし、自動更新するものとする。",
    "会議の議事録を共有フォルダに保存しましたのでご確認ください。",
    "Raspberry Pi 5 で OCR とベクトル検索を動かす検証を行います。",
    "第三条（秘密保持）甲及び乙は、相手方の秘密情報を第三者に開示しない。",
    "見積有効期限：発行日より 30 日間　担当：営業部 山田",
    "ご不明な点がございましたら、下記までお問い合わせください。",
    "電話 03-1234-5678 / メール info@example.co.jp",
    "健康診断の結果、特に異常は認められませんでした。",
    "領収書の再発行には、本人確認書類の提示が必要です。",
    "取扱説明書をよくお読みのうえ、正しくお使いください。",
    "マイナンバーカードの更新手続きについてのお知らせ",
    "東京都千代田区丸の内一丁目 9 番 2 号 グラントウキョウ 10F",
]


def find_font(path: str | None) -> str:
    if path:
        return path
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit(
        "日本語フォントが見つかりません。`sudo apt install fonts-noto-cjk` を実行するか、"
        "--font でフォントファイルを指定してください。"
    )


def horizontal(font_path: str, size: int, lines: list[str], width: int = 1654) -> Image.Image:
    font = ImageFont.truetype(font_path, size)
    line_h = int(size * 1.7)
    margin = int(width * 0.08)
    img = Image.new("RGB", (width, margin * 2 + line_h * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((margin, margin + i * line_h), line, font=font, fill="black")
    return img


def vertical(font_path: str, size: int, lines: list[str]) -> Image.Image:
    """縦書き（右の列から左へ、各列は上から下）。"""
    font = ImageFont.truetype(font_path, size)
    col_w = int(size * 1.7)
    margin = size * 2
    max_len = max(len(l) for l in lines)
    w = margin * 2 + col_w * len(lines)
    h = margin * 2 + int(size * 1.1) * max_len
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for c, line in enumerate(lines):
        x = w - margin - (c + 1) * col_w + (col_w - size) // 2
        for r, ch in enumerate(line):
            d.text((x, margin + r * int(size * 1.1)), ch, font=font, fill="black")
    return img


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """スマホ撮影・スキャンに近づける（わずかな傾き・ぼかし・ノイズ・JPEG 劣化）。"""
    img = img.rotate(rng.uniform(-2.0, 2.0), expand=True, fillcolor="white")
    img = img.filter(ImageFilter.GaussianBlur(radius=0.8))
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 200):
        x, y = rng.randrange(w), rng.randrange(h)
        v = rng.randrange(120, 255)
        px[x, y] = (v, v, v)
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, "JPEG", quality=55)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "samples" / "synthetic")
    ap.add_argument("--font", help="日本語フォントのパス（省略時は自動検出）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    font = find_font(args.font)
    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    def pick(n: int) -> list[str]:
        return rng.sample(SENTENCES, n)

    vertical_lines = [s.replace(" ", "") for s in pick(6)]
    vertical_lines = [l[:24] for l in vertical_lines]

    cases: dict[str, tuple[Image.Image, list[str]]] = {}
    lines = pick(12)
    cases["h_clean_32px"] = (horizontal(font, 32, lines), lines)
    lines = pick(12)
    cases["h_small_20px"] = (horizontal(font, 20, lines), lines)
    lines = pick(12)
    cases["h_photo_32px"] = (degrade(horizontal(font, 32, lines), rng), lines)
    cases["v_clean_32px"] = (vertical(font, 32, vertical_lines), vertical_lines)

    for name, (img, gt) in cases.items():
        img.save(args.out / f"{name}.png")
        (args.out / f"{name}.gt.txt").write_text("\n".join(gt) + "\n", encoding="utf-8")
        print(f"{name}: {img.size[0]}x{img.size[1]}")
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
