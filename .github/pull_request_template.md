## 変更の内容

<!-- 何を・なぜ。関係する設計書の章があれば -->

## 確かめたこと

<!-- 実行したテスト・手で試したこと -->

## 文書（docs/dev/docs-guide.md）

<!-- 当てはまらないものは消すか「なし」と書く -->

- [ ] 利用者に見える変更 → `docs/user/`
- [ ] 管理者の作業がある → `docs/admin/`（任意の機能なら `docs/admin/README.md` の表）
- [ ] 設定を足した → `config.py`・`.env.example`・`docs/admin/configuration.md`
- [ ] コマンドを足した → `docs/admin/cli.md`
- [ ] 更新に追加の作業がある → `docs/admin/upgrade.md`
- [ ] 仕組みを変えた → `docs/design/`
- [ ] `pytest tests/test_docs.py` が通る
