# acme_dns 用: DNS のプラグイン入りの Caddy（docs/design/extensions.md 2.2）。
#   docker compose --profile acme build caddy-acme
# Cloudflare 以外の DNS 事業者は、DNS_PLUGIN を https://caddyserver.com/docs/modules/ の dns.providers.* に変え、
# docker/Caddyfile の (tls_acme_dns) の「dns cloudflare ...」をその事業者の書き方にする。
FROM caddy:2-builder AS builder
ARG DNS_PLUGIN=github.com/caddy-dns/cloudflare
RUN xcaddy build --with ${DNS_PLUGIN}

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
