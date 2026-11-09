"""配置の設定ファイル（Caddyfile・compose.yaml・.env.example）の整合性。Caddy は CI で動かさないので文面で確かめる。"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = (ROOT / "docker" / "Caddyfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


def _env_modes() -> set[str]:
    """.env.example のコメントに書いた証明書の方式（「#   internal: ...」の形）。"""
    block = ENV_EXAMPLE.split("# 証明書の方式", 1)[1].split("DOCSEED_TLS_MODE=", 1)[0]
    return set(re.findall(r"^#\s+(\w+)\s*:", block, flags=re.MULTILINE))


def test_every_tls_mode_has_snippet():
    snippets = set(re.findall(r"^\(tls_(\w+)\)", CADDYFILE, flags=re.MULTILINE))
    modes = _env_modes()
    assert modes == {"internal", "file", "tailscale", "acme_dns"}
    assert modes <= snippets
    assert "import tls_{$DOCSEED_TLS_MODE:internal}" in CADDYFILE


def test_file_mode_paths_are_under_mounted_dir():
    assert "tls /certs/{$DOCSEED_TLS_CERT} /certs/{$DOCSEED_TLS_KEY}" in CADDYFILE
    assert "${DOCSEED_TLS_CERT_DIR:-./docker/certs}:/certs:ro" in COMPOSE
    for var in ("DOCSEED_TLS_MODE", "DOCSEED_TLS_CERT", "DOCSEED_TLS_KEY"):
        assert f"{var}: ${{{var}:-" in COMPOSE  # caddy のコンテナに渡している
    assert (ROOT / "docker" / "certs").is_dir()  # 既定のマウント元（空）


def test_ip_site_keeps_internal_ca():
    ip_block = CADDYFILE.split("{$DOCSEED_LAN_IP:localhost} {", 1)[1].split("}", 1)[0]
    assert "tls internal" in ip_block


def test_tailscale_mode_mounts_socket():
    block = CADDYFILE.split("(tls_tailscale) {", 1)[1].split("\n}", 1)[0]
    assert "get_certificate tailscale" in block
    assert ":/var/run/tailscale:ro" in COMPOSE


def test_acme_dns_mode():
    block = CADDYFILE.split("(tls_acme_dns) {", 1)[1].split("\n}", 1)[0]
    assert "dns cloudflare {env.DOCSEED_ACME_DNS_TOKEN}" in block
    assert "import acme_ca_{$DOCSEED_ACME_STAGING:false}" in block
    assert re.search(r"^\(acme_ca_true\) \{\n\tca https://acme-staging-v02", CADDYFILE, flags=re.MULTILINE)
    assert re.search(r"^\(acme_ca_false\) \{\n\tca https://acme-v02", CADDYFILE, flags=re.MULTILINE)
    for var in ("DOCSEED_ACME_EMAIL", "DOCSEED_ACME_DNS_TOKEN", "DOCSEED_ACME_STAGING"):
        assert f"{var}: ${{{var}:-" in COMPOSE
        assert var in ENV_EXAMPLE
    assert "image: ${DOCSEED_CADDY_IMAGE:-caddy:2}" in COMPOSE
    dockerfile = (ROOT / "docker" / "caddy" / "Dockerfile").read_text()
    assert "xcaddy build --with ${DNS_PLUGIN}" in dockerfile and "caddy-dns/cloudflare" in dockerfile


def test_api_checks_certificate():
    api = COMPOSE.split("\n  api:", 1)[1].split("\n  worker:", 1)[0]
    assert "DOCSEED_TLS_CHECK_ADDR: caddy:${DOCSEED_HTTPS_PORT:-443}" in api


def test_lan_ip_never_empty_in_caddy():
    """Caddy の {$VAR:default} は変数が空文字だと既定値を使わないので、compose で空にしない。"""
    assert "DOCSEED_LAN_IP: ${DOCSEED_LAN_IP:-localhost}" in COMPOSE
    caddy_env = COMPOSE.split("\n  caddy:", 1)[1].split("\n  api:", 1)[0]
    pattern = r"\$\{DOCSEED_(?:LAN_IP|TLS_MODE|HTTPS_PORT|HTTP_PORT|ACME_STAGING):-\}"
    assert re.findall(pattern, caddy_env) == []
