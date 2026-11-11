"""Self-signed certificate for the phone link.

The phone pins this certificate's fingerprint during pairing, so no public
certificate authority is involved and nobody on the Wi-Fi can read or alter
the traffic between the phone and Felix.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import restrict

log = logging.getLogger("felix.tls")


class TlsUnavailable(Exception):
    """No way to create a certificate on this machine."""


def fingerprint(cert_path: Path) -> str:
    """SHA-256 of the certificate's DER encoding, lower-case hex."""
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def ensure_certificate(directory: Path) -> tuple[Path, Path, str]:
    """Return (cert, key, fingerprint), creating the pair on first use."""
    cert, key = directory / "phone-link.crt", directory / "phone-link.key"
    if cert.exists() and key.exists():
        try:
            return cert, key, fingerprint(cert)
        except (ValueError, OSError):
            log.warning("stored phone-link certificate is unreadable; creating a new one")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        _make_with_cryptography(cert, key)
    except ImportError:
        _make_with_openssl(cert, key)
    restrict(key)
    return cert, key, fingerprint(cert)


def _make_with_cryptography(cert: Path, key: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Felix Core")])
    now = datetime.now(timezone.utc)
    names: list[x509.GeneralName] = [x509.DNSName("felix.local"), x509.DNSName("localhost"),
                                     x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    certificate = builder.sign(private, hashes.SHA256())
    key.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    restrict(key)
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _make_with_openssl(cert: Path, key: Path) -> None:
    binary = shutil.which("openssl")
    if not binary:
        raise TlsUnavailable(
            "Cannot create the phone-link certificate: install the 'cryptography' Python package "
            "(pip install cryptography) or OpenSSL.")
    command = [binary, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
               "-keyout", str(key), "-out", str(cert), "-days", "3650", "-subj", "/CN=Felix Core",
               "-addext", "subjectAltName=DNS:felix.local,DNS:localhost,IP:127.0.0.1"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise TlsUnavailable(f"OpenSSL could not create the certificate: {result.stderr.strip()[:200]}")


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    return context


_ADDRESSES: tuple[float, list[str]] | None = None


def local_addresses(max_age: float = 30.0) -> list[str]:
    """Addresses a phone on the same network could use to reach this laptop.

    Looking up the laptop's own name can take seconds on some systems (macOS asks
    the network), so that part is given a short deadline and the answer is kept
    for a little while.
    """
    global _ADDRESSES
    if _ADDRESSES is not None and time.monotonic() - _ADDRESSES[0] < max_age:
        return list(_ADDRESSES[1])
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))     # no packet is sent; this only selects a route
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    hostname = socket.gethostname()
    for address in _resolve(hostname, deadline=1.5):
        if address not in found:
            found.append(address)
    usable = [a for a in found if not a.startswith("127.")]
    if hostname and "." not in hostname:
        usable.append(f"{hostname}.local")
    result = usable or ["127.0.0.1"]
    _ADDRESSES = (time.monotonic(), result)
    return list(result)


def _resolve(hostname: str, deadline: float) -> list[str]:
    """IPv4 addresses of ``hostname``, or nothing if the lookup takes too long."""
    answer: list[str] = []

    def work() -> None:
        try:
            answer.extend(info[4][0] for info in socket.getaddrinfo(hostname, None, socket.AF_INET))
        except OSError:
            pass

    thread = threading.Thread(target=work, name="felix-resolve", daemon=True)
    thread.start()
    thread.join(deadline)
    return [] if thread.is_alive() else list(answer)
