"""
iPad mode: serve Oreo's face to an iPad on the same Wi-Fi, over HTTPS.

iPad Safari only allows the microphone on HTTPS pages, so this makes a small private
certificate authority ("Oreo Local CA") the first time it runs, and a server certificate
for this Mac's name and Wi-Fi address signed by it. You install the CA on the iPad once
(AirDrop certs/oreo-local-ca.pem, then trust it in Settings). Nothing here touches the
Mac's own trust settings, and everything stays on your network.

A pairing key keeps other devices on the Wi-Fi out: the iPad opens the URL with ?key=...
once, gets a cookie, and from then on every page, API call and WebSocket must carry it.
Requests from this Mac itself (localhost) don't need it.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("oreo.ipad")

CA_NAME = "Oreo Local CA"
COOKIE = "oreo_key"


def mac_hostname() -> str:
    """This Mac's Bonjour name, e.g. Siddarthas-Macbook.local (stays the same when the IP changes)."""
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
        if name:
            return f"{name}.local"
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname()


def wifi_ip() -> str | None:
    """This Mac's address on the local network (the route a packet to the internet would take)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent
            return s.getsockname()[0]
    except OSError:
        return None


def pairing_key(folder: Path) -> str:
    path = folder / "pairing-key.txt"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(16))
        path.chmod(0o600)
    return path.read_text().strip()


def ensure_certs(folder: Path, hostname: str, ip: str | None) -> tuple[Path, Path, Path]:
    """Return (server cert, server key, CA cert), creating them when missing or when the
    Mac's name or IP changed. The CA is kept, so the iPad only has to trust it once."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder.mkdir(parents=True, exist_ok=True)
    ca_cert_path, ca_key_path = folder / "oreo-local-ca.pem", folder / "oreo-local-ca.key"
    cert_path, key_path = folder / "oreo-server.pem", folder / "oreo-server.key"
    now = datetime.now(timezone.utc)

    def write_key(path: Path, key) -> None:
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        path.chmod(0o600)

    if ca_cert_path.exists() and ca_key_path.exists():
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), None)
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Oreo (this Mac only)")])
        ca_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
        write_key(ca_key_path, ca_key)
        ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        log.info("Created %s", ca_cert_path)

    names = [hostname, "localhost"]
    ips = ["127.0.0.1"] + ([ip] if ip else [])
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = set(san.get_values_for_type(x509.DNSName)) | {str(a) for a in san.get_values_for_type(x509.IPAddress)}
        # Apple caps server certificates at 825 days; renew a month early.
        if set(names + ips) <= have and cert.not_valid_after_utc - now > timedelta(days=30):
            return cert_path, key_path, ca_cert_path

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=800))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(a)) for a in ips]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_key(key_path, key)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Created server certificate for %s", ", ".join(names + ips))
    return cert_path, key_path, ca_cert_path


def is_local(host: str | None) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "testclient")
