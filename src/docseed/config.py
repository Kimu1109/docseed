"""設定（環境変数 DOCSEED_* から読む）。docs/design/deployment.md 3 章。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/srv/docseed")
    secret_key: str = ""
    max_upload_mb: int = 1024
    trash_retention_days: int = 30
    cookie_secure: bool = True
    session_days: int = 14
    session_hours_short: int = 12
    log_level: str = "INFO"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "docseed.sqlite"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def tmp_dir(self) -> Path:
        return self.files_dir / ".tmp"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.files_dir, self.tmp_dir, self.data_dir / "derived"):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            data_dir=Path(e.get("DOCSEED_DATA_DIR", "/srv/docseed")),
            secret_key=e.get("DOCSEED_SECRET_KEY", ""),
            max_upload_mb=int(e.get("DOCSEED_MAX_UPLOAD_MB", "1024")),
            trash_retention_days=int(e.get("DOCSEED_TRASH_RETENTION_DAYS", "30")),
            cookie_secure=_bool(e.get("DOCSEED_COOKIE_SECURE"), True),
            log_level=e.get("DOCSEED_LOG_LEVEL", "INFO"),
        )
