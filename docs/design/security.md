# 認証・権限・共有 詳細設計

[設計書](../design.md) の「8.3 セキュリティ」の詳細。

## 1. 脅威の想定

| 想定 | 対策の方針 |
|---|---|
| LAN 内の他の機器・同居人以外の人 | ログイン必須。管理者以外はユーザーを作れない |
| インターネットからの攻撃 | 原則公開しない（VPN 経由）。公開する場合も共有リンク以外はログイン必須、レート制限 |
| 共有リンクの漏えい・推測 | 推測できない長さのトークン、期限・パスワード・回数制限・無効化 |
| 悪意のあるファイルのアップロード | ファイルを実行しない、ブラウザで HTML として表示させない、処理はタイムアウトとメモリ上限つき |
| SSD の盗難 | 範囲外（必要ならディスク全体の暗号化で対処） |

## 2. 認証

パスキー・確認コード（TOTP）・OIDC と再認証は Phase 7 で追加する（[拡張機能](extensions.md) 5 章）。

### 2.1 ユーザー

- 初回起動時に管理者を作る: `docseed create-admin --username <name>`（パスワードは対話入力）。
- 管理者が管理画面でユーザーを作成する（自分で登録する機能は無し）。
- パスワード: 12 文字以上。argon2id（argon2-cffi の既定パラメータ: time_cost=3, memory_cost=64MiB, parallelism=4）でハッシュ化。
- 無効化（`is_active = 0`）するとそのユーザーのセッションと API トークンはすべて使えなくなる。

### 2.2 セッション（Web UI）

| 項目 | 内容 |
|---|---|
| セッション ID | 32 バイトの乱数。DB には SHA-256 を保存 |
| Cookie | `docseed_session`、`HttpOnly; Secure; SameSite=Lax; Path=/` |
| 有効期限 | 最終アクセスから 14 日（「ログインしたままにする」が無い場合はブラウザを閉じるまで、最大 12 時間） |
| ログイン時 | 新しいセッション ID を発行（セッション固定攻撃の対策） |
| パスワード変更時 | そのユーザーの他のセッションをすべて削除 |
| CSRF | セッションごとの CSRF トークンを HTML の meta に埋め込み、POST/PUT/PATCH/DELETE では `X-CSRF-Token` ヘッダ（フォームは hidden 項目）で送らせて照合 |

### 2.3 API トークン（CLI・スクリプト用）

- 設定画面で発行。形式 `dsk_<32 バイトの base64url>`。発行時に 1 回だけ表示し、DB には SHA-256 を保存。
- `Authorization: Bearer dsk_...` で送る。API トークンでの呼び出しは CSRF チェック不要。
- スコープ: `read`（閲覧・検索・ダウンロード）、`write`（アップロード・変更・削除）。共有リンクの作成・ユーザー管理は API トークンでは不可。

### 2.4 ログイン試行の制限

- 同じユーザー名または同じ IP で、15 分間に 5 回失敗したら 15 分間ロック。
- 失敗回数はメモリに保持する（api は 1 プロセスのため）。再起動でリセットされるのは許容する。
- ユーザーが存在しない場合も、存在する場合と同じ時間をかけて同じエラーを返す（ユーザー名の存在を推測させない）。

## 3. 権限

### 3.1 ロール

| ロール | 対象 | できること |
|---|---|---|
| admin | システム | ユーザー管理、ジョブ・システム状態の確認、再処理の実行。**他人のファイルは見られない**（必要ならフォルダ共有を受ける） |
| owner | フォルダ | ルートフォルダの持ち主。配下のすべての操作、メンバーの追加・削除、共有リンクの作成 |
| editor | フォルダ | 閲覧、アップロード、名前変更・移動（同じ owner の範囲内）、削除、タグ付け、共有リンクの作成 |
| viewer | フォルダ | 閲覧、検索、ダウンロード |

- フォルダのメンバー設定はサブフォルダに継承される。子フォルダでより強いロールを与えることはできるが、弱めることはできない（簡単にするため）。
- 文書単位の権限は持たない。権限は必ずフォルダで決まる。
- 異なる owner のフォルダ間の移動はできない（コピーで対応。blob を共有するので容量は増えない）。

### 3.2 操作と必要なロール

| 操作 | 必要なロール |
|---|---|
| 一覧・閲覧・ダウンロード・検索 | viewer 以上 |
| アップロード、フォルダ作成、名前変更、移動、タグ、削除、復元 | editor 以上 |
| 共有リンクの作成・無効化 | editor 以上（無効化は作成者または owner） |
| メンバーの追加・変更・削除 | owner |
| ゴミ箱を空にする | owner |

### 3.3 実効ロールの計算

```sql
-- 指定フォルダの祖先（自分を含む）
WITH RECURSIVE ancestors(id, parent_id, owner_id) AS (
  SELECT id, parent_id, owner_id FROM folders WHERE id = :folder_id
  UNION ALL
  SELECT f.id, f.parent_id, f.owner_id
    FROM folders f JOIN ancestors a ON f.id = a.parent_id
)
SELECT CASE
         WHEN (SELECT owner_id FROM folders WHERE id = :folder_id) = :user_id THEN 'owner'
         ELSE (SELECT CASE MAX(CASE m.role WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 END)
                        WHEN 2 THEN 'editor' WHEN 1 THEN 'viewer' END
                 FROM folder_members m JOIN ancestors a ON a.id = m.folder_id
                WHERE m.user_id = :user_id)
       END AS role;
```

検索で使う「アクセスできるフォルダ一覧」:

```sql
WITH RECURSIVE accessible_folders(folder_id) AS (
  SELECT id FROM folders WHERE owner_id = :user_id AND deleted_at IS NULL
  UNION
  SELECT folder_id FROM folder_members WHERE user_id = :user_id
  UNION
  SELECT f.id FROM folders f JOIN accessible_folders a ON f.parent_id = a.folder_id
   WHERE f.deleted_at IS NULL
)
SELECT folder_id FROM accessible_folders;
```

- 権限チェックはすべて API 層の共通関数（`require_folder_role(user, folder_id, role)`）で行い、各エンドポイントで個別に書かない。
- 権限が無い場合、対象の存在を知られないように 403 ではなく 404 を返す。

## 4. 共有リンク

### 4.1 発行

| 項目 | 内容 |
|---|---|
| 対象 | 文書 1 件、またはフォルダ（配下を含む） |
| トークン | 32 バイトの乱数（base64url, 43 文字）。URL: `https://<host>/s/<token>` |
| 権限 | `read`（閲覧・ダウンロード）／`upload`（フォルダのみ。外部の人からファイルを受け取る。中身の一覧は見せない） |
| 期限 | 既定 7 日。「無期限」も選べる（UI で警告を表示） |
| パスワード | 任意。argon2id で保存 |
| ダウンロード回数上限 | 任意 |
| 無効化 | いつでも可能。対象の文書・フォルダを削除した場合も自動的に使えなくなる |

### 4.2 アクセス時の確認

1. トークンが存在し、`revoked_at` が NULL、期限内、回数上限未満であること
2. 対象の文書・フォルダが削除されていないこと
3. 作成者がまだ対象に editor 以上の権限を持ち、ユーザーが有効であること（権限を失った人のリンクは自動的に無効）
4. パスワード付きなら、パスワード確認済みであること
   - 確認に成功したら、トークンごとの署名付き Cookie（`docseed_share_<token の先頭 8 文字>`、有効 12 時間）を発行
   - パスワードの失敗は同じトークン・同じ IP で 15 分に 10 回まで

いずれかを満たさない場合は、理由を区別せず「リンクが無効か期限切れです」と表示する。

### 4.3 共有リンクで見える範囲

- 文書の共有: ファイル名・サイズ・プレビュー・ダウンロード。抽出テキストや他の文書は見せない。
- フォルダの共有: 配下のフォルダと文書の一覧、プレビュー、ダウンロード、フォルダの ZIP 一括ダウンロード（上限 2GB）。検索は提供しない。
- `upload` 権限: アップロード画面のみ。ファイル名の衝突は別名で保存。1 ファイル 1GB、1 リンク合計 10GB まで。受け取ったファイルは `documents.uploaded_via_share_id` に記録し、一覧で「共有リンクから受け取り」と表示する。

すべてのアクセス（閲覧・ダウンロード・アップロード・パスワード失敗）を `audit_log` に記録する。

## 5. ファイルの配信

アップロードされたファイルを使った攻撃（保存型 XSS 等）を防ぐ。

| 項目 | 内容 |
|---|---|
| ダウンロード | 常に `Content-Disposition: attachment; filename*=UTF-8''...` |
| プレビュー（inline 表示） | 許可した形式のみ: PDF、PNG、JPEG、WebP、GIF、プレーンテキスト（`text/plain; charset=utf-8` で返す）。HTML・SVG・XML はプレビューしない |
| 共通ヘッダ | `X-Content-Type-Options: nosniff`、`Content-Security-Policy: default-src 'none'; sandbox`（ファイル配信のレスポンス）、`Cross-Origin-Resource-Policy: same-origin` |
| アプリの画面 | `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'`、`X-Frame-Options: DENY`（PDF プレビュー用の iframe は同一オリジンのみ許可）、`Referrer-Policy: same-origin` |
| ファイル名 | 保存時にパス区切り文字・制御文字を除去し、255 バイトまでに切り詰める。実体のパスには使わない |

## 6. ファイル処理の安全策

- 画像: Pillow の `MAX_IMAGE_PIXELS` を 1 億画素に設定（画像の展開爆弾対策）。
- PDF: 1 ファイル 2000 ページまで。1 ページの画像化・OCR にタイムアウト。
- Office: ZIP 展開後の合計サイズが 500MB を超えるもの、圧縮率が 200 倍を超えるもの、ZIP の項目が 2 万を超えるものは処理しない（`office_too_large`。保存・共有はできる）。Excel は 1 シート 100 万セルまで読む。外部参照（リンクされた画像等）は取得しない。パスワード付き・古い形式（OLE の複合ファイル）は開かない（`encrypted_office`）。
- worker コンテナはネットワークを外部に出さない設定にする（モデルは事前に配置済みのため不要）。メモリ上限 3GB を設定する。

## 7. 監査ログ

`audit_log` に次を記録し、管理画面（自分の操作は設定画面）で確認できるようにする。保持期間は 1 年。

- ログイン成功・失敗、ログアウト、パスワード変更
- アップロード、ダウンロード、削除、復元、完全削除
- フォルダのメンバー追加・変更・削除
- 共有リンクの作成・無効化・アクセス・パスワード失敗
- ユーザーの作成・無効化、API トークンの発行・無効化
