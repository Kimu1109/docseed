# 新機能の実現手段の調査（URL 取り込み・小型 LLM・画像形式・アイコン・HTTPS・ログイン）

調査日: 2026-11-08
前提: Raspberry Pi 5（CPU のみ）、worker は外部と通信しない、画面の CSP は `default-src 'self'`、証明書は Caddy の内部 CA、外部からは Tailscale 経由を推奨（[設計書](../design.md) 2・8.3 章）。

> 注意: 速度の数値は公開されている記事から集めた目安で、条件がそろっていない。採用前に Phase 0 と同じように**実機で計測する**こと。

---

## 0. 結論

| # | 機能 | 推奨 |
|---|---|---|
| 1 | URL の取り込み | **trafilatura**（本文とメタデータ）＋ OGP・Twitter カードは自前で解析。取得は外部通信できる専用の処理で行い、SSRF 対策を必ず入れる |
| 2 | 小型 LLM | フォルダの振り分けは **既存の ruri の埋め込みで近い文書を探す方式**。要約とタグは **llama.cpp（`llama-server`）＋ 1〜2B の日本語に強いモデル**を、JSON スキーマで出力を縛って非同期に実行 |
| 3 | heic / avif / webp | **Pillow 11.3 以降**（AVIF は標準の wheel に入る）＋ HEIC は **pillow-heif**。HEIC は Safari しか表示できないので、プレビュー用に WebP / JPEG を作る |
| 4 | アイコン | **Material Symbols の SVG を自前で配信**（使うものだけを SVG スプライトにする）。Web フォントや CDN は使わない |
| 5 | HTTPS 証明書 | ① Tailscale の名前（`*.ts.net`）なら Caddy が自動で取得、② 自分のドメインなら Let's Encrypt の DNS-01、③ 今の `tls internal` は最後の手段として残す |
| 6 | 安全なログイン | **パスキー（py_webauthn）を先に入れる**。Google ログインは汎用の OIDC（Authlib）で、既存アカウントとの紐付け型・任意機能にする |

**順番:** パスキーは IP アドレスでは使えず、ドメイン名と信頼された証明書が必要なので **5 → 6** の順。1 → 2 の順にすると、URL から取り出した本文を要約・タグ付けにそのまま使える。

---

## 1. URL から OGP・Twitter カード・本文を取り出して検索できるようにする

**推奨: httpx で取得 → trafilatura で本文とメタデータ → `og:*` と `twitter:*` は自前で解析 → 「Web クリップ」という種類の文書として保存し、既存の取り込み処理（チャンク分割と埋め込み）に流す**

- **trafilatura** は日本語のページでも本文の抜き出し精度が高く、タイトル・著者・日付・サイト名・タグも取れる。v1.8.0 から **Apache-2.0**（それより前は GPLv3）で、docseed と合う。
- OGP と Twitter カードは `<meta property="og:*">` / `<meta name="twitter:*">` を読むだけなので、lxml（trafilatura が依存）で足りる。extruct などの追加の依存は不要。
- **og:image は Pi に保存して配信する。** CSP が `img-src 'self'` で、外部の画像を直接表示するのはプライバシーの面でも好ましくない。保存した画像は 3 章の処理でサムネイルにする。
- **どこで取得するか:** worker は `internal` ネットワーク（外部と通信しない）なので取得できない。外部へ出られる小さな `fetcher` ジョブ / サービスを分け、HTML を保存してから worker に渡すと、今の安全の設計を崩さない。
- **SSRF 対策（必須）:** LAN の機器（ルーターの管理画面など）を突かれないように、
  - 名前を引いた結果がプライベート・ループバック・リンクローカルの IP なら拒否し、引いた IP に固定して接続する
  - リダイレクトのたびに同じ確認をする
  - サイズの上限（5MB など）、タイムアウト、`http` / `https` 以外の拒否
- **JavaScript で描画するサイト**（X など）はメタデータや本文が取れないことがある。Playwright は Pi 5 には重いので初版では入れず、取れたメタデータだけを保存して「本文が取れなかった」と表示する。
- 埋め込む文書にはタイトル・説明（`og:description`）・本文を入れる（ファイル名を入れている今の「検索文書: 」の書き方と同じ）。

## 2. 超小型 LLM による要約・自動フォルダ分け・自動タグ付け

**推奨: 処理の種類で手段を分ける**

| 処理 | 手段 | 理由 |
|---|---|---|
| フォルダの振り分け候補 | **ruri-v3 の埋め込み**で既存のフォルダの文書と比べる（近い文書の多数決、またはフォルダの平均ベクトル） | 埋め込みは計算済みで追加の負荷がほぼない。利用者自身の分類に合う |
| タグ付け | 既存タグから選ぶのは埋め込みで同じ方式。新しいタグの提案だけ LLM | 表記ゆれ（「経理」と「会計」など）を防げる |
| 要約 | LLM | 文章の生成が必要 |

**実行方法:** llama.cpp の `llama-server` を別のコンテナで、外部と通信しないネットワークに置く。生成は ONNX Runtime より llama.cpp の方が速く、GGUF の量子化モデルが揃っている。**JSON スキーマ（文法）で出力を縛れる**ので、タグを JSON 配列で確実に返させられる。

**モデルの候補（Q4〜Q8 を想定）:**

| モデル | ライセンス | 備考 |
|---|---|---|
| **sarashina2.2-1b-instruct**（SB Intuitions） | MIT | 日本語の学習が中心でライセンスも単純。最初に試す候補 |
| **LFM2.5-1.2B-JP-202606**（Liquid AI） | LFM Open License v1.0（年間売上 1,000 万ドル未満なら商用も無料） | 2026-06 公開。CPU 向けの設計で日本語に強いが Apache-2.0 ではない |
| **Qwen3-1.7B / 0.6B** | Apache-2.0 | 多言語。思考モード（think）は切って使う |
| Gemma 3 270M | Gemma の利用規約 | 英語が中心。日本語の要約には追加学習が必要 |

**Pi 5 での速さの目安:** 生成は Qwen3 1.7B で約 8〜9 トークン/秒、0.6B（Q8）で約 10〜21 トークン/秒という報告がある（条件はそれぞれ違う）。200 字ほどの要約で数十秒〜1 分程度の見込みで、入力の読み込み時間で大きく変わる。

- **OCR と同時に動かすと CPU とメモリ（worker 3GB・api 1.5GB の上限）が足りなくなる。** OCR と埋め込みの後に、1 件ずつ低い優先度で流す。入力は先頭の数千字に絞り、温度による一時停止も同じ仕組みを使う。
- 自分の文書 20〜30 件で実機計測と出来の比較をしてからモデルを決める（`scripts/poc` の流れを使う）。
- 設計書 1.2 で「LLM による回答生成」はスコープ外になっているので、要約を入れるなら設計書も更新する。

## 3. .heic / .avif / .webp の処理

**推奨: Pillow を 11.3 以上に上げる＋ pillow-heif を追加する＋プレビュー用の画像を作る**

- **WebP:** `pipeline/extract.py` の `IMAGE_TYPES` に入っていて対応済み。アニメーション WebP は最初のフレームだけ OCR すれば十分。
- **AVIF:** **Pillow 11.3.0 から標準の wheel で読み書きできる**（Windows ARM64 と iOS を除く。Linux aarch64 は対象）。11.2.1 の wheel には入っていないので `pillow>=11.3` にする。
- **HEIC:** Pillow 単体では読めないので **pillow-heif**（BSD-3、Linux aarch64 の wheel あり）を使い、`register_heif_opener()` で登録する。同梱のライブラリ（libheif は LGPL、x265 の encoder は GPL）は別ファイルにライセンスが書かれているので、配布時に確認する。docseed では読むだけで足りる。
- **画面での表示:** HEIC をそのまま表示できるのは **Safari だけ**（Chrome・Firefox・Edge は不可）。HEIC と、念のため AVIF も、**worker でプレビュー用の WebP（または JPEG）を作って保存**し、それを配信する。`files.py` の `INLINE_TYPES` に HEIC は追加しない。
- **その他:**
  - iPhone の写真の向きは `ImageOps.exif_transpose` で直す（OCR では使用済み）
  - 位置情報（GPS）などの EXIF を共有リンクで配信するときに消すかを決める
  - 解凍爆弾への対策として `Image.MAX_IMAGE_PIXELS` を設定する
  - ファイルの種類の判定に使っている `filetype` が heic / avif を正しく見分けるか確かめる

## 4. アイコン

**推奨: Material Symbols（Rounded か Outlined）を、使うものだけ SVG にして `static/icons.svg` に入れる**

- Material Symbols は **Apache-2.0** で SVG でも配布されている。約 15,000 個あるが、docseed で使うのは 30〜50 個ほど。
- **Web フォントや Google Fonts の CDN は使わない。**
  - フォントは全部入れると数 MB あり、必要なものだけに減らす手間がかかる
  - CDN は CSP（`default-src 'self'`）と「外部に依存しない」方針に反する
  - LAN の中だけで使っているときに表示が崩れる
- 書き方は `<svg class="icon"><use href="/static/icons.svg#folder"/></svg>`。Jinja のマクロ（`icon('folder')`）にしておく。`currentColor` で文字と同じ色になり、htmx で差し替えても問題ない。
- アイコンだけのボタンには `aria-label`、飾りのアイコンには `aria-hidden="true"` を付ける。
- Apache-2.0 の条件として、ライセンスの文面をリポジトリに入れる。
- 線だけの細いデザインが好みなら **Lucide**（ISC）でも同じ方式で使える。

## 5. HTTPS 証明書

**推奨: 使い方に合わせて 3 つの方式から選べるようにし、`.env` で切り替える**

| 方式 | 向いている使い方 | 方法 | 注意 |
|---|---|---|---|
| **A. Tailscale の証明書**（第一候補） | Tailscale 経由での利用 | 管理画面で HTTPS を有効にし、`DOCSEED_HOST=docseed.<tailnet>.ts.net` にする。Caddy が tailscaled から自動で取得する（Caddy 2.5 以降）。Docker では `/var/run/tailscale/tailscaled.sock` を Caddy のコンテナにマウントする | 証明書の公開ログ（CT ログ）に機械の名前が載る。LAN から IP で開くと名前と合わない |
| **B. Let's Encrypt の DNS-01** | 自分のドメインがあり、LAN の中だけで名前を使いたい | DNS 事業者用のプラグイン（例: caddy-dns/cloudflare）入りの Caddy を xcaddy でビルドし、`docseed.example.com` の A レコードに LAN の IP を設定する | ポートの開放は不要。Caddy のイメージを独自にビルドする |
| **C. 内部 CA（現行）** | ドメインも Tailscale も使わない | 各端末に CA 証明書を入れる | スマートフォンへの CA の追加は手間。最後の手段として残す |

- Let's Encrypt は 2026-01 から **IP アドレスの証明書**も出しているが、インターネットから検証できる必要があるので **LAN の IP（192.168.x.x）には使えない。**
- Let's Encrypt の証明書の有効期間は 2027-02 に 64 日、2028-02 に 45 日へ短くなる。Caddy が自動で更新するので対応は不要。
- `monitor.py` に「証明書の期限が近い・更新に失敗した」項目を足すと運用が安心。

## 6. パスキーと Google アカウントのような安全なログイン

**推奨: ① パスキー（必須にもできる）→ ② 必要な人向けに汎用の OIDC（Google など）→ ③ 予備として TOTP**

### 6.1 パスキー

- **py_webauthn**（PyPI 名 `webauthn`。3.0 系、BSD-3）でサーバー側を実装する。画面と API は登録・ログイン・端末の一覧と削除。
- テーブル `webauthn_credentials`（`credential_id`・公開鍵・`sign_count`・transports・名前・`last_used_at`）を追加する。discoverable credential（ユーザー名を入力しないログイン）とブラウザの自動入力（`mediation: "conditional"`）に対応させる。
- **制約:** RP ID は**ドメイン名でなければならず、IP アドレスは使えない。** 信頼された HTTPS でなければ動かない。つまり **5 章の A か B が前提**。`DOCSEED_LAN_IP` で開いたときはパスワードでのログインに戻す。
- 名前を変えると登録済みのパスキーが使えなくなるので、RP ID は `.env` の固定値（`DOCSEED_RP_ID`）にする。
- パスキーを失ったときの復旧手段として、管理者による再設定と `docseed` コマンドでのリセットを用意する。

### 6.2 Google ログイン（OIDC）

- Google 専用ではなく **Authlib による汎用の OIDC** にすると、Google のほか Pocket ID や Authelia（自分で動かすログインサービス）も使える。
- Google のリダイレクト URI は **HTTPS** で、**Public Suffix List に載ったドメイン**でなければならない（`.local` や IP アドレスは不可）。`*.ts.net` か自分のドメインなら、Pi がインターネットから届かなくても使える（リダイレクトはブラウザの中で起きるため）。
- ただし **api から Google へ通信する**必要があり、「外部のクラウドに依存しない」方針とぶつかる。そのため任意の機能とし、
  - **登録済みのユーザーとの紐付けだけ**にする（メールアドレスで自動的にアカウントを作らない。`sub` で紐付ける）
  - 管理者が許可したメールアドレスだけにする

### 6.3 その他

- パスワードでログインする人向けに TOTP の 2 段階認証を用意すると、パスキーに対応していない端末でも安全性を上げられる。
- Tailscale の `tailscale serve` が付ける利用者のヘッダーでログインする方法もあるが、Caddy の経路と合わないので優先度は低い。

---

## 参考

- trafilatura: [PyPI 1.8.0（ライセンス変更）](https://pypi.org/project/trafilatura/1.8.0)、[GitHub](https://github.com/adbar/trafilatura)
- Pillow: [11.3.0 release notes（AVIF が wheel に入った）](https://pillow.readthedocs.io/en/stable/releasenotes/11.3.0.html)、[11.2.1](https://pillow.readthedocs.io/en/stable/releasenotes/11.2.1.html)
- pillow-heif: [PyPI](https://pypi.org/project/pillow-heif/0.16.0)、[GitHub](https://github.com/bigcat88/pillow_heif)
- [HEIF のブラウザ対応状況](https://www.testmuai.com/learning-hub/heif-browser-support/)
- Pi 5 での LLM: [Qwen3 on a Raspberry Pi 5](https://hashnode-hub.vercel.app/posts/682432507da5ff91f5f0985a)、[DigiKey: Local LLMs on Raspberry Pi](https://www.digikey.com/en/maker/tutorials/2025/local-llms-on-raspberry-pi)、[simeononsecurity](https://simeononsecurity.com/articles/ai-models-raspberry-pi-4-5/)
- 日本語の小型 LLM: [awesome-japanese-llm](https://github.com/llm-jp/awesome-japanese-llm)、[Gemma 3 270M](https://developers.googleblog.com/en/introducing-gemma-3-270m/)、[日本語 LLM 比較 2026-04](https://lilting.ch/en/articles/japanese-llm-options-compared)、[LFM2.5-1.2B-JP-202606](https://huggingface.co/LiquidAI/LFM2.5-1.2B-JP-202606)、[LFM ライセンスの解説](https://book.st-hakky.com/en/data-science/lfm2-5-1-2b-jp-vllm-cpu-commercial-license)
- アイコン: [Material Symbols ガイド](https://developers.google.com/fonts/docs/material_icons)、[Lucide license](https://lucide.dev/license)
- 証明書: [Caddy certificates on Tailscale](https://tailscale.com/kb/1190/caddy-certificates)、[Let's Encrypt: 45 日への短縮](https://isrg.org/post/from-90-to-45/)、[証明書の有効期間の方針](https://letsencrypt.org/docs/cert-lifetimes/)、[IP アドレス証明書](https://letsencrypt.org/2025/07/01/issuing-our-first-ip-address-certificate.html)
- ログイン: [py_webauthn（PyPI）](https://pypi.org/project/webauthn/)、[W3C WebAuthn: RP ID に IP を使う議論](https://lists.w3.org/Archives/Public/public-webauthn/2020Jan/0033.html)、[Ory: passkeys security error](https://ory.sh/docs/troubleshooting/passkeys-webauthn-security-error)、[Google: OAuth クライアントの管理](https://support.google.com/cloud/answer/15549257)
