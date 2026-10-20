"""Issue private internal TLS certificates without printing or committing keys."""

from __future__ import annotations

import argparse
import os
import secrets
import ssl
import subprocess
import tempfile
from pathlib import Path

SERVICES = ("chatwoot", "agent-bot", "redis", "postgres")


def openssl(*args: str) -> bytes:
    result = subprocess.run(["openssl", *args], capture_output=True, check=False, timeout=30)
    if result.returncode:
        # OpenSSL diagnostics can include filenames; never expose key material.
        raise RuntimeError(f"OpenSSL {args[0]} failed")
    return result.stdout


def prepare(directory: Path, *, days: int = 90, renew: bool = False) -> None:
    if not 1 <= days <= 365:
        raise ValueError("Leaf certificate lifetime must be between 1 and 365 days")
    if directory.is_symlink():
        raise ValueError("TLS directory must not be a symlink")
    allowed = {"ca.key", "ca.crt", "ca-bundle.pem", ".gitignore"} | {
        f"{name}.{suffix}" for name in SERVICES for suffix in ("key", "crt")
    }
    if directory.exists() and any(
        child.name not in allowed or child.is_symlink() for child in directory.iterdir()
    ):
        raise ValueError("Use a dedicated TLS directory without unrelated files or symlinks")
    ignore = directory / ".gitignore"
    if ignore.exists() and ignore.read_text() != "*\n":
        raise ValueError("Refusing to replace an existing unrelated .gitignore")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    # Ignore keys even if certificate issuance later fails.
    ignore.write_text("*\n")
    ca_key, ca_cert = directory / "ca.key", directory / "ca.crt"
    if ca_key.exists() != ca_cert.exists():
        raise ValueError("Incomplete internal CA; recover it before issuing certificates")
    if not renew and any((directory / f"{name}.key").exists() for name in SERVICES):
        raise ValueError(
            "Certificates already exist; use --renew to retain the CA and renew leaves"
        )
    with tempfile.TemporaryDirectory(prefix=".issue-", dir=directory) as temporary:
        staging = Path(temporary)
        if not ca_key.exists():
            openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-noenc",
                "-keyout",
                str(staging / "ca.key"),
                "-out",
                str(staging / "ca.crt"),
                "-sha256",
                "-days",
                "3650",
                "-subj",
                "/CN=Women Help Internal CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
            )
            os.chmod(staging / "ca.key", 0o600)
            os.replace(staging / "ca.key", ca_key)
            os.replace(staging / "ca.crt", ca_cert)
        os.chmod(ca_key, 0o600)
        os.chmod(ca_cert, 0o644)
        openssl("x509", "-in", str(ca_cert), "-checkend", str(days * 86400), "-noout")
        for name in SERVICES:
            key, request, certificate = (
                staging / f"{name}.{suffix}" for suffix in ("key", "csr", "crt")
            )
            extensions = staging / f"{name}.ext"
            extensions.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "subjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid,issuer\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
                f"subjectAltName=DNS:{name},DNS:localhost,IP:127.0.0.1,IP:::1\n"
            )
            openssl(
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-keyout",
                str(key),
                "-out",
                str(request),
                "-subj",
                f"/CN={name}",
            )
            openssl(
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-set_serial",
                str(secrets.randbits(128) or 1),
                "-out",
                str(certificate),
                "-days",
                str(days),
                "-sha256",
                "-extfile",
                str(extensions),
            )
            openssl("verify", "-CAfile", str(ca_cert), "-verify_hostname", name, str(certificate))
            os.chmod(key, 0o600)
            os.chmod(certificate, 0o644)
        # Extend public trust instead of replacing it: LLM, S3 and Telegram
        # still need the normal public root certificates.
        bundle = (
            "".join(
                ssl.DER_cert_to_PEM_cert(cert)
                for cert in ssl.create_default_context().get_ca_certs(binary_form=True)
            )
            + ca_cert.read_text()
        )
        (staging / "ca-bundle.pem").write_text(bundle)
        os.chmod(staging / "ca-bundle.pem", 0o644)
        for name in SERVICES:
            for suffix in ("key", "crt"):
                os.replace(staging / f"{name}.{suffix}", directory / f"{name}.{suffix}")
        os.replace(staging / "ca-bundle.pem", directory / "ca-bundle.pem")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--renew", action="store_true")
    args = parser.parse_args()
    prepare(args.output_dir, days=args.days, renew=args.renew)
    print(
        f"Internal certificates prepared for {len(SERVICES)} services; private keys were not displayed."
    )


if __name__ == "__main__":
    main()
