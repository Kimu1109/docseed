# API 詳細設計

[設計書](../design.md) の「6. API 概要」の詳細。

## 1. 共通仕様

| 項目 | 内容 |
|---|---|
| ベースパス | `/api/v1` |
| 形式 | JSON（UTF-8）。アップロードのみ `multipart/form-data` |
| 認証 | セッション Cookie + `X-CSRF-Token`（Web UI）、または `Authorization: Bearer dsk_...`（API トークン） |
| 日時 | ISO 8601 UTC（`2026-11-07T12:34:56Z`） |
| ID | 整数 |
| 一覧のページング | `?cursor=<不透明な文字列>&limit=50`（最大 200）。応答に `next_cursor`（無ければ null） |
| 仕様書 | FastAPI が生成する OpenAPI を `/api/v1/openapi.json`、画面を `/api/docs` で提供（ログイン必須） |

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

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/auth/login` | `{username, password, remember}` → セッション Cookie を発行。応答 `{user, csrf_token}` |
| POST | `/auth/logout` | セッション削除 |
| GET | `/me` | 自分の情報、ルートフォルダ ID、使用容量 |
| PATCH | `/me` | 表示名の変更 |
| POST | `/me/password` | `{current_password, new_password}` |
| GET | `/me/tokens` | API トークン一覧（先頭 8 文字・名前・最終使用日時） |
| POST | `/me/tokens` | `{name, scopes, expires_at}` → `{token}`（この 1 回だけ表示） |
| DELETE | `/me/tokens/{id}` | 無効化 |

### 3.2 フォルダ

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/folders/shared-with-me` | 他のユーザーから共有されたフォルダの一覧 |
| GET | `/folders/{id}` | フォルダ情報 |
| GET | `/folders/{id}/children` | 子フォルダと文書の一覧。`?sort=name\|updated_at\|size&order=asc\|desc&cursor=&limit=` |
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
| GET | `/documents/{id}` | Document |
| GET | `/documents/{id}/content` | 原本ダウンロード。`Range` 対応。`?inline=1` で許可形式のみプレビュー表示 |
| GET | `/documents/{id}/thumbnail` | WebP サムネイル（未生成なら種類別のアイコン） |
| GET | `/documents/{id}/pages/{page_no}/image` | ページ画像（PDF のプレビュー・OCR 結果の確認用。WebP、`?width=` で幅指定、最大 2000） |
| GET | `/documents/{id}/text` | 抽出テキスト。`?page=` でページ指定。応答 `{pages: [{page_no, source, text, ocr_confidence}]}` |
| GET | `/documents/{id}/ocr` | OCR の行と座標。`?page=`（ページ画像上に枠を重ねて表示するため） |
| PATCH | `/documents/{id}` | `{filename?, folder_id?, tags?}` |
| POST | `/documents/{id}/copy` | `{folder_id, filename?}` |
| DELETE | `/documents/{id}` | ゴミ箱へ |
| POST | `/documents/{id}/reprocess` | `{force_ocr: bool}`（editor 以上） |

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
- フォルダ構造ごとのアップロード: `files` と同じ順番で `relative_paths`（例 `2026/10/見積書.pdf`）を送ると、`folder_id` の下に必要なフォルダを作成する（空文字のファイルはそのまま `folder_id` に置く）。
- 大きなファイル: Caddy とアプリの上限は 1GB。分割・再開可能なアップロード（tus 等）は初版では対応しない。

### 3.4 ゴミ箱

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/trash` | 自分が owner のフォルダ配下で削除された項目（削除単位でまとめる） |
| POST | `/trash/{type}/{id}/restore` | 復元（type = folder \| document） |
| DELETE | `/trash/{type}/{id}` | 完全削除 |
| DELETE | `/trash` | ゴミ箱を空にする |

### 3.5 検索

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
- `q` 内の `type:` `tag:` `folder:` `after:` `before:` もパラメータと同じ意味で解釈する（[検索設計](search.md) 2 章）。
- `sem_min`（0〜1、省略可）: 意味検索の類似度の下限を一時的に変える。評価スクリプト用で、省略時は設定値（既定 0.80）。
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
| GET | `/admin/users` | ユーザー一覧 |
| POST | `/admin/users` | `{username, display_name, password, role}` |
| PATCH | `/admin/users/{id}` | ロール変更、無効化、パスワード再設定 |
| GET | `/admin/jobs` | ジョブ一覧（`?status=`）と状態ごとの件数 |
| POST | `/admin/jobs/{id}/retry` | 失敗ジョブの再実行 |
| POST | `/admin/reprocess` | `{scope: "outdated_pipeline" \| "outdated_embedding" \| "failed"}` で一括再処理 |
| GET | `/admin/system` | CPU 温度・負荷、メモリ、ディスク使用量、DB サイズ、文書数・チャンク数、モデル名、版 |
| GET | `/admin/audit` | 監査ログ（`?user_id=&action=&from=&to=`） |

### 3.10 その他

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/healthz` | 認証不要。`{status: "ok", db: "ok", worker_last_seen: "..."}`。worker の生存は worker が 30 秒ごとに `settings` の `worker.heartbeat` を更新することで判定 |
| GET | `/api/v1/events` | Server-Sent Events。自分に関係する文書の処理状況の変化を通知（UI の進捗表示用）。api が 2 秒ごとに `blobs.updated_at` の変化を DB から読み、接続中のクライアントへ送る |
