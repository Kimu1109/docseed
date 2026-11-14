# API 詳細設計

[設計書](../design.md) の「6. API 概要」の詳細。Phase 6〜9 で足すエンドポイントは [拡張機能](extensions.md) 8 章。

## 1. 共通仕様

| 項目 | 内容 |
|---|---|
| ベースパス | `/api/v1` |
| 形式 | JSON（UTF-8）。アップロードのみ `multipart/form-data` |
| 認証 | セッション Cookie + `X-CSRF-Token`（Web UI）、または `Authorization: Bearer dsk_...`（API トークン） |
| 日時 | ISO 8601 UTC（`2026-11-07T12:34:56Z`） |
| ID | 整数 |
| 一覧のページング | `?cursor=<不透明な文字列>&limit=50`（最大 200）。応答に `next_cursor`（無ければ null） |
| 仕様書 | FastAPI が生成する OpenAPI を `/api/v1/openapi.json`、画面を `/api/docs` で提供（ログイン必須。内側の接続元からだけ） |

### 1.1 エラー

RFC 9457（`application/problem+json`）形式。

```json
{
  "type": "https://docseed.local/errors/name_conflict",
  "title": "同じ名前のファイルがあります",
  "status": 409,
  "code": "name_conflict",
  "detail": "「見積書.pdf」は既にこのフォルダにあります",
  "instance": "/api/v1/documents"
}
```

| status | code の例 | 状況 |
|---|---|---|
| 400 | `invalid_request` | パラメータの形式が不正 |
| 401 | `unauthenticated` | 未ログイン、トークン無効 |
| 403 | `forbidden`, `csrf_failed` | 権限が足りない（対象の存在がわかってよい場合のみ）、CSRF 失敗 |
| 404 | `not_found` | 存在しない、またはアクセス権が無い |
| 409 | `name_conflict`, `invalid_move` | 名前の衝突、自分の子孫への移動など |
| 413 | `file_too_large` | サイズ上限超過 |
| 415 | `unsupported_media_type` | |
| 422 | `validation_error` | 項目単位の入力エラー（`errors: [{field, message}]` を付ける） |
| 429 | `rate_limited` | ログイン試行などの制限。`Retry-After` ヘッダを付ける |
| 500 | `internal_error` | |

## 2. リソースの表現

### Document

```json
{
  "id": 123,
  "folder_id": 10,
  "filename": "見積書_2026-10.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 482133,
  "sha256": "9f2c...",
  "page_count": 3,
  "processing": {
    "status": "ready",
    "progress": {"done": 3, "total": 3},
    "warnings": [],
    "error": null
  },
  "tags": ["経理"],
  "created_by": {"id": 1, "display_name": "Kimu"},
  "created_at": "2026-11-07T12:00:00Z",
  "updated_at": "2026-11-07T12:00:00Z",
  "thumbnail_url": "/api/v1/documents/123/thumbnail",
  "my_role": "owner"
}
```

`processing.status`: `pending` | `processing` | `ready` | `failed` | `unsupported`

### Folder

```json
{
  "id": 10,
  "parent_id": 1,
  "name": "経理",
  "path": [{"id": 1, "name": ""}, {"id": 10, "name": "経理"}],
  "owner": {"id": 1, "display_name": "Kimu"},
  "my_role": "owner",
  "shared_with_others": true,
  "created_at": "...",
  "updated_at": "..."
}
```

## 3. エンドポイント

### 3.1 認証・自分

パスキー・確認コード・OIDC と、自分のログイン方法の API（`/me/security` など）は [拡張機能](extensions.md) 8.1〜8.3 節。

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/auth/login` | `{username, password, remember}` → セッション Cookie を発行。応答 `{user, csrf_token}`。確認コードを設定したユーザーは `{second_factor_required: true}` を返し、`POST /auth/second-factor`（`/api/v1` の外）で 2 段階目を送る |
| POST | `/auth/logout` | セッション削除 |
| GET | `/me` | 自分の情報、ルートフォルダ ID、使用容量 |
| PATCH | `/me` | 表示名の変更 |
| POST | `/me/password` | `{current_password, new_password}` |
| GET | `/me/tokens` | API トークン一覧（先頭 8 文字・名前・最終使用日時） |
| POST | `/me/tokens` | `{name, scopes, expires_at}` → `{token}`（この 1 回だけ表示） |
| DELETE | `/me/tokens/{id}` | 無効化 |
| GET | `/me/usage` | 使用量 `{current, trash, versions, total, quota, ratio, custom}`（バイト。quota 0 = 上限なし） |
| GET / PUT | `/me/trash-retention` | ゴミ箱の保持日数 `{days}`（7 / 14 / 30 / 60 / 90 / 180 / 365、null で既定に戻す）。応答 `{days, default, custom}` |
| GET / PUT | `/me/theme` | 画面の色 `{theme: "auto" \| "light" \| "dark"}` |
| GET | `/me/favorites` | お気に入り（`{favorites: [{type, id, filename\|name, ..., favorited_at}]}`。見られるものだけ） |
| PUT / DELETE | `/me/favorites/{type}/{id}` | お気に入りに入れる・外す（type = document \| folder）。204 |
| GET / DELETE | `/me/recent` | 最近開いたファイル（画面で開いたもの。`?limit=` 最大 50）/ 記録を消す |
| GET | `/me/search-history` | 画面で検索した条件の履歴（`{history: [{id, params, url, label, searched_at}]}`） |
| DELETE | `/me/search-history`、`/me/search-history/{id}` | 履歴をすべて・1 件消す |
| GET / POST | `/me/saved-searches` | 保存した検索の一覧 / `{name?, params}` で保存（params は画面の `/search` と同じキー: q・mode・type・tag・after・before・taken_from・taken_to・folder。ほかは捨てる）。201 |
| PATCH / DELETE | `/me/saved-searches/{id}` | `{name}` で名前を変える / 消す |

### 3.2 フォルダ

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/folders/shared-with-me` | 他のユーザーから共有されたフォルダの一覧 |
| GET | `/folders/{id}` | フォルダ情報 |
| GET | `/folders/{id}/children` | 子フォルダと文書の一覧。`?sort=name\|updated_at\|size&order=asc\|desc&cursor=&limit=`。`limit`（1〜1000）を付けるとページ送り: フォルダ（名前順）→ 文書の順に並べた全体から `limit` 件を返し、続きがあれば `next_cursor` を返す（次は `cursor=<next_cursor>`）。省略時はすべて。応答に `total: {folders, documents}` |
| POST | `/folders` | `{parent_id, name}` |
| PATCH | `/folders/{id}` | `{name?, parent_id?}`（名前変更・移動） |
| DELETE | `/folders/{id}` | 配下ごとゴミ箱へ |
| GET | `/folders/{id}/members` | メンバー一覧 |
| PUT | `/folders/{id}/members/{user_id}` | `{role: "viewer"\|"editor"}` 追加・変更（owner のみ） |
| DELETE | `/folders/{id}/members/{user_id}` | メンバー削除（owner のみ） |
| GET | `/folders/{id}/archive` | 配下を ZIP でダウンロード（ストリーミング、上限 2GB） |

### 3.3 文書

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/documents` | アップロード（3.3.1） |
| OPTIONS・POST・HEAD・PATCH・GET・DELETE | `/uploads`・`/uploads/{id}` | 分割・再開できるアップロード（tus 1.0。3.3.2） |
| GET | `/documents/{id}` | Document |
| GET | `/documents/{id}/content` | 原本ダウンロード。`Range` 対応。`?inline=1` で許可形式のみプレビュー表示 |
| GET | `/documents/{id}/thumbnail` | WebP サムネイル（未生成なら種類別のアイコン） |
| GET | `/documents/{id}/pages/{page_no}/image` | ページ画像（PDF のプレビュー・OCR 結果の確認用。WebP、`?width=` で長辺を指定（200〜2000、既定 1600）。OCR に渡したのと同じ向き（PDF の /Rotate・画像の EXIF の回転を適用）。保存せず開いたときに描く。PDF と画像だけ） |
| POST | `/documents/{id}/pages/{page_no}/reprocess` | そのページだけを読み直す（PDF・画像、editor 以上。`{force_ocr?}` はそのページだけに効く。202。処理待ち・処理中は 422） |
| GET | `/documents/{id}/text` | 抽出テキスト。`?page=` でページ指定。応答 `{pages: [{page_no, source, text, ocr_confidence}]}` |
| GET | `/documents/{id}/ocr` | OCR の行と座標。`?page=`（ページ画像上に枠を重ねて表示するため）。応答に `width`・`height`（座標の基準の画像の大きさ）と `min_score`（これ未満の行は本文に使っていない） |
| PATCH | `/documents/{id}` | `{filename?, folder_id?, tags?}` |
| POST | `/documents/{id}/copy` | `{folder_id, filename?}` |
| DELETE | `/documents/{id}` | ゴミ箱へ |
| POST | `/documents/{id}/reprocess` | `{force_ocr: bool}`（editor 以上） |
| GET | `/documents/{id}/related` | 内容が似ている文書（`?limit=`、既定 5・最大 20）。`{documents: [{id, filename, kind, mime_type, folder_id, updated_at, similarity, path}]}`。文書のベクトル（先頭 20 チャンクの平均）に近いチャンクを持つ文書を、類似度 0.80 以上・見られるものだけ・同じ中身のコピーを除いて並べる。文字を読み取れていない文書は空 |
| GET | `/documents/{id}/versions` | 過去の版の一覧（新しい順）。`{versions: [{id, filename, size_bytes, mime_type, sha256, created_by, created_at, archived_by, archived_at}]}` |
| POST | `/documents/{id}/versions` | 新しい版をアップロード（multipart の `file`、editor 以上）。名前・フォルダ・タグはそのまま、今の中身は過去の版になる。201 `{changed, document}`（中身が今と同じなら `changed: false` で版は増えない）。Web クリップは 422（再取得を使う） |
| GET | `/documents/{id}/versions/{version_id}/content` | 過去の版のダウンロード（常に attachment） |
| POST | `/documents/{id}/versions/{version_id}/restore` | 過去の版に戻す（editor 以上）。今の版は過去の版として残る。応答は Document |
| DELETE | `/documents/{id}/versions/{version_id}` | 過去の版を完全に削除（editor 以上。ゴミ箱には入らない）。204 |

#### 3.3.1 アップロード

```
POST /api/v1/documents
Content-Type: multipart/form-data

folder_id=10
on_conflict=rename          # rename（既定）| replace | error
files=@見積書.pdf
files=@領収書.jpg
```

- 1 リクエストで複数ファイル可。応答はファイルごとの結果の配列（一部だけ失敗することがある）。

```json
{
  "results": [
    {"filename": "見積書.pdf", "status": "created", "document": { ... }},
    {"filename": "領収書.jpg", "status": "created", "document": { ... }, "deduplicated": true},
    {"filename": "巨大.zip",   "status": "error", "error": {"code": "file_too_large", "detail": "..."}}
  ]
}
```

- `deduplicated: true` は同じ内容のファイルが既にあり、処理結果を使い回したことを示す。
- `on_conflict=replace` で置き換えたときは `status: "replaced"`。それまでの中身は過去の版（`/documents/{id}/versions`）に残る。
- フォルダ構造ごとのアップロード: `files` と同じ順番で `relative_paths`（例 `2026/10/見積書.pdf`）を送ると、`folder_id` の下に必要なフォルダを作成する（空文字のファイルはそのまま `folder_id` に置く）。
- 大きなファイル: Caddy とアプリの上限は 1GB。通信が不安定な環境や、1 リクエストの大きさに上限がある経路（Cloudflare Tunnel は 100MB）では、分割・再開できるアップロード（3.3.2）を使う。画面からのアップロードはすべてこちらを使う。

#### 3.3.2 分割・再開できるアップロード（tus 1.0）

[tus 1.0](https://tus.io/protocols/resumable-upload) の core と、拡張 `creation`・`creation-with-upload`・`termination`・`expiration` に対応する。tus-js-client などの既存のクライアントも使える。処理は `resumable.py`、表は data-model.md 3.3 の `resumable_uploads`。

| メソッド | パス | 説明 |
|---|---|---|
| OPTIONS | `/uploads` | `Tus-Version`・`Tus-Extension`・`Tus-Max-Size`（= `DOCSEED_MAX_UPLOAD_MB`） |
| POST | `/uploads` | 受け取りを始める。`Upload-Length`（必須。`Upload-Defer-Length` は不可）と `Upload-Metadata`（`filename`・`folder_id` が必須、`relative_path`・`on_conflict` は 3.3.1 と同じ）。201 と `Location`。`Content-Type: application/offset+octet-stream` で本文を付けると、そのまま書き始める |
| HEAD | `/uploads/{id}` | `Upload-Offset`（受け取り済みのバイト数）・`Upload-Length`・`Upload-Expires` |
| PATCH | `/uploads/{id}` | 続きを送る。`Content-Type: application/offset+octet-stream`（以外は 415）と `Upload-Offset`（受け取り済みと違えば 409 `offset_mismatch`）。204 と新しい `Upload-Offset` |
| GET | `/uploads/{id}` | docseed 独自。`{id, offset, length, complete, expires_at, result}` |
| DELETE | `/uploads/{id}` | 取り消す（受け取った分も消す）。204 |

- 送り始める前（POST）に、フォルダの権限（editor 以上）・1 ファイルの上限（413）・容量の上限（507）・空き容量を確かめる。遅い回線で送り終えてから断らないため。
- PATCH の途中で接続が切れても、届いた分は残る。クライアントは HEAD で位置を聞き、そこから送る。同じアップロードへの書き込みは 1 本ずつで、前の接続がまだ書いている間は 423 `upload_busy`（少し待って再試行する）。
- 最後まで届いた PATCH（または POST）では文書として保存し、**200（POST は 201）と GET と同じ JSON** を返す（tus の決まりでは 204 だが、既存のクライアントは 2xx を成功として扱う）。`result` は 3.3.1 の `results[]` の 1 件と同じ形で、名前の重複（`on_conflict=error`）などの失敗も `status: "error"` としてここに入る。
- 最後の応答を受け取れなかったときは、`Upload-Offset` を最後の位置にした空の PATCH（または GET）で、同じ結果をもう一度受け取れる（2 回保存しない）。
- 期限は最後の書き込みから 24 時間（`Upload-Expires`）。過ぎると 410 `upload_gone`。受け取り中のデータ（`files/.tmp/tus-<id>`）は worker の毎日の片付けで消す。受け取り中のまま置ける数は 1 人 200 件まで。
- 他の人のアップロードは 404（存在を明かさない）。API トークン（write）でも使える。ブラウザのセッションでは、POST・PATCH・DELETE に `X-CSRF-Token` が必要。

画面（`app.js`）は 1 ファイルを 8MB ずつ PATCH で送る。通信が失敗したら（接続の失敗・60 秒進まない・408・423・429・5xx・409）、1・2・4・8・15・30…秒待ってから HEAD で位置を聞き直して続きを送る。前に進めば待ち時間は最初に戻り、オフラインの間は回線が戻るのを待つ。送り先の URL は localStorage に覚えるので、再読み込みのあと同じファイルを選び直すと続きから送る。待っても届かなければ失敗として「再試行」ボタンを出す。

### 3.4 ゴミ箱

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/trash` | 自分が owner のフォルダ配下で削除された項目（削除単位でまとめる） |
| GET | `/trash/folder/{id}` | ゴミ箱の中のフォルダと、一緒に削除された文書 `{id, name, deleted_at, location, folders, documents: [{id, filename, path, size, mime_type}]}`（path はフォルダの中の相対パス） |
| GET | `/trash/document/{id}/content` | ゴミ箱の中の文書の原本（`?inline=1` でプレビュー用。`/documents/{id}/content` と同じ） |
| GET | `/trash/document/{id}/thumbnail` ・ `/view` | ゴミ箱の中の文書のサムネイル・表示用のプレビュー |
| POST | `/trash/{type}/{id}/restore` | 復元（type = folder \| document） |
| DELETE | `/trash/{type}/{id}` | 完全削除 |
| DELETE | `/trash` | ゴミ箱を空にする |

### 3.5 検索

（質問に答える `POST /ask` はこの節の最後）

```
GET /api/v1/search?q=振込先 請求書&mode=hybrid&type=pdf&tag=経理&folder=10&after=2026-01-01&limit=20&offset=0
```

```json
{
  "query": "振込先 請求書",
  "mode": "hybrid",
  "total_estimate": 37,
  "took_ms": 182,
  "results": [
    {
      "document": { "...": "Document（簡略版）" },
      "score": 0.0312,
      "snippets": [
        {
          "chunk_id": 9981,
          "page_no": 2,
          "text": "お手数ですが下記の<mark>振込先</mark>まで…",
          "match": ["keyword", "semantic"]
        }
      ]
    }
  ],
  "notices": ["2 文字以下の語「税」は意味検索の候補内でのみ照合しました"]
}
```

- `text` は HTML エスケープ済みで、`<mark>` タグだけを含む。
- `q` 内の `type:` `tag:` `folder:` `after:` `before:` `taken:` もパラメータと同じ意味で解釈する（[検索設計](search.md) 2 章）。
- `taken`: 写真の撮影日（`2026` / `2026-05` / `2026-05-03` / `2026-05-01..2026-05-10` / `..2025`）。結果の `document.taken_at` に撮影日時（無ければ null）。
- 検索語が無く `type` `tag` `after` `before` `taken` のどれかがあれば、条件に合う文書を並べる（`mode: "filter"`、`snippets` は空）。
- `sem_min`（0〜1、省略可）: 意味検索の類似度の下限を一時的に変える。評価スクリプト用で、省略時は設定値（既定 0.80）。

#### 3.5.1 質問に答える（RAG。実装で追加）

```
POST /api/v1/ask
{"question": "A社への振込先は？", "filters": {"type": "pdf", "folder": "10"}, "stream": false}
```

- 見られる文書だけを根拠に、LLM が答える（[検索設計](search.md) 10 章）。`DOCSEED_LLM_ENABLED=false` なら 409 `ask_disabled`。
- `stream: false`: `{question, answer, sources: [{n, document_id, filename, page_no, path, text}], cited: [n…], model, took_ms, notices}`。資料が見つからなければ `answer` は空（LLM を呼ばない）。
- `stream: true`: `application/x-ndjson`。`{"type": "sources", …}` → `{"type": "text", "text"}` … → `{"type": "done", "cited", "took_ms"}`（資料が無ければ `"no_sources": true`）。途中の失敗は `{"type": "error", "code", "message"}`。
- 混んでいる（同時に 2 件）: 429 `ask_busy`。llm サービスの失敗・時間切れ: 503 `ask_failed`。
- 実装では `total_estimate` ではなく `total`（絞り込み後の文書数）を返す。

### 3.6 タグ

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/tags` | 自分が見られる文書に付いているタグと件数 |

タグの付け外しは `PATCH /documents/{id}` の `tags` で行う（存在しないタグは自動作成）。

### 3.7 共有リンク（管理）

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/shares` | 自分が作成した共有リンクの一覧（`?target_type=&target_id=` で絞り込み） |
| POST | `/shares` | `{target_type, target_id, permission, expires_at, password, max_downloads}` → `{id, url, ...}` |
| PATCH | `/shares/{id}` | 期限・パスワード・上限の変更 |
| DELETE | `/shares/{id}` | 無効化 |

### 3.8 共有リンク（公開側, `/api/v1` の外）

ログイン不要。HTML ページとして提供し、ファイルは以下から配信する。

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/s/{token}` | 共有ページ（パスワード入力・一覧・プレビュー・アップロード画面） |
| POST | `/s/{token}/auth` | パスワード確認 |
| GET | `/s/{token}/items` | フォルダ共有の一覧（`?folder_id=` で配下へ移動） |
| GET | `/s/{token}/download/{document_id}` | ダウンロード（文書共有は document_id 省略可） |
| GET | `/s/{token}/preview/{document_id}` | プレビュー（許可形式のみ） |
| GET | `/s/{token}/archive` | フォルダ共有の ZIP ダウンロード |
| POST | `/s/{token}/upload` | `upload` 権限のリンクへのアップロード |

### 3.9 管理（admin のみ）

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/admin/users` | ユーザー一覧（`quota: {bytes, custom, used}` を含む。bytes 0 = 上限なし、custom = 個別に決めたか） |
| GET / PUT | `/admin/quota` | 容量の上限の全体の既定 `{quota_bytes}`（0 = 上限なし）。ユーザーごとは `PATCH /admin/users/{id}` の `quota_bytes`（0 = 上限なし）か `quota_default: true`（既定に戻す） |
| POST | `/admin/users` | `{username, display_name, password, role}` |
| PATCH | `/admin/users/{id}` | ロール変更、無効化、パスワード再設定 |
| GET | `/admin/jobs` | ジョブ一覧（`?status=`）と状態ごとの件数 |
| POST | `/admin/jobs/{id}/retry` | 失敗ジョブの再実行 |
| POST | `/admin/reprocess` | `{scope: "failed" \| "outdated" \| "unsupported" \| "all", force_ocr: bool}` で一括再処理（`unsupported` は対応形式が増えたとき用） |
| GET | `/admin/status` | 状態と警告（Phase 5）。`alerts`（`level`: error / warning / info、`code`、`message`）、worker の最終応答、ジョブの件数、ディスク、CPU 温度、バックアップ（DB スナップショット・restic の最終結果）。外部の監視からは管理者の API トークン（read）で呼べる |
| GET | `/admin/metrics` | Prometheus のテキスト形式の指標（[配置・運用](deployment.md) 6.2）。管理者のセッションか、管理者の API トークン（read） |
| GET | `/admin/system` | CPU 温度・負荷・コア数、メモリ（`total`・`available`・スワップ）、ディスク（`total`・`used`・`free`・`free_ratio`）、`db_bytes`、件数（`documents`・`documents_trash`・`versions`・`folders`・`users`・`blobs`・`pages`・`chunks`）、`storage`（`blob_bytes`・過去の版だけが使う `version_only_bytes`）、モデル（`ocr`・`embedding`・`llm`）、版（`version`・`schema_version`・`python`・`machine`）、`uptime_s` |
| GET | `/admin/notifications` | 通知の設定（`enabled`・`events`・`destinations`（形式とホスト名だけ））と最近の通知（`?limit=`、既定 50） |
| POST | `/admin/notify/test` | テスト通知をすぐに送る。応答の `status` が `sent` なら送れた（`error` は一部の宛先の失敗）。宛先が無ければ 422 |
| GET | `/admin/audit` | 監査ログ（`?user_id=&action=&from=&to=`） |

### 3.10 その他

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/healthz` | 認証不要（Caddy で内側の接続元からだけに制限。[セキュリティ](security.md) 8 章）。`{status: "ok", db: "ok", worker_last_seen: "..."}`。worker の生存は worker が 30 秒ごとに `settings` の `worker.heartbeat` を更新することで判定 |
| GET | `/api/v1/events` | Server-Sent Events。自分に関係する文書の処理状況の変化を通知（UI の進捗表示用）。api が 2 秒ごとに `blobs.updated_at` の変化を DB から読み、接続中のクライアントへ送る |
