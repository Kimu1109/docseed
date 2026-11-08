"""アプリ共通の例外。API では RFC 9457（problem+json）に変換する。docs/design/api.md 1.1。"""

from __future__ import annotations


class AppError(Exception):
    status = 400
    code = "invalid_request"
    title = "リクエストが正しくありません"

    def __init__(self, detail: str | None = None, *, errors: list[dict] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.errors = errors


class Unauthenticated(AppError):
    status, code, title = 401, "unauthenticated", "ログインが必要です"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "この操作の権限がありません"


class CsrfFailed(AppError):
    status, code, title = 403, "csrf_failed", "ページの有効期限が切れました。再読み込みしてください"


class NotFound(AppError):
    status, code, title = 404, "not_found", "見つかりません"


class NameConflict(AppError):
    status, code, title = 409, "name_conflict", "同じ名前があります"


class InvalidMove(AppError):
    status, code, title = 409, "invalid_move", "その場所には移動できません"


class FileTooLarge(AppError):
    status, code, title = 413, "file_too_large", "ファイルが大きすぎます"


class ValidationFailed(AppError):
    status, code, title = 422, "validation_error", "入力内容を確認してください"


class RateLimited(AppError):
    status, code, title = 429, "rate_limited", "試行回数が多すぎます。しばらくしてから再度お試しください"

    def __init__(self, detail: str | None = None, *, retry_after: int = 900):
        super().__init__(detail)
        self.retry_after = retry_after


class InsufficientStorage(AppError):
    status, code, title = 507, "insufficient_storage", "保存先の空き容量が不足しています"
