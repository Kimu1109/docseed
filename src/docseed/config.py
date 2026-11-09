"""設定（環境変数 DOCSEED_* から読む）。docs/design/deployment.md 3 章。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OidcProvider:
    """OIDC の提供者（docs/design/extensions.md 5.4）。"""
    name: str            # URL に使う名前（例 google）
    issuer: str
    client_id: str
    client_secret: str
    label: str


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
    # worker（docs/design/pipeline.md、deployment.md 3 章）
    ocr_engine: str = "ppocrv6_small"
    ocr_dpi: int = 200
    ocr_det_side: int = 1600
    ocr_threads: int = 3
    ocr_min_score: float = 0.5
    embed_model: str = "cl-nagoya/ruri-v3-70m"
    embed_threads: int = 3
    query_threads: int = 1
    search_sem_min: float = 0.80
    inbox_enabled: bool = False
    inbox_keep: bool = False
    thermal_pause_c: float = 80.0
    thermal_resume_c: float = 70.0
    # ログイン（Phase 7、docs/design/extensions.md 5 章）
    host: str = ""                    # DOCSEED_HOST（Caddy と同じ値）
    https_port: int = 443
    rp_id_override: str = ""          # DOCSEED_RP_ID
    public_url_override: str = ""     # DOCSEED_PUBLIC_URL（通常は空。DOCSEED_HOST とポートから作る）
    oidc_providers: tuple[OidcProvider, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def rp_id(self) -> str:
        """パスキーの RP ID。既定は DOCSEED_HOST の最初の名前。IP アドレスや空なら ""（パスキーを使わない）。"""
        rp = self.rp_id_override or _first_host(self.host)
        return "" if not rp or _is_ip(rp) else rp.lower()

    @property
    def public_url(self) -> str:
        """外から開くときの URL（https://<名前>[:ポート]）。OIDC のリダイレクト URI とパスキーの origin に使う。"""
        if self.public_url_override:
            return self.public_url_override.rstrip("/")
        host = self.rp_id or _first_host(self.host)
        if not host:
            return ""
        return f"https://{host}" + ("" if self.https_port == 443 else f":{self.https_port}")

    def oidc_provider(self, name: str) -> OidcProvider | None:
        return next((p for p in self.oidc_providers if p.name == name), None)

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
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """データディレクトリの中を作る。書き込めなければ、対処を書いたメッセージで終了する。"""
        try:
            for d in (self.db_path.parent, self.files_dir, self.tmp_dir, self.data_dir / "derived",
                      self.models_dir):
                d.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise SystemExit(permission_message(self.data_dir, e)) from None

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
            ocr_engine=e.get("DOCSEED_OCR_ENGINE", "ppocrv6_small"),
            ocr_dpi=int(e.get("DOCSEED_OCR_DPI", "200")),
            ocr_det_side=int(e.get("DOCSEED_OCR_DET_SIDE", "1600")),
            ocr_threads=int(e.get("DOCSEED_OCR_THREADS", "3")),
            ocr_min_score=float(e.get("DOCSEED_OCR_MIN_SCORE", "0.5")),
            embed_model=e.get("DOCSEED_EMBED_MODEL", "cl-nagoya/ruri-v3-70m"),
            embed_threads=int(e.get("DOCSEED_EMBED_THREADS", "3")),
            query_threads=int(e.get("DOCSEED_QUERY_THREADS", "1")),
            search_sem_min=float(e.get("DOCSEED_SEARCH_SEM_MIN", "0.80")),
            inbox_enabled=_bool(e.get("DOCSEED_INBOX_ENABLED"), False),
            inbox_keep=_bool(e.get("DOCSEED_INBOX_KEEP"), False),
            thermal_pause_c=float(e.get("DOCSEED_THERMAL_PAUSE_C", "80")),
            host=e.get("DOCSEED_HOST", ""),
            https_port=int(e.get("DOCSEED_HTTPS_PORT") or "443"),
            rp_id_override=e.get("DOCSEED_RP_ID", "").strip(),
            public_url_override=e.get("DOCSEED_PUBLIC_URL", "").strip(),
            oidc_providers=_oidc_from_env(e),
        )


def _first_host(v: str) -> str:
    """DOCSEED_HOST（"a.example, b.example" のように複数書ける）の最初の名前。"""
    for part in v.replace(",", " ").split():
        return part.strip().rstrip(".")
    return ""


def _is_ip(v: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(v.strip("[]"))
        return True
    except ValueError:
        return False


def _oidc_from_env(e) -> tuple[OidcProvider, ...]:
    out = []
    for name in (x.strip().lower() for x in e.get("DOCSEED_OIDC_PROVIDERS", "").split(",")):
        if not name:
            continue
        if not name.replace("-", "").replace("_", "").isalnum():
            raise SystemExit(f"DOCSEED_OIDC_PROVIDERS の名前「{name}」は英数字と - _ だけにしてください")
        key = f"DOCSEED_OIDC_{name.upper().replace('-', '_')}_"
        issuer = e.get(key + "ISSUER", "").strip().rstrip("/")
        client_id = e.get(key + "CLIENT_ID", "").strip()
        secret = e.get(key + "CLIENT_SECRET", "").strip()
        if not issuer or not client_id or not secret:
            raise SystemExit(f"OIDC の提供者「{name}」には {key}ISSUER・{key}CLIENT_ID・{key}CLIENT_SECRET が必要です")
        out.append(OidcProvider(name, issuer, client_id, secret, e.get(key + "LABEL", "").strip() or name))
    return tuple(out)


def permission_message(data_dir: Path, err: OSError) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else -1
    try:
        st = data_dir.stat()
        owner = f"UID {st.st_uid}"
    except OSError:
        owner = "不明"
    return (
        f"データディレクトリに書き込めません: {err.filename or data_dir}\n"
        f"  このプロセスは UID {uid} で動いていますが、{data_dir} の所有者は {owner} です。\n"
        f"  ホスト（Raspberry Pi）で次を実行してから、もう一度起動してください:\n"
        f"    sudo chown -R {uid}:{uid} <ホスト側のデータディレクトリ（.env の DOCSEED_DATA_DIR、既定 /srv/docseed）>"
    )
