"""配置の設定ファイル（Caddyfile・compose.yaml・.env.example）の整合性。Caddy は CI で動かさないので文面で確かめる。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_fetcher_service():
    block = COMPOSE.split("\n  fetcher:", 1)[1].split("\n\n", 1)[0]
    assert 'profiles: ["webclip"]' in block and 'command: ["docseed", "fetcher"]' in block
    assert "networks:" not in block            # default ネットワーク（外部へ通信する）
    worker = COMPOSE.split("\n  worker:", 1)[1].split("\n\n", 1)[0]
    assert "networks: [internal]" in worker     # worker は外部と通信しないまま
    for var in ("DOCSEED_WEBCLIP_ENABLED", "DOCSEED_WEBCLIP_MAX_MB", "DOCSEED_WEBCLIP_PROXY"):
        assert var in ENV_EXAMPLE


def test_llm_service():
    block = COMPOSE.split("\n  llm:", 1)[1].split("\n\n", 1)[0]
    assert 'profiles: ["llm"]' in block and "networks: [internal]" in block   # 外部と通信しない
    assert "ghcr.io/ggml-org/llama.cpp:server" in block and "--parallel" in block and "mem_limit: 2g" in block
    assert "/models:ro" in block
    for var in ("DOCSEED_SUGGEST_ENABLED", "DOCSEED_LLM_ENABLED", "DOCSEED_LLM_MODEL", "DOCSEED_LLM_TIMEOUT_S"):
        assert var in ENV_EXAMPLE


def _service(name: str) -> str:
    return COMPOSE.split(f"\n  {name}:", 1)[1].split("\n\n", 1)[0]


def test_containers_are_hardened():
    """インターネットに公開しても、乗っ取られたコンテナでできることを小さくする（deployment.md 2.3）。"""
    anchor = COMPOSE.split("x-hardening: &hardening", 1)[1].split("\n\n", 1)[0]
    for line in ("read_only: true", "cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]', "/tmp:"):
        assert line in anchor
    for name in ("caddy", "api", "worker", "fetcher", "llm"):
        block = _service(name)
        assert "<<: *hardening" in block, name
        assert "pids_limit:" in block, name
    # Caddy は 80 / 443 を待ち受ける権限だけ戻し、設定の控えを書く /config を用意する
    caddy = _service("caddy")
    assert "cap_add: [NET_BIND_SERVICE]" in caddy and "/config:" in caddy
    # 大きなアップロードの一時ファイルは RAM（tmpfs の /tmp）ではなくデータディレクトリに置く
    for name in ("api", "worker", "fetcher"):
        assert "TMPDIR: /srv/docseed/files/.tmp" in _service(name), name


def test_caddy_skips_trust_install():
    """read_only の Caddy は、内部 CA をコンテナの信頼ストアに入れられない（入れる意味もない）。"""
    global_options = CADDYFILE.split("\n{\n", 1)[1].split("\n}", 1)[0]
    assert "skip_install_trust" in global_options


def test_share_upload_limits_documented():
    for var in ("DOCSEED_SHARE_UPLOAD_LINK_GB", "DOCSEED_SHARE_UPLOAD_DAILY_GB", "DOCSEED_SHARE_UPLOAD_MIN_FREE_PCT",
                "DOCSEED_TRUSTED_NETS"):
        assert var in ENV_EXAMPLE


def test_cloudflared_service():
    """Cloudflare Tunnel（docs/design/security.md 10 章）。"""
    block = _service("cloudflared")
    assert 'profiles: ["tunnel"]' in block and "<<: *hardening" in block
    assert "TUNNEL_TOKEN: ${DOCSEED_TUNNEL_TOKEN:-}" in block
    assert "networks: [tunnel]" in block                 # api・worker には直接届かない
    assert "networks: [default, tunnel]" in _service("caddy")
    tunnel_net = COMPOSE.split("\n  tunnel:\n", 1)[1]
    assert "subnet: ${DOCSEED_TUNNEL_SUBNET:-10.213.47.0/28}" in tunnel_net
    # Caddy と compose の既定値がそろっていること（空の環境変数だと Caddy の既定値は使われない）
    assert "DOCSEED_TUNNEL_SUBNET: ${DOCSEED_TUNNEL_SUBNET:-10.213.47.0/28}" in _service("caddy")
    assert CADDYFILE.count("{$DOCSEED_TUNNEL_SUBNET:10.213.47.0/28}") >= 3
    for var in ("DOCSEED_TUNNEL_TOKEN", "DOCSEED_TUNNEL_SUBNET", "DOCSEED_RATE_LIMIT"):
        assert var in ENV_EXAMPLE


def test_caddy_judges_by_client_ip():
    """トンネル経由の CF-Connecting-IP を反映した接続元で内側・外側を判定する（remote_ip だとトンネルが内側になる）。"""
    assert "not remote_ip" not in CADDYFILE
    assert "not client_ip {$DOCSEED_TRUSTED_NETS" in CADDYFILE
    assert "client_ip_headers CF-Connecting-IP" in CADDYFILE
    assert "header_up X-Forwarded-For {client_ip}" in CADDYFILE


FAIL2BAN = ROOT / "deploy" / "fail2ban"


def test_fail2ban_filter_covers_all_security_events():
    events = set()
    for p in (ROOT / "src" / "docseed").rglob("*.py"):
        events |= set(re.findall(r'security_event\(\s*"([a-z_]+)"', p.read_text()))
    from docseed.ratelimit import AUDIT_EVENTS
    events |= AUDIT_EVENTS | {"share_password_failed"}
    filt = (FAIL2BAN / "filter.d" / "docseed.conf").read_text()
    alternatives = set(re.search(r"event=\(\?:([a-z_|]+)\)", filt).group(1).split("|"))
    assert events <= alternatives, events - alternatives
    jail = (FAIL2BAN / "jail.d" / "docseed.local").read_text()
    assert "chain=DOCKER-USER" in jail and "@LOGPATH@" in jail
    install = (ROOT / "scripts" / "ops" / "install-fail2ban.sh").read_text()
    assert "logs/security.log" in install


def test_fail2ban_regex_matches_security_log(tmp_path):
    """実際の fail2ban-regex で、セキュリティログの各行から IP を取り出せること（fail2ban が無ければ飛ばす）。"""
    from docseed import ratelimit
    if not shutil.which("fail2ban-regex"):
        pytest.skip("fail2ban-regex がない")
    ratelimit.setup_security_log(tmp_path)
    try:
        ratelimit.security_event("login_failed", "203.0.113.5")
        ratelimit.security_event("share_invalid", "2001:db8::1")
        ratelimit.security_event("rate_limited", "198.51.100.3", rule="share_upload")
        for h in ratelimit.sec_log.handlers:
            h.flush()
        out = subprocess.run(["fail2ban-regex", "-v", str(tmp_path / "security.log"),
                              str(FAIL2BAN / "filter.d" / "docseed.conf")], capture_output=True, text=True).stdout
    finally:
        for h in list(ratelimit.sec_log.handlers):
            ratelimit.sec_log.removeHandler(h)
            h.close()
    assert "3 matched, 0 missed" in out, out
    for ip in ("203.0.113.5", "2001:db8::1", "198.51.100.3"):
        assert ip in out


def test_update_timer_and_scripts():
    """イメージの取り直し（docs/design/deployment.md 7.1）。"""
    systemd = ROOT / "deploy" / "systemd"
    svc = (systemd / "docseed-update.service").read_text()
    assert "ExecStart=@DOCSEED_DIR@/scripts/ops/update-images.sh" in svc
    assert "OnCalendar=Mon" in (systemd / "docseed-update.timer").read_text()
    script = (ROOT / "scripts" / "ops" / "update-images.sh").read_text()
    # docseed:latest は手元で作るので取りに行かない（docker compose pull は使わない）
    assert "grep -v '^docseed'" in script and "docker compose pull" not in script
    assert "docseed notify --test" in script
    for p in ("update-images.sh", "install-update.sh", "install-fail2ban.sh"):
        assert (ROOT / "scripts" / "ops" / p).stat().st_mode & 0o111, p


def test_dependabot_and_security_workflow():
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text()
    for eco in ("pip", "github-actions", "docker", "docker-compose"):
        assert f"package-ecosystem: {eco}\n" in dependabot, eco
    wf = (ROOT / ".github" / "workflows" / "security.yml").read_text()
    assert "pip-audit" in wf and "--exit-code 1 docseed:scan" in wf and "schedule:" in wf
    assert "DOCSEED_NOTIFY_URL" in ENV_EXAMPLE


def test_notify_private_ranges_match_caddy():
    """通知の「内側」の判定は Caddy の private_ranges と同じ範囲（CI の caddy でも実物と比べる）。"""
    from docseed.security_notify import CADDY_PRIVATE_RANGES
    assert CADDY_PRIVATE_RANGES == ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "127.0.0.1/8", "fd00::/8", "::1")


def test_internal_paths_hidden_under_any_prefix():
    """DOCSEED_BASE_PATH=/docseed のときの「/docseed/admin」なども、外からは 404（docs/design/security.md 8 章）。"""
    block = CADDYFILE.split("(internal_paths) {", 1)[1].split("\n}\n", 1)[0]
    regexps = re.findall(r"^\s*path_regexp (\S+)$", block, flags=re.MULTILINE)
    assert len(regexps) == 2 and len(set(regexps)) == 1   # 外側から・トンネル経由の両方
    assert "respond @internal_only_prefixed 404" in block
    assert "respond @internal_only_prefixed_tunnel 404" in block
    rx = re.compile(regexps[0])   # RE2 と同じ意味になる書き方だけを使っている
    for hidden in ("/docseed/admin", "/docseed/admin/users", "/docseed/ADMIN", "/a/b/api/docs",
                   "/docseed/api/docs/oauth2", "/docseed/api/v1/admin/jobs", "/docseed/api/v1/openapi.json",
                   "/docseed/healthz"):
        assert rx.match(hidden), hidden
    for shown in ("/docseed/", "/docseed/d/1", "/docseed/api/v1/documents", "/docseed/administrator",
                  "/docseed/api/v1/openapi_json", "/admin"):   # 接頭辞なしの /admin は path の一覧で隠す
        assert not rx.match(shown), shown
