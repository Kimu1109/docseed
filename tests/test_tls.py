"""証明書の監視（docs/design/extensions.md 2.4・10 章）。期限の近い証明書を返す TLS サーバーを手元に立てて確かめる。"""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import threading
from dataclasses import replace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from docseed import tlscheck
from docseed.db import connect
from docseed.monitor import status

NAME = "docseed.example.com"


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Local CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test CA")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, days: float, name=NAME):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


@pytest.fixture()
def tls_server(tmp_path):
    """serve(days, name) で TLS サーバーを立て、(addr, CA の PEM のパス) を返す。"""
    servers = []
    ca_key, ca_cert = _ca()
    ca_pem = tmp_path / "root.crt"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    def serve(days: float, name: str = NAME):
        key, cert = _leaf(ca_key, ca_cert, days, name)
        crt, kf = tmp_path / f"leaf{len(servers)}.crt", tmp_path / f"leaf{len(servers)}.key"
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, kf)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        servers.append(sock)

        def loop():
            while True:
                try:
                    c, _ = sock.accept()
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(c, server_side=True) as s:
                        s.recv(1)
                except (OSError, ssl.SSLError):
                    pass
        threading.Thread(target=loop, daemon=True).start()
        return f"127.0.0.1:{sock.getsockname()[1]}", ca_pem

    yield serve
    for s in servers:
        s.close()


def test_valid_certificate(tls_server):
    addr, ca = tls_server(60)
    r = tlscheck.check(addr, NAME, cafile=ca)
    assert r["ok"] and r["error"] is None and 59 <= r["days_left"] <= 60 and r["issuer"] == "Test CA"
    assert tlscheck.alerts(r) == []


def test_expiring_certificate_warns(tls_server):
    addr, ca = tls_server(3)
    r = tlscheck.check(addr, NAME, cafile=ca)
    assert r["ok"] and r["days_left"] < 7
    assert tlscheck.alerts(r)[0][:2] == ("warning", "tls_expiring")


def test_name_mismatch_is_error(tls_server):
    addr, ca = tls_server(60, name="other.example.com")
    r = tlscheck.check(addr, NAME, cafile=ca)
    assert not r["ok"] and "mismatch" in r["error"].lower() and r["days_left"] is not None
    assert tlscheck.alerts(r)[0][:2] == ("error", "tls_invalid")


def test_untrusted_ca_is_error(tls_server):
    addr, _ = tls_server(60)
    r = tlscheck.check(addr, NAME)                 # 内部 CA を信頼していない
    assert not r["ok"] and r["error"]


def test_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = tlscheck.check(f"127.0.0.1:{port}", NAME)
    assert not r["ok"] and r["error"].startswith("接続できません") and r["not_after"] is None


def test_store_and_monitor_with_internal_ca(tls_server, settings, app):
    addr, ca = tls_server(5)
    root = tlscheck.internal_ca(settings)
    root.parent.mkdir(parents=True)
    root.write_bytes(ca.read_bytes())
    s = replace(settings, host=f"{NAME}, docseed.local", tls_check_addr=addr, tls_mode="internal")
    conn = connect(settings.db_path)
    res = tlscheck.check_and_store(conn, s)
    assert res["ok"] and res["mode"] == "internal"
    st = status(conn, s)
    assert st["tls"]["server_name"] == NAME
    assert any(a["code"] == "tls_expiring" for a in st["alerts"])
    # 監視しない設定なら何もしない
    assert tlscheck.check_and_store(conn, replace(s, tls_check_addr="")) is None
    assert tlscheck.start_background(replace(s, tls_check_addr="")) is None
