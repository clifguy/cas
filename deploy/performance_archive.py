"""Seal private trial evidence before publishing an artifact in a public repository."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_BYTES = 512 * 1024**2


def public_key(pem):
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("evidence recipient requires an RSA public key of at least 3072 bits")
    return key


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024**2), b""):
            result.update(block)
    return result.hexdigest()


def seal(source, destination, pem):
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir() or destination.is_relative_to(source):
        raise ValueError("sealed output must be outside the private evidence directory")
    key = public_key(pem)
    files = sorted(x for x in source.rglob("*") if x.is_file())
    if not files or len(files) > 4096 or any(x.is_symlink() for x in source.rglob("*")):
        raise ValueError("missing evidence or symlink in private export")
    if sum(x.stat().st_size for x in files) > MAX_BYTES:
        raise ValueError("private evidence exceeds archive limit")
    symmetric, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    encrypted_key = key.encrypt(
        symmetric,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    fingerprint = hashlib.sha256(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()
    header = (
        json.dumps(
            {
                "format": "cas-performance-evidence-v1",
                "recipient_sha256": fingerprint,
                "key": base64.b64encode(encrypted_key).decode(),
                "nonce": base64.b64encode(nonce).decode(),
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    encryptor = Cipher(algorithms.AES(symmetric), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(header)

            class EncryptingWriter:
                def write(self, data):
                    output.write(encryptor.update(data))
                    return len(data)

            with tarfile.open(mode="w|", fileobj=EncryptingWriter()) as archive:
                for file in files:
                    archive.add(file, arcname=file.relative_to(source).as_posix(), recursive=False)
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
        receipt = {
            "format": "cas-performance-evidence-v1",
            "recipient_sha256": fingerprint,
            "ciphertext_sha256": digest(destination),
            "bytes": destination.stat().st_size,
            "files": len(files),
        }
        destination.with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def decrypt(archive, private_key, destination):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("decryption destination must be new")
    key = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
    with archive.open("rb") as source:
        header = source.readline(8192)
        if not header.endswith(b"\n"):
            raise ValueError("invalid encrypted evidence header")
        values = json.loads(header)
        if values["format"] != "cas-performance-evidence-v1":
            raise ValueError("unsupported evidence archive")
        start = source.tell()
        source.seek(-16, io.SEEK_END)
        end, tag = source.tell(), source.read(16)
        symmetric = key.decrypt(
            base64.b64decode(values["key"]),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        decryptor = Cipher(
            algorithms.AES(symmetric), modes.GCM(base64.b64decode(values["nonce"]), tag)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        source.seek(start)
        # Authentication must finish before any plaintext is extracted.
        with tempfile.TemporaryFile() as plaintext:
            remaining = end - start
            if remaining < 0 or remaining > MAX_BYTES + 16 * 1024**2:
                raise ValueError("invalid bounded archive size")
            while remaining:
                block = source.read(min(1024**2, remaining))
                if not block:
                    raise ValueError("truncated evidence archive")
                remaining -= len(block)
                plaintext.write(decryptor.update(block))
            plaintext.write(decryptor.finalize())
            plaintext.seek(0)
            destination.mkdir(parents=True, mode=0o700)
            with tarfile.open(fileobj=plaintext, mode="r:") as bundle:
                bundle.extractall(destination, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seal", "decrypt"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--approved-sha256")
    parser.add_argument("--private-key", type=Path)
    args = parser.parse_args()
    if args.operation == "decrypt":
        if args.private_key is None:
            parser.error("--private-key required")
        decrypt(args.source, args.private_key, args.output)
    else:
        if args.spec is None or args.approved_sha256 is None:
            parser.error("--spec and --approved-sha256 required")
        raw = args.spec.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.approved_sha256:
            raise ValueError("specification differs from reviewed digest")
        seal(args.source, args.output, json.loads(raw)["evidence_public_key_pem"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
